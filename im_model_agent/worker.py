from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace
from typing import Any

from openevent.im_sdk import SendRequestInput, create_client as create_im_client
from openevent.model_proxy_sdk import (
    InferRequest,
    InferRequestInput,
    InferResult,
    create_client as create_model_client,
)
from openevent.model_proxy_sdk import parse_message as parse_llm_message
from openevent.model_proxy_sdk import publish_infer_request

from .config import AgentRuntimeConfig, ConfigError
from .prompt import (
    UserPromptMessage,
    append_assistant_message,
    build_model_messages,
    extract_assistant_text,
    extract_user_text,
)
from .state import AgentRuntimeState, AttemptState, SessionState
from .wal import encode_prepare, freeze_request_id, model_request_id, now_ms, parse_prepare

LOG = logging.getLogger(__name__)


class ImModelAgent:
    def __init__(self, config: AgentRuntimeConfig, openevent_client: Any):
        self.config = config
        self.openevent_client = openevent_client
        self.im_client = create_im_client(openevent_client)
        self.model_client = create_model_client(openevent_client, config.agent.token)
        self.state = AgentRuntimeState(config.sessions)
        self.stopped = False

    def start(self) -> None:
        scan_end_seq = self.recover()
        self.process_ready_sessions()
        self.consume(scan_end_seq + 1)

    def stop(self) -> None:
        self.stopped = True

    def recover(self) -> int:
        self.validate_channels()
        status = self.openevent_client.get_status(self.config.agent.principal, self.config.agent.token)
        scan_end_seq = int(status.max_seq)
        from_seq = 1
        while from_seq <= scan_end_seq:
            response = self.openevent_client.fetch(
                self.config.agent.principal,
                self.config.agent.token,
                from_seq=from_seq,
                limit=1000,
                only_my_recipient=False,
            )
            for message in response.messages:
                if int(message.seq) <= scan_end_seq:
                    self.observe_message(message, realtime=False)
            next_seq = int(response.next_seq)
            if next_seq <= from_seq:
                raise RuntimeError("Fetch did not advance next_seq during recovery")
            if not getattr(response, "has_more", False) and next_seq > scan_end_seq:
                break
            from_seq = next_seq
        self.state.max_seen_seq = scan_end_seq
        self._recover_session_progress()
        return scan_end_seq

    def validate_channels(self) -> None:
        for session in self.config.enabled_sessions:
            im = self._get_channel(session.im_channel_id)
            if im.protocol != "im.v1":
                raise ConfigError(f"channel {session.im_channel_id} protocol must be im.v1")
            im_desc = _json_description(im.description, session.im_channel_id)
            if im_desc.get("session_type") != "p2p":
                raise ConfigError(f"channel {session.im_channel_id} description.session_type must be p2p")
            _require_members(
                im,
                session.im_channel_id,
                {session.user_principal, self.config.agent.principal, self.config.im_sync_worker.principal},
            )

            model = self._get_channel(session.model_channel_id)
            if model.protocol != "llm.v1":
                raise ConfigError(f"channel {session.model_channel_id} protocol must be llm.v1")
            _require_members(
                model,
                session.model_channel_id,
                {self.config.agent.principal, self.config.model_proxy.principal},
            )

            wal = self._get_channel(session.wal_channel_id)
            if wal.protocol != "agent.wal.v1":
                raise ConfigError(f"channel {session.wal_channel_id} protocol must be agent.wal.v1")
            wal_desc = _json_description(wal.description, session.wal_channel_id)
            if wal_desc.get("version") != "v1":
                raise ConfigError(f"channel {session.wal_channel_id} WAL description.version must be v1")
            if wal_desc.get("session_id") != session.session_id:
                raise ConfigError(f"channel {session.wal_channel_id} WAL description.session_id mismatch")
            if int(wal_desc.get("im_channel_id", 0)) != session.im_channel_id:
                raise ConfigError(f"channel {session.wal_channel_id} WAL description.im_channel_id mismatch")
            if int(wal_desc.get("model_channel_id", 0)) != session.model_channel_id:
                raise ConfigError(f"channel {session.wal_channel_id} WAL description.model_channel_id mismatch")
            _require_members(wal, session.wal_channel_id, {self.config.agent.principal})

    def consume(self, from_seq: int) -> None:
        next_seq = from_seq
        while not self.stopped:
            response = self.openevent_client.fetch(
                self.config.agent.principal,
                self.config.agent.token,
                from_seq=next_seq,
                limit=1000,
                only_my_recipient=self.config.openevent.subscribe.only_my_recipient,
            )
            if not response.messages:
                next_seq = int(response.next_seq)
                time.sleep(self.config.openevent.subscribe.idle_sleep_ms / 1000)
                continue
            for message in response.messages:
                self.observe_message(message, realtime=True)
                next_seq = int(message.seq) + 1
                self.state.max_seen_seq = max(self.state.max_seen_seq, int(message.seq))
            self.process_ready_sessions()

    def observe_message(self, message: Any, realtime: bool) -> None:
        channel_id = int(message.channel_id)
        if channel_id in self.state.sessions_by_im:
            self._observe_im_message(message)
            return
        if channel_id in self.state.sessions_by_wal:
            self.state.observe_wal(int(message.seq), channel_id, parse_prepare(message.payload))
            return
        if channel_id in self.state.sessions_by_model:
            parsed = parse_llm_message(message)
            self.state.observe_llm_message(parsed)
            if isinstance(parsed.payload, InferResult) and realtime:
                self._handle_model_result(parsed)

    def process_ready_sessions(self) -> None:
        for session in self.state.sessions_by_id.values():
            if session.in_flight_turn_id:
                self._check_in_flight_timeout(session)
                continue
            if session.frozen or not session.pending:
                continue
            self._start_turn(session)

    def _observe_im_message(self, message: Any) -> None:
        try:
            parsed = self.im_client.parse_message(message)
        except Exception as exc:
            LOG.warning("im payload parse failed", extra={"seq": int(message.seq), "error": str(exc)})
            return
        session = self.state.sessions_by_im[parsed.channel_id]
        if parsed.kind == "send.request" and parsed.principal == self.config.agent.principal:
            self.state.observe_im_send_request(session, parsed)
            return
        if parsed.kind == "send.result" and parsed.principal == self.config.im_sync_worker.principal:
            self.state.observe_im_send_result(session, parsed)
            if parsed.data.get("status") == "FAILED":
                session.frozen = True
            return
        if parsed.kind != "sync.record":
            return
        if self.state.is_agent_echo(session, parsed, self.config.agent.principal):
            return
        if parsed.principal != session.config.user_principal:
            return
        user_message = UserPromptMessage(
            seq=parsed.seq,
            event_ms=parsed.event_ms,
            text=extract_user_text(parsed.data, self.config.agent.non_text_placeholder),
        )
        if session.frozen:
            session.frozen = False
        self.state.observe_user_message(session, user_message)

    def _start_turn(self, session: SessionState) -> None:
        pending = list(session.pending)
        if not pending:
            return
        turn = f"{session.config.session_id}:{pending[0].seq}"
        if turn in session.terminal_send_by_turn:
            session.remove_pending_seqs(tuple(item.seq for item in pending))
            return
        messages = build_model_messages(
            system_prompt=self.config.agent.system_prompt,
            previous_messages=session.prompt_messages,
            pending_user_messages=pending,
            max_context_messages=self.config.agent.max_context_messages,
        )
        self._publish_attempt(session, pending, messages, turn)

    def _publish_attempt(
        self,
        session: SessionState,
        pending: list[UserPromptMessage],
        messages: list[dict[str, Any]],
        turn: str,
    ) -> None:
        user_message_seqs = tuple(item.seq for item in pending)
        wal_payload = encode_prepare(
            pre_llm_seq=session.last_llm_request_seq,
            user_message_seqs=user_message_seqs,
            ts_ms=now_ms(),
        )
        wal_response = self.openevent_client.publish_auto_seq(
            principal=self.config.agent.principal,
            token=self.config.agent.token,
            channel_id=session.config.wal_channel_id,
            payload=wal_payload,
            recipients=(),
        )
        wal_seq = int(wal_response.seq)
        self.state.observe_wal(wal_seq, session.config.wal_channel_id, parse_prepare(wal_payload))
        request_id = model_request_id(session.config.session_id, wal_seq)
        ts_ms = now_ms()
        body = {
            "model": self.config.agent.model,
            "messages": messages,
            "stream": False,
        }
        request_seq = publish_infer_request(
            self.model_client,
            channel_id=session.config.model_channel_id,
            principal=self.config.agent.principal,
            req=InferRequestInput(
                request_id=request_id,
                method="POST",
                path="/v1/chat/completions",
                body=body,
                ts_ms=ts_ms,
            ),
        )
        attempt = session.attempts[wal_seq]
        attempt.request_seq = request_seq
        attempt.request = InferRequest(
            request_id=request_id,
            method="POST",
            path="/v1/chat/completions",
            ts_ms=ts_ms,
            body=body,
        )
        session.last_llm_request_seq = max(session.last_llm_request_seq, request_seq)
        session.in_flight_turn_id = turn

    def _recover_session_progress(self) -> None:
        for session in self.state.sessions_by_id.values():
            self._rebuild_prompt_from_terminal_turns(session)
            self._recover_model_results_without_im_reply(session)
            self._recover_orphan_wal(session)

    def _rebuild_prompt_from_terminal_turns(self, session: SessionState) -> None:
        prompt_messages: list[dict[str, Any]] = []
        for attempt in sorted(session.attempts.values(), key=lambda item: item.request_seq or 0):
            if attempt.wal.turn_id not in session.terminal_send_by_turn:
                continue
            successful_messages = self._prompt_messages_for_successful_attempt(attempt)
            if successful_messages is not None:
                prompt_messages = successful_messages
        session.prompt_messages = prompt_messages

    def _recover_model_results_without_im_reply(self, session: SessionState) -> None:
        attempts = sorted(
            session.attempts.values(),
            key=lambda item: item.result_seq or item.request_seq or item.wal.seq,
        )
        for attempt in attempts:
            if attempt.request is None or attempt.result is None:
                continue
            if attempt.wal.turn_id in session.terminal_send_by_turn:
                continue
            if attempt.result.status_code < 200 or attempt.result.status_code >= 300:
                continue
            self._handle_model_result(
                SimpleNamespace(channel_id=session.config.model_channel_id, payload=attempt.result)
            )

    def _recover_orphan_wal(self, session: SessionState) -> None:
        if session.in_flight_turn_id:
            return
        attempts = sorted(session.attempts.values(), key=lambda item: item.wal.seq)
        for attempt in attempts:
            if attempt.request is not None or attempt.wal.turn_id in session.terminal_send_by_turn:
                continue
            messages = self._messages_for_orphan_wal(session, attempt)
            self._publish_model_request_for_wal(session, attempt, messages)
            return

    def _messages_for_orphan_wal(self, session: SessionState, attempt: Any) -> list[dict[str, Any]]:
        same_turn_requests = [
            item
            for item in session.attempts.values()
            if item.wal.turn_id == attempt.wal.turn_id and item.request is not None
        ]
        if same_turn_requests:
            latest = max(same_turn_requests, key=lambda item: item.request_seq or 0)
            return list(latest.request.body["messages"])
        pending = []
        for seq in attempt.wal.payload.user_message_seqs:
            message = self.state.user_messages.get(seq)
            if message is None:
                raise RuntimeError(f"cannot rebuild orphan WAL {attempt.wal.seq}: missing user message {seq}")
            pending.append(message)
        return build_model_messages(
            system_prompt=self.config.agent.system_prompt,
            previous_messages=session.prompt_messages,
            pending_user_messages=pending,
            max_context_messages=self.config.agent.max_context_messages,
        )

    def _publish_model_request_for_wal(
        self,
        session: SessionState,
        attempt: Any,
        messages: list[dict[str, Any]],
    ) -> int:
        request_id = model_request_id(session.config.session_id, attempt.wal.seq)
        ts_ms = now_ms()
        body = {"model": self.config.agent.model, "messages": messages, "stream": False}
        request_seq = publish_infer_request(
            self.model_client,
            channel_id=session.config.model_channel_id,
            principal=self.config.agent.principal,
            req=InferRequestInput(
                request_id=request_id,
                method="POST",
                path="/v1/chat/completions",
                body=body,
                ts_ms=ts_ms,
            ),
        )
        attempt.request_seq = request_seq
        attempt.request = InferRequest(
            request_id=request_id,
            method="POST",
            path="/v1/chat/completions",
            ts_ms=ts_ms,
            body=body,
        )
        session.last_llm_request_seq = max(session.last_llm_request_seq, request_seq)
        session.in_flight_turn_id = attempt.wal.turn_id
        return request_seq

    def _handle_model_result(self, message: Any) -> None:
        parsed = message.payload
        if parsed.status_code < 200 or parsed.status_code >= 300:
            LOG.warning("model result non-2xx", extra={"request_id": parsed.request_id, "status_code": parsed.status_code})
            return
        session = self.state.sessions_by_model.get(message.channel_id)
        if session is None:
            return
        assistant_text = extract_assistant_text(parsed.body)
        if not assistant_text:
            LOG.warning("model result missing assistant text", extra={"request_id": parsed.request_id})
            return
        matched_attempt = self._find_attempt_for_result(session, parsed)
        if matched_attempt is None or matched_attempt.wal.turn_id in session.terminal_send_by_turn:
            return
        turn = matched_attempt.wal.turn_id
        send_seq = self._publish_im_reply(session, f"im:{parsed.request_id}", assistant_text)
        session.terminal_send_by_turn[turn] = SimpleNamespace(seq=send_seq, request_id=f"im:{parsed.request_id}")
        session.prompt_messages = self._prompt_messages_for_successful_attempt(matched_attempt, assistant_text) or []
        session.in_flight_turn_id = None

    def _find_attempt_for_result(self, session: SessionState, result: InferResult) -> AttemptState | None:
        for attempt in session.attempts.values():
            if attempt.request is None:
                continue
            if attempt.request.request_id == result.request_id and attempt.request_seq == result.prev_seq:
                return attempt
        return None

    def _prompt_messages_for_successful_attempt(
        self,
        attempt: AttemptState,
        assistant_text: str | None = None,
    ) -> list[dict[str, Any]] | None:
        if attempt.request is None or attempt.result is None:
            return None
        if attempt.result.status_code < 200 or attempt.result.status_code >= 300:
            return None
        resolved_text = assistant_text if assistant_text is not None else extract_assistant_text(attempt.result.body)
        if not resolved_text:
            return None
        return append_assistant_message(
            list(attempt.request.body.get("messages", [])),
            resolved_text,
            self.config.agent.max_context_messages,
        )

    def _check_in_flight_timeout(self, session: SessionState) -> None:
        turn = session.in_flight_turn_id
        if turn is None:
            return
        attempts = [item for item in session.attempts.values() if item.wal.turn_id == turn]
        if not attempts:
            session.in_flight_turn_id = None
            return
        latest = max(attempts, key=lambda item: item.wal.seq)
        if latest.result is not None or latest.request is None:
            return
        if now_ms() < latest.request.ts_ms + self.config.agent.model_timeout_ms:
            return
        if len(attempts) >= self.config.agent.max_model_attempts:
            if turn not in session.terminal_send_by_turn:
                request_id = freeze_request_id(turn)
                send_seq = self._publish_im_reply(session, request_id, self.config.agent.freeze_message)
                session.terminal_send_by_turn[turn] = SimpleNamespace(seq=send_seq, request_id=request_id)
            session.frozen = True
            session.in_flight_turn_id = None
            return
        pending = [self.state.user_messages[seq] for seq in latest.wal.payload.user_message_seqs]
        messages = list(latest.request.body["messages"])
        self._publish_attempt(session, pending, messages, turn)

    def _publish_im_reply(self, session: SessionState, request_id: str, text: str) -> int:
        seq = self.im_client.publish_send_request(
            principal=self.config.agent.principal,
            token=self.config.agent.token,
            channel_id=session.config.im_channel_id,
            req=SendRequestInput(
                request_id=request_id,
                msg_type="text",
                content={"text": text},
                event_ms=now_ms(),
            ),
        )
        return seq

    def _get_channel(self, channel_id: int) -> Any:
        response = self.openevent_client.get_channel(
            self.config.agent.principal,
            self.config.agent.token,
            channel_id,
        )
        return response.channel


def _json_description(value: str, channel_id: int) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"channel {channel_id} description must be JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"channel {channel_id} description must be a JSON object")
    return data


def _require_members(channel: Any, channel_id: int, required: set[int]) -> None:
    visibility = int(getattr(channel, "visibility", 0))
    if visibility == 0:
        return
    members = set(int(item) for item in getattr(channel, "members", []))
    missing = required - members
    if missing:
        raise ConfigError(f"channel {channel_id} missing members: {sorted(missing)}")
