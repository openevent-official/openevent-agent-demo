from __future__ import annotations

import json
import logging
import time
from typing import Any

from grpc import RpcError, StatusCode
from openevent.cmd_sdk import (
    CmdOutputReadRequest,
    CmdOutputReadRequestInput,
    CmdOutputReadResult,
    CmdRunRequest,
    CmdRunRequestInput,
    CmdRunResult,
    create_client as create_cmd_client,
    parse_message as parse_cmd_message,
)
from openevent.im_sdk import SendRequestInput, create_client as create_im_client
from openevent.model_proxy_sdk import InferRequest, InferRequestInput, InferResult, create_client as create_model_client
from openevent.model_proxy_sdk import parse_message as parse_llm_message
from openevent.model_proxy_sdk import publish_infer_request

from .config import AgentRuntimeConfig, ConfigError
from .prompt import (
    AssistantMessage,
    ToolCall,
    UserPromptMessage,
    build_model_messages,
    command_result_event,
    extract_user_text,
    model_tools,
    output_read_event,
    parse_assistant_message,
    system_prompt_content,
    tool_result_message,
    trim_messages,
    visible_content,
)
from .state import AgentRuntimeState, AgentStateError, AttemptState, CommandState, ImRequestState, SessionState
from .wal import CmdTimeout, InputRef, WalPrepare, encode_cmd_timeout, encode_prepare, model_request_id, now_ms, parse_wal


LOG = logging.getLogger(__name__)
DEFAULT_OUTPUT_READ_BYTES = 65536
_GUARANTEED_NOT_COMMITTED = frozenset(
    {
        StatusCode.UNAUTHENTICATED,
        StatusCode.PERMISSION_DENIED,
        StatusCode.NOT_FOUND,
        StatusCode.INVALID_ARGUMENT,
        StatusCode.RESOURCE_EXHAUSTED,
        StatusCode.ABORTED,
    }
)


class ImModelAgent:
    def __init__(self, config: AgentRuntimeConfig, openevent_client: Any):
        self.config = config
        self.openevent_client = openevent_client
        self.im_client = create_im_client(openevent_client)
        self.model_client = create_model_client(openevent_client, config.agent.token)
        self.cmd_client = create_cmd_client(openevent_client)
        self.state = AgentRuntimeState(config.sessions)
        self.stopped = False

    def start(self) -> None:
        scan_end = self.recover()
        self.process_ready_sessions()
        self.consume(scan_end + 1)

    def stop(self) -> None:
        self.stopped = True

    def recover(self) -> int:
        self.validate_channels()
        scan_end = int(self.openevent_client.get_status(self.config.agent.principal, self.config.agent.token).max_seq)
        next_seq = 1
        while next_seq <= scan_end:
            response = self.openevent_client.fetch(
                self.config.agent.principal,
                self.config.agent.token,
                from_seq=next_seq,
                limit=1000,
                only_my_recipient=False,
                channels=self._fetch_channels(),
            )
            for message in response.messages:
                if int(message.seq) <= scan_end:
                    self.observe_message(message, realtime=False)
            advanced = int(response.next_seq)
            if advanced <= next_seq:
                raise RuntimeError("Fetch did not advance during recovery")
            next_seq = advanced
        self.state.max_seen_seq = scan_end
        self._rebuild_sessions()
        return scan_end

    def validate_channels(self) -> None:
        for session in self.config.enabled_sessions:
            im = self._get_channel(session.im_channel_id)
            if im.protocol != "im.v1":
                raise ConfigError(f"channel {session.im_channel_id} protocol must be im.v1")
            description = _json_description(im.description, session.im_channel_id)
            if description.get("session_type") != "p2p":
                raise ConfigError(f"channel {session.im_channel_id} description.session_type must be p2p")
            _require_members(im, session.im_channel_id, {session.user_principal, self.config.agent.principal, self.config.im_sync_worker.principal})

            model = self._get_channel(session.model_channel_id)
            if model.protocol != "llm.v1":
                raise ConfigError(f"channel {session.model_channel_id} protocol must be llm.v1")
            _require_members(model, session.model_channel_id, {self.config.agent.principal, self.config.model_proxy.principal})

            wal = self._get_channel(session.wal_channel_id)
            if wal.protocol != "agent.wal.v1":
                raise ConfigError(f"channel {session.wal_channel_id} protocol must be agent.wal.v1")
            wal_description = _json_description(wal.description, session.wal_channel_id)
            expected = {
                "version": "v1",
                "session_id": session.session_id,
                "im_channel_id": session.im_channel_id,
                "model_channel_id": session.model_channel_id,
            }
            if any(wal_description.get(key) != value for key, value in expected.items()):
                raise ConfigError(f"channel {session.wal_channel_id} WAL description mismatch")
            _require_members(wal, session.wal_channel_id, {self.config.agent.principal})

            cmd = self._get_channel(session.cmd_channel_id)
            if cmd.protocol != "cmd.v1":
                raise ConfigError(f"channel {session.cmd_channel_id} protocol must be cmd.v1")
            _require_members(cmd, session.cmd_channel_id, {self.config.agent.principal, self.config.cmd_worker.principal})

    def consume(self, from_seq: int) -> None:
        next_seq = from_seq
        while not self.stopped:
            response = self.openevent_client.fetch(
                self.config.agent.principal,
                self.config.agent.token,
                from_seq=next_seq,
                limit=1000,
                only_my_recipient=self.config.openevent.subscribe.only_my_recipient,
                channels=self._fetch_channels(),
            )
            for message in response.messages:
                self.observe_message(message, realtime=True)
                self.state.max_seen_seq = max(self.state.max_seen_seq, int(message.seq))
            next_seq = int(response.next_seq)
            self.process_ready_sessions()
            if not response.messages:
                time.sleep(self.config.openevent.subscribe.idle_sleep_ms / 1000)

    def observe_message(self, message: Any, realtime: bool) -> None:
        channel_id = int(message.channel_id)
        if channel_id in self.state.sessions_by_im:
            self._observe_im(message)
        elif channel_id in self.state.sessions_by_wal:
            self._observe_wal(message)
        elif channel_id in self.state.sessions_by_model:
            self._observe_model(message, realtime)
        elif channel_id in self.state.sessions_by_cmd:
            self._observe_cmd(message, realtime)

    def process_ready_sessions(self) -> None:
        for session in self.state.sessions_by_id.values():
            self._advance(session)

    def unblock(self, session_id: str) -> None:
        session = self.state.sessions_by_id[session_id]
        wal = session.active_wal
        if wal is None or not wal.blocked:
            return
        wal.blocked = False
        self._publish_model_request(session, wal, wal.latest_retry + 1, self._retry_messages(wal))

    def _observe_im(self, message: Any) -> None:
        parsed = self.im_client.parse_message(message)
        session = self.state.sessions_by_im[parsed.channel_id]
        if parsed.kind == "send.request" and parsed.principal == self.config.agent.principal:
            self.state.observe_im_request(session, parsed)
            return
        if parsed.kind == "send.result" and parsed.principal == self.config.im_sync_worker.principal:
            if parsed.data.get("status") == "FAILED":
                LOG.warning("ignoring legacy failed IM result", extra={"request_id": parsed.request_id})
                return
            self.state.observe_im_result(session, parsed)
            return
        if parsed.kind != "sync.record" or self.state.is_agent_echo(session, parsed, self.config.agent.principal):
            return
        if parsed.principal != session.config.user_principal:
            return
        self.state.observe_user(
            session,
            UserPromptMessage(
                seq=parsed.seq,
                event_ms=parsed.event_ms,
                text=extract_user_text(parsed.data, self.config.agent.non_text_placeholder),
            ),
        )

    def _observe_wal(self, message: Any) -> None:
        session = self.state.sessions_by_wal[int(message.channel_id)]
        parsed = parse_wal(message.payload)
        if isinstance(parsed, WalPrepare):
            self.state.observe_prepare(session, int(message.seq), parsed)
        else:
            self.state.observe_timeout(session, int(message.seq), parsed)

    def _observe_model(self, message: Any, realtime: bool) -> None:
        parsed = parse_llm_message(message)
        session = self.state.sessions_by_model[parsed.channel_id]
        attempt: AttemptState | None
        if isinstance(parsed.payload, InferRequest):
            if parsed.principal != self.config.agent.principal:
                return
            attempt = self.state.observe_model_request(session, parsed.seq, parsed.payload)
        elif isinstance(parsed.payload, InferResult):
            if parsed.principal != self.config.model_proxy.principal or self.config.agent.principal not in parsed.recipients:
                return
            attempt = self.state.observe_model_result(session, parsed.seq, parsed.payload)
            if attempt is not None:
                if realtime:
                    self._accept_or_retry(session, attempt)
                elif 200 <= parsed.payload.status_code < 300:
                    assistant = parse_assistant_message(parsed.payload.body)
                    if assistant is not None:
                        attempt.assistant, attempt.accepted = assistant, True

    def _observe_cmd(self, message: Any, realtime: bool) -> None:
        parsed = parse_cmd_message(message)
        session = self.state.sessions_by_cmd[parsed.channel_id]
        payload = parsed.payload
        if isinstance(payload, (CmdRunRequest, CmdOutputReadRequest)):
            if parsed.principal != self.config.agent.principal:
                return
            existing = session.commands_by_seq.get(parsed.seq)
            if existing is not None:
                if existing.request != payload:
                    raise AgentStateError(f"conflicting Cmd request at seq {parsed.seq}")
                return
            pending = self._next_unbound_tool(session)
            if pending is None:
                raise AgentStateError(f"Cmd request {parsed.seq} has no pending accepted tool call")
            attempt, index, tool_call = pending
            if not _cmd_request_matches_tool(payload, tool_call):
                raise AgentStateError(f"Cmd request {parsed.seq} does not match the next accepted tool call")
            assert attempt.request is not None
            model_id = attempt.request.request_id
            request_id = f"cmd:{model_id}:{index}"
            command = session.commands_by_id.setdefault(request_id, CommandState(model_id, index, tool_call))
            command.request_seq, command.request = parsed.seq, payload
            session.commands_by_seq[parsed.seq] = command
            return
        if isinstance(payload, (CmdRunResult, CmdOutputReadResult)):
            if parsed.principal != self.config.cmd_worker.principal or self.config.agent.principal not in parsed.recipients:
                return
            command = session.commands_by_seq.get(payload.prev_seq)
            if command is None or command.timeout is not None:
                return
            command.result_seq, command.result = parsed.seq, payload
            ref_type = "exec_result" if isinstance(payload, CmdRunResult) else f"read_{command.request.stream}_result"
            session.pending_tool_refs.setdefault(parsed.seq, InputRef(ref_type, parsed.seq))
            if realtime:
                self._advance(session)

    def _rebuild_sessions(self) -> None:
        for session in self.state.sessions_by_id.values():
            session.prompt_messages = []
            for wal in sorted(session.wal_by_seq.values(), key=lambda item: item.seq):
                attempt = wal.latest_attempt
                if attempt is None or attempt.result is None:
                    continue
                self._accept_or_retry(session, attempt, recovery=True)

    def _advance(self, session: SessionState) -> None:
        wal = session.active_wal
        if wal is not None:
            if wal.blocked:
                return
            attempt = wal.latest_attempt
            if attempt is None:
                self._publish_model_request(session, wal, 1, self._messages_for_prepare(session, wal.payload))
                return
            if attempt.result is None:
                if attempt.request and now_ms() >= attempt.request.ts_ms + self.config.agent.model_timeout_ms:
                    self._retry(session, attempt, "model request timeout")
                return
            if not attempt.accepted:
                self._accept_or_retry(session, attempt)
                return
            if attempt.assistant and attempt.assistant.tool_calls:
                self._advance_tools(session, attempt)
                return
        if session.pending_users or session.pending_tool_refs:
            self._publish_prepare(session)

    def _publish_prepare(self, session: SessionState) -> None:
        users = tuple(sorted(session.pending_users))
        refs = tuple(session.pending_tool_refs[seq] for seq in sorted(session.pending_tool_refs))
        payload = encode_prepare(
            pre_llm_seq=session.last_llm_request_seq,
            user_message_seqs=users,
            input_event_refs=refs,
        )
        expected = json.loads(payload.decode("utf-8"))
        seq = self._reliable_publish(
            channel_id=session.config.wal_channel_id,
            stable_id=expected["prepare_id"],
            expected=expected,
            publish=lambda: int(self.openevent_client.publish_auto_seq(
                principal=self.config.agent.principal,
                token=self.config.agent.token,
                channel_id=session.config.wal_channel_id,
                payload=payload,
                recipients=(),
            ).seq),
            decode=lambda message: _wal_identity(message, self.config.agent.principal),
        )
        wal = self.state.observe_prepare(session, seq, parse_wal(payload))
        self._publish_model_request(session, wal, 1, self._messages_for_prepare(session, wal.payload))

    def _messages_for_prepare(self, session: SessionState, prepare: WalPrepare) -> list[dict[str, Any]]:
        messages = [dict(item) for item in session.prompt_messages]
        if not messages:
            messages = [{"role": "system", "content": system_prompt_content(self.config.agent.system_prompt)}]
        for ref in prepare.input_event_refs:
            command = self._command_for_result_seq(session, ref.seq)
            if command is None:
                raise RuntimeError(f"missing command for input ref {ref.seq}")
            messages.append(tool_result_message(command.tool_call.id, _result_from_command(command)))
        users = [self.state.user_messages[seq] for seq in prepare.user_message_seqs]
        if users:
            messages = build_model_messages(
                system_prompt=self.config.agent.system_prompt,
                previous_messages=messages,
                pending_user_messages=users,
                max_context_messages=self.config.agent.max_context_messages,
            )
        return trim_messages(messages, self.config.agent.max_context_messages)

    def _publish_model_request(self, session: SessionState, wal: Any, retry: int, messages: list[dict[str, Any]]) -> None:
        request_id = model_request_id(session.config.session_id, wal.seq, retry)
        body = {"model": self.config.agent.model, "messages": messages, "tools": model_tools(), "stream": False}
        timestamp = now_ms()
        request = InferRequest(request_id=request_id, method="POST", path="/v1/chat/completions", ts_ms=timestamp, body=body)
        seq = self._reliable_publish(
            channel_id=session.config.model_channel_id,
            stable_id=request_id,
            expected=request,
            publish=lambda: publish_infer_request(
                self.model_client,
                channel_id=session.config.model_channel_id,
                principal=self.config.agent.principal,
                req=InferRequestInput(request_id=request_id, method="POST", path="/v1/chat/completions", body=body, ts_ms=timestamp),
            ),
            decode=lambda message: _model_request_identity(message, self.config.agent.principal),
        )
        attempt = wal.attempts.setdefault(retry, AttemptState(wal=wal, retry_index=retry))
        attempt.request_seq, attempt.request = seq, request
        session.last_llm_request_seq = max(session.last_llm_request_seq, seq)

    def _accept_or_retry(self, session: SessionState, attempt: AttemptState, recovery: bool = False) -> None:
        if attempt.stale or attempt.retry_index != attempt.wal.latest_retry:
            attempt.stale = True
            return
        result = attempt.result
        if result is None:
            return
        if result.status_code < 200 or result.status_code >= 300:
            if not recovery:
                self._retry(session, attempt, f"model status {result.status_code}")
            return
        assistant = parse_assistant_message(result.body)
        if assistant is None:
            if not recovery:
                self._retry(session, attempt, "invalid assistant message")
            return
        attempt.assistant, attempt.accepted = assistant, True
        session.prompt_messages = trim_messages(
            [*attempt.request.body.get("messages", []), assistant.raw],
            self.config.agent.max_context_messages,
        )
        content = visible_content(assistant.content)
        if content is not None:
            self._ensure_im_content(session, result.request_id, content)
        if assistant.tool_calls and not recovery:
            self._advance_tools(session, attempt)

    def _retry(self, session: SessionState, attempt: AttemptState, reason: str) -> None:
        wal = attempt.wal
        if attempt.retry_index != wal.latest_retry:
            return
        if wal.latest_retry % self.config.agent.max_model_attempts == 0:
            wal.blocked = True
            LOG.error("model WAL blocked", extra={"wal_seq": wal.seq, "reason": reason})
            return
        self._publish_model_request(session, wal, wal.latest_retry + 1, self._retry_messages(wal))

    def _retry_messages(self, wal: Any) -> list[dict[str, Any]]:
        latest = wal.latest_attempt
        if latest is None or latest.request is None:
            raise RuntimeError(f"cannot retry WAL {wal.seq} without request")
        return list(latest.request.body.get("messages", []))

    def _advance_tools(self, session: SessionState, attempt: AttemptState) -> None:
        assert attempt.assistant is not None
        for index, tool_call in enumerate(attempt.assistant.tool_calls):
            request_id = f"cmd:{attempt.result.request_id}:{index}"
            command = session.commands_by_id.get(request_id)
            if command is None:
                self._publish_cmd(session, attempt, index, tool_call)
                return
            if not command.complete:
                self._check_command_timeout(session, command)
                return
        self._publish_prepare(session)

    def _publish_cmd(self, session: SessionState, attempt: AttemptState, index: int, tool_call: ToolCall) -> None:
        request_id = f"cmd:{attempt.result.request_id}:{index}"
        timestamp = now_ms()
        if tool_call.name == "exec":
            request: CmdRunRequest | CmdOutputReadRequest = CmdRunRequest(command=tool_call.arguments["command"], workdir=tool_call.arguments.get("workdir"), timeout_ms=tool_call.arguments.get("timeout_ms"), ts_ms=timestamp)
            publish = lambda: self.cmd_client.publish_run_request(
                principal=self.config.agent.principal,
                token=self.config.agent.token,
                channel_id=session.config.cmd_channel_id,
                req=CmdRunRequestInput(command=tool_call.arguments["command"], workdir=tool_call.arguments.get("workdir"), timeout_ms=tool_call.arguments.get("timeout_ms"), ts_ms=timestamp),
            )
        else:
            stream = "stdout" if tool_call.name == "read_stdout" else "stderr"
            request = CmdOutputReadRequest(target_seq=tool_call.arguments["exec_id"], stream=stream, offset=0, nbytes=DEFAULT_OUTPUT_READ_BYTES, ts_ms=timestamp)
            publish = lambda: self.cmd_client.publish_output_read_request(
                principal=self.config.agent.principal,
                token=self.config.agent.token,
                channel_id=session.config.cmd_channel_id,
                req=CmdOutputReadRequestInput(target_seq=tool_call.arguments["exec_id"], stream=stream, offset=0, nbytes=DEFAULT_OUTPUT_READ_BYTES, ts_ms=timestamp),
            )
        seq = self._reliable_publish(
            channel_id=session.config.cmd_channel_id,
            stable_id=request_id,
            expected=request,
            publish=publish,
            decode=lambda message: self._cmd_request_identity(message, request_id, request),
        )
        command = CommandState(attempt.result.request_id, index, tool_call, seq, request)
        session.commands_by_id[request_id] = command
        session.commands_by_seq[seq] = command

    def _check_command_timeout(self, session: SessionState, command: CommandState) -> None:
        if command.request is None or command.request_seq is None:
            return
        if now_ms() < command.request.ts_ms + self.config.agent.cmd_result_timeout_ms:
            return
        if self._reconcile_cmd_result(session, command):
            return
        payload = encode_cmd_timeout(
            cmd_request_id=command.request_id,
            cmd_request_seq=command.request_seq,
            tool_call_id=command.tool_call.id,
            tool_name=command.tool_call.name,
        )
        expected = json.loads(payload.decode("utf-8"))
        seq = self._reliable_publish(
            channel_id=session.config.wal_channel_id,
            stable_id=expected["timeout_id"],
            expected=expected,
            publish=lambda: int(self.openevent_client.publish_auto_seq(
                principal=self.config.agent.principal,
                token=self.config.agent.token,
                channel_id=session.config.wal_channel_id,
                payload=payload,
                recipients=(),
            ).seq),
            decode=lambda message: _wal_identity(message, self.config.agent.principal),
        )
        timeout = parse_wal(payload)
        assert isinstance(timeout, CmdTimeout)
        self.state.observe_timeout(session, seq, timeout)

    def _reconcile_cmd_result(self, session: SessionState, command: CommandState) -> bool:
        watermark = int(self.openevent_client.get_status(self.config.agent.principal, self.config.agent.token).max_seq)
        next_seq = command.request_seq + 1
        while next_seq <= watermark:
            response = self.openevent_client.fetch(
                self.config.agent.principal,
                self.config.agent.token,
                from_seq=next_seq,
                limit=1000,
                only_my_recipient=False,
                channels=(session.config.cmd_channel_id,),
            )
            for message in response.messages:
                if int(message.seq) <= watermark:
                    self._observe_cmd(message, realtime=False)
            advanced = int(response.next_seq)
            if advanced <= next_seq:
                raise RuntimeError("Fetch did not advance during Cmd timeout reconciliation")
            next_seq = advanced
        return command.result is not None

    def _ensure_im_content(self, session: SessionState, model_request_id_value: str, content: str) -> None:
        request_id = f"model-content:{model_request_id_value}"
        existing = session.im_requests_by_id.get(request_id)
        if existing is not None:
            if (
                existing.principal != self.config.agent.principal
                or existing.recipients
                or existing.data.get("msg_type") != "text"
                or existing.data.get("content") != {"text": content}
            ):
                raise AgentStateError(f"conflicting IM request ID {request_id}")
            return
        event_ms = now_ms()
        expected = (request_id, "text", {"text": content}, event_ms)
        seq = self._reliable_publish(
            channel_id=session.config.im_channel_id,
            stable_id=request_id,
            expected=expected,
            publish=lambda: self.im_client.publish_send_request(
                principal=self.config.agent.principal,
                token=self.config.agent.token,
                channel_id=session.config.im_channel_id,
                req=SendRequestInput(request_id=request_id, msg_type="text", content={"text": content}, event_ms=event_ms),
            ),
            decode=lambda message: self._im_request_identity(message),
        )
        self.state.record_im_request(
            session,
            ImRequestState(
                seq=seq,
                principal=self.config.agent.principal,
                recipients=(),
                request_id=request_id,
                event_ms=event_ms,
                data={"msg_type": "text", "content": {"text": content}},
            ),
        )

    def _reliable_publish(self, *, channel_id: int, stable_id: str, expected: Any, publish: Any, decode: Any) -> int:
        before = int(self.openevent_client.get_status(self.config.agent.principal, self.config.agent.token).max_seq)
        try:
            return int(publish())
        except Exception as exc:
            if not _publish_outcome_unknown(exc):
                raise
            after = int(self.openevent_client.get_status(self.config.agent.principal, self.config.agent.token).max_seq)
            found: list[int] = []
            next_seq = before + 1
            while next_seq <= after:
                response = self.openevent_client.fetch(
                    self.config.agent.principal,
                    self.config.agent.token,
                    from_seq=next_seq,
                    limit=1000,
                    only_my_recipient=False,
                    channels=(channel_id,),
                )
                for message in response.messages:
                    if int(message.seq) > after:
                        continue
                    identity = decode(message)
                    if identity is None or identity[0] != stable_id:
                        continue
                    if identity[1] != expected:
                        raise RuntimeError(f"conflicting content for stable ID {stable_id}")
                    found.append(int(message.seq))
                advanced = int(response.next_seq)
                if advanced <= next_seq:
                    raise RuntimeError("Fetch did not advance during publish reconciliation")
                next_seq = advanced
            if found:
                return min(found)
            return int(publish())

    def _cmd_request_identity(
        self,
        message: Any,
        stable_id: str,
        expected: CmdRunRequest | CmdOutputReadRequest,
    ) -> tuple[str, Any] | None:
        parsed = parse_cmd_message(message)
        if (
            parsed.principal != self.config.agent.principal
            or parsed.recipients
            or parsed.payload != expected
        ):
            return None
        return stable_id, parsed.payload

    def _im_request_identity(self, message: Any) -> tuple[str, Any] | None:
        parsed = self.im_client.parse_message(message)
        if (
            parsed.kind != "send.request"
            or parsed.principal != self.config.agent.principal
            or parsed.recipients
        ):
            return None
        return parsed.request_id or "", (parsed.request_id or "", parsed.data.get("msg_type"), parsed.data.get("content"), parsed.event_ms)

    def _next_unbound_tool(self, session: SessionState) -> tuple[AttemptState, int, ToolCall] | None:
        attempts = sorted(
            (
                attempt
                for wal in session.wal_by_seq.values()
                for attempt in wal.attempts.values()
                if attempt.accepted and not attempt.stale and attempt.assistant is not None
            ),
            key=lambda attempt: (attempt.result_seq or 0, attempt.retry_index),
        )
        for attempt in attempts:
            assert attempt.request is not None
            assert attempt.assistant is not None
            for index, tool_call in enumerate(attempt.assistant.tool_calls):
                request_id = f"cmd:{attempt.request.request_id}:{index}"
                if request_id not in session.commands_by_id:
                    return attempt, index, tool_call
        return None

    def _command_for_result_seq(self, session: SessionState, seq: int) -> CommandState | None:
        for command in session.commands_by_id.values():
            if command.result_seq == seq or command.timeout_seq == seq:
                return command
        return None

    def _fetch_channels(self) -> tuple[int, ...]:
        result: list[int] = []
        for session in self.config.enabled_sessions:
            result.extend((session.im_channel_id, session.model_channel_id, session.wal_channel_id, session.cmd_channel_id))
        return tuple(dict.fromkeys(result))

    def _get_channel(self, channel_id: int) -> Any:
        return self.openevent_client.get_channel(self.config.agent.principal, self.config.agent.token, channel_id).channel


def _result_from_command(command: CommandState) -> dict[str, Any]:
    if command.timeout is not None:
        if command.tool_call.name == "exec":
            return command_result_event(
                exec_id=None,
                command=command.tool_call.arguments["command"],
                status="error",
                stdout=None,
                stderr=None,
                error_code="timeout",
                error_message="timed out waiting for Cmd result",
            )
        stream = "stdout" if command.tool_call.name == "read_stdout" else "stderr"
        return output_read_event(
            stream=stream,
            exec_id=command.tool_call.arguments["exec_id"],
            status="error",
            content=None,
            error_code="timeout",
            error_message="timed out waiting for Cmd result",
        )
    if isinstance(command.request, CmdRunRequest) and isinstance(command.result, CmdRunResult):
        timed_out = command.result.status == "TIMEOUT"
        return command_result_event(
            exec_id=command.result.prev_seq,
            command=command.request.command,
            status="ok" if command.result.status == "SUCCESS" else "error",
            stdout=_inline_text(command.result.stdout),
            stderr=_inline_text(command.result.stderr),
            error_code="timeout" if timed_out else None,
            error_message=command.result.error_message or ("command execution timed out" if timed_out else None),
        )
    if isinstance(command.request, CmdOutputReadRequest) and isinstance(command.result, CmdOutputReadResult):
        status = "ok" if command.result.status == "SUCCESS" else "error"
        error = command.result.error_message
        content = command.result.content if command.result.content_encoding == "utf-8" else None
        if status == "error" and not error:
            error = "command output not found" if command.result.status == "NOT_FOUND" else f"command output read failed with status {command.result.status}"
        if command.result.content_encoding != "utf-8" and not error:
            status, error = "error", "command output is not UTF-8 text"
        return output_read_event(stream=command.request.stream, exec_id=command.request.target_seq, status=status, content=content, error_message=error)
    raise RuntimeError("command request/result kind mismatch")


def _inline_text(output: Any) -> str | None:
    return output.content if getattr(output, "content_encoding", None) == "utf-8" and isinstance(getattr(output, "content", None), str) else None


def _cmd_request_matches_tool(payload: CmdRunRequest | CmdOutputReadRequest, tool_call: ToolCall) -> bool:
    if isinstance(payload, CmdRunRequest):
        return (
            tool_call.name == "exec"
            and payload.command == tool_call.arguments["command"]
            and payload.workdir == tool_call.arguments.get("workdir")
            and payload.timeout_ms == tool_call.arguments.get("timeout_ms")
        )
    stream = "stdout" if tool_call.name == "read_stdout" else "stderr"
    return (
        tool_call.name in {"read_stdout", "read_stderr"}
        and payload.target_seq == tool_call.arguments["exec_id"]
        and payload.stream == stream
        and payload.offset == 0
        and payload.nbytes == DEFAULT_OUTPUT_READ_BYTES
    )


def _wal_identity(message: Any, agent_principal: int) -> tuple[str, Any] | None:
    if int(message.principal) != agent_principal or tuple(message.recipients):
        return None
    try:
        data = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    stable_id = data.get("prepare_id") if data.get("kind") == "llm.request.prepare" else data.get("timeout_id")
    return (stable_id, data) if isinstance(stable_id, str) and stable_id else None


def _model_request_identity(message: Any, agent_principal: int) -> tuple[str, Any] | None:
    parsed = parse_llm_message(message)
    if (
        not isinstance(parsed.payload, InferRequest)
        or parsed.principal != agent_principal
        or parsed.recipients
    ):
        return None
    return parsed.payload.request_id, parsed.payload


def _publish_outcome_unknown(exc: Exception) -> bool:
    if bool(getattr(exc, "outcome_unknown", False)):
        return True
    if not isinstance(exc, RpcError):
        return False
    try:
        return exc.code() not in _GUARANTEED_NOT_COMMITTED
    except Exception:
        return True


def _json_description(value: str, channel_id: int) -> dict[str, Any]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"channel {channel_id} description must be JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"channel {channel_id} description must be a JSON object")
    return data


def _require_members(channel: Any, channel_id: int, required: set[int]) -> None:
    if int(getattr(channel, "visibility", 0)) == 0:
        return
    missing = required - {int(item) for item in getattr(channel, "members", [])}
    if missing:
        raise ConfigError(f"channel {channel_id} missing members: {sorted(missing)}")
