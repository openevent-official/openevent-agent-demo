"""Serial Cmd tools, with output reconstructed from immutable result events."""

from __future__ import annotations

import base64
import asyncio
import copy
from dataclasses import dataclass
import uuid
from openevent.sdk.proto import openevent_pb2

from openevent.cmd_sdk import (
    CmdKillRequestInput, CmdOutputReadRequestInput, CmdRunRequestInput, create_client, dumps_payload,
)

from .display_limits import UINT64_MAX, publication_fits
from .budget import event_size
from .errors import FailureCode
from .ordering import OrderedFacts
from .prompt import (ModelOutputError, ToolCall, command_result_event, output_read_event,
                     tool_call_event, tool_error_result, tool_result_event)
from .state import HistoryError
from .tool_results import (
    READ_CANCELLED, TOOL_CANCELLED, TOOL_TIMEOUT, future_error_results, oversized_result,
)
from .wal import Record, encode


_UUID_PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


@dataclass
class ReadProgress:
    """Caller-owned progress for one tool in the forward-only history index."""

    next_page: int = 0
    total_bytes: int = 0
    source_seq: int | None = None


def _bytes(content, encoding):
    """Decode content already validated by the Cmd SDK."""
    return content.encode("utf-8") if encoding == "utf-8" else base64.b64decode(content)


class ToolRunner:
    def __init__(self, conversation):
        self.conversation = conversation
        self.engine, self.cfg = conversation.engine, conversation.cfg
        self.journal, self.cid = conversation.journal, conversation.cid
        self.client = create_client(self.engine.events)
        self.prepare_locks = {}
        self.kill_locks = {}

    def _prepare_data(self, key, call, ts_ms, *, task_id=_UUID_PLACEHOLDER):
        arguments = copy.deepcopy(call.arguments)
        request = {"ts_ms": ts_ms}
        if call.name == "exec":
            request["workdir"] = arguments.get("workdir", str(self.cfg.agent.work_root / str(self.cid)))
        else:
            request.update(target_seq=arguments["exec_id"], stream=call.name.removeprefix("read_"),
                           offset=arguments.get("offset", 0))
        return {"tool_key": key, "task_id": task_id, "answer": self.conversation.answer.key,
                "call_id": call.id, "name": call.name, "arguments": arguments,
                "request": request, "output_limit": self.cfg.agent.max_tool_output_bytes,
                "chunk_bytes": self.cfg.agent.output_read_chunk_bytes,
                "nbytes": arguments["nbytes"] if call.name != "exec" else self.cfg.agent.max_tool_output_bytes}

    @property
    def target(self):
        return self.conversation.answer.data["settings"]["context_history_max_bytes"]

    def validate_errors(self, call, *, target=None):
        target = self.target if target is None else target
        for result in future_error_results(call):
            if event_size(tool_result_event(_UUID_PLACEHOLDER, UINT64_MAX, result)) > target:
                raise ModelOutputError("tool error result exceeds the context event limit",
                                       code=FailureCode.EVENT_TOO_LARGE)

    @staticmethod
    def _params(data):
        # Keep the potentially large command only once in tool.prepare. The
        # concrete publication intent still freezes the complete wire request.
        params = dict(data["request"])
        if data["name"] == "exec":
            params.update({name: data["arguments"][name] for name in ("command", "timeout_ms")
                           if name in data["arguments"]})
        return params

    def validate(self, assistant, attempt_key):
        """Preflight every tool before accepting any of this assistant's calls."""
        def check(channel, payload, **kwargs):
            if not publication_fits(self.cfg, channel, payload, **kwargs):
                raise ModelOutputError("tool input exceeds the transport message limit",
                                       code=FailureCode.TOOL_REQUEST_TOO_LARGE)

        def check_wal(kind, key, data):
            try:
                payload = encode(kind, key, self.cid, data)
            except (ValueError, RecursionError) as error:
                raise ModelOutputError("tool input cannot be encoded for publication",
                                       code=FailureCode.TOOL_REQUEST_TOO_LARGE) from error
            check(self.cfg.channels.wal, payload)

        for number, call in enumerate(assistant.tool_calls):
            key = attempt_key + f":tool:{number}"
            data = self._prepare_data(key, call, UINT64_MAX)
            event = tool_call_event(self.conversation.answer.data["turn_id"],
                                    data["task_id"], UINT64_MAX, call)
            if event_size(event) > self.target:
                raise ModelOutputError("tool call exceeds the context event limit", code=FailureCode.EVENT_TOO_LARGE)
            self.validate_errors(call)
            check_wal("tool.prepare", key + ":prepare", data)
            params = self._params(data)
            kind = "cmd.run.request" if call.name == "exec" else "cmd.output.read.request"
            page = 0
            attachments = []
            if call.name == "exec":
                attachments = [openevent_pb2.ObjectKey(**self.engine.objects.get(ident))
                               for ident in call.arguments.get("object_ids", ())]
            else:
                limit = min(data["nbytes"], data["output_limit"])
                # Bound every page's encoded offset, length and intent-key width
                # without iterating over or dispatching the individual pages.
                page = (limit - 1) // data["chunk_bytes"]
                params.update(offset=params["offset"] + page * data["chunk_bytes"],
                              nbytes=min(data["chunk_bytes"], limit))
            intent = {"channel": self.cfg.channels.cmd, "module": "cmd", "kind": kind,
                      "uuid": UINT64_MAX, "request": params, "tool_key": key}
            check_wal("effect.prepare", key + f":request:{page}:prepare", intent)
            check(self.cfg.channels.cmd, dumps_payload({"kind": kind, **params}), object_keys=attachments)
            # Completion records stay bounded even for one-byte read chunks.
            # Check their largest legal encodings before accepting the tool.
            completions = [("timeout", {"request_seq": UINT64_MAX}), ("cancelled", {})]
            if call.name == "exec":
                completions.append(("exec", {"request_seq": UINT64_MAX, "result_seq": UINT64_MAX}))
            else:
                completions.extend([
                    ("read", {"request_seq": UINT64_MAX, "page_count": UINT64_MAX,
                              "result_seq": UINT64_MAX}),
                    ("read_cancelled", {"request_seq": UINT64_MAX})])
            for outcome, facts in completions:
                check_wal("tool.done", key + ":done", {"tool_key": key, "outcome": outcome, **facts})
                if outcome in {"exec", "read"}:
                    check_wal("tool.done", key + ":done", {"tool_key": key, "outcome": outcome,
                              **facts, "rejection": "result_too_large"})
            check_wal("tool.timeout", key + ":timeout", {"tool_key": key, "request_seq": UINT64_MAX})

    async def _prepare(self, key, call):
        async with self.prepare_locks.setdefault(key, asyncio.Lock()):
            return await self._prepare_locked(key, call)

    async def _prepare_locked(self, key, call):
        old = self.journal.get(key + ":prepare")
        if old is not None:
            old = await self.engine.blocking(self.journal.materialize, old)
            if (old.data["call_id"], old.data["name"], old.data["arguments"]) != (call.id, call.name, call.arguments):
                raise HistoryError("tool call changed during replay")
            return old
        data = self._prepare_data(key, call, self.engine.clock(), task_id=str(uuid.uuid4()))
        if call.name == "exec" and "workdir" not in call.arguments:
            directory = self.cfg.agent.work_root / str(self.cid)
            await self.engine.blocking(directory.mkdir, parents=True, exist_ok=True)
        return await self.conversation.put("tool.prepare", key + ":prepare", data)

    async def _request(self, prepared, number, params, cancelled, *, deadline=None):
        key = prepared.data["tool_key"] + f":request:{number}"
        old = self.journal.get(key + ":prepare")
        kind = "cmd.run.request" if prepared.data["name"] == "exec" else "cmd.output.read.request"
        if old is not None:
            old = await self.engine.blocking(self.journal.materialize, old)
            data = {name: value for name, value in old.data.items() if name != "channel"}
            if data["request"] != params or data["kind"] != kind:
                raise HistoryError("Cmd request changed during replay")
        else:
            if cancelled() or OrderedFacts(self.engine, self.cid).record(prepared.data["tool_key"] + ":timeout"):
                return None
            value = await self.engine.blocking(self.engine.rpc.call, "GetUUID", self.engine.events.get_uuid)
            data = {"module": "cmd", "kind": kind, "uuid": value, "request": params,
                    "tool_key": prepared.data["tool_key"]}

        def publish():
            self.engine.check_running()
            method, cls = ((self.client.publish_run_request, CmdRunRequestInput) if kind == "cmd.run.request"
                           else (self.client.publish_output_read_request, CmdOutputReadRequestInput))
            attachments = ({"object_keys": [openevent_pb2.ObjectKey(**self.engine.objects.get(ident))
                            for ident in prepared.data["arguments"].get("object_ids", ())]}
                           if kind == "cmd.run.request" else {})
            return self.engine.rpc.publish_cmd(data["uuid"], lambda: method(
                self.cfg.agent.principal, self.cfg.agent.token, self.cfg.channels.cmd,
                cls(**data["request"]), uuid=data["uuid"], **attachments))

        return await self.conversation.effect(key, self.cfg.channels.cmd,
            lambda event: self._matches(event, data["uuid"], kind), publish, data,
            allowed=lambda: not cancelled()
                and OrderedFacts(self.engine, self.cid).record(prepared.data["tool_key"] + ":timeout") is None
                and (deadline is None or self.engine.clock() < deadline))

    def _matches(self, event, uuid, kind):
        raw = self.engine.messages[event.seq]
        return (event.principal == self.cfg.agent.principal and event.payload.kind == kind
                and int(raw.uuid) == uuid)

    def _result_event(self, request_seq, kind):
        with self.engine.condition:
            event = self.engine.cmd_result(request_seq, kind)
            if event is not None and event.seq > self.engine.scanned_through:
                return None
        if event is not None and event.principal != self.cfg.cmd_worker.principal:
            raise HistoryError("Cmd result has an unexpected publisher")
        return event

    def _source(self, seq):
        # tool.done only references already observed, immutable result events.
        # Callers materialize bodies in a worker; do not wait for reader progress.
        with self.engine.condition:
            event = self.engine.messages.get(seq)
            if event is None or int(event.channel_id) != self.cfg.channels.cmd:
                raise HistoryError("completed tool result is absent from readable Cmd history")
        return self.engine.parsed_event(self.cfg.channels.cmd, seq).payload

    def _deadline(self, seq):
        # Requests returned by _request have already passed publication
        # confirmation. Their timestamp is in the lightweight source index.
        with self.engine.condition:
            event = self.engine.messages.get(seq)
            if event is None or int(event.channel_id) != self.cfg.channels.cmd:
                raise HistoryError("tool request is absent from readable Cmd history")
            return int(event.ts_ms) + self.cfg.agent.cmd_result_timeout_ms

    async def _done(self, prepared, outcome, **facts):
        key = prepared.data["tool_key"]
        data = {"tool_key": key, "outcome": outcome, **facts}
        if outcome in {"exec", "read", "read_cancelled"}:
            # A source selected before the timeout event keeps its outcome
            # while its body is loaded, validated and persisted, also on replay.
            candidate = Record(UINT64_MAX, "tool.done", key + ":done", self.cid, data)
            result = await self.engine.blocking(self.result, candidate)
            ts_ms = int(self.engine.messages[facts["result_seq"]].ts_ms) if "result_seq" in facts else UINT64_MAX
            # Retain the accepted tool's event allowance when message capacity
            # falls. A deliberate change to the configured context budget still
            # bounds new verdicts; later model requests use current capacity.
            target = min(self.target, self.engine.configured_budget.target)
            if event_size(tool_result_event(prepared.data["task_id"], ts_ms, result)) > target:
                if outcome not in {"exec", "read"}:
                    raise HistoryError("accepted tool's local error no longer fits its frozen budget")
                data["rejection"] = "result_too_large"
                if event_size(tool_result_event(prepared.data["task_id"], ts_ms, oversized_result(result))) > target:
                    raise HistoryError("accepted tool's short result exceeds its reserved budget")
            self.engine.check_running()
        return await self.conversation.put("tool.done", key + ":done", data)

    async def _timeout(self, prepared, request_seq):
        key = prepared.data["tool_key"]
        return await self.conversation.put("tool.timeout", key + ":timeout", {
            "tool_key": key, "request_seq": request_seq})

    def _ordered_result(self, prepared, request_seq, kind):
        """Select from one confirmed prefix, never from the processing clock."""
        with self.engine.condition:
            facts = OrderedFacts(self.engine, self.cid)
            result = self._result_event(request_seq, kind)
            timeout = facts.record(prepared.data["tool_key"] + ":timeout")
            if result is not None and result.seq > facts.read:
                result = None
            if result is not None and (timeout is None or result.seq < timeout.seq):
                return result, timeout
            return None, timeout

    def source_seq(self, done):
        """The event which decided the result, not its later WAL confirmation."""
        outcome = done.data["outcome"]
        if outcome in {"exec", "read"}:
            return done.data["result_seq"]
        facts = OrderedFacts(self.engine, self.cid, read=done.seq)
        key = done.data["tool_key"]
        if outcome == "timeout":
            return facts.record(key + ":timeout").seq
        prepared = facts.record(key + ":prepare")
        answer = facts.record(prepared.data["answer"])
        source = facts.cancellation_seq(answer)
        if source is None:
            raise HistoryError("cancelled tool has no source Chat cancellation")
        return source

    async def _kill(self, prepared, request_seq):
        async with self.kill_locks.setdefault(prepared.data["tool_key"], asyncio.Lock()):
            return await self._kill_locked(prepared, request_seq)

    async def _kill_locked(self, prepared, request_seq):
        if self._result_event(request_seq, "cmd.run.result") is not None:
            return
        key = prepared.data["tool_key"] + ":kill"
        old = self.journal.get(key + ":prepare")
        if old is not None:
            old = await self.engine.blocking(self.journal.materialize, old)
        data = ({name: value for name, value in old.data.items() if name != "channel"} if old else {
            "module": "cmd", "kind": "cmd.kill.request", "tool_key": prepared.data["tool_key"],
            "uuid": await self.engine.blocking(self.engine.rpc.call, "GetUUID", self.engine.events.get_uuid),
            "request": {"target_seq": request_seq, "ts_ms": self.engine.clock()}})
        def publish():
            self.engine.check_running()
            return self.engine.rpc.publish_cmd(data["uuid"], lambda: self.client.publish_kill_request(
                self.cfg.agent.principal, self.cfg.agent.token, self.cfg.channels.cmd,
                CmdKillRequestInput(**data["request"]), uuid=data["uuid"]))
        await self.conversation.effect(key, self.cfg.channels.cmd,
            lambda event: self._matches(event, data["uuid"], "cmd.kill.request"),
            publish, data, allowed=lambda: self._result_event(request_seq, "cmd.run.result") is None)

    async def kill_pending(self):
        answer = self.conversation.answer
        for prepared in await self.engine.blocking(self.conversation._answer_tools, answer):
            if prepared.data["name"] != "exec":
                continue
            prepared = await self.engine.blocking(self.journal.materialize, prepared)
            # allowed=False reconciles an old publication but never starts an
            # unsubmitted command merely to cancel it.
            request_seq = await self._request(prepared, 0, self._params(prepared.data), lambda: True)
            if request_seq is not None:
                await self._kill(prepared, request_seq)

    def _requests(self, prepared, *, through=None, start=0):
        facts = OrderedFacts(self.engine, self.cid, read=through)
        key = prepared.data["tool_key"]
        number = start
        while True:
            commit = facts.record(key + f":request:{number}:commit")
            if commit is not None:
                request_seq = commit.data["seq"]
            else:
                intent = facts.record(key + f":request:{number}:prepare")
                if intent is None:
                    return
                # A fast worker can finish while effect.commit is still being
                # confirmed. Publication follows its durable intent, so older
                # Cmd history cannot contain this request.
                matches = [event.seq for event in self.engine.cmd_headers(intent.data["uuid"], after=intent.seq)
                           if event.seq <= facts.read
                           and self._matches(event, intent.data["uuid"], intent.data["kind"])]
                if not matches:
                    return
                if len(matches) != 1:
                    raise HistoryError("Cmd intent has multiple source requests")
                request_seq = matches[0]
            yield request_seq
            number += 1

    def pending_source(self, prepared, *, through=None, progress=None):
        """A complete unpublished result reserves its original source position."""
        facts = OrderedFacts(self.engine, self.cid, read=through)
        if facts.record(prepared.data["tool_key"] + ":done") is not None:
            return None
        timeout = facts.record(prepared.data["tool_key"] + ":timeout")
        stop_seq = timeout.seq if timeout is not None else None
        cancellation = facts.cancellation_seq(facts.record(prepared.data["answer"]))
        if prepared.data["name"] != "exec" and cancellation is not None:
            stop_seq = cancellation if stop_seq is None else min(cancellation, stop_seq)
        def first(source=None):
            if source is None or source > facts.read:
                return stop_seq
            return source if stop_seq is None else min(source, stop_seq)
        if prepared.data["name"] == "exec":
            request = next(self._requests(prepared, through=facts.read), None)
            if request is None and cancellation is not None:
                return first(cancellation)
            event = self._result_event(request, "cmd.run.result") if request is not None else None
            return first(event.seq if event is not None else None)
        # Only the forward-only projection shares progress across calls. Old
        # prefix queries use fresh progress and cannot observe newer results.
        if progress is None:
            progress = ReadProgress()
        if progress.source_seq is not None:
            return first(progress.source_seq)
        limit = min(prepared.data["nbytes"], prepared.data["output_limit"])
        for request in self._requests(prepared, through=facts.read, start=progress.next_page):
            event = self._result_event(request, "cmd.output.read.result")
            if event is None or event.seq > facts.read:
                return first()
            if event.payload.status != "SUCCESS":
                progress.source_seq = event.seq
                return first(event.seq)
            progress.total_bytes += event.payload.nbytes
            progress.next_page += 1
            if progress.total_bytes >= limit:
                progress.source_seq = event.seq
                return first(event.seq)
        return first()

    def settled(self, prepared):
        kind = "cmd.run.result" if prepared.data["name"] == "exec" else "cmd.output.read.result"
        return all(self._result_event(seq, kind) is not None for seq in self._requests(prepared))

    async def cleanup(self, prepared):
        """A local verdict or a sent kill never proves that actual work stopped."""
        while not self.settled(prepared):
            if self.conversation.cancelled() and prepared.data["name"] == "exec":
                request_seq = next(self._requests(prepared), None)
                if request_seq is not None:
                    await self._kill(prepared, request_seq)
            await self.conversation.wait(ready=lambda: self.settled(prepared) or (
                self.conversation.cancelled() and prepared.data["name"] == "exec"
                and self.journal.get(prepared.data["tool_key"] + ":kill:commit") is None))

    async def _wait_result(self, prepared, request_seq, kind, origin_seq, cancelled):
        deadline = self._deadline(origin_seq)
        cancellation_handled = False
        while True:
            self.engine.check_running()
            result, timeout = self._ordered_result(prepared, request_seq, kind)
            if result is not None:
                return result
            if timeout is not None:
                return None
            if cancelled() and not cancellation_handled:
                await self.kill_pending()
                cancellation_handled = True
            if self.engine.clock() >= deadline:
                await self._timeout(prepared, origin_seq)
                continue
            await self.conversation.wait(max(0, (deadline - self.engine.clock()) / 1000),
                ready=lambda: (self._result_event(request_seq, kind) is not None
                               or (cancelled() and not cancellation_handled)))

    async def run(self, prepared, cancelled):
        """Execute an already confirmed and fully loaded tool preparation."""
        completed = OrderedFacts(self.engine, self.cid).record(prepared.data["tool_key"] + ":done")
        if completed is not None:
            return completed
        if prepared.data["name"] == "exec":
            request_seq = await self._request(prepared, 0, self._params(prepared.data), cancelled)
            if request_seq is None:
                return await self._done(prepared, "cancelled")
            result = await self._wait_result(prepared, request_seq, "cmd.run.result", request_seq, cancelled)
            if result is None:
                return await self._done(prepared, "timeout", request_seq=request_seq)
            return await self._done(prepared, "exec", request_seq=request_seq, result_seq=result.seq)
        return await self._read(prepared, cancelled)

    async def _read(self, prepared, cancelled):
        request = prepared.data["request"]
        limit = min(prepared.data["nbytes"], prepared.data["output_limit"])
        total, number, origin_seq = 0, 0, None
        while total < limit:
            deadline = None
            if origin_seq is not None:
                deadline = self._deadline(origin_seq)
            params = {**request, "offset": request["offset"] + total,
                      "nbytes": min(prepared.data["chunk_bytes"], limit - total)}
            # Page request timestamps are stable during replay as well.
            request_seq = await self._request(prepared, number, params, cancelled, deadline=deadline)
            if request_seq is None:
                if origin_seq is not None:
                    return await self._read_stopped(prepared, origin_seq, cancelled)
                return await self._done(prepared, "cancelled")
            if origin_seq is None:
                origin_seq = request_seq
            result = await self._wait_result(prepared, request_seq, "cmd.output.read.result", origin_seq, cancelled)
            if result is None:
                return await self._done(prepared, "timeout", request_seq=origin_seq)
            value = result.payload
            if (value.target_seq, value.stream, value.offset, value.nbytes) != (
                    params["target_seq"], params["stream"], params["offset"], params["nbytes"]):
                raise HistoryError("Cmd output result does not echo its request")
            number += 1
            if value.status != "SUCCESS":
                break
            total += value.nbytes
        return await self._done(prepared, "read", request_seq=origin_seq,
                                page_count=number, result_seq=result.seq)

    async def _read_stopped(self, prepared, origin_seq, cancelled):
        # Submitted pages have already been replayed. A cancellation stops the
        # remaining pages; timeout competes at its own persisted source seq.
        while True:
            cancel_observed = cancelled()
            facts = OrderedFacts(self.engine, self.cid)
            timeout = facts.record(prepared.data["tool_key"] + ":timeout")
            cancel_seq = facts.cancellation_seq(self.conversation.answer)
            if (cancel_observed or cancel_seq is not None) and (
                    timeout is None or (cancel_seq is not None and cancel_seq < timeout.seq)):
                await self.kill_pending()
                return await self._done(prepared, "read_cancelled", request_seq=origin_seq)
            if timeout is not None:
                return await self._done(prepared, "timeout", request_seq=origin_seq)
            await self._timeout(prepared, origin_seq)

    def _read_pages(self, record):
        """Resolve the bounded verdict through immutable per-page request facts."""
        count = record.data["page_count"]
        if count < 1:
            raise HistoryError("completed read has no pages")
        pages = []
        for number in range(count):
            commit = self.journal.get(record.data["tool_key"] + f":request:{number}:commit")
            if commit is None:
                raise HistoryError("completed read page has no committed request")
            request_seq = commit.data["seq"]
            result = self._result_event(request_seq, "cmd.output.read.result")
            if result is None or result.seq >= record.seq:
                raise HistoryError("completed read page is absent from readable Cmd history")
            if number == 0 and request_seq != record.data["request_seq"]:
                raise HistoryError("completed read first request changed")
            pages.append(result)
        if pages[-1].seq != record.data["result_seq"]:
            raise HistoryError("completed read final result changed")
        return [self._source(page.seq) for page in pages]

    def result(self, record):
        result = self._result(record)
        return oversized_result(result) if record.data.get("rejection") == "result_too_large" else result

    def _result(self, record):
        prepared = self.journal.get(record.data["tool_key"] + ":prepare")
        if prepared is None:
            raise HistoryError("tool result has no prepared input")
        prepared = self.journal.materialize(prepared)
        data, outcome = prepared.data, record.data["outcome"]
        call = ToolCall(data["call_id"], data["name"], data["arguments"])
        if outcome == "read_cancelled":
            return output_read_event(stream=data["request"]["stream"], exec_id=data["arguments"]["exec_id"],
                offset=data["request"]["offset"], nbytes=data["nbytes"], error_code="cancelled",
                error_message=READ_CANCELLED)
        if outcome in {"cancelled", "timeout"}:
            message = TOOL_CANCELLED if outcome == "cancelled" else TOOL_TIMEOUT
            return tool_error_result(call, error_code=outcome, error_message=message)
        if outcome == "exec":
            value = self._source(record.data["result_seq"])
            outputs = {}
            for stream in ("stdout", "stderr"):
                output = getattr(value, stream)
                if output.content is not None:
                    outputs[stream] = _bytes(output.content, output.content_encoding)
            return command_result_event(exec_id=record.data["request_seq"],
                cmd_status=value.status, output_limit=data["output_limit"], exit_code=value.exit_code,
                stdout_bytes=value.stdout.bytes, stderr_bytes=value.stderr.bytes,
                error_message=value.error_message, **outputs)
        if outcome != "read":
            raise HistoryError("invalid completed tool result")
        final = self._source(record.data["result_seq"])
        args = dict(stream=data["request"]["stream"], exec_id=data["arguments"]["exec_id"],
                    offset=data["request"]["offset"], nbytes=data["nbytes"])
        if final.status != "SUCCESS":
            return output_read_event(**args, error_message=final.error_message or f"Cmd output read failed: {final.status}")
        pages = self._read_pages(record)
        output = b"".join(_bytes(page.content, page.content_encoding) for page in pages)
        return output_read_event(**args, data=output, eof=final.eof,
                                 truncated=len(output) < data["nbytes"])
