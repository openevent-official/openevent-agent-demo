from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from openevent.cmd_sdk.model import CmdOutputReadRequest, CmdOutputReadResult, CmdRunRequest, CmdRunResult
from openevent.im_sdk import ParsedMessage as ImMessage
from openevent.model_proxy_sdk import InferRequest, InferResult

from .config import SessionConfig
from .prompt import AssistantMessage, ToolCall, UserPromptMessage
from .wal import CmdTimeout, InputRef, WalPrepare, parse_model_request_id


class AgentStateError(RuntimeError):
    pass


@dataclass
class WalRecord:
    seq: int
    payload: WalPrepare
    attempts: dict[int, "AttemptState"] = field(default_factory=dict)
    blocked: bool = False

    @property
    def latest_retry(self) -> int:
        return max(self.attempts, default=0)

    @property
    def latest_attempt(self) -> "AttemptState | None":
        return self.attempts.get(self.latest_retry)


@dataclass
class AttemptState:
    wal: WalRecord
    retry_index: int
    request_seq: int | None = None
    request: InferRequest | None = None
    result_seq: int | None = None
    result: InferResult | None = None
    assistant: AssistantMessage | None = None
    accepted: bool = False
    stale: bool = False

@dataclass
class CommandState:
    model_request_id: str
    tool_call_index: int
    tool_call: ToolCall
    request_seq: int | None = None
    request: CmdRunRequest | CmdOutputReadRequest | None = None
    result_seq: int | None = None
    result: CmdRunResult | CmdOutputReadResult | None = None
    timeout_seq: int | None = None
    timeout: CmdTimeout | None = None

    @property
    def request_id(self) -> str:
        return f"cmd:{self.model_request_id}:{self.tool_call_index}"

    @property
    def complete(self) -> bool:
        return self.result is not None or self.timeout is not None


@dataclass(frozen=True)
class ImRequestState:
    seq: int
    principal: int
    recipients: tuple[int, ...]
    request_id: str
    event_ms: int
    data: dict[str, Any]


@dataclass
class SessionState:
    config: SessionConfig
    pending_users: dict[int, UserPromptMessage] = field(default_factory=dict)
    pending_tool_refs: dict[int, InputRef] = field(default_factory=dict)
    prompt_messages: list[dict[str, Any]] = field(default_factory=list)
    wal_by_seq: dict[int, WalRecord] = field(default_factory=dict)
    wal_by_prepare_id: dict[str, WalRecord] = field(default_factory=dict)
    wal_aliases: dict[int, int] = field(default_factory=dict)
    prepared_user_seqs: set[int] = field(default_factory=set)
    prepared_input_seqs: set[int] = field(default_factory=set)
    commands_by_id: dict[str, CommandState] = field(default_factory=dict)
    commands_by_seq: dict[int, CommandState] = field(default_factory=dict)
    timeouts_by_id: dict[str, tuple[int, CmdTimeout]] = field(default_factory=dict)
    timeout_aliases: dict[int, int] = field(default_factory=dict)
    im_requests_by_id: dict[str, ImRequestState] = field(default_factory=dict)
    im_requests_by_seq: dict[int, ImRequestState] = field(default_factory=dict)
    model_request_aliases: dict[int, int] = field(default_factory=dict)
    model_result_aliases: dict[int, int] = field(default_factory=dict)
    im_request_aliases: dict[int, int] = field(default_factory=dict)
    im_results_by_prev_seq: dict[int, ImMessage] = field(default_factory=dict)
    im_results_by_provider_id: dict[str, ImMessage] = field(default_factory=dict)
    last_llm_request_seq: int = 0

    @property
    def active_wal(self) -> WalRecord | None:
        for wal in sorted(self.wal_by_seq.values(), key=lambda item: item.seq):
            attempt = wal.latest_attempt
            if wal.blocked or attempt is None or not attempt.accepted:
                return wal
            if attempt.assistant and attempt.assistant.tool_calls:
                commands = [self.commands_by_id.get(f"cmd:{_request_id(self.config.session_id, wal.seq, attempt.retry_index)}:{i}") for i in range(len(attempt.assistant.tool_calls))]
                if not all(command is not None and command.complete for command in commands):
                    return wal
        return None

    def queue_user(self, message: UserPromptMessage) -> None:
        self.pending_users.setdefault(message.seq, message)

    def consume_prepare_inputs(self, prepare: WalPrepare) -> None:
        for seq in prepare.user_message_seqs:
            self.pending_users.pop(seq, None)
        for ref in prepare.input_event_refs:
            self.pending_tool_refs.pop(ref.seq, None)


class AgentRuntimeState:
    def __init__(self, sessions: tuple[SessionConfig, ...]):
        enabled = tuple(session for session in sessions if session.enabled)
        self.sessions_by_id = {config.session_id: SessionState(config) for config in enabled}
        self.sessions_by_im = {config.im_channel_id: self.sessions_by_id[config.session_id] for config in enabled}
        self.sessions_by_model = {config.model_channel_id: self.sessions_by_id[config.session_id] for config in enabled}
        self.sessions_by_wal = {config.wal_channel_id: self.sessions_by_id[config.session_id] for config in enabled}
        self.sessions_by_cmd = {config.cmd_channel_id: self.sessions_by_id[config.session_id] for config in enabled}
        self.user_messages: dict[int, UserPromptMessage] = {}
        self.user_sessions_by_seq: dict[int, SessionState] = {}
        self.max_seen_seq = 0

    def observe_user(self, session: SessionState, message: UserPromptMessage) -> None:
        owner = self.user_sessions_by_seq.get(message.seq)
        if owner is not None and owner is not session:
            raise AgentStateError(f"user message {message.seq} maps to multiple sessions")
        self.user_messages[message.seq] = message
        self.user_sessions_by_seq[message.seq] = session
        session.queue_user(message)

    def observe_prepare(self, session: SessionState, seq: int, prepare: WalPrepare) -> WalRecord:
        existing = session.wal_by_seq.get(seq)
        if existing is not None:
            if existing.payload != prepare:
                raise AgentStateError(f"conflicting WAL at seq {seq}")
            return existing
        canonical = session.wal_by_prepare_id.get(prepare.prepare_id)
        if canonical is not None:
            if canonical.payload != prepare:
                raise AgentStateError(f"conflicting prepare_id {prepare.prepare_id}")
            if seq < canonical.seq:
                raise AgentStateError(f"WAL duplicate {seq} arrived before canonical seq {canonical.seq}")
            session.wal_aliases[seq] = canonical.seq
            return canonical
        self._validate_prepare(session, seq, prepare)
        wal = WalRecord(seq=seq, payload=prepare)
        session.wal_by_seq[seq] = wal
        session.wal_by_prepare_id[prepare.prepare_id] = wal
        session.prepared_user_seqs.update(prepare.user_message_seqs)
        session.prepared_input_seqs.update(ref.seq for ref in prepare.input_event_refs)
        session.consume_prepare_inputs(prepare)
        return wal

    def _validate_prepare(self, session: SessionState, seq: int, prepare: WalPrepare) -> None:
        if prepare.pre_llm_seq != session.last_llm_request_seq:
            raise AgentStateError(
                f"WAL {seq} pre_llm_seq {prepare.pre_llm_seq} does not match latest model request {session.last_llm_request_seq}"
            )
        for user_seq in prepare.user_message_seqs:
            if user_seq >= seq:
                raise AgentStateError(f"WAL {seq} user reference {user_seq} must precede the WAL")
            if self.user_sessions_by_seq.get(user_seq) is not session:
                raise AgentStateError(f"WAL {seq} user reference {user_seq} does not belong to this session")
            if user_seq in session.prepared_user_seqs:
                raise AgentStateError(f"WAL {seq} reuses user reference {user_seq}")
        for ref in prepare.input_event_refs:
            if ref.seq >= seq:
                raise AgentStateError(f"WAL {seq} input reference {ref.seq} must precede the WAL")
            if ref.seq in session.prepared_input_seqs:
                raise AgentStateError(f"WAL {seq} reuses input reference {ref.seq}")
            command = self.command_for_input_ref(session, ref)
            if command is None:
                raise AgentStateError(f"WAL {seq} has invalid {ref.type} reference {ref.seq}")

    def command_for_input_ref(self, session: SessionState, ref: InputRef) -> CommandState | None:
        for command in session.commands_by_id.values():
            if ref.type == "cmd_timeout" and command.timeout_seq == ref.seq:
                return command
            if command.result_seq != ref.seq:
                continue
            if ref.type == "exec_result" and isinstance(command.result, CmdRunResult):
                return command
            if not isinstance(command.request, CmdOutputReadRequest) or not isinstance(command.result, CmdOutputReadResult):
                continue
            if ref.type == f"read_{command.request.stream}_result":
                return command
        return None

    def observe_model_request(self, session: SessionState, seq: int, request: InferRequest) -> AttemptState | None:
        parsed = parse_model_request_id(request.request_id)
        if parsed is None:
            return None
        session_id, wal_seq, retry = parsed
        if session_id != session.config.session_id:
            raise AgentStateError(f"model request {seq} maps to wrong session")
        if wal_seq in session.wal_aliases:
            return None
        wal = session.wal_by_seq.get(wal_seq)
        if wal is None:
            raise AgentStateError(f"model request {seq} references missing WAL {wal_seq}")
        attempt = wal.attempts.setdefault(retry, AttemptState(wal=wal, retry_index=retry))
        if attempt.request_seq is not None:
            if attempt.request != request:
                raise AgentStateError(f"conflicting model request ID {request.request_id}")
            if seq < attempt.request_seq:
                raise AgentStateError(
                    f"model request duplicate {seq} arrived before canonical seq {attempt.request_seq}"
                )
            if seq != attempt.request_seq:
                session.model_request_aliases[seq] = attempt.request_seq
            return attempt
        attempt.request_seq, attempt.request = seq, request
        session.last_llm_request_seq = max(session.last_llm_request_seq, seq)
        return attempt

    def observe_model_result(self, session: SessionState, seq: int, result: InferResult) -> AttemptState | None:
        parsed = parse_model_request_id(result.request_id)
        if parsed is None:
            return None
        session_id, wal_seq, retry = parsed
        if session_id != session.config.session_id:
            raise AgentStateError(f"model result {seq} maps to wrong session")
        wal = session.wal_by_seq.get(wal_seq)
        attempt = wal.attempts.get(retry) if wal else None
        if attempt is None or attempt.request_seq != result.prev_seq:
            return None
        if attempt.result_seq is not None:
            if attempt.result != result:
                raise AgentStateError(f"conflicting model result for request ID {result.request_id}")
            if seq < attempt.result_seq:
                raise AgentStateError(
                    f"model result duplicate {seq} arrived before canonical seq {attempt.result_seq}"
                )
            if seq != attempt.result_seq:
                session.model_result_aliases[seq] = attempt.result_seq
            return attempt
        attempt.result_seq, attempt.result = seq, result
        attempt.stale = retry != wal.latest_retry
        return attempt

    def observe_timeout(self, session: SessionState, seq: int, timeout: CmdTimeout) -> CommandState | None:
        existing = session.timeouts_by_id.get(timeout.timeout_id)
        if existing is not None:
            existing_seq, existing_timeout = existing
            if existing_timeout != timeout:
                raise AgentStateError(f"conflicting timeout_id {timeout.timeout_id}")
            if seq < existing_seq:
                raise AgentStateError(f"timeout duplicate {seq} arrived before canonical seq {existing_seq}")
            session.timeout_aliases[seq] = existing_seq
            return session.commands_by_id.get(timeout.cmd_request_id)
        command = session.commands_by_id.get(timeout.cmd_request_id)
        if command is None:
            raise AgentStateError(f"timeout {seq} references unknown Cmd request {timeout.cmd_request_id}")
        if command.request_seq != timeout.cmd_request_seq:
            raise AgentStateError(f"timeout {seq} cmd_request_seq does not match {timeout.cmd_request_id}")
        if command.tool_call.id != timeout.tool_call_id or command.tool_call.name != timeout.tool_name:
            raise AgentStateError(f"timeout {seq} tool call does not match {timeout.cmd_request_id}")
        session.timeouts_by_id[timeout.timeout_id] = (seq, timeout)
        if command.result_seq is not None and command.result_seq < seq:
            return command
        command.timeout_seq, command.timeout = seq, timeout
        session.pending_tool_refs.setdefault(seq, InputRef("cmd_timeout", seq))
        return command

    def observe_im_request(self, session: SessionState, message: ImMessage) -> None:
        self.record_im_request(
            session,
            ImRequestState(
                seq=message.seq,
                principal=message.principal,
                recipients=tuple(message.recipients),
                request_id=message.request_id or "",
                event_ms=message.event_ms,
                data=message.data,
            ),
        )

    def record_im_request(self, session: SessionState, request: ImRequestState) -> None:
        existing = session.im_requests_by_id.get(request.request_id)
        if existing is not None:
            if _im_request_content(existing) != _im_request_content(request):
                raise AgentStateError(f"conflicting IM request ID {request.request_id}")
            if request.seq < existing.seq:
                raise AgentStateError(
                    f"IM request duplicate {request.seq} arrived before canonical seq {existing.seq}"
                )
            if request.seq != existing.seq:
                session.im_request_aliases[request.seq] = existing.seq
            return
        session.im_requests_by_id[request.request_id] = request
        session.im_requests_by_seq[request.seq] = request

    def observe_im_result(self, session: SessionState, message: ImMessage) -> None:
        if message.prev_seq is not None:
            session.im_results_by_prev_seq[message.prev_seq] = message
        provider_id = message.data.get("provider_message_id")
        if isinstance(provider_id, str) and provider_id:
            session.im_results_by_provider_id[provider_id] = message

    def is_agent_echo(self, session: SessionState, message: ImMessage, agent_principal: int) -> bool:
        if message.principal == agent_principal:
            return True
        provider_id = message.data.get("provider_message_id")
        if isinstance(provider_id, str) and provider_id in session.im_results_by_provider_id:
            return True
        return message.prev_seq in session.im_results_by_prev_seq if message.prev_seq is not None else False

def _request_id(session_id: str, wal_seq: int, retry: int) -> str:
    return f"agent:{session_id}:wal:{wal_seq}:retry:{retry}"


def _im_request_content(message: ImRequestState) -> tuple[Any, ...]:
    return (
        message.principal,
        tuple(message.recipients),
        message.request_id,
        message.event_ms,
        message.data,
    )
