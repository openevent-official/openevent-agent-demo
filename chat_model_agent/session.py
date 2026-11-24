"""A serial, replayable conversation workflow over immutable OpenEvent facts."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from threading import Lock
from weakref import WeakValueDictionary

from openevent.chat_sdk import TextPart, TurnRef, TurnWriter, create_client as create_chat, serialize_payload
from openevent.model_proxy_sdk import (
    InferCancelInput, InferRequestInput, create_client as create_model,
    prepare_payload, publish_infer_cancel, publish_infer_request,
)

from .prompt import (MODEL_IO_VERSION, CompletionAccumulator, ModelOutputError,
                     assistant_event, parse_summary, user_cancelled_event)
from .errors import FailureCode
from .budget import (INPUT_REJECTION, FAILURE_REASONS, body_size, event_size,
                     summary_size, history_size, build_normal_body, build_compaction_body,
                     failure_reason, failure_notice)
from .history import ConversationIndex, position
from .ordering import OrderedFacts
from .state import HistoryError, ModelView
from .display_limits import UINT64_MAX, display_fits, publication_fits, publication_size_fits
from .rpc import InfrastructureError, RETRY_INTERVAL_MS
from .worker import Stopped
from .wal import encode

LOG = logging.getLogger(__name__)
PLACEHOLDER = "Processing..."
FAILURE_NOTICE = failure_notice(failure_reason(FailureCode.REQUEST_FAILED))
TOOL_COMPLETION = "Tool processing has ended."


class UserCancelled(Exception):
    pass


class ModelInputError(ModelOutputError):
    """The frozen request failed for a reason that rules out another attempt."""

    def __init__(self, message, *, code=FailureCode.REQUEST_TOO_LARGE):
        super().__init__(message, code=code)


@dataclass
class ContentFragment:
    seq: int
    text: str
    queued_at: float


@dataclass
class ModelResponse:
    view: ModelView
    cursor: int = 0
    accumulator: CompletionAccumulator = field(default_factory=CompletionAccumulator)
    fragments: list[ContentFragment] = field(default_factory=list)
    summary_fragment_count: int = 0
    summary_bytes: int = 0
    display_checked_count: int = 0

    def completion(self, *, validate_fragments=None):
        if validate_fragments is not None:
            validate_fragments(self)
        while self.view.pending_chunks:
            event = self.view.pending_chunks[0]
            try:
                part = self.accumulator.add_chunk(event.payload.body)
            except ModelOutputError as error:
                error.source_seq = event.seq
                raise
            if part:
                self.fragments.append(ContentFragment(event.seq, part, time.monotonic()))
                if validate_fragments is not None:
                    validate_fragments(self)
            self.view.pending_chunks.popleft()
        return self.accumulator

    def summary_size(self, limit):
        # Escaped string content is additive across complete UTF-8 fragments.
        for fragment in self.fragments[self.summary_fragment_count:]:
            self.summary_bytes += summary_size(fragment.text)
            if self.summary_bytes > limit:
                error = ModelOutputError("summary exceeds its size target", code=FailureCode.EVENT_TOO_LARGE)
                error.source_seq = fragment.seq
                raise error
        self.summary_fragment_count = len(self.fragments)
        return self.summary_bytes


class AnswerWorkflow:
    """Publication and answer processing shared by a session and its executions.

    This class has no conversation scheduling methods. Every instance owns its
    writer, model buffers and tool runner; an execution shares only the explicit
    conversation services and coordination locks below.
    """

    def __init__(self, engine, channel_id, *, owner=None, prepared=None):
        self.engine, self.cfg, self.cid = engine, engine.config, channel_id
        self.journal = engine.journal
        self.budget = engine.budget
        if owner is None:
            self.index = ConversationIndex(self)
            self.children = {}
            # Holders and queued acquirers keep their lock alive through the
            # handoff. The registries need not retain idle historical locks.
            self.effect_locks = WeakValueDictionary()
            self.close_locks = WeakValueDictionary()
            self.cancel_locks = WeakValueDictionary()
            self.model = create_model(engine.events, self.cfg.agent.token,
                                      max_retries=self.cfg.rpc.max_retries,
                                      retry_interval_ms=RETRY_INTERVAL_MS)
        else:
            self.index, self.model = owner.index, owner.model
            self.children = owner.children
            self.effect_locks = owner.effect_locks
            self.close_locks = owner.close_locks
            self.cancel_locks = owner.cancel_locks
        self.active_writer = None
        self.answer = prepared
        self.model_running = False
        self.assistants = {}
        self.model_body = None
        self.model_body_key = None
        self.prepared_request_key = self.prepared_request = None
        self.model_response = None
        self.display_prefix = None
        self.display_seq = 0
        self.display_count = 0
        from .tool_runner import ToolRunner
        self.tools = ToolRunner(self)

    async def put(self, kind, key, data):
        return await self.journal.put(kind, key, self.cid, data)

    async def wait(self, timeout=None, *, ready=None):
        """Wait for relevant facts or a business timer, without an idle-read delay."""
        self.engine.check_running()
        await self.engine.wait_async(
            lambda: self.engine.stop.is_set() or (ready is not None and ready()), timeout)
        self.engine.check_running()

    async def effect(self, key, channel, match, publish, data, *, allowed=None):
        async with self.effect_locks.setdefault(key, asyncio.Lock()):
            return await self._effect(key, channel, match, publish, data, allowed=allowed)

    def _effect_candidates(self, prepared):
        data = prepared.data
        if data["module"] == "model":
            return self.engine.model_headers(data["stream_id"])
        if data["module"] == "cmd":
            return self.engine.cmd_headers(data["uuid"], after=prepared.seq)
        return self.engine.events_in(data["channel"])

    async def _effect(self, key, channel, match, publish, data, *, allowed=None):
        """Confirm one intent before publishing; recover it by identity, never text."""
        prepared = self.journal.get(key + ":prepare")
        is_new = prepared is None
        if is_new:
            if allowed is not None and not allowed():
                return None
            self.engine.check_running()
            prepared = await self.put("effect.prepare", key + ":prepare", {"channel": channel, **data})
        elif not prepared.same_data({"channel": channel, **data}):
            raise HistoryError("effect intent changed during replay")
        committed = self.journal.get(key + ":commit")
        if committed is not None:
            seq = committed.data["seq"]
            await self.engine.aconfirm(channel, seq)
            if not match(self.engine.parsed[seq]):
                raise HistoryError("effect commit conflicts with source history")
            return seq
        # A fresh intent cannot have published before this serial coordinator wrote it.
        matches = []
        if not is_new:
            matches = [event for event in self._effect_candidates(prepared) if match(event)]
        if len(matches) > 1:
            raise HistoryError("effect has multiple matching source events")
        if matches:
            seq = matches[0].seq
        else:
            if allowed is not None and not allowed():
                return None
            self.engine.check_running()
            def dispatch():
                # Pool admission is not publication: recheck when execution starts.
                self.engine.check_running()
                return publish() if allowed is None or allowed() else None
            try:
                published = await self.engine.blocking(dispatch)
                if published is None:
                    return None
                seq = int(published)
            except InfrastructureError as error:
                await self._publication_error(key, error)
                raise
        # Finish this handoff even when SIGTERM arrived during the publication.
        raw = self.engine.messages.get(seq)
        await self.put("effect.commit", key + ":commit", {"seq": seq, "uuid": int(raw.uuid) if raw else data.get("uuid")})
        await self.engine.aconfirm(channel, seq)
        if not match(self.engine.parsed[seq]):
            raise HistoryError("published effect conflicts with its source event")
        return seq

    async def _publication_error(self, key, error):
        cause = error
        while cause is not None:
            if hasattr(cause, "commit_state") or hasattr(cause, "uncertain"):
                state = getattr(cause, "commit_state", None)
                state = state.value if state is not None else ("UNKNOWN" if cause.uncertain else "NOT_COMMITTED")
                facts = {"effect_key": key, "state": state,
                         "uuid": getattr(cause, "event_uuid", getattr(cause, "uuid", None))}
                try:
                    await self.put("effect.error", key + ":error:" + str(uuid.uuid4()), facts)
                except InfrastructureError:
                    pass  # The deployment still exits with the original unresolved publication.
                return
            cause = cause.__cause__

    def _turn(self, answer=None):
        answer = answer or self.answer
        if answer is None or answer.kind != "answer.prepare":
            return None
        return self.engine.current_turn(self.cid, self.cfg.agent.principal, answer.data["turn_id"])

    def cancelled(self):
        return self.cancellation_seq() is not None

    def cancellation_seq(self, *, through=None):
        return OrderedFacts(self.engine, self.cid, read=through).cancellation_seq(self.answer)

    def _check_cancelled(self):
        self.engine.check_running()
        if self.cancelled():
            self.active_writer = None
            raise UserCancelled()

    def _writer(self, turn_id):
        if self.active_writer is None or self.active_writer.turn_id != turn_id:
            turn = self.engine.current_turn(self.cid, self.cfg.agent.principal, turn_id,
                                            include_content_seqs=True)
            if turn is None or turn.status != "streaming":
                if turn and turn.status == "cancelled" and turn.cancelled_by == self.cfg.agent.user_principal:
                    raise UserCancelled()
                raise HistoryError("Chat writer requires an existing open turn")
            self.active_writer = TurnWriter(
                self.chat, turn_id=turn_id, start_seq=turn.creation_seq,
                last_seq=turn.last_seq, content_seqs=turn.content_seqs)
        return self.active_writer

    async def _chat_effect(self, key, kind, *, source=None, text="", turn_id=None, terminal_ok=False):
        turn_id = turn_id or self.answer.data["turn_id"]
        old = self.journal.get(key + ":prepare")
        if old:
            data = {k: v for k, v in old.data.items() if k != "channel"}
        else:
            turn = self.engine.current_turn(self.cid, self.cfg.agent.principal, turn_id)
            data = {"module": "chat", "kind": kind, "turn_id": turn_id,
                    "source": source,
                    "pre_seq": turn.last_seq if turn and kind in {"turn.append", "turn.reset", "turn.end"} else None}

        def match(event):
            payload = event.payload
            if event.principal != self.cfg.agent.principal or payload["kind"] != kind:
                return False
            if kind == "turn.cancel":
                return payload["target_turn"] == {"principal": self.cfg.agent.principal, "turn_id": turn_id}
            return payload.get("turn_id") == turn_id and (
                data["pre_seq"] is None or payload.get("pre_seq") == data["pre_seq"])

        def allowed():
            turn = self.engine.current_turn(self.cid, self.cfg.agent.principal, turn_id)
            if kind in {"turn.single", "turn.start"}:
                return terminal_ok or not self.cancelled()
            return bool(turn and turn.status == "streaming")

        def publish():
            body = self._source_text(data["source"]) if data["source"] else text
            if data["source"] and data["source"]["type"] == "model" and not display_fits(
                    self.cfg, self.cid, kind, turn_id, data["pre_seq"], body):
                raise ModelOutputError("model display exceeds the complete Chat message size limit",
                                       code=FailureCode.DISPLAY_TOO_LARGE)
            content = (TextPart(body),)
            if kind == "turn.start":
                writer = self.engine.rpc.sdk("Chat publish", lambda: self.chat.start_turn(
                    turn_id=turn_id, content=content, reply_to_seqs=self.answer.data["reply_to_seqs"]))
                self.active_writer = writer
                return writer.start_seq
            if kind == "turn.single":
                return self.engine.rpc.sdk("Chat publish", lambda: self.chat.single_turn(
                    turn_id=turn_id, content=content, reply_to_seqs=(
                        [self.journal.get(source["record"]).data["input_seq"]]
                        if source and source["type"] == "input_rejection" else self.answer.data["reply_to_seqs"])))
            if kind == "turn.cancel":
                turn = self.engine.current_turn(self.cid, self.cfg.agent.principal, turn_id)
                return self.engine.rpc.sdk("Chat publish", lambda: self.chat.cancel_turn(
                    target_turn=TurnRef(self.cfg.agent.principal, turn_id), start_seq=turn.creation_seq))
            writer = self._writer(turn_id)
            if kind == "turn.end":
                return self.engine.rpc.sdk("Chat publish", writer.complete)
            return self.engine.rpc.sdk("Chat publish", lambda: getattr(
                writer, "reset" if kind == "turn.reset" else "append")(content=content))

        seq = await self.effect(key, self.cid, match, publish, data, allowed=allowed)
        if self.active_writer is not None and self.active_writer.turn_id == turn_id:
            turn = self.engine.current_turn(self.cid, self.cfg.agent.principal, turn_id)
            if turn is not None and turn.status != "streaming":
                self.active_writer = None
        return seq

    def _observed_model(self, request_seq, stream_id, through=None):
        """Inspect terminal state using source headers, never a history RPC."""
        view = ModelView(request_seq, stream_id)
        for event in self.engine.model_headers(stream_id, 0, through):
            view.apply(event, self.cfg.agent.principal, self.cfg.model_proxy.principal)
        return view

    def _model_response(self, request_seq, stream_id, through=None, *, live=False):
        response = self.model_response
        same_stream = response is not None and (response.view.request_seq, response.view.stream_id) == (request_seq, stream_id)
        if (not same_stream or (through is not None and response.cursor > through)
                or (not live and response.cursor != through)):
            response = ModelResponse(ModelView(request_seq, stream_id))
        # Historical display reads must neither advance nor rewind live state.
        if live:
            self.model_response = response
        for event in self.engine.model_events(stream_id, response.cursor, through, consume=live):
            response.view.apply(event, self.cfg.agent.principal, self.cfg.model_proxy.principal)
            response.cursor = event.seq
        return response

    def _accepted_assistant(self, accepted):
        with self.index.cache_lock:
            cached = self.assistants.get(accepted.key)
            if cached is None and self is not self.index.session:
                cached = self.index.session.assistants.get(accepted.key)
        if cached is not None:
            return cached
        data = accepted.data
        response = self._model_response(data["request_seq"], data["stream_id"], data["terminal_seq"])
        view = response.view
        accumulator = response.completion()
        if view.terminal is None:
            raise HistoryError("accepted model terminal missing from history")
        assistant = accumulator.finish(view.terminal.payload.to_dict().get("body"))
        self._cache_assistant(accepted, assistant)
        return assistant

    def _cache_assistant(self, accepted, assistant):
        with self.index.cache_lock:
            self.index.cache_tool_count(accepted, len(assistant.tool_calls))
            if self.index.retain_assistant(accepted):
                self.index.session.assistants[accepted.key] = assistant
            if self is not self.index.session and self.model_running:
                self.assistants[accepted.key] = assistant

    def _source_text(self, source):
        if source["type"] == "placeholder":
            return PLACEHOLDER
        if source["type"] == "failure":
            failures = self.journal.select(channel=self.cid, kind="attempt.fail", prefix=self.answer.key + ":")
            if not failures:
                raise HistoryError("failure notice has no persisted failure reason")
            return failure_notice(failures[-1].data["reason"])
        if source["type"] == "input_rejection":
            return INPUT_REJECTION
        if source["type"] == "tool_completion":
            return TOOL_COMPLETION
        attempt = self.journal.get(source["attempt"])
        if attempt is None or attempt.kind != "attempt.prepare":
            raise HistoryError("display source has no model attempt")
        request = self.journal.get(attempt.key + ":request:commit")
        if request is None:
            raise HistoryError("display source has no model request")
        request_seq, stream = request.data["seq"], attempt.data["stream_id"]
        start, end = source["start_seq"], source["end_seq"]
        response = self.model_response
        cached = (response is not None and (response.view.request_seq, response.view.stream_id) == (request_seq, stream)
                  and response.fragments and response.fragments[-1].seq >= end)
        if source["field"] == "display":
            if start != request_seq:
                raise HistoryError("final display source does not begin at its model request")
            response = self._model_response(request_seq, stream, end)
            accumulator = response.completion()
            if response.view.terminal is None or response.view.terminal.seq != end:
                raise HistoryError("final display source has no matching model terminal")
            terminal = response.view.terminal.payload.to_dict()
            self._check_successful_terminal(terminal)
            return accumulator.finish(terminal.get("body")).display_text or ""
        if not cached:
            response = self._model_response(request_seq, stream, end)
            response.completion()
        first = bisect_left(response.fragments, start, key=lambda fragment: fragment.seq)
        last = bisect_right(response.fragments, end, lo=first, key=lambda fragment: fragment.seq)
        fragments = response.fragments[first:last]
        if not fragments or fragments[0].seq != start or fragments[-1].seq != end:
            raise HistoryError("display source endpoints must contribute complete nonempty content")
        return "".join(fragment.text for fragment in fragments)

    async def _restore_display(self, attempt):
        """Replay persisted batches unchanged, including a publish missing its commit."""
        prefix = attempt.key + ":text:"
        if self.display_prefix != prefix:
            records = self.journal.select(kind="effect.prepare", channel=self.cid, prefix=prefix, after=attempt.seq)
            self.display_prefix, self.display_seq, self.display_count = prefix, 0, 0
            for record in records:
                source = record.data["source"]
                if source["field"] != "content" or source["attempt"] != attempt.key or source["start_seq"] <= self.display_seq:
                    raise HistoryError("persisted content batches conflict with their model attempt")
                await self._chat_effect(record.key.removesuffix(":prepare"), record.data["kind"], source=source)
                self.display_seq = source["end_seq"]
                self._check_cancelled()

    async def _display_content(self, attempt, response, *, flush=False):
        """Merge whole event fragments; return the current batch's next deadline."""
        await self._restore_display(attempt)
        fragments = response.fragments
        while self.display_count < len(fragments) and fragments[self.display_count].seq <= self.display_seq:
            self.display_count += 1
        while self.display_count < len(fragments):
            self._check_cancelled()
            first = fragments[self.display_count]
            end = self.display_count + 1
            size = len(first.text.encode("utf-8"))
            kind = "turn.reset" if self.display_seq == 0 else "turn.append"
            turn = self._turn()
            if turn is None or turn.status != "streaming":
                self._check_cancelled()
                raise HistoryError("model display requires an open Chat turn")
            fits = lambda text: display_fits(self.cfg, self.cid, kind, turn.turn_id, turn.last_seq, text)
            if not fits(first.text):
                error = ModelOutputError("one complete model fragment exceeds the Chat message size limit",
                                         code=FailureCode.DISPLAY_TOO_LARGE)
                error.source_seq = first.seq
                raise error
            combined = first.text
            due = first.queued_at + .05
            # The first contribution replaces the placeholder immediately, whole.
            send = self.display_seq == 0 or size >= 4096
            while not send and end < len(fragments):
                next_size = len(fragments[end].text.encode("utf-8"))
                candidate = combined + fragments[end].text
                if size + next_size > 4096 or not fits(candidate):
                    send = True
                    break
                combined = candidate
                size += next_size
                end += 1
                send = size >= 4096
            if not (send or flush or time.monotonic() >= due):
                return due
            last = fragments[end - 1]
            source = {"type": "model", "attempt": attempt.key, "field": "content",
                      "start_seq": first.seq, "end_seq": last.seq}
            await self._chat_effect(attempt.key + f":text:{first.seq}",
                              kind, source=source)
            self.display_seq, self.display_count = last.seq, end
            self._check_cancelled()
        return None

    def _check_successful_terminal(self, terminal):
        if terminal["kind"] == "infer.result" and terminal["status_code"] == 60009:
            LOG.error("Model request seq=%s names unconfigured provider %r",
                      terminal["prev_seq"], self.answer.data["settings"]["provider"])
            raise ModelInputError("model provider is not configured", code=FailureCode.PROVIDER_NOT_CONFIGURED)
        body = terminal.get("body")
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and error.get("code") in (
                "context_length_exceeded", "CONTEXT_LENGTH_EXCEEDED", "context_window_exceeded"):
            raise ModelInputError("provider context window exceeded", code=FailureCode.CONTEXT_LIMIT)
        if (terminal["kind"] != "infer.end" or terminal["end_status"] != "completed"
                or not 200 <= terminal["status_code"] < 300):
            raise ModelOutputError("model request ended unsuccessfully", code=FailureCode.REQUEST_FAILED)

    def _validate_display_sources(self, response, assistant=None):
        """Judge source content before later cancellation suppresses its send."""
        turn_id = self.answer.data["turn_id"]
        for index in range(response.display_checked_count, len(response.fragments)):
            fragment = response.fragments[index]
            kind = "turn.reset" if index == 0 else "turn.append"
            if not display_fits(self.cfg, self.cid, kind, turn_id, UINT64_MAX, fragment.text):
                error = ModelOutputError("one complete model fragment exceeds the Chat message size limit",
                                         code=FailureCode.DISPLAY_TOO_LARGE)
                error.source_seq = fragment.seq
                raise error
            response.display_checked_count = index + 1
        if assistant is not None:
            text = assistant.display_text or ""
            replacement = assistant.refusal or text != "".join(fragment.text for fragment in response.fragments)
            if replacement and text and not display_fits(
                    self.cfg, self.cid, "turn.reset", turn_id, UINT64_MAX, text):
                error = ModelOutputError("model display exceeds the complete Chat message size limit",
                                         code=FailureCode.DISPLAY_TOO_LARGE)
                error.source_seq = response.view.terminal.seq
                raise error
            return bool(replacement)

    async def _finish_display(self, attempt, response, assistant, *, replacement):
        await self._restore_display(attempt)
        display = assistant.display_text or ""
        if replacement:
            if display:
                source = {"type": "model", "attempt": attempt.key, "field": "display",
                          "start_seq": response.view.request_seq, "end_seq": response.view.terminal.seq}
                await self._chat_effect(attempt.key + ":final", "turn.reset", source=source)
            elif self.display_seq:
                # A complete tool-only JSON response supersedes provisional text.
                await self._chat_effect(attempt.key + ":final", "turn.reset", source={"type": "placeholder"})
        else:
            await self._display_content(attempt, response, flush=True)
        self._check_cancelled()

    def _attachments(self, keys):
        return self.engine.objects.prepare_many(keys)

    def _validate_tool_attachments(self, assistant):
        selected = {ident for call in assistant.tool_calls if call.name == "exec"
                    for ident in call.arguments.get("object_ids", ())}
        if selected and not selected.issubset(self.index.object_ids(self.answer, selected)):
            raise ModelOutputError("exec object_ids must select attachments from the current model input",
                                   code=FailureCode.INVALID_TOOL_ARGUMENTS)

    def _settings(self, purpose="answer"):
        cfg = self.cfg.agent
        prompt = "system_prompt" if purpose == "answer" else "context_compression_prompt"
        return {"model": cfg.model, "provider": cfg.model_provider, prompt: getattr(cfg, prompt),
                "context_max_input_bytes": cfg.context_max_input_bytes, "context_history_max_bytes": self.budget.target,
                "io_version": MODEL_IO_VERSION}

    def _validate_model_io(self):
        if self.answer.data["settings"]["io_version"] != MODEL_IO_VERSION:
            raise HistoryError("unsupported frozen model I/O version")

    def _model_body(self):
        if self.model_body_key != self.answer.key:
            self._validate_model_io()
            prepared = self.journal.materialize(self.answer)
            settings = prepared.data["settings"]
            summary, _ = self.index.summary(prepared.data["summary"])
            events = [self.index.render(ref) for ref in prepared.data["events"]]
            build = build_normal_body if self.answer.kind == "answer.prepare" else build_compaction_body
            self.model_body = build(settings, summary, events)
            self.model_body_key = self.answer.key
        return self.model_body

    def _model_request(self, body, stream):
        return InferRequestInput(stream_id=stream, method="POST", path="/v1/chat/completions",
                                 body=body, provider=self.answer.data["settings"]["provider"])

    def _request_published(self, attempt):
        return (self.journal.get(attempt.key + ":request:commit") is not None
                or any(event.principal == self.cfg.agent.principal
                       and event.payload.to_dict()["kind"] == "infer.request"
                       for event in self.engine.model_headers(attempt.data["stream_id"])))

    async def _prepare_model_request(self, attempt):
        if self._request_published(attempt):
            return None
        if self.prepared_request_key != attempt.key:
            self.prepared_request_key = self.prepared_request = None
            body = await self.engine.blocking(self._model_body)
            if body_size(body) > self.answer.data["settings"]["context_max_input_bytes"]:
                raise ModelInputError("model request exceeds frozen context budget")
            request = await self.engine.blocking(lambda: prepare_payload(
                self._model_request(body, attempt.data["stream_id"])))
            if not publication_fits(self.cfg, self.cfg.channels.model, request.payload):
                raise ModelInputError("model request exceeds transport budget")
            self.prepared_request_key, self.prepared_request = attempt.key, request
        return self.prepared_request

    async def _request(self, attempt, *, send=True):
        stream, cid = attempt.data["stream_id"], self.cfg.channels.model
        req = None
        if send and not self.cancelled():
            req = await self._prepare_model_request(attempt)
        def match(event):
            value = event.payload.to_dict()
            return (event.principal == self.cfg.agent.principal and value["kind"] == "infer.request"
                    and value["stream_id"] == stream)
        def publish():
            self._check_cancelled()
            return self.engine.rpc.sdk("Model request", lambda: publish_infer_request(
                self.model, cid, self.cfg.agent.principal, req))
        try:
            return await self.effect(attempt.key + ":request", cid, match, publish,
                {"module": "model", "kind": "infer.request", "attempt": attempt.key, "stream_id": stream},
                allowed=lambda: send and not self.cancelled())
        finally:
            if self.prepared_request_key == attempt.key:
                self.prepared_request_key = self.prepared_request = None

    async def _cancel_model(self, attempt):
        commit = self.journal.get(attempt.key + ":request:commit")
        if commit is None:
            if not self.journal.get(attempt.key + ":request:prepare"):
                return
            # Share the intent lock and confirmation path, but never prepare
            # or send an uncommitted request just to cancel it.
            request_seq = await self._request(attempt, send=False)
            if request_seq is None:
                return
        else:
            request_seq = commit.data["seq"]
        stream = attempt.data["stream_id"]
        if self._observed_model(request_seq, stream).terminal is not None:
            return
        def match(event):
            data = event.payload.to_dict()
            return (event.principal == self.cfg.agent.principal and data["kind"] == "infer.cancel"
                    and data["stream_id"] == stream and data["request_seq"] == request_seq)
        await self.effect(attempt.key + ":cancel", self.cfg.channels.model, match,
            lambda: self.engine.rpc.sdk("Model cancel", lambda: publish_infer_cancel(
                self.model, self.cfg.channels.model, self.cfg.agent.principal,
                InferCancelInput(stream_id=stream, request_seq=request_seq))),
            {"module": "model", "kind": "infer.cancel", "stream_id": stream, "request_seq": request_seq})

    async def _attempt(self, attempt):
        purpose = attempt.data["purpose"]
        def acquire_or_cancel():
            self.engine.check_running()
            if self.cancelled() and not self._pending_before_cancel():
                raise UserCancelled()
            return self.engine.model_slots.acquire(blocking=False)
        await self.engine.wait_async(acquire_or_cancel)
        source_seq = self.answer.seq
        try:
            self.engine.begin_model_stream(attempt.data["stream_id"])
            request_seq = await self._request(attempt)
            if request_seq is None:
                raise UserCancelled()
            request = self.engine.event_header(self.cfg.channels.model, request_seq)
            deadline = int(request.ts_ms) + self.cfg.agent.model_timeout_ms
            target = self.answer.data["settings"]["context_history_max_bytes"]
            while True:
                self.engine.check_running()
                facts = OrderedFacts(self.engine, self.cid)
                through, stop_kind, stop_seq = facts.attempt_boundary(attempt, self.answer)
                response = await self.engine.blocking(self._model_response, request_seq, attempt.data["stream_id"], through, live=True)
                validate = ((lambda value: value.summary_size(target)) if purpose == "compaction"
                            else self._validate_display_sources)
                view, accumulator = response.view, response.completion(validate_fragments=validate)
                source_seq = response.cursor or request_seq
                if view.terminal is not None:
                    source_seq = view.terminal.seq
                    terminal = view.terminal.payload.to_dict()
                    self._check_successful_terminal(terminal)
                    assistant = accumulator.finish(terminal.get("body"))
                    if purpose == "compaction":
                        parse_summary(assistant, target)
                    else:
                        event = assistant_event(self.answer.data["turn_id"], int(view.terminal.ts_ms), assistant)
                        if event is not None and event_size(event) > target:
                            raise ModelOutputError("complete assistant event exceeds its size target",
                                                   code=FailureCode.EVENT_TOO_LARGE)
                        self._validate_tool_attachments(assistant)
                        await self.engine.blocking(self.tools.validate, assistant, attempt.key)
                        replacement = self._validate_display_sources(response, assistant)
                    # The source terminal preceded cancellation/timeout in this
                    # prefix. Later cancellation may close the Chat display,
                    # but cannot erase this earlier model result on recovery.
                    if purpose == "answer" and not self.cancelled():
                        try:
                            await self._finish_display(attempt, response, assistant, replacement=replacement)
                        except UserCancelled:
                            pass
                    accepted = await self.put("attempt.accept", attempt.key + ":accept", {
                        "attempt": attempt.key, "request_seq": request_seq,
                        "stream_id": attempt.data["stream_id"], "terminal_seq": view.terminal.seq})
                    self._cache_assistant(accepted, assistant)
                    return accepted, assistant
                if stop_kind == "cancel":
                    raise UserCancelled()
                if stop_kind == "timeout":
                    source_seq = stop_seq
                    raise ModelOutputError("model response timed out", code=FailureCode.TIMEOUT)
                if self.engine.clock() >= deadline:
                    # A timer competes with source events only after it obtains
                    # an OpenEvent position. Re-read after the publication: a
                    # terminal may have committed before this timeout event.
                    await self.put("attempt.timeout", attempt.key + ":timeout", {"attempt": attempt.key})
                    continue
                try:
                    batch_deadline = None if purpose == "compaction" else await self._display_content(attempt, response)
                except UserCancelled:
                    # Cancellation may have followed another model source
                    # while display was waiting. Process that ordered prefix
                    # before deciding that this attempt was cancelled.
                    continue
                wait_seconds = max(0, (deadline - self.engine.clock()) / 1000)
                if batch_deadline is not None:
                    wait_seconds = min(wait_seconds, max(0, batch_deadline - time.monotonic()))
                await self.wait(wait_seconds, ready=lambda: self.cancelled() or bool(
                    self.engine.has_model_events(attempt.data["stream_id"], response.cursor)))
        except ModelOutputError as error:
            failed = await self.put("attempt.fail", attempt.key + ":fail", {
                "attempt": attempt.key, "reason": failure_reason(error, purpose),
                "retryable": not isinstance(error, ModelInputError),
                "source_seq": getattr(error, "source_seq", source_seq)})
            await self._cancel_model(attempt)
            return failed, None
        finally:
            self.engine.end_model_stream(attempt.data["stream_id"])
            with self.engine.condition:
                self.engine.model_slots.release()
                self.engine.condition.notify_all()

    async def _new_attempt(self, number):
        self._validate_model_io()
        key = self.answer.key + f":attempt:{number}"
        existing = self.journal.get(key)
        if existing is not None:
            return existing
        if number:
            failed = self.journal.get(self.answer.key + f":attempt:{number - 1}:fail")
            failed_at = self.engine.event_header(self.cfg.channels.wal, failed.seq).ts_ms
            remaining = (failed_at + self.cfg.agent.model_retry_interval_ms - self.engine.clock()) / 1000
            self._check_cancelled()
            if remaining > 0:
                # Reuse the persisted failure time after restart. The async
                # condition keeps a monotonic deadline across notifications.
                await self.engine.wait_async(
                    lambda: self.engine.stop.is_set() or self.cancelled(), remaining)
            self._check_cancelled()
        return await self.put("attempt.prepare", key, {
            "purpose": "answer" if self.answer.kind == "answer.prepare" else "compaction",
            "owner": self.answer.key, "number": number, "stream_id": str(uuid.uuid4())})

    async def _ensure_tools(self, accepted):
        if await self.engine.blocking(self._tools_registered, accepted.data["attempt"]):
            return
        assistant = await self.engine.blocking(self._accepted_assistant, accepted)
        for index, call in enumerate(assistant.tool_calls):
            await self.tools._prepare(accepted.data["attempt"] + f":tool:{index}", call)
        return assistant

    async def _run_answer(self, prepared):
        self.model_running = True
        try:
            await self._run_answer_model(await self.engine.blocking(self.journal.materialize, prepared))
        finally:
            # Tools and cancellation use durable source records after the
            # model stage; its full request body can be rebuilt if needed.
            self.model_body_key = self.model_body = None
            self.prepared_request_key = self.prepared_request = None
            self.model_response = None
            if self is not self.index.session:
                with self.index.cache_lock:
                    self.assistants.clear()
            self.answer = self.journal.get(prepared.key)
            self.model_running = False
            await self._release_finished_answer()
            with self.engine.condition:
                self.engine.condition.notify_all()

    async def _run_answer_model(self, prepared):
        self.answer = prepared
        try:
            if OrderedFacts(self.engine, self.cid).final_failure(prepared) is not None:
                await self._freeze()
                if self.cancelled():
                    await self._cancel_answer()
                return
            if self.cancelled() and not self._pending_before_cancel():
                raise UserCancelled()
            for number in range(prepared.data["max_model_attempts"]):
                if self.journal.get(prepared.key + f":attempt:{number}") is None:
                    self._check_cancelled()
                attempt = await self._new_attempt(number)
                accepted = self.journal.get(attempt.key + ":accept")
                if accepted:
                    await self._ensure_tools(accepted)
                    await self._finish_answer()
                    return
                failed = self.journal.get(attempt.key + ":fail")
                if failed is None:
                    try:
                        await self._prepare_model_request(attempt)
                    except ModelInputError:
                        await self._fail_model_input(attempt)
                        break
                    await self._chat_effect(prepared.key + ":start", "turn.start", source={"type": "placeholder"})
                    if number:
                        await self._chat_effect(attempt.key + ":placeholder", "turn.reset", source={"type": "placeholder"})
                    accepted, assistant = await self._attempt(attempt)
                    if assistant is not None:
                        await self._ensure_tools(accepted)
                        await self._finish_answer()
                        return
                    failed = accepted
                else:
                    await self._cancel_model(attempt)
                if not failed.data["retryable"]:
                    break
                if number + 1 < prepared.data["max_model_attempts"]:
                    self._check_cancelled()
            await self._freeze()
        except UserCancelled:
            await self._cancel_answer(sources_settled=True)

    async def _fail_model_input(self, attempt):
        reason = failure_reason(FailureCode.REQUEST_TOO_LARGE)
        facts = OrderedFacts(self.engine, self.cid, read=self.answer.seq)
        batch = self.journal.latest(channel=self.cid, kind="compaction.prepare", through=facts.read)
        previous = self.journal.latest(channel=self.cid, kind="answer.prepare", through=self.answer.seq - 1)
        # Only the first answer after a finally failed batch inherits its cause.
        # Use this answer's preparation prefix during execution and recovery.
        if (batch is not None and (previous is None or previous.seq < batch.seq)
                and facts.record(batch.key + ":commit") is None):
            for number in range(2):
                failed = facts.record(batch.key + f":attempt:{number}:fail")
                if failed is None:
                    break
                if not failed.data["retryable"] or number == 1:
                    reason = failed.data["reason"]
                    break
        await self.put("attempt.fail", attempt.key + ":fail", {
            "attempt": attempt.key, "reason": reason, "retryable": False, "source_seq": self.answer.seq})

    async def _freeze(self):
        async with self.close_locks.setdefault(self.answer.key, asyncio.Lock()):
            await self._freeze_locked()

    async def _freeze_locked(self):
        # A final failure is ordered by its source, not by the coroutine's
        # position. Later cancellation still needs cleanup, not a new verdict.
        facts = OrderedFacts(self.engine, self.cid)
        if facts.final_failure(self.answer) is None:
            self._check_cancelled()
            raise HistoryError("failure closure has no final failure source")
        if facts.record(self.answer.key + ":freeze") is not None:
            return
        if self._turn() is not None:
            await self._chat_effect(self.answer.key + ":failure-cancel", "turn.cancel")
        seq = await self._chat_effect(self.answer.key + ":notice", "turn.single", source={"type": "failure"},
                                     turn_id=self.answer.data["turn_id"] + ":failure", terminal_ok=True)
        await self.put("answer.freeze", self.answer.key + ":freeze", {"notice_seq": seq})

    def _pending_before_cancel(self):
        facts = OrderedFacts(self.engine, self.cid)
        cancel = facts.cancellation_seq(self.answer)
        if cancel is None:
            return False
        for attempt in self._answer_attempts(self.answer, facts=facts):
            if facts.record(attempt.key + ":accept") or facts.record(attempt.key + ":fail"):
                continue
            timeout = facts.record(attempt.key + ":timeout")
            if timeout is not None and timeout.seq < cancel:
                return True
            if any(event.principal == self.cfg.model_proxy.principal
                   for event in self.engine.model_headers(attempt.data["stream_id"], 0, cancel - 1)):
                return True
        return False

    async def _cancel_answer(self, *, sources_settled=False):
        if not sources_settled and self._pending_before_cancel():
            # Raw cancellation already prevents new dispatches. Earlier model
            # facts must finish their common processing before cancellation is
            # considered complete, including after restart.
            for attempt in self._answer_attempts(self.answer):
                await self._cancel_model(attempt)
            await self.tools.kill_pending()
            return
        async with self.cancel_locks.setdefault(self.answer.key, asyncio.Lock()):
            await self._cancel_answer_locked()
        await self._release_finished_answer()

    async def _cancel_answer_locked(self):
        turn = self._turn()
        if turn is None or not self.cancelled():
            raise HistoryError("user cancellation has no source event")
        await self.put("answer.cancel", self.answer.key + ":cancel", {"cancel_seq": turn.terminal_seq})
        if OrderedFacts(self.engine, self.cid).final_failure(self.answer) is not None:
            await self._freeze()
        for attempt in self._answer_attempts(self.answer):
            await self._cancel_model(attempt)
            accepted = self.journal.get(attempt.key + ":accept")
            if accepted:
                await self._ensure_tools(accepted)
        await self.tools.kill_pending()
        for prepared in self._answer_tools(self.answer):
            key = prepared.data["tool_key"]
            if (not self.journal.get(key + ":done")
                    and not self.journal.get(key + ":request:0:prepare")):
                await self.tools._done(prepared, "cancelled")
        await self._finish_answer()

    def _tools_registered(self, attempt_key, *, facts=None):
        facts = facts or OrderedFacts(self.engine, self.cid)
        accepted = facts.record(attempt_key + ":accept")
        if accepted is None:
            return False
        return all(facts.record(attempt_key + f":tool:{i}:prepare")
                   for i in range(self.index.tool_count(accepted)))

    def _answer_attempts(self, answer, *, facts=None):
        # Attempts and tool preparations have contiguous, numbered stable keys.
        # Look up this answer's records without filtering the whole conversation.
        facts = facts or OrderedFacts(self.engine, self.cid)
        number = 0
        while (attempt := facts.record(answer.key + f":attempt:{number}")) is not None:
            yield attempt
            number += 1

    def _answer_accepted(self, answer, *, facts=None):
        facts = facts or OrderedFacts(self.engine, self.cid)
        return next((accepted for attempt in self._answer_attempts(answer, facts=facts)
                     if (accepted := facts.record(attempt.key + ":accept")) is not None), None)

    def _answer_tools(self, answer):
        facts = OrderedFacts(self.engine, self.cid)
        accepted = self._answer_accepted(answer, facts=facts)
        if accepted is None:
            return []
        return [prepared for i in range(self.index.tool_count(accepted))
                if (prepared := facts.record(accepted.data["attempt"] + f":tool:{i}:prepare")) is not None]

    def _stage_complete(self, answer):
        facts = OrderedFacts(self.engine, self.cid)
        if facts.final_failure(answer) is not None:
            return facts.record(answer.key + ":freeze") is not None
        accepted = self._answer_accepted(answer, facts=facts)
        if accepted is not None:
            return self._tools_registered(accepted.data["attempt"], facts=facts)
        return facts.record(answer.key + ":cancel") is not None

    def _answer_finished(self, answer):
        """Derive closure from original facts, without a separate completion WAL."""
        execution = self.children.get(answer.key)
        if execution is None and self.answer is not None and self.answer.key == answer.key:
            execution = self
        if execution is not None and execution.model_running:
            return False
        if any(lock is not None and lock.locked() for lock in (
                self.close_locks.get(answer.key), self.cancel_locks.get(answer.key))):
            return False
        if not self._stage_complete(answer):
            return False
        turn = self._turn(answer)
        if turn is None:
            return self.journal.get(answer.key + ":freeze") is not None
        if turn.terminal_seq is None:
            return False
        if turn.cancelled_by == self.cfg.agent.user_principal:
            if self.journal.get(answer.key + ":cancel") is None:
                return False
            # Cancellation is recorded before model cancellation is sent. A
            # restart in between must still finish stopping every sent stream.
            for attempt in self._answer_attempts(answer):
                commit = self.journal.get(attempt.key + ":request:commit")
                stream = attempt.data["stream_id"]
                request_seq = (commit.data["seq"] if commit is not None else next((event.seq
                    for event in self.engine.model_headers(stream)
                    if event.principal == self.cfg.agent.principal
                    and event.payload.to_dict()["kind"] == "infer.request"), None))
                if request_seq is not None and self._observed_model(request_seq, stream).terminal is None:
                    return False
        return all(self.journal.get(tool.data["tool_key"] + ":done") and self.tools.settled(tool)
                   for tool in self._answer_tools(answer))

    async def _finish_answer(self):
        async with self.close_locks.setdefault(self.answer.key, asyncio.Lock()):
            try:
                await self._finish_answer_locked()
            except UserCancelled:
                return  # The coordinator records and clears the winning cancellation.
        await self._release_finished_answer()

    async def _finish_answer_locked(self):
        if self.cancelled() and self.model_running:
            return
        if not await self.engine.blocking(self._stage_complete, self.answer):
            return
        tools = await self.engine.blocking(self._answer_tools, self.answer)
        if any(not self.journal.get(record.data["tool_key"] + ":done") or not self.tools.settled(record) for record in tools):
            return
        turn = self._turn()
        if turn is None:
            return
        if turn.status == "streaming":
            accepted = self._answer_accepted(self.answer)
            if tools and accepted and not (await self.engine.blocking(self._accepted_assistant, accepted)).display_text:
                await self._chat_effect(self.answer.key + ":tool-completion", "turn.reset", source={"type": "tool_completion"})
            await self._chat_effect(self.answer.key + ":end", "turn.end")

    def _release_child(self):
        if self.children.get(self.answer.key) is not self:
            return
        del self.children[self.answer.key]
        self.answer = self.journal.get(self.answer.key)
        self.model_body_key = self.model_body = self.model_response = None
        self.prepared_request_key = self.prepared_request = None
        self.active_writer = None
        with self.index.cache_lock:
            self.assistants.clear()

    async def _release_finished_answer(self):
        if self.model_running or self.answer is None or self.answer.kind != "answer.prepare":
            return
        if await self.engine.blocking(self._answer_finished, self.answer):
            owner = self.index.session
            owner._track_answers()
            owner.pending_answers.pop(self.answer.key, None)
            self._release_child()

    async def _run_compaction(self):
        try:
            self.answer = await self.engine.blocking(self.journal.materialize, self.answer)
            for number in range(2):
                attempt = await self._new_attempt(number)
                accepted = self.journal.get(attempt.key + ":accept")
                failed = self.journal.get(attempt.key + ":fail")
                if accepted is None and failed is None:
                    accepted, assistant = await self._attempt(attempt)
                    if assistant is None:
                        failed, accepted = accepted, None
                if accepted is not None:
                    await self.put("compaction.commit", self.answer.key + ":commit", {"attempt": attempt.key})
                    self.index._prune_bodies(position(self.answer.data["range"]["through"]), attempt.key + ":accept")
                    return None
                await self._cancel_model(attempt)
                if not failed.data["retryable"]:
                    return failed.data["reason"]
            return failed.data["reason"]
        finally:
            failures = [failure for number in range(2)
                        if (failure := self.journal.get(self.answer.key + f":attempt:{number}:fail")) is not None]
            if (self.journal.get(self.answer.key + ":commit") or len(failures) >= 2
                    or any(not record.data["retryable"] for record in failures)):
                self._release_child()


class Conversation(AnswerWorkflow):
    """Coordinate one channel's recovery, input admission and execution tasks."""

    def __init__(self, engine, channel_id):
        super().__init__(engine, channel_id)
        self._chat = None
        self._chat_lock = Lock()
        self.pending_answers = {}
        self.answer_cursor = 0
        self.admission_lock = asyncio.Lock()
        self.admission_cursor = 0
        self.rejection_tasks = {}
        self.reconciled = False

    @property
    def chat(self):
        # History reads and publications can reach the first SDK call concurrently.
        with self._chat_lock:
            if self._chat is None:
                self._chat = create_chat(self.engine.events, principal=self.cfg.agent.principal,
                                         token=self.cfg.agent.token, channel_id=self.cid,
                                         max_retries=self.cfg.rpc.max_retries,
                                         max_payload_bytes=self.cfg.openevent.max_payload_bytes)
            return self._chat

    def _matches_intent(self, event, data):
        if event.principal != self.cfg.agent.principal:
            return False
        if data["module"] == "cmd":
            return (event.payload.kind == data["kind"]
                    and int(self.engine.messages[event.seq].uuid) == data["uuid"])
        if data["module"] == "model":
            payload = event.payload.to_dict()
            return (payload["kind"] == data["kind"] and payload["stream_id"] == data["stream_id"]
                    and (data["kind"] == "infer.request" or payload["request_seq"] == data["request_seq"]))
        payload = event.payload
        if payload["kind"] != data["kind"]:
            return False
        if data["kind"] == "turn.cancel":
            return payload["target_turn"] == {"principal": self.cfg.agent.principal, "turn_id": data["turn_id"]}
        return (payload.get("turn_id") == data["turn_id"]
                and (data["pre_seq"] is None or payload.get("pre_seq") == data["pre_seq"]))

    def _chat_recovery_index(self, channel):
        index = {}
        for event in self.engine.events_in(channel):
            if event.principal != self.cfg.agent.principal:
                continue
            payload = event.payload
            if payload["kind"] == "turn.cancel":
                target = payload["target_turn"]
                if target["principal"] != self.cfg.agent.principal:
                    continue
                identity = payload["kind"], target["turn_id"], None
            else:
                identity = payload["kind"], payload.get("turn_id"), payload.get("pre_seq")
            index.setdefault(identity, []).append(event)
            if identity[-1] is not None:
                # A missing expected tail in the intent matches any tail, as before.
                index.setdefault((*identity[:-1], None), []).append(event)
        return index

    def _find_unconfirmed_effect(self, prepared, chat_indexes):
        data = prepared.data
        if data["module"] == "chat":
            channel, kind = data["channel"], data["kind"]
            if channel not in chat_indexes:
                chat_indexes[channel] = self._chat_recovery_index(channel)
            identity = kind, data["turn_id"], None if kind == "turn.cancel" else data["pre_seq"]
            candidates = chat_indexes[channel].get(identity, ())
        else:
            candidates = self._effect_candidates(prepared)
        matches = [event for event in candidates if self._matches_intent(event, data)]
        if len(matches) > 1:
            raise HistoryError("effect has multiple matching source events")
        return self.engine.messages[matches[0].seq] if matches else None

    async def _reconcile(self):
        """Confirm historical effects, then finish rejection notices before input."""
        if self.reconciled:
            return
        chat_indexes, committed_errors = {}, {}
        for known in self.journal.select(kind="effect.error", channel=self.cid):
            if known.data["state"] == "COMMITTED":
                committed_errors.setdefault(known.data["effect_key"], []).append(known.data["uuid"])
        for prepared in self.journal.select(kind="effect.prepare", channel=self.cid):
            key, data = prepared.key.removesuffix(":prepare"), prepared.data
            commit = self.journal.get(key + ":commit")
            if commit is None:
                header = self._find_unconfirmed_effect(prepared, chat_indexes)
            else:
                header = self.engine.event_header(data["channel"], commit.data["seq"])
                if not self._matches_intent(self.engine.parsed[header.seq], data):
                    raise HistoryError("effect commit conflicts with source history")
                if commit.data["uuid"] is not None and commit.data["uuid"] != header.uuid:
                    raise HistoryError("effect UUID conflicts with source history")
            for uuid in committed_errors.get(key, ()):
                if header is None or (uuid is not None and uuid != header.uuid):
                    raise HistoryError("SDK reported a committed effect missing from complete history")
            if commit is None and header is not None:
                await self.put("effect.commit", key + ":commit", {
                    "seq": header.seq, "uuid": header.uuid})
        # A later answer's input cursor does not complete earlier notices. The
        # history checks above must finish before resuming any missing sends.
        for rejected in self.journal.select(kind="input.reject", channel=self.cid):
            key = rejected.key + ":notice"
            if self.journal.get(key + ":commit") is None:
                await self._chat_effect(key, "turn.single", terminal_ok=True,
                    turn_id=rejected.data["turn_id"],
                    source={"type": "input_rejection", "record": rejected.key})
        self.reconciled = True
        self.engine.finish_recovery(self.cid)

    def _track_answers(self):
        for answer in self.journal.select(channel=self.cid, kind="answer.prepare", after=self.answer_cursor):
            self.pending_answers[answer.key] = answer
            self.answer_cursor = answer.seq

    def _pending_answers(self):
        self._track_answers()
        for key, answer in tuple(self.pending_answers.items()):
            if self._answer_finished(answer):
                del self.pending_answers[key]
                child = self.children.get(key)
                if child is not None:
                    child._release_child()
        return tuple(self.pending_answers.values())

    def _child(self, prepared):
        child = self.children.get(prepared.key)
        if child is None:
            child = AnswerExecution(self, prepared)
            self.children[prepared.key] = child
        return child

    async def _reject_inputs(self):
        notices = []
        async with self.admission_lock:
            answer = self.journal.latest(channel=self.cid, kind="answer.prepare")
            consumed = position(answer.data["input_through"]) if answer is not None else (-1, -1)
            after = max(self.admission_cursor, consumed[0])
            with self.engine.condition:
                through = self.engine.scanned_through
                ready, _ = self.engine.pending_inputs(self.cid, after=after)
            for seq, _ in ready:
                if seq > through:
                    break
                if (seq, 0) <= consumed or self.engine.parsed[seq].payload["kind"] != "turn.single":
                    continue
                key = f"chat:{self.cid}:input:{seq}:reject"
                rejected = self.journal.get(key)
                if rejected is None and await self.engine.blocking(
                        self.index.user, seq, self.budget.target) is None:
                    snapshot = await self.engine.blocking(self.index.snapshot)
                    rejected = await self.put("input.reject", key, {
                        "input_seq": seq, "reason": "message_too_long", "snapshot": snapshot.watermarks,
                        "turn_id": str(uuid.uuid4())})
                if rejected is not None:
                    self.index.reject_user(seq)
                if rejected is not None and not self.journal.get(key + ":notice:commit"):
                    notices.append(rejected)
            # Input content and configuration are immutable for this session.
            # Remember checks, not consumption; later input still waits in the
            # history index until its ordinary answer is durably prepared.
            self.admission_cursor = max(after, through)
        for rejected in notices:
            key = rejected.key
            if key not in self.rejection_tasks:
                task = self._spawn(self._chat_effect(key + ":notice", "turn.single", terminal_ok=True,
                    turn_id=rejected.data["turn_id"], source={"type": "input_rejection", "record": key}),
                    f"chat-{self.cid}-rejection-{rejected.data['input_seq']}")
                self.rejection_tasks[key] = task
                def release(done, key=key):
                    if self.rejection_tasks.get(key) is done:
                        del self.rejection_tasks[key]
                task.add_done_callback(release)

    def _input_snapshot(self):
        # Later source events may already be visible, but an unchecked user
        # event still blocks consumption and waking at its original position.
        return self.index.snapshot(admitted_through=self.admission_cursor)

    async def _compress(self, snapshot) -> None:
        summary_key = snapshot.summary
        summary, after = await self.engine.blocking(self.index.summary, summary_key)
        pending = [item for item in snapshot.events if item.trigger and position(item.cursor) > position(snapshot.consumed)]
        if not pending:
            return
        target = max(0, (self.budget.limit - self.budget.fixed) // 2)
        if not target:
            return
        old = [item for item in snapshot.events if position(after) < position(item.cursor) < position(pending[0].cursor)]
        sizes = await self.engine.blocking(lambda: [event_size(item.event) for item in old])
        remaining, start = sum(sizes), 0
        while (summary_size(summary) if summary is not None else 0) + remaining > target:
            if start == len(old):
                return
            settings = self._settings("compaction")
            settings["context_history_max_bytes"] = target
            size = body_size(build_compaction_body(settings, summary, []))
            end = start
            for offset in range(start, len(old)):
                # event_size reserves a comma; the first array item needs none.
                growth = sizes[offset] - (offset == start)
                if size + growth > self.budget.limit:
                    break
                size += growth
                end = offset + 1
            if end == start:
                return
            batch = old[start:end]
            size = await self.engine.blocking(lambda: body_size(
                build_compaction_body(settings, summary, [item.event for item in batch])))
            if size > self.budget.limit:
                return
            key = f"chat:{self.cid}:compaction:{self.journal.count(channel=self.cid, kind='compaction.prepare')}"
            prepared = await self.put("compaction.prepare", key, {"snapshot": snapshot.watermarks,
                "settings": settings, "summary": summary_key, "events": [item.ref for item in batch],
                "range": {"after": after, "through": batch[-1].cursor}})
            if await self._child(prepared)._run_compaction():
                return
            summary_key = key + ":commit"
            summary, after = await self.engine.blocking(self.index.summary, summary_key)
            remaining -= sum(sizes[start:end])
            start = end
            snapshot = await self.engine.blocking(self._input_snapshot)

    def _batch(self, snapshot):
        summary, after = self.index.summary(snapshot.summary)
        items = [item for item in snapshot.events if position(item.cursor) > position(after)]
        first = next((i for i, item in enumerate(items) if item.trigger and position(item.cursor) > position(snapshot.consumed)), None)
        if first is None:
            return None
        settings = self._settings()
        key = f"chat:{self.cid}:answer:{self.journal.count(channel=self.cid, kind='answer.prepare')}"
        turn_id, best = str(uuid.uuid4()), None
        data = {"events": [], "input_through": None,
                "summary": snapshot.summary,
                "snapshot": snapshot.watermarks, "reply_to_seqs": [], "settings": settings,
                "turn_id": turn_id, "max_model_attempts": self.cfg.agent.max_model_attempts}
        wal_fixed = len(encode("answer.prepare", key, self.cid, data)) - body_size(None)
        normal_fixed = body_size(build_normal_body(settings, summary, []))
        cancel_fixed = event_size(user_cancelled_event(turn_id, [1], UINT64_MAX)) - body_size(1)
        displays = [{"kind": kind, "turn_id": ident, "content": [{"type": "text", "text": text}],
                     "reply_to_seqs": []}
                    for kind, ident, text in (("turn.start", turn_id, PLACEHOLDER),
                        ("turn.single", turn_id + ":failure", max(map(failure_notice, FAILURE_REASONS), key=len)))]
        display_sizes = [len(serialize_payload(payload)) for payload in displays]
        refs, users, tool_targets, seen_targets = [], [], [], set()
        event_bytes = ref_bytes = user_bytes = tool_bytes = 0
        for end, item in enumerate(items, 1):
            size = event_size(item.event)
            event_bytes += size
            refs.append(item.ref)
            ref_bytes += body_size(item.ref) + 1
            if position(item.cursor) > position(snapshot.consumed):
                if item.ref["type"] == "user":
                    users.append(item.ref["seq"])
                    user_bytes += body_size(item.ref["seq"]) + 1
                if item.trigger and not users:
                    for seq in item.targets:
                        if seq not in seen_targets:
                            seen_targets.add(seq)
                            tool_targets.append(seq)
                            tool_bytes += body_size(seq) + 1
            if end <= first:
                continue
            # All three payload formats use compact JSON arrays of uint64s.
            # New users replace tool reply targets, so check each prefix in order.
            targets = users or tool_targets
            target_bytes = (user_bytes if users else tool_bytes) - bool(targets)
            wal_size = wal_fixed + ref_bytes - 1 + body_size(item.cursor) + target_bytes
            fits = cancel_fixed + target_bytes <= self.budget.target and publication_size_fits(
                self.cfg, self.cfg.channels.wal, wal_size)
            fits = fits and all(publication_size_fits(self.cfg, self.cid, size + target_bytes)
                                for size in display_sizes)
            if not fits:
                if best is None:
                    raise HistoryError("publication limits cannot hold one admitted answer")
                break
            if normal_fixed + event_bytes - 1 > self.budget.limit:
                if best is None:
                    best = end, len(targets), bool(users), True  # Local failure only; never publish this body.
                break
            best = end, len(targets), bool(users), False
        if best is None:
            return None
        end, target_count, use_users, local_failure = best
        targets = (users if use_users else tool_targets)[:target_count]
        data.update(events=refs[:end], input_through=items[end - 1].cursor, reply_to_seqs=targets)
        # Retain complete encoding checks at the durable preparation boundary.
        fits = event_size(user_cancelled_event(turn_id, targets, UINT64_MAX)) <= self.budget.target
        fits = fits and publication_fits(self.cfg, self.cfg.channels.wal, encode("answer.prepare", key, self.cid, data))
        for payload in displays:
            payload["reply_to_seqs"] = targets
            fits = fits and publication_fits(self.cfg, self.cid, serialize_payload(payload))
        if not fits:
            raise HistoryError("publication limits cannot hold selected answer")
        if body_size(build_normal_body(settings, summary, [item.event for item in items[:end]])) > self.budget.limit and not local_failure:
            raise HistoryError("selected model request exceeds its encoded budget")
        return key, data

    async def _next_answer(self):
        await self._reconcile()
        await self._reject_inputs()
        for answer in await self.engine.blocking(self._pending_answers):
            if not await self.engine.blocking(self._stage_complete, answer):
                return answer
        answer = self.journal.latest(channel=self.cid, kind="answer.prepare")
        # A new ordinary answer cannot be prepared before an older freeze is
        # lifted, so only the latest answer can still pause new input.
        if answer is not None:
            freeze = self.journal.get(answer.key + ":freeze")
            if freeze and not self.journal.get(answer.key + ":wake"):
                snapshot = await self.engine.blocking(self._input_snapshot)
                fresh = next((item for item in snapshot.events if item.ref["type"] == "user"
                              and item.ref["seq"] > freeze.data["notice_seq"]), None)
                if fresh is None:
                    return None
                await self.put("answer.wake", answer.key + ":wake", {"input_seq": fresh.ref["seq"]})
        # Compression batches are serial; only the latest can still need work
        # or a fallback answer. Older failed batches retain their original limit.
        prepared = self.journal.latest(channel=self.cid, kind="compaction.prepare")
        compress = True
        if prepared is not None and self.journal.get(prepared.key + ":commit") is None:
            failures = [failure for number in range(2)
                        if (failure := self.journal.get(prepared.key + f":attempt:{number}:fail")) is not None]
            if len(failures) >= 2 or any(not item.data["retryable"] for item in failures):
                compress = answer is not None and failures[-1].seq < answer.seq
            else:
                compress = not await self._child(prepared)._run_compaction()
        if compress:
            snapshot = await self.engine.blocking(self._input_snapshot)
            await self._compress(snapshot)
        async with self.admission_lock:
            snapshot = await self.engine.blocking(self._input_snapshot)
            candidate = await self.engine.blocking(self._batch, snapshot)
            if candidate is None:
                return None
            key, data = candidate
            return await self.put("answer.prepare", key, data)

    async def _model_step(self):
        prepared = await self._next_answer()
        if prepared is not None:
            await self._child(prepared)._run_answer(prepared)

    async def _tool_step(self, prepared):
        prepared = await self.engine.blocking(self.journal.materialize, prepared)
        owner = self.journal.get(prepared.data["answer"])
        child = self._child(owner)
        await child.tools.run(prepared, child.cancelled)
        await child.tools.cleanup(prepared)

    def _spawn(self, operation, name):
        with self.engine.condition:
            if self.engine.stop.is_set():
                operation.close()
                raise Stopped()
            task = asyncio.create_task(operation, name=name)
            self.engine.tasks.add(task)
        def finished(done):
            self.engine.tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None and not isinstance(error, Stopped):
                    self.engine.fail(error)
            with self.engine.condition:
                self.engine.condition.notify_all()
        task.add_done_callback(finished)
        return task

    async def _check_recovery_budget(self):
        await self.engine.blocking(self._validate_recovery_budget)

    def _validate_recovery_budget(self):
        target = self.engine.configured_budget.target
        stages = (self.journal.select(channel=self.cid, kind="answer.prepare")
                  + self.journal.select(channel=self.cid, kind="compaction.prepare"))
        # Each accepted event, summary and future short result fit its stage's
        # frozen target. Those bounds can prove compatibility without reading bodies.
        if all(stage.data["settings"]["context_history_max_bytes"] <= target for stage in stages):
            return
        snapshot = self.index.snapshot()
        summary, after = self.index.summary(snapshot.summary)
        if summary is not None and summary_size(summary) > target:
            raise HistoryError("current context budget cannot retain the adopted summary")
        consumed = position(snapshot.consumed)
        for item in snapshot.all_events:
            if position(item.cursor) > position(after) and (item.ref["type"] != "user" or position(item.cursor) <= consumed):
                if event_size(item.event) > target:
                    raise HistoryError("current context budget cannot retain a required history event")
        for prepared in stages:
            complete = (self._stage_complete(prepared) if prepared.kind == "answer.prepare" else
                        self.journal.get(prepared.key + ":commit") or len(self.journal.select(channel=self.cid, kind="attempt.fail", prefix=prepared.key + ":")) >= 2 or any(not record.data["retryable"] for record in self.journal.select(channel=self.cid, kind="attempt.fail", prefix=prepared.key + ":")))
            if not complete and prepared.data["settings"]["context_history_max_bytes"] > target:
                raise HistoryError("unfinished model stage was admitted under a larger history target")
            if prepared.kind == "answer.prepare" and not self._answer_finished(prepared) and not self.journal.get(prepared.key + ":freeze"):
                if event_size(user_cancelled_event(prepared.data["turn_id"], prepared.data["reply_to_seqs"], UINT64_MAX)) > target:
                    raise HistoryError("current budget cannot retain a future cancellation")
        from .prompt import ToolCall
        for prepared in self.journal.select(channel=self.cid, kind="tool.prepare"):
            if not self.journal.get(prepared.data["tool_key"] + ":done"):
                data = self.journal.materialize(prepared).data
                self.tools.validate_errors(ToolCall(data["call_id"], data["name"], data["arguments"]), target=target)

    async def run(self):
        model_task = tool_task = None
        try:
            await self._reconcile()
            await self._check_recovery_budget()
            while True:
                self.engine.check_running()
                watermark = self.engine.activity(self.cid)
                for task in (model_task, tool_task):
                    if task is not None and task.done():
                        task.result()
                await self._reject_inputs()
                pending_answers = await self.engine.blocking(self._pending_answers)
                for answer in pending_answers:
                    child = self._child(answer)
                    if child.cancelled():
                        await child._cancel_answer()
                    await child._finish_answer()
                if tool_task is None or tool_task.done():
                    tools = await self.engine.blocking(lambda: sorted(
                        (tool for answer in pending_answers for tool in self._answer_tools(answer)),
                        key=lambda record: record.seq))
                    pending = await self.engine.blocking(lambda: next((record for record in tools
                                    if self._tools_registered(record.data["tool_key"].rsplit(":tool:", 1)[0]) and
                                    (not self.journal.get(record.data["tool_key"] + ":done") or not self.tools.settled(record))), None))
                    tool_task = self._spawn(self._tool_step(pending), f"chat-{self.cid}-tool") if pending else None
                if model_task is None or model_task.done():
                    model_task = self._spawn(self._model_step(), f"chat-{self.cid}-model")
                    # Give an idle model step its one scheduling pass before waiting.
                    await asyncio.sleep(0)
                    if model_task.done():
                        model_task.result()
                await self.engine.wait_async(lambda: self.engine.stop.is_set() or self.engine.activity(self.cid) != watermark
                    or (tool_task is not None and tool_task.done()) or (model_task is not None and model_task.done()))
                if model_task is not None and model_task.done() and self.engine.activity(self.cid) == watermark:
                    model_task.result()
                    await self.engine.wait_async(lambda: self.engine.stop.is_set() or self.engine.activity(self.cid) != watermark
                        or (tool_task is not None and tool_task.done()))
        except Stopped:
            return
        except Exception as error:
            self.engine.fail(error)


class AnswerExecution(AnswerWorkflow):
    """One answer or compaction, with its own state and shared session services."""

    def __init__(self, owner, prepared):
        self.owner = owner
        super().__init__(owner.engine, owner.cid, owner=owner, prepared=prepared)

    @property
    def chat(self):
        return self.owner.chat

    async def wait(self, timeout=None, *, ready=None):
        await self.owner.wait(timeout, ready=ready)
