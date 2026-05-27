from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openevent.im_sdk import ParsedMessage as ImMessage
from openevent.model_proxy_sdk import InferRequest, InferResult, ParsedMessage as LlmMessage

from .config import SessionConfig
from .prompt import UserPromptMessage
from .wal import WalPrepare, parse_model_request_id, turn_id


class AgentStateError(RuntimeError):
    pass


@dataclass
class WalRecord:
    seq: int
    session_id: str
    payload: WalPrepare

    @property
    def turn_id(self) -> str:
        return turn_id(self.session_id, self.payload.user_message_seqs)


@dataclass
class AttemptState:
    wal: WalRecord
    request_seq: int | None = None
    request: InferRequest | None = None
    result_seq: int | None = None
    result: InferResult | None = None

    @property
    def request_id(self) -> str:
        return f"agent:{self.wal.session_id}:wal:{self.wal.seq}"


@dataclass
class SessionState:
    config: SessionConfig
    pending: list[UserPromptMessage] = field(default_factory=list)
    prompt_messages: list[dict[str, Any]] = field(default_factory=list)
    last_llm_request_seq: int = 0
    attempts: dict[int, AttemptState] = field(default_factory=dict)
    terminal_send_by_turn: dict[str, ImMessage] = field(default_factory=dict)
    send_results_by_prev_seq: dict[int, ImMessage] = field(default_factory=dict)
    agent_send_request_by_seq: dict[int, ImMessage] = field(default_factory=dict)
    send_result_by_provider_message_id: dict[str, ImMessage] = field(default_factory=dict)
    frozen: bool = False
    in_flight_turn_id: str | None = None

    def add_pending(self, message: UserPromptMessage) -> None:
        if any(item.seq == message.seq for item in self.pending):
            return
        self.pending.append(message)
        self.pending.sort(key=lambda item: item.seq)

    def remove_pending_seqs(self, seqs: tuple[int, ...]) -> None:
        seq_set = set(seqs)
        self.pending = [item for item in self.pending if item.seq not in seq_set]

    def latest_attempt(self) -> AttemptState | None:
        if not self.attempts:
            return None
        return self.attempts[max(self.attempts)]


class AgentRuntimeState:
    def __init__(self, sessions: tuple[SessionConfig, ...]):
        enabled = [session for session in sessions if session.enabled]
        self.sessions_by_id = {session.session_id: SessionState(session) for session in enabled}
        self.sessions_by_im = {session.im_channel_id: self.sessions_by_id[session.session_id] for session in enabled}
        self.sessions_by_model = {session.model_channel_id: self.sessions_by_id[session.session_id] for session in enabled}
        self.sessions_by_wal = {session.wal_channel_id: self.sessions_by_id[session.session_id] for session in enabled}
        self.user_messages: dict[int, UserPromptMessage] = {}
        self.wal_records: dict[int, WalRecord] = {}
        self.max_seen_seq = 0

    def observe_user_message(self, session: SessionState, message: UserPromptMessage) -> None:
        self.user_messages[message.seq] = message
        session.add_pending(message)

    def observe_wal(self, message_seq: int, channel_id: int, payload: WalPrepare) -> WalRecord:
        session = self.sessions_by_wal.get(channel_id)
        if session is None:
            raise AgentStateError(f"WAL channel {channel_id} is not configured")
        record = WalRecord(seq=message_seq, session_id=session.config.session_id, payload=payload)
        self.wal_records[message_seq] = record
        session.remove_pending_seqs(payload.user_message_seqs)
        session.attempts.setdefault(message_seq, AttemptState(wal=record))
        return record

    def observe_llm_message(self, message: LlmMessage) -> None:
        if isinstance(message.payload, InferRequest):
            parsed = parse_model_request_id(message.payload.request_id)
            if parsed is None:
                return
            session_id, wal_seq = parsed
            session = self.sessions_by_id.get(session_id)
            if session is None or session.config.model_channel_id != message.channel_id:
                raise AgentStateError(f"infer.request {message.seq} maps to wrong session/channel")
            record = self.wal_records.get(wal_seq)
            if record is None:
                raise AgentStateError(f"infer.request {message.seq} references missing WAL {wal_seq}")
            attempt = session.attempts.setdefault(wal_seq, AttemptState(wal=record))
            attempt.request_seq = message.seq
            attempt.request = message.payload
            session.last_llm_request_seq = max(session.last_llm_request_seq, message.seq)
            if attempt.result is None and record.turn_id not in session.terminal_send_by_turn:
                session.in_flight_turn_id = record.turn_id
            return
        if isinstance(message.payload, InferResult):
            request = self._find_request(message.channel_id, message.payload.request_id, message.payload.prev_seq)
            if request is None:
                return
            attempt = request
            attempt.result_seq = message.seq
            attempt.result = message.payload
            session = self.sessions_by_model.get(message.channel_id)
            if session is not None and session.in_flight_turn_id == attempt.wal.turn_id:
                session.in_flight_turn_id = None

    def observe_im_send_request(self, session: SessionState, message: ImMessage) -> None:
        session.agent_send_request_by_seq[message.seq] = message
        request_id = message.request_id or ""
        turn = turn_from_im_request_id(request_id, self.wal_records)
        if turn is None:
            return
        existing = session.terminal_send_by_turn.get(turn)
        if existing is not None and existing.seq != message.seq:
            raise AgentStateError(f"turn {turn} has multiple terminal send.request messages")
        session.terminal_send_by_turn[turn] = message
        if session.in_flight_turn_id == turn:
            session.in_flight_turn_id = None

    def observe_im_send_result(self, session: SessionState, message: ImMessage) -> None:
        if message.prev_seq is not None:
            session.send_results_by_prev_seq[message.prev_seq] = message
        provider_message_id = message.data.get("provider_message_id")
        if isinstance(provider_message_id, str) and provider_message_id:
            session.send_result_by_provider_message_id[provider_message_id] = message

    def is_agent_echo(self, session: SessionState, message: ImMessage, agent_principal: int) -> bool:
        if message.principal == agent_principal:
            return True
        provider_message_id = message.data.get("provider_message_id")
        if isinstance(provider_message_id, str) and provider_message_id:
            result = session.send_result_by_provider_message_id.get(provider_message_id)
            if result is not None and result.prev_seq in session.agent_send_request_by_seq:
                return True
        if message.prev_seq is not None:
            result = session.send_results_by_prev_seq.get(message.prev_seq)
            if result is not None and result.prev_seq in session.agent_send_request_by_seq:
                return True
        return False

    def _find_request(self, channel_id: int, request_id: str, request_seq: int) -> AttemptState | None:
        for session in self.sessions_by_model.values():
            if session.config.model_channel_id != channel_id:
                continue
            for attempt in session.attempts.values():
                if attempt.request_seq == request_seq and attempt.request is not None and attempt.request.request_id == request_id:
                    return attempt
        return None


def turn_from_im_request_id(request_id: str, wal_records: dict[int, WalRecord]) -> str | None:
    if request_id.startswith("freeze:"):
        return request_id[len("freeze:"):]
    if not request_id.startswith("im:"):
        return None
    parsed = parse_model_request_id(request_id[len("im:"):])
    if parsed is None:
        return None
    _, wal_seq = parsed
    record = wal_records.get(wal_seq)
    if record is None:
        return None
    return record.turn_id
