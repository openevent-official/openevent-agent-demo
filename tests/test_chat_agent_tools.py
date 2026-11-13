"""Cmd integration: real SDK publications, source replay, and tool deadlines."""

import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import patch

from openevent.cmd_sdk import CmdOutput, CmdOutputReadResultInput, CmdRunResultInput, create_client
from openevent.chat_sdk import TextPart, TurnRef, create_client as create_chat
from openevent.model_proxy_sdk import (
    InferEndInput, InferRequestInput, create_client as create_model, publish_infer_end, publish_infer_request,
)

from chat_model_agent.config import parse_config
from chat_model_agent.budget import ContextBudget, event_size
from chat_model_agent.prompt import UINT64_MAX, ModelOutputError, parse_tool_call, tool_result_event
from chat_model_agent.session import Conversation
from chat_model_agent.tool_runner import ToolRunner
from chat_model_agent.wal import Record, encode
from chat_model_agent.worker import ChatModelAgent
from async_helpers import AsyncRunner, SyncProxy
from test_chat_agent_session import (MemoryEvents, Crash, BUILD, AGENT, USER, PROVIDER, CMD,
                                     MODEL_CHANNEL, CMD_CHANNEL, WAL_CHANNEL, CHAT_CHANNEL)


class ToolRunnerTests(unittest.TestCase):
    def setUp(self):
        self.async_runner = AsyncRunner()
        self.addCleanup(self.async_runner.close)
        BUILD.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=BUILD)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config = parse_config({
            "version": "chat-agent.v1", "deployment_id": "tools-test",
            "openevent": {"target": "events:9527"},
            "agent": {"principal": AGENT, "user_principal": USER, "token": "agent-token",
                      "system_prompt": "", "model": "model", "work_root": str(root / "work"),
                      "context_compression_prompt": "Compress the history.", "context_max_input_bytes": 1048576,
                      "cmd_result_timeout_ms": 100, "max_tool_output_bytes": 4,
                      "output_read_chunk_bytes": 2, "idle_sleep_ms": 1},
            "model_proxy": {"principal": PROVIDER}, "cmd_worker": {"principal": CMD},
            "channels": {"model": MODEL_CHANNEL, "cmd": CMD_CHANNEL, "wal": WAL_CHANNEL},
            "rpc": {"max_retries": 0},
        })
        self.events = MemoryEvents()
        self.cmd = create_client(self.events)
        self.wait_count = 0
        self.restart()

    def restart(self, **changes):
        self.stop_reader()
        self.config = replace(self.config, agent=replace(self.config.agent, **changes))
        self.events.engine = None
        self.engine = ChatModelAgent(self.config, self.events, clock=lambda: self.events.now)
        self.engine.recover()
        self.engine._start_reader()
        self.addCleanup(self.stop_reader)
        self.events.engine = self.engine
        self.conversation = SyncProxy(Conversation(self.engine, CHAT_CHANNEL), self.async_runner)
        self.conversation.answer = SimpleNamespace(key="chat:30:answer:0", seq=0, data={
            "turn_id": "00000000-0000-0000-0000-000000000000",
            "settings": {"context_history_max_bytes": ContextBudget.from_config(self.config.agent).target}})
        self.conversation.wait = self.advance
        self.runner = SyncProxy(self.conversation.tools, self.async_runner)

    def stop_reader(self):
        engine = getattr(self, "engine", None)
        if engine is not None:
            self.async_runner.run(engine.aclose())

    def advance(self, timeout=None, *, ready=None):
        self.wait_count += 1
        self.events.now += 50
        if self.wait_count > 20:
            self.fail("tool failed to honor its bounded deadline")

    def call(self, name="exec", **arguments):
        if name == "exec" and not arguments:
            arguments = {"command": "printf test"}
        return parse_tool_call({"id": "tool-1", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)}})

    def run_tool(self, call=None, *, cancelled=lambda: False, key="chat:30:answer:0:attempt:0:tool:0"):
        prepared = self.runner._prepare(key, call or self.call())
        return self.runner.run(self.engine.journal.materialize(prepared), cancelled)

    def accept_exec(self):
        """Give cancellation the accepted model source that owns this command."""
        attempt = self.conversation.answer.key + ":attempt:0"
        self.conversation.put("attempt.prepare", attempt, {
            "purpose": "answer", "owner": self.conversation.answer.key, "number": 0, "stream_id": attempt})
        request_seq = publish_infer_request(self.conversation.model, MODEL_CHANNEL, AGENT,
            InferRequestInput(stream_id=attempt, method="POST", path="/v1/chat/completions", body={}))
        terminal_seq = publish_infer_end(create_model(self.events, "provider-token", max_retries=0),
            MODEL_CHANNEL, PROVIDER, AGENT, InferEndInput(stream_id=attempt, request_seq=request_seq,
                status_code=200, end_status="completed", body={"choices": [{"index": 0,
                    "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                        "tool_calls": [{"id": "tool-1", "type": "function", "function": {
                            "name": "exec", "arguments": '{"command":"printf test"}'}}]}}]}))
        self.conversation.put("attempt.accept", attempt + ":accept", {
            "attempt": attempt, "request_seq": request_seq, "stream_id": attempt, "terminal_seq": terminal_seq})

    def cancellable_answer(self):
        user = create_chat(self.events, principal=USER, token="user-token",
                           channel_id=CHAT_CHANNEL, max_retries=0)
        user.single_turn(turn_id="tool-input", content=(TextPart("run tool"),))
        answer = self.conversation._next_answer()
        self.conversation.answer = answer
        writer = self.conversation.chat.start_turn(turn_id=answer.data["turn_id"],
            content=(TextPart("working"),), reply_to_seqs=answer.data["reply_to_seqs"])
        return user, answer, writer

    def set_current_result_budget(self, target):
        """Change current settings while retaining the accepted model's frozen T."""
        fixed = ContextBudget.from_config(self.config.agent).fixed
        self.config = replace(self.config, agent=replace(self.config.agent,
            context_max_input_bytes=fixed + 2 * target))
        self.engine.config = self.conversation.cfg = self.runner.cfg = self.config

    def emit_run(self, request_seq, *, status="SUCCESS", data=b"ok", exit_code=0):
        text, encoding = self.encoding(data)
        fields = dict(prev_seq=request_seq, status=status, timeout_ms=1000,
                      stdout=CmdOutput(f"{CMD_CHANNEL}/{request_seq}.stdout", len(data),
                                       encoding if len(data) <= 1024 else None, text if len(data) <= 1024 else None),
                      stderr=CmdOutput(f"{CMD_CHANNEL}/{request_seq}.stderr", 0, "utf-8", ""))
        if status in {"SUCCESS", "FAILED"}:
            fields["exit_code"] = exit_code
        return self.cmd.publish_run_result(CMD, "worker-token", CMD_CHANNEL, AGENT, CmdRunResultInput(**fields))

    @staticmethod
    def encoding(data):
        try:
            return data.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            return base64.b64encode(data).decode("ascii"), "base64"

    def emit_read(self, request, payload, data, *, eof=False, status="SUCCESS", **changes):
        text, encoding = self.encoding(data)
        fields = dict(prev_seq=request.seq, status=status, content=text,
                      bytes=len(data), eof=eof, content_encoding=encoding, target_seq=payload["target_seq"],
                      stream=payload["stream"], offset=payload["offset"], nbytes=payload["nbytes"])
        fields.update(changes)
        return self.cmd.publish_output_read_result(CMD, "worker-token", CMD_CHANNEL, AGENT,
                                                   CmdOutputReadResultInput(**fields))

    def assert_request_intents(self):
        facts = [message for message in self.events.messages("effect.prepare", WAL_CHANNEL)
                 if json.loads(message.payload)["data"].get("module") == "cmd"]
        for fact in facts:
            self.assertNotIn("previous_result", json.loads(fact.payload)["data"])
        for request in self.events.history:
            if request.channel_id != CMD_CHANNEL or not json.loads(request.payload)["kind"].endswith(".request"):
                continue
            matching = [fact for fact in facts if json.loads(fact.payload)["data"]["uuid"] == request.uuid]
            self.assertEqual(len(matching), 1)
            self.assertLess(matching[0].seq, request.seq)
        for prepared in self.events.messages("tool.prepare", WAL_CHANNEL):
            data = json.loads(prepared.payload)["data"]
            self.assertNotIn("uuid", data)
            self.assertNotIn("object_ids", data)

    def test_production_tool_step_loads_preparation_once_before_dispatch(self):
        _, answer, _ = self.cancellable_answer()
        for number, name in enumerate(("exec", "read_stdout", "read_stderr")):
            with self.subTest(name=name):
                call = self.call() if name == "exec" else self.call(name, exec_id=999, nbytes=4)
                key = answer.key + f":attempt:0:tool:{number}"
                self.runner._prepare(key, call)
                prepared = self.engine.journal.get(key + ":prepare")
                source_event = self.engine.source_event
                loads, dispatch_loads = [], []

                def load_source(channel, seq):
                    if channel == WAL_CHANNEL and seq == prepared.seq:
                        loads.append(seq)
                    return source_event(channel, seq)

                def on_publish(message, payload):
                    if payload.get("kind") == "cmd.run.request":
                        dispatch_loads.append(len(loads))
                        self.emit_run(message.seq, data=b"abcd")
                    elif payload.get("kind") == "cmd.output.read.request":
                        dispatch_loads.append(len(loads))
                        start, size = payload["offset"], payload["nbytes"]
                        self.emit_read(message, payload, b"abcd"[start:start + size],
                                       eof=start + size == 4)

                self.events.on_publish = on_publish
                with patch.object(self.engine, "source_event", side_effect=load_source):
                    self.conversation._tool_step(prepared)
                self.assertEqual(dispatch_loads, [1] if name == "exec" else [1, 1])
                done = self.engine.journal.get(key + ":done")
                result = self.runner.result(done)
                self.assertEqual(result["status"], "ok")
                stream = "stderr" if name == "read_stderr" else "stdout"
                self.assertEqual(result[stream]["content"], "abcd")
                self.assertTrue(self.runner.settled(prepared))
                if name != "exec":
                    self.assertEqual(done.data["page_count"], 2)
                    self.assertTrue(result["eof"])

    def test_exec_result_has_real_exit_code_and_binary_output_but_no_file_path(self):
        def on_publish(message, payload):
            if payload.get("kind") == "cmd.run.request":
                self.emit_run(message.seq, status="FAILED", data=b"\xffabcdef", exit_code=7)
        self.events.on_publish = on_publish
        done = self.run_tool()
        result = self.runner.result(done)
        self.assertEqual((result["status"], result["exit_code"]), ("error", 7))
        self.assertEqual(base64.b64decode(result["stdout"]["content"]), b"\xffabc")
        self.assertTrue(result["stdout"]["truncated"])
        self.assertEqual((result["stdout_bytes"], result["stderr_bytes"]), (7, 0))
        self.assertNotIn("command", result)
        self.assertNotIn("file", json.dumps(result))
        requests = self.events.messages("cmd.run.request", CMD_CHANNEL)
        self.assertEqual(len(requests), 1)
        request = json.loads(requests[0].payload)
        self.assertEqual(request["workdir"], str(self.config.agent.work_root / str(CHAT_CHANNEL)))
        facts = [json.loads(message.payload) for message in self.events.messages("tool.done", WAL_CHANNEL)]
        self.assertNotIn("stdout", json.dumps(facts))
        self.assertNotIn("deadline", json.dumps(facts))
        self.assert_request_intents()

    def test_exec_result_keeps_output_sizes_when_long_output_is_not_inlined(self):
        self.events.on_publish = lambda message, payload: self.emit_run(message.seq, data=b"x" * 2048) if payload.get("kind") == "cmd.run.request" else None
        done = self.run_tool()
        result = self.runner.result(done)
        self.assertEqual((result["stdout_bytes"], result["stderr_bytes"]), (2048, 0))
        self.assertNotIn("stdout", result)
        self.assertNotIn("file", json.dumps(result))

    def test_tool_preparation_can_recover_without_allocating_a_request_uuid(self):
        command = "printf " + "x" * 2493
        call = self.call(command=command, timeout_ms=1234)
        attempt_key = "chat:30:answer:0:attempt:0"
        key = attempt_key + ":tool:0"
        last_uuid = self.events.next_uuid
        history_size = len(self.events.history)
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", 4096):
            self.runner.validate(SimpleNamespace(tool_calls=(call,)), attempt_key)
        self.assertEqual(self.events.next_uuid, last_uuid)
        self.assertEqual(len(self.events.history), history_size)
        def crash_after_prepare(message, payload):
            if payload.get("kind") == "tool.prepare":
                raise Crash()
        self.events.on_publish = crash_after_prepare
        with self.assertRaises(Crash):
            self.run_tool(call, key=key)
        prepared, = self.events.messages("tool.prepare", WAL_CHANNEL)
        data = json.loads(prepared.payload)["data"]
        self.assertNotIn("uuid", data)
        self.assertEqual(data["arguments"], {"command": command, "timeout_ms": 1234})
        self.assertEqual(data["request"], {"ts_ms": self.events.now,
                         "workdir": str(self.config.agent.work_root / str(CHAT_CHANNEL))})
        self.assertEqual(prepared.payload.count(b'"command"'), 1)
        self.assertEqual(prepared.payload.count(b'"timeout_ms"'), 1)
        self.assertLess(len(prepared.payload), 4096)
        self.assertGreater(len(prepared.payload) + len(command), 4096)
        self.assertEqual(self.events.next_uuid, last_uuid + 1)  # Only the WAL publication has an identity.
        self.assertEqual(self.events.messages("effect.prepare", WAL_CHANNEL), [])
        self.events.on_publish = None
        self.restart(work_root=self.config.agent.work_root / "changed-after-restart")
        self.events.on_publish = lambda message, payload: self.emit_run(message.seq) if payload.get("kind") == "cmd.run.request" else None
        done = self.run_tool(call, key=key)
        self.assertEqual(done.data["outcome"], "exec")
        intent, = self.events.messages("effect.prepare", WAL_CHANNEL)
        request, = self.events.messages("cmd.run.request", CMD_CHANNEL)
        expected = {**data["request"], "command": command, "timeout_ms": 1234}
        self.assertEqual(json.loads(intent.payload)["data"]["request"], expected)
        actual = json.loads(request.payload)
        self.assertEqual({field: actual[field] for field in expected}, expected)
        result = self.runner.result(done)
        self.assertNotIn("command", result)
        self.events.on_publish = None
        self.restart()
        self.assertEqual(self.run_tool(call, key=key), done)
        self.assertEqual(self.runner.result(done), result)
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("tool.prepare", WAL_CHANNEL)), 1)
        self.assert_request_intents()

    def test_prepared_request_reuses_its_uuid_after_restart(self):
        def crash_after_intent(message, payload):
            if payload.get("kind") == "effect.prepare" and payload["data"].get("kind") == "cmd.run.request":
                raise Crash()
        self.events.on_publish = crash_after_intent
        with self.assertRaises(Crash):
            self.run_tool()
        prepared, = self.events.messages("effect.prepare", WAL_CHANNEL)
        request_uuid = json.loads(prepared.payload)["data"]["uuid"]
        self.assertEqual(self.events.messages("cmd.run.request", CMD_CHANNEL), [])
        self.events.on_publish = None
        self.restart()
        self.events.on_publish = lambda message, payload: self.emit_run(message.seq) if payload.get("kind") == "cmd.run.request" else None
        self.run_tool()
        request, = self.events.messages("cmd.run.request", CMD_CHANNEL)
        self.assertEqual(request.uuid, request_uuid)
        self.assert_request_intents()

    def test_cancelled_prepared_request_does_not_publish_after_restart(self):
        def crash_after_intent(message, payload):
            if payload.get("kind") == "effect.prepare" and payload["data"].get("kind") == "cmd.run.request":
                raise Crash()
        self.events.on_publish = crash_after_intent
        with self.assertRaises(Crash):
            self.run_tool()
        self.events.on_publish = None
        self.restart()
        done = self.run_tool(cancelled=lambda: True)
        self.assertEqual(done.data["outcome"], "cancelled")
        self.assertEqual(self.events.messages("cmd.run.request", CMD_CHANNEL), [])
        self.assertEqual(self.events.messages("cmd.kill.request", CMD_CHANNEL), [])
        self.assertEqual(len(self.events.messages("effect.prepare", WAL_CHANNEL)), 1)
        self.assert_request_intents()

    def test_timeout_is_final_and_late_success_does_not_add_exec_id(self):
        done = self.run_tool()
        request = self.events.messages("cmd.run.request", CMD_CHANNEL)[0]
        result = self.runner.result(done)
        self.assertEqual(result["error_code"], "timeout")
        self.assertNotIn("command", result)
        self.assertNotIn("exec_id", result)
        self.emit_run(request.seq)
        again = self.run_tool()
        self.assertEqual(again, done)
        self.assertEqual(self.runner.result(again), result)
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)

    def test_recovery_keeps_a_completed_source_before_any_timeout_event(self):
        for name, outcome in (("exec", "exec"), ("read_stdout", "read")):
            with self.subTest(name=name):
                key = "chat:30:answer:0:" + name
                def respond(message, payload):
                    if payload.get("kind") == "cmd.run.request":
                        self.emit_run(message.seq)
                    elif payload.get("kind") == "cmd.output.read.request":
                        self.emit_read(message, payload, b"ab")
                    elif payload.get("kind") in {"cmd.run.result", "cmd.output.read.result"}:
                        raise Crash()
                self.events.on_publish = respond
                call = self.call() if name == "exec" else self.call(name, exec_id=999, nbytes=2)
                with self.assertRaises(Crash):
                    self.run_tool(call, key=key)
                self.assertIsNone(self.engine.journal.get(key + ":done"))
                self.events.on_publish = None
                self.events.now += 500
                self.restart()
                done = self.run_tool(call, key=key)
                self.assertEqual(done.data["outcome"], outcome)
                self.assertEqual(self.runner.result(done)["status"], "ok")
                self.assertIsNone(self.engine.journal.get(key + ":timeout"))

    def test_result_published_before_inflight_timeout_wins_by_event_order(self):
        publish = self.events.publish_auto_seq
        result_seqs = []
        def result_before_timeout(**kwargs):
            payload = json.loads(kwargs["payload"])
            if payload.get("kind") == "tool.timeout":
                result_seqs.append(self.emit_run(payload["data"]["request_seq"]))
            return publish(**kwargs)
        with patch.object(self.events, "publish_auto_seq", side_effect=result_before_timeout):
            done = self.run_tool()
        timeout = self.engine.journal.get(done.data["tool_key"] + ":timeout")
        self.assertLess(result_seqs[0], timeout.seq)
        self.assertEqual(done.data["outcome"], "exec")
        self.assertEqual(done.data["result_seq"], result_seqs[0])
        self.assertEqual(self.runner.source_seq(done), result_seqs[0])

    def test_timeout_published_before_result_wins_even_before_done_is_saved(self):
        result_seqs = []
        def result_after_timeout(message, payload):
            if payload.get("kind") == "tool.timeout":
                result_seqs.append(self.emit_run(payload["data"]["request_seq"]))
        self.events.on_publish = result_after_timeout
        done = self.run_tool()
        timeout = self.engine.journal.get(done.data["tool_key"] + ":timeout")
        self.assertLess(timeout.seq, result_seqs[0])
        self.assertLess(result_seqs[0], done.seq)
        self.assertEqual(done.data["outcome"], "timeout")
        self.assertEqual(self.runner.source_seq(done), timeout.seq)
        self.events.on_publish = None
        self.restart(cmd_result_timeout_ms=100000)
        self.assertEqual(self.run_tool(), done)

    def test_partial_read_cancellation_and_timeout_follow_their_source_order(self):
        for cancel_first in (True, False):
            with self.subTest(cancel_first=cancel_first):
                fixture = ToolRunnerTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                user, answer, writer = fixture.cancellable_answer()
                key = answer.key + ":attempt:0:tool:0"
                cancellation = []
                def cancel():
                    cancellation.append(user.cancel_turn(
                        target_turn=TurnRef(AGENT, answer.data["turn_id"]), start_seq=writer.start_seq))
                def timeout(request):
                    fixture.events.now = request.ts_ms + fixture.config.agent.cmd_result_timeout_ms
                    fixture.conversation.put("tool.timeout", key + ":timeout", {
                        "tool_key": key, "request_seq": request.seq})
                def respond(message, payload):
                    if payload.get("kind") == "cmd.output.read.request":
                        fixture.emit_read(message, payload, b"ab")
                        if cancel_first:
                            cancel()
                            timeout(message)
                        else:
                            timeout(message)
                            cancel()
                fixture.events.on_publish = respond
                done = fixture.run_tool(fixture.call("read_stdout", exec_id=999, nbytes=4),
                    key=key, cancelled=fixture.conversation.cancelled)
                trigger = fixture.engine.journal.get(key + ":timeout")
                self.assertEqual(done.data["outcome"], "read_cancelled" if cancel_first else "timeout")
                self.assertEqual(fixture.runner.source_seq(done),
                                 cancellation[0] if cancel_first else trigger.seq)
                self.assertEqual(len(fixture.events.messages("cmd.output.read.request", CMD_CHANNEL)), 1)
                prepared = fixture.engine.journal.get(key + ":prepare")
                before_done = min(cancellation[0], trigger.seq)
                self.assertEqual(fixture.runner.pending_source(prepared, through=before_done), before_done)

    def test_frozen_prefix_does_not_treat_a_later_cmd_publication_as_already_submitted(self):
        user, answer, writer = self.cancellable_answer()
        key = answer.key + ":attempt:0:tool:0"
        publish = self.events.publish_auto_seq
        cancelled = []
        def cancel_during_request(**kwargs):
            payload = json.loads(kwargs["payload"])
            if payload.get("kind") != "cmd.run.request":
                return publish(**kwargs)
            cancelled.append(user.cancel_turn(target_turn=TurnRef(AGENT, answer.data["turn_id"]),
                                               start_seq=writer.start_seq))
            publish(**kwargs)
            raise Crash()
        with patch.object(self.events, "publish_auto_seq", side_effect=cancel_during_request):
            with self.assertRaises(Crash):
                self.run_tool(key=key, cancelled=self.conversation.cancelled)
        request, = self.events.messages("cmd.run.request", CMD_CHANNEL)
        prepared = self.engine.journal.get(key + ":prepare")
        self.assertLess(cancelled[0], request.seq)
        self.assertIsNone(self.engine.journal.get(key + ":request:0:commit"))
        self.assertEqual(self.runner.pending_source(prepared, through=cancelled[0]), cancelled[0])
        self.assertIsNone(self.runner.pending_source(prepared, through=request.seq))

    def test_selected_result_survives_deadline_while_allocating_its_wal_uuid(self):
        for outcome in ("exec", "read", "read_cancelled"):
            with self.subTest(outcome=outcome):
                key = "chat:30:answer:0:" + outcome
                requests, results = [], []
                def respond(message, payload):
                    if payload.get("kind") == "cmd.run.request":
                        requests.append(message)
                        results.append(self.emit_run(message.seq))
                    elif payload.get("kind") == "cmd.output.read.request":
                        requests.append(message)
                        results.append(self.emit_read(message, payload, b"ab"))
                self.events.on_publish = respond
                get_uuid = self.events.get_uuid
                expired = False
                def slow_uuid():
                    nonlocal expired
                    value = get_uuid()
                    if not expired and self.engine.journal.get(key + ":request:0:commit") is not None:
                        self.assertTrue(results)
                        deadline = requests[0].ts_ms + self.config.agent.cmd_result_timeout_ms
                        self.assertLess(self.events.now, deadline)
                        self.events.now = deadline
                        expired = True
                    return value
                call = (self.call() if outcome == "exec" else
                        self.call("read_stdout", exec_id=999, nbytes=4 if outcome == "read_cancelled" else 2))
                with patch.object(self.events, "get_uuid", side_effect=slow_uuid):
                    done = self.run_tool(call, key=key,
                        cancelled=lambda: outcome == "read_cancelled" and bool(results))
                self.assertTrue(expired)
                self.assertEqual(done.data["outcome"], outcome)
                self.assertEqual(done.data["request_seq"], requests[0].seq)
                self.assertNotIn("pages", done.data)
                if outcome == "read_cancelled":
                    self.assertNotIn("result_seq", done.data)
                    self.assertEqual(self.runner.result(done)["error_code"], "cancelled")
                else:
                    self.assertEqual(done.data["result_seq"], results[0])
                    self.assertEqual(self.runner.result(done)["status"], "ok")
                self.assertEqual(len(requests), 1)
                verdicts = [record for record in self.events.messages("tool.done", WAL_CHANNEL)
                            if json.loads(record.payload)["data"]["tool_key"] == key]
                self.assertEqual(len(verdicts), 1)

    def test_selected_result_queued_past_deadline_is_not_changed_to_timeout(self):
        self.events.on_publish = lambda message, payload: self.emit_run(message.seq) if payload.get("kind") == "cmd.run.request" else None
        tool_key = "chat:30:answer:0:attempt:0:tool:0"

        async def exercise():
            blocking = self.engine.journal.blocking
            queued_once = False
            async def delay_verdict(operation, *args, **kwargs):
                nonlocal queued_once
                if (queued_once or operation.__name__ != "publish"
                        or self.engine.journal.get(tool_key + ":request:0:commit") is None):
                    return await blocking(operation, *args, **kwargs)
                queued_once = True
                release = threading.Event()
                started = [threading.Event() for _ in range(self.engine.rpc_pool.size)]
                def occupy(ready):
                    ready.set()
                    if not release.wait(5):
                        raise AssertionError("test RPC worker was not released")
                workers = [asyncio.create_task(blocking(occupy, ready)) for ready in started]
                queued = None
                try:
                    async def all_started():
                        while not all(ready.is_set() for ready in started):
                            await asyncio.sleep(.001)
                    await asyncio.wait_for(all_started(), 2)
                    queued = asyncio.create_task(blocking(operation, *args, **kwargs))
                    await asyncio.sleep(0)
                    self.assertEqual(self.engine.rpc_pool._queue.qsize(), 1)
                    request, = self.events.messages("cmd.run.request", CMD_CHANNEL)
                    self.assertLess(self.events.now, request.ts_ms + self.config.agent.cmd_result_timeout_ms)
                    self.events.now = request.ts_ms + self.config.agent.cmd_result_timeout_ms
                    release.set()
                    return await queued
                finally:
                    release.set()
                    await asyncio.gather(*workers, *([queued] if queued is not None else []),
                                         return_exceptions=True)
            with patch.object(self.engine.journal, "blocking", delay_verdict):
                prepared = await self.runner._target._prepare(tool_key, self.call())
                prepared = await self.engine.blocking(self.engine.journal.materialize, prepared)
                done = await self.runner._target.run(prepared, lambda: False)
            self.assertTrue(queued_once)
            return done

        done = self.async_runner.run(exercise())
        self.assertEqual(done.data["outcome"], "exec")
        self.assertIn("result_seq", done.data)
        self.assertEqual(self.runner.result(done)["status"], "ok")
        self.assertEqual(len(self.events.messages("tool.done", WAL_CHANNEL)), 1)
        self.assertEqual(self.wait_count, 0)

    def test_verdict_started_before_deadline_keeps_result_across_publish_and_restart(self):
        import grpc

        class LostReply(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE

        self.config = replace(self.config, rpc=replace(self.config.rpc, max_retries=1))
        self.restart()
        self.events.on_publish = lambda message, payload: self.emit_run(message.seq) if payload.get("kind") == "cmd.run.request" else None
        publish = self.events.publish_auto_seq
        started = []
        def delayed_publish(**kwargs):
            payload = json.loads(kwargs["payload"])
            if payload.get("kind") != "tool.done":
                return publish(**kwargs)
            request, = self.events.messages("cmd.run.request", CMD_CHANNEL)
            self.assertLess(self.events.now, request.ts_ms + self.config.agent.cmd_result_timeout_ms)
            started.append(payload["data"]["outcome"])
            self.events.now = request.ts_ms + self.config.agent.cmd_result_timeout_ms
            publish(**kwargs)
            raise LostReply()
        with patch.object(self.events, "publish_auto_seq", side_effect=delayed_publish), \
                patch.object(self.engine.rpc, "_pause"):
            done = self.run_tool()
        self.assertEqual(started, ["exec"])
        self.assertEqual(done.data["outcome"], "exec")
        before = self.runner.result(done)
        self.assertEqual(before["stdout"]["content"], "ok")
        self.events.on_publish = None
        self.restart(cmd_result_timeout_ms=1)
        self.assertEqual(self.run_tool(), done)
        self.assertEqual(self.runner.result(done), before)
        self.assertEqual(len(self.events.messages("tool.done", WAL_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)

    def test_waiting_for_cmd_result_leaves_every_rpc_worker_available(self):
        async def exercise():
            waiting = asyncio.Event()
            conversation = self.conversation._target
            async def wait_result(timeout=None, *, ready=None):
                waiting.set()
                await Conversation.wait(conversation, timeout, ready=ready)
            self.conversation.wait = wait_result
            prepared = await self.runner._target._prepare("chat:30:answer:0:attempt:0:tool:0", self.call())
            prepared = await self.engine.blocking(self.engine.journal.materialize, prepared)
            task = asyncio.create_task(self.runner._target.run(prepared, lambda: False))
            release = threading.Event()
            started = [threading.Event() for _ in range(4)]
            workers = []
            def occupy(index):
                started[index].set()
                if not release.wait(5):
                    raise AssertionError("test RPC worker was not released")
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                workers = [asyncio.create_task(self.engine.blocking(occupy, index)) for index in range(4)]
                async def all_started():
                    while not all(event.is_set() for event in started):
                        await asyncio.sleep(.001)
                await asyncio.wait_for(all_started(), 2)
                self.assertFalse(task.done())
                release.set()
                await asyncio.gather(*workers)
                request, = self.events.messages("cmd.run.request", CMD_CHANNEL)
                await self.engine.blocking(self.emit_run, request.seq)
                done = await asyncio.wait_for(task, 2)
                self.assertEqual(done.data["outcome"], "exec")
                self.assertEqual(self.runner.result(done)["stdout"]["content"], "ok")
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, *workers, return_exceptions=True)
        self.async_runner.run(exercise())

    def test_unsent_cancel_does_not_publish_run_request(self):
        last_uuid = self.events.next_uuid
        done = self.run_tool(cancelled=lambda: True)
        result = self.runner.result(done)
        self.assertEqual(result["error_code"], "cancelled")
        self.assertNotIn("command", result)
        self.assertNotIn("exec_id", result)
        self.assertEqual(self.events.messages("cmd.run.request", CMD_CHANNEL), [])
        self.assertEqual(self.events.messages("effect.prepare", WAL_CHANNEL), [])
        self.assertEqual(self.events.next_uuid, last_uuid + 2)  # tool.prepare and tool.done only.
        self.assert_request_intents()

    def test_results_stay_with_their_requests_and_completed_commands_are_not_killed_on_recovery(self):
        outputs = {"printf one": b"one", "printf two": b"two"}
        def respond(message, payload):
            if payload.get("kind") == "cmd.run.request":
                self.emit_run(message.seq, data=outputs[payload["command"]])
        self.events.on_publish = respond
        completed = [(self.run_tool(self.call(command=command),
                      key=f"chat:30:answer:0:attempt:0:tool:{index}"), output.decode())
                     for index, (command, output) in enumerate(outputs.items())]
        self.events.on_publish = None
        for recovered in (False, True):
            if recovered:
                self.restart()
            for done, output in completed:
                request_seq, result_seq = done.data["request_seq"], done.data["result_seq"]
                with self.subTest(recovered=recovered, request_seq=request_seq):
                    prepared = self.runner.journal.get(done.data["tool_key"] + ":prepare")
                    with patch.object(self.engine, "events_in", side_effect=AssertionError("Cmd history rescanned")):
                        found = self.runner._wait_result(prepared, request_seq, "cmd.run.result", request_seq, lambda: False)
                        self.runner._kill(prepared, request_seq)
                        self.assertIsNone(self.runner._result_event(request_seq, "cmd.output.read.result"))
                        self.assertIsNone(self.runner._result_event(request_seq + 2000, "cmd.run.result"))
                    self.assertEqual(found.seq, result_seq)
                    self.assertEqual(self.runner.result(done)["stdout"]["content"], output)
        self.assertEqual(self.events.messages("cmd.kill.request", CMD_CHANNEL), [])

    def test_cancel_recovers_committed_request_without_commit_wal_and_kills_it(self):
        self.accept_exec()
        def crash_after_run(message, payload):
            if payload.get("kind") == "cmd.run.request":
                raise Crash()
        self.events.on_publish = crash_after_run
        with self.assertRaises(Crash):
            self.run_tool()
        request = self.events.messages("cmd.run.request", CMD_CHANNEL)[0]
        self.events.on_publish = None
        self.restart()
        def killed(message, payload):
            if payload.get("kind") == "cmd.kill.request":
                self.emit_run(payload["target_seq"], status="KILLED", data=b"", exit_code=None)
        self.events.on_publish = killed
        self.runner.kill_pending()
        done = self.run_tool(cancelled=lambda: True)
        result = self.runner.result(done)
        self.assertEqual(result["exec_id"], request.seq)
        self.assertEqual(result["error_code"], "cancelled")
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)
        kill, = self.events.messages("cmd.kill.request", CMD_CHANNEL)
        self.assertEqual(json.loads(kill.payload)["target_seq"], request.seq)
        self.assert_request_intents()

    def test_prepared_kill_reuses_its_uuid_after_restart(self):
        self.accept_exec()
        self.events.on_publish = lambda message, payload: (_ for _ in ()).throw(Crash()) if payload.get("kind") == "cmd.run.request" else None
        with self.assertRaises(Crash):
            self.run_tool()
        self.events.on_publish = None
        self.restart()

        def crash_after_kill_intent(message, payload):
            if payload.get("kind") == "effect.prepare" and payload["data"].get("kind") == "cmd.kill.request":
                raise Crash()
        self.events.on_publish = crash_after_kill_intent
        with self.assertRaises(Crash):
            self.runner.kill_pending()
        intents = [json.loads(message.payload)["data"] for message in self.events.messages("effect.prepare", WAL_CHANNEL)]
        kill_intent, = [data for data in intents if data["kind"] == "cmd.kill.request"]
        self.assertEqual(self.events.messages("cmd.kill.request", CMD_CHANNEL), [])
        self.events.on_publish = None
        self.restart()
        self.events.on_publish = lambda message, payload: self.emit_run(payload["target_seq"], status="KILLED", data=b"", exit_code=None) if payload.get("kind") == "cmd.kill.request" else None
        self.runner.kill_pending()
        self.run_tool(cancelled=lambda: True)
        kill, = self.events.messages("cmd.kill.request", CMD_CHANNEL)
        self.assertEqual(kill.uuid, kill_intent["uuid"])
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)
        self.assert_request_intents()

    def test_cancellation_only_kills_commands_from_the_current_model_call(self):
        self.accept_exec()
        previous = self.run_tool()
        self.assertEqual(previous.data["outcome"], "timeout")
        self.conversation.answer = SimpleNamespace(key="chat:30:answer:1", seq=len(self.events.history))
        self.accept_exec()
        current = self.run_tool(key="chat:30:answer:1:attempt:0:tool:0")
        self.assertEqual(current.data["outcome"], "timeout")

        self.runner.kill_pending()

        kill, = self.events.messages("cmd.kill.request", CMD_CHANNEL)
        self.assertEqual(json.loads(kill.payload)["target_seq"], current.data["request_seq"])
        self.assertNotEqual(current.data["request_seq"], previous.data["request_seq"])
        self.assertEqual([record.data["answer"] for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="tool.prepare")],
                         ["chat:30:answer:0", "chat:30:answer:1"])
        self.assert_request_intents()

    def test_read_pages_join_bytes_before_decoding_unicode(self):
        original = "中Z".encode()
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                offset = payload["offset"]
                part = original[offset:offset + payload["nbytes"]]
                self.emit_read(message, payload, part, eof=offset + len(part) == len(original))
        self.events.on_publish = read
        done = self.run_tool(self.call("read_stdout", exec_id=999, offset=0, nbytes=4))
        result = self.runner.result(done)
        self.assertEqual(result["stdout"]["content"], "中Z")
        self.assertEqual(result["stdout"]["encoding"], "utf-8")
        self.assertEqual(result["stdout"]["bytes"], 4)
        self.assertTrue(result["eof"])
        offsets = [json.loads(message.payload)["offset"] for message in self.events.messages("cmd.output.read.request", CMD_CHANNEL)]
        self.assertEqual(offsets, [0, 2])
        requests = self.events.messages("cmd.output.read.request", CMD_CHANNEL)
        results = self.events.messages("cmd.output.read.result", CMD_CHANNEL)
        self.assertEqual(done.data, {"tool_key": done.data["tool_key"], "outcome": "read",
                                   "request_seq": requests[0].seq, "page_count": 2,
                                   "result_seq": results[-1].seq})
        self.assert_request_intents()
        self.events.on_publish = None
        self.events.now += 500
        self.restart(output_read_chunk_bytes=1, max_tool_output_bytes=1, cmd_result_timeout_ms=1)
        self.assertEqual(self.runner.result(self.run_tool(self.call("read_stdout", exec_id=999, offset=0, nbytes=4))), result)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 2)

    def test_completion_record_size_is_preflighted_before_acceptance(self):
        checked = []
        def fits(config, channel, payload, **kwargs):
            parsed = json.loads(payload)
            checked.append(parsed)
            return not (parsed.get("kind") == "tool.done" and parsed["data"]["outcome"] == "read")
        count = len(self.events.history)
        with patch("chat_model_agent.tool_runner.publication_fits", side_effect=fits):
            with self.assertRaisesRegex(ModelOutputError, "tool input exceeds"):
                self.runner.validate(SimpleNamespace(tool_calls=(self.call("read_stdout", exec_id=999, nbytes=4),)),
                                     "chat:30:answer:0:attempt:0")
        self.assertTrue(any(payload.get("kind") == "tool.done" for payload in checked))
        self.assertEqual(len(self.events.history), count)

    def test_thousands_of_tiny_read_pages_finish_below_the_deployment_cap(self):
        # Keep the real read loop, WAL encoding, preflight and reconstruction;
        # use immutable indexed page facts to avoid thousands of transport RPCs.
        config = replace(self.config,
            openevent=replace(self.config.openevent, max_payload_bytes=16384),
            agent=replace(self.config.agent, max_tool_output_bytes=3500, output_read_chunk_bytes=1))
        key = "chat:30:answer:0:attempt:0:tool:0"
        call = self.call("read_stdout", exec_id=999, nbytes=3500)
        records, results, parsed, messages = {}, {}, {}, {}
        async def blocking(operation, *args, **kwargs):
            return operation(*args, **kwargs)
        async def put(kind, record_key, data):
            payload = encode(kind, record_key, CHAT_CHANNEL, data)
            self.assertLessEqual(len(payload), config.openevent.max_payload_bytes)
            record = Record(2_000_000, kind, record_key, CHAT_CHANNEL, data)
            records[record_key] = record
            return record
        engine = SimpleNamespace(events=SimpleNamespace(), clock=lambda: self.events.now,
            check_running=lambda: None, condition=threading.RLock(),
            scanned_through=2_000_000,
            cmd_result=lambda request_seq, kind: results.get(request_seq),
            parsed=parsed, messages=messages, blocking=blocking,
            parsed_event=lambda channel, seq: parsed[seq])
        conversation = SimpleNamespace(engine=engine, cfg=config, cid=CHAT_CHANNEL,
            journal=SimpleNamespace(get=records.get, materialize=lambda record: record),
            answer=self.conversation.answer, put=put)
        engine.journal = conversation.journal
        runner = ToolRunner(conversation)
        runner.validate(SimpleNamespace(tool_calls=(call,)), "chat:30:answer:0:attempt:0")
        prepared = Record(100, "tool.prepare", key + ":prepare", CHAT_CHANNEL,
                          runner._prepare_data(key, call, self.events.now))
        records[prepared.key] = prepared
        async def request(prepared, number, params, cancelled, deadline=None):
            seq = 1_000_000 + 2 * number
            records[key + f":request:{number}:commit"] = Record(seq, "effect.commit", "unused",
                CHAT_CHANNEL, {"seq": seq, "uuid": number + 1})
            result = SimpleNamespace(seq=seq + 1, principal=CMD, payload=SimpleNamespace(
                target_seq=999, stream="stdout", offset=number, nbytes=1, status="SUCCESS",
                content="x", content_encoding="utf-8", bytes=1, eof=number == 3499))
            results[seq] = parsed[seq + 1] = result
            messages[seq] = SimpleNamespace(channel_id=CMD_CHANNEL, ts_ms=self.events.now)
            messages[seq + 1] = SimpleNamespace(channel_id=CMD_CHANNEL, ts_ms=self.events.now)
            return seq
        async def wait_result(prepared, request_seq, kind, origin_seq, cancelled):
            return results[request_seq]
        runner._request, runner._wait_result = request, wait_result
        done = self.async_runner.run(runner._read(prepared, lambda: False))
        self.assertEqual(done.data, {"tool_key": key, "outcome": "read", "request_seq": 1_000_000,
                                    "page_count": 3500, "result_seq": 1_006_999})
        self.assertLess(len(encode("tool.done", key + ":done", CHAT_CHANNEL, done.data)), 512)
        old_data = {"tool_key": key, "outcome": "read", "request_seq": 1_000_000,
                    "pages": [event.seq for event in results.values()]}
        self.assertGreater(len(encode("tool.done", key + ":done", CHAT_CHANNEL, old_data)), 16384)
        result = runner.result(done)
        self.assertEqual((result["stdout"]["content"], result["eof"]), ("x" * 3500, True))
        # A fresh runner uses the durable verdict even after cancellation and
        # deadline expiry, without dispatching or waiting for any page again.
        self.events.now += 500
        recovered = ToolRunner(conversation)
        again = self.async_runner.run(recovered.run(prepared, lambda: True))
        self.assertEqual(again, done)
        self.assertEqual(recovered.result(again), result)

    def test_prepared_read_page_reuses_its_uuid_after_restart(self):
        def crash_after_next_page_intent(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                self.emit_read(message, payload, b"ab")
            elif (payload.get("kind") == "effect.prepare"
                  and payload["data"].get("kind") == "cmd.output.read.request"
                  and payload["data"]["request"]["offset"] == 2):
                raise Crash()
        self.events.on_publish = crash_after_next_page_intent
        call = self.call("read_stdout", exec_id=999, nbytes=4)
        with self.assertRaises(Crash):
            self.run_tool(call)
        intents = [json.loads(message.payload)["data"] for message in self.events.messages("effect.prepare", WAL_CHANNEL)]
        self.assertEqual(len(intents), 2)
        self.events.on_publish = None
        self.restart()
        self.events.on_publish = lambda message, payload: self.emit_read(message, payload, b"cd", eof=True) if payload.get("kind") == "cmd.output.read.request" else None
        done = self.run_tool(call)
        self.assertEqual(self.runner.result(done)["stdout"]["content"], "abcd")
        requests = self.events.messages("cmd.output.read.request", CMD_CHANNEL)
        self.assertEqual([request.uuid for request in requests], [data["uuid"] for data in intents])
        self.assert_request_intents()

    def test_read_output_limit_is_distinct_from_eof(self):
        original = b"0123456789"
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                start, size = payload["offset"], payload["nbytes"]
                self.emit_read(message, payload, original[start:start + size])
        self.events.on_publish = read
        done = self.run_tool(self.call("read_stderr", exec_id=999, offset=2, nbytes=8))
        result = self.runner.result(done)
        self.assertEqual((result["offset"], result["nbytes"], result["stderr"]["content"]), (2, 8, "2345"))
        self.assertTrue(result["stderr"]["truncated"])
        self.assertFalse(result["eof"])

    def test_read_failure_statuses_with_protocol_echoes_become_tool_errors(self):
        for status in ("NOT_FOUND", "READ_FAILED"):
            with self.subTest(status=status):
                def read(message, payload):
                    if payload.get("kind") == "cmd.output.read.request":
                        changes = {"error_message": "read refused"} if status != "NOT_FOUND" else {}
                        self.emit_read(message, payload, b"", status=status, eof=status != "READ_FAILED", **changes)
                self.events.on_publish = read
                done = self.run_tool(self.call("read_stdout", exec_id=999, offset=2, nbytes=4), key=f"chat:30:answer:0:{status}")
                result = self.runner.result(done)
                self.assertEqual(result["status"], "error")
                self.assertEqual((result["exec_id"], result["offset"], result["nbytes"]), (999, 2, 4))
                self.assertTrue(result["error_message"])
                self.assertNotIn("stdout", result)

    def test_read_does_not_return_partial_success_at_eof(self):
        original = b"ab"
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                start, size = payload["offset"], payload["nbytes"]
                if start + size > len(original):
                    self.emit_read(message, payload, b"", status="READ_FAILED", error_message="range exceeds output")
                else:
                    self.emit_read(message, payload, original[start:start + size], eof=start + size == len(original))
        self.events.on_publish = read
        done = self.run_tool(self.call("read_stdout", exec_id=999, nbytes=4))
        result = self.runner.result(done)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_message"], "range exceeds output")
        self.assertNotIn("stdout", result)
        self.assertNotIn("eof", result)
        requests = [json.loads(message.payload) for message in self.events.messages("cmd.output.read.request", CMD_CHANNEL)]
        self.assertEqual([(request["offset"], request["nbytes"]) for request in requests], [(0, 2), (2, 2)])
        self.assertEqual(done.data["page_count"], 2)
        self.assertNotIn("pages", done.data)
        self.events.on_publish = None
        self.restart()
        self.assertEqual(self.runner.result(self.run_tool(self.call("read_stdout", exec_id=999, nbytes=4))), result)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 2)

    def test_quota_truncation_can_coincide_with_eof_and_survives_replay(self):
        original = b"abcd"
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                start, size = payload["offset"], payload["nbytes"]
                self.emit_read(message, payload, original[start:start + size], eof=start + size == len(original))
        self.events.on_publish = read
        call = self.call("read_stdout", exec_id=999, nbytes=UINT64_MAX)
        done = self.run_tool(call)
        result = self.runner.result(done)
        self.assertEqual(result["stdout"]["content"], "abcd")
        self.assertEqual(result["nbytes"], UINT64_MAX)
        self.assertTrue(result["stdout"]["truncated"])
        self.assertTrue(result["eof"])
        self.events.on_publish = None
        self.restart(max_tool_output_bytes=1)
        self.assertEqual(self.runner.result(self.run_tool(call)), result)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 2)

    def test_all_read_pages_share_the_first_request_deadline(self):
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request" and payload["offset"] == 0:
                self.emit_read(message, payload, b"ab")
                self.events.now += 60
        self.events.on_publish = read
        done = self.run_tool(self.call("read_stdout", exec_id=999, nbytes=4))
        self.assertEqual(self.runner.result(done)["error_code"], "timeout")
        self.assertEqual(self.wait_count, 1)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 2)

    def test_next_read_page_queued_past_original_deadline_is_not_published(self):
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                self.emit_read(message, payload, b"ab")
        self.events.on_publish = read
        tool_key = "chat:30:answer:0:attempt:0:tool:0"

        async def exercise():
            blocking = self.engine.blocking
            queued_once = False
            async def delay_next_page(operation, *args, **kwargs):
                nonlocal queued_once
                if (queued_once or operation.__name__ != "dispatch"
                        or self.engine.journal.get(tool_key + ":request:1:prepare") is None):
                    return await blocking(operation, *args, **kwargs)
                queued_once = True
                release = threading.Event()
                started = [threading.Event() for _ in range(4)]
                def occupy(index):
                    started[index].set()
                    if not release.wait(5):
                        raise AssertionError("test RPC worker was not released")
                workers = [asyncio.create_task(blocking(occupy, index)) for index in range(4)]
                queued = None
                try:
                    async def all_started():
                        while not all(event.is_set() for event in started):
                            await asyncio.sleep(.001)
                    await asyncio.wait_for(all_started(), 2)
                    queued = asyncio.create_task(blocking(operation, *args, **kwargs))
                    await asyncio.sleep(0)
                    self.assertEqual(self.engine.rpc_pool._queue.qsize(), 1)
                    request, = self.events.messages("cmd.output.read.request", CMD_CHANNEL)
                    self.events.now = request.ts_ms + self.config.agent.cmd_result_timeout_ms
                    release.set()
                    return await queued
                finally:
                    release.set()
                    await asyncio.gather(*workers, *([queued] if queued is not None else []),
                                         return_exceptions=True)
            with patch.object(self.engine, "blocking", delay_next_page):
                prepared = await self.runner._target._prepare(
                    tool_key, self.call("read_stdout", exec_id=999, nbytes=4))
                prepared = await self.engine.blocking(self.engine.journal.materialize, prepared)
                done = await self.runner._target.run(prepared, lambda: False)
            self.assertTrue(queued_once)
            return done

        done = self.async_runner.run(exercise())
        self.assertEqual(done.data["outcome"], "timeout")
        self.assertEqual(self.runner.result(done)["error_code"], "timeout")
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 1)
        self.assertNotIn("pages", done.data)
        self.assertNotIn("page_count", done.data)
        self.assertEqual(self.wait_count, 0)

    def test_cancellation_between_read_pages_finishes_without_waiting_and_survives_restart(self):
        stopped = []
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                self.emit_read(message, payload, b"ab")
                stopped.append(True)
        self.events.on_publish = read
        call = self.call("read_stdout", exec_id=999, nbytes=4)
        done = self.run_tool(call, cancelled=lambda: bool(stopped))
        result = self.runner.result(done)
        self.assertEqual(done.data["outcome"], "read_cancelled")
        request, = self.events.messages("cmd.output.read.request", CMD_CHANNEL)
        self.assertEqual(done.data["request_seq"], request.seq)
        self.assertNotIn("pages", done.data)
        self.assertNotIn("page_count", done.data)
        self.assertNotIn("result_seq", done.data)
        self.assertEqual((result["status"], result["error_code"]), ("error", "cancelled"))
        self.assertEqual((result["exec_id"], result["offset"], result["nbytes"]), (999, 0, 4))
        self.assertIn("not fully read", result["error_message"])
        self.assertNotIn("not dispatched", result["error_message"])
        self.assertNotIn("stdout", result)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 1)
        self.assertEqual(self.wait_count, 0)

        self.events.on_publish = None
        self.events.now += 500
        self.restart(max_tool_output_bytes=1, cmd_result_timeout_ms=1)
        again = self.run_tool(call, cancelled=lambda: True)
        self.assertEqual(self.runner.result(again), result)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("tool.done", WAL_CHANNEL)), 1)
        self.assertEqual(self.wait_count, 0)

    def test_cancellation_waits_for_submitted_read_page_and_keeps_completed_result(self):
        pending = []
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                if payload["offset"] == 0:
                    self.emit_read(message, payload, b"ab")
                else:
                    pending.append((message, payload))
        def finish_page(timeout=None, *, ready=None):
            self.assertFalse(ready())  # An already handled cancellation must not cause a busy loop.
            self.advance(timeout, ready=ready)
            self.assertEqual(self.wait_count, 1)
            self.emit_read(*pending[0], b"cd", eof=True)
            self.assertTrue(ready())
        self.events.on_publish = read
        self.conversation.wait = finish_page
        done = self.run_tool(self.call("read_stdout", exec_id=999, nbytes=4), cancelled=lambda: bool(pending))
        result = self.runner.result(done)
        self.assertEqual(done.data["outcome"], "read")
        self.assertEqual((result["status"], result["stdout"]["content"], result["eof"]), ("ok", "abcd", True))
        self.assertEqual(self.wait_count, 1)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 2)

    def test_cancellation_does_not_shorten_outstanding_read_page_deadline(self):
        pending = []
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                if payload["offset"] == 0:
                    self.emit_read(message, payload, b"ab")
                else:
                    pending.append(message.seq)
        self.events.on_publish = read
        done = self.run_tool(self.call("read_stdout", exec_id=999, nbytes=4), cancelled=lambda: bool(pending))
        self.assertEqual(self.runner.result(done)["error_code"], "timeout")
        self.assertEqual(self.wait_count, 2)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 2)

    def test_recovery_reconciles_submitted_read_page_before_finishing_cancellation(self):
        self.restart(max_tool_output_bytes=6)
        pending = []
        def read(message, payload):
            if payload.get("kind") == "cmd.output.read.request":
                if payload["offset"] == 0:
                    self.emit_read(message, payload, b"ab")
                else:
                    pending.append((message, payload))
                    raise Crash()
        self.events.on_publish = read
        call = self.call("read_stdout", exec_id=999, nbytes=6)
        with self.assertRaises(Crash):
            self.run_tool(call)
        self.events.on_publish = None
        self.restart()
        def finish_page(timeout=None, *, ready=None):
            self.advance(timeout, ready=ready)
            self.assertEqual(self.wait_count, 1)
            self.emit_read(*pending[0], b"cd")
        self.conversation.wait = finish_page
        done = self.run_tool(call, cancelled=lambda: True)
        self.assertEqual(done.data["outcome"], "read_cancelled")
        self.assertNotIn("pages", done.data)
        self.assertNotIn("page_count", done.data)
        self.assertEqual(self.runner.result(done)["error_code"], "cancelled")
        self.assertEqual(self.wait_count, 1)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 2)
        self.assertEqual(len(self.events.messages("tool.done", WAL_CHANNEL)), 1)

    def test_restart_uses_original_request_write_time_and_current_timeout(self):
        self.events.on_publish = lambda message, payload: (_ for _ in ()).throw(Crash()) if payload.get("kind") == "cmd.run.request" else None
        with self.assertRaises(Crash):
            self.run_tool()
        self.events.on_publish = None
        self.events.now += 30
        self.restart(cmd_result_timeout_ms=20)
        done = self.run_tool()
        self.assertEqual(self.runner.result(done)["error_code"], "timeout")
        self.assertEqual(self.wait_count, 0)
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)

    def test_committed_tool_result_reconstructs_original_output_limit_after_restart(self):
        self.events.on_publish = lambda message, payload: self.emit_run(message.seq, data=b"abcdef") if payload.get("kind") == "cmd.run.request" else None
        done = self.run_tool()
        before = self.runner.result(done)
        self.events.on_publish = None
        self.events.now += 500
        self.restart(max_tool_output_bytes=1, cmd_result_timeout_ms=1)
        again = self.run_tool()
        self.assertEqual(self.runner.result(again), before)
        self.assertEqual(before["stdout"]["content"], "abcd")
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)

    def test_large_exec_result_keeps_original_identity_and_true_facts(self):
        from chat_model_agent.tool_runner import future_error_results
        self.restart(max_tool_output_bytes=4096)
        limit = max(event_size(tool_result_event("0" * 36, (1 << 64) - 1, result))
                    for result in future_error_results(self.call())) + 32
        self.set_current_result_budget(limit)
        self.assertGreater(self.conversation.answer.data["settings"]["context_history_max_bytes"], limit)
        self.events.on_publish = lambda message, payload: self.emit_run(message.seq, data=b"x" * 900) if payload.get("kind") == "cmd.run.request" else None
        done = self.run_tool()
        result = self.runner.result(done)
        self.assertEqual(done.data["outcome"], "exec")
        self.assertEqual(done.data["rejection"], "result_too_large")
        self.assertEqual(result["error_code"], "result_too_large")
        self.assertEqual((result["exit_code"], result["stdout_bytes"], result["stderr_bytes"]), (0, 900, 0))
        self.assertNotIn("stdout", result)
        self.assertEqual(result["exec_id"], done.data["request_seq"])
        prepared = self.engine.journal.get(done.data["tool_key"] + ":prepare")
        import uuid
        self.assertEqual(str(uuid.UUID(prepared.data["task_id"])), prepared.data["task_id"])
        self.assertLessEqual(event_size(tool_result_event(prepared.data["task_id"], self.events.now, result)), limit)
        self.events.on_publish = None
        self.restart()
        self.assertEqual(self.runner.result(self.run_tool()), result)
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)

    def test_large_read_result_keeps_range_and_eof_without_repeating_pages(self):
        self.restart(max_tool_output_bytes=2048, output_read_chunk_bytes=512)
        self.set_current_result_budget(800)
        self.events.on_publish = lambda message, payload: self.emit_read(message, payload, b"x" * payload["nbytes"], eof=payload["offset"] == 512) if payload.get("kind") == "cmd.output.read.request" else None
        call = self.call("read_stdout", exec_id=999, nbytes=1024)
        done = self.run_tool(call)
        result = self.runner.result(done)
        self.assertEqual((done.data["outcome"], done.data["page_count"], done.data["rejection"]), ("read", 2, "result_too_large"))
        self.assertEqual((result["exec_id"], result["offset"], result["nbytes"], result["eof"]), (999, 0, 1024, True))
        self.assertNotIn("stdout", result)
        self.events.on_publish = None
        self.restart()
        self.assertEqual(self.runner.result(self.run_tool(call)), result)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 2)

    def test_accepted_read_keeps_its_event_allowance_when_message_capacity_falls(self):
        self.restart(max_tool_output_bytes=2048, output_read_chunk_bytes=2048)
        key = "chat:30:answer:0:attempt:0:tool:0"
        call = self.call("read_stdout", exec_id=999, nbytes=UINT64_MAX)
        self.runner.validate(SimpleNamespace(tool_calls=(call,)), "chat:30:answer:0:attempt:0")
        prepared = self.runner._prepare(key, call)
        self.config = replace(self.config, openevent=replace(self.config.openevent, max_payload_bytes=4096))
        self.restart()
        self.events.on_publish = lambda message, payload: self.emit_read(message, payload, b"x" * payload["nbytes"], eof=True) if payload.get("kind") == "cmd.output.read.request" else None
        done = self.run_tool(call, key=key)
        result = self.runner.result(done)
        self.assertEqual((result["nbytes"], result["stdout"]["content"]), (UINT64_MAX, "x" * 2048))
        self.assertTrue(result["stdout"]["truncated"])
        self.assertGreater(event_size(tool_result_event(prepared.data["task_id"], self.events.now, result)),
                           self.conversation.budget.target)
        self.assertEqual(len(self.events.messages("cmd.output.read.request", CMD_CHANNEL)), 1)

    def test_future_read_error_with_uint64_arguments_is_rejected_before_acceptance(self):
        self.conversation.answer.data["settings"]["context_history_max_bytes"] = 449
        call = self.call("read_stdout", exec_id=UINT64_MAX, offset=UINT64_MAX, nbytes=UINT64_MAX)
        count = len(self.events.history)
        with self.assertRaisesRegex(ModelOutputError, "tool error result exceeds"):
            self.runner.validate_errors(call)
        self.assertEqual(len(self.events.history), count)

    def test_complete_result_reserves_source_until_done_and_timeout_keeps_actual_slot(self):
        key = "chat:30:answer:0:attempt:0:tool:0"
        prepared = self.runner._prepare(key, self.call())
        self.assertIsNone(self.runner.pending_source(prepared))
        self.assertTrue(self.runner.settled(prepared))
        prepared = self.engine.journal.materialize(prepared)
        request_seq = self.runner._request(prepared, 0, self.runner._params(prepared.data), lambda: False)
        self.assertIsNone(self.runner.pending_source(prepared))
        self.assertFalse(self.runner.settled(prepared))
        result_seq = self.emit_run(request_seq)
        self.async_runner.run(self.engine.aread_through(CMD_CHANNEL, result_seq))
        self.assertEqual(self.runner.pending_source(prepared), result_seq)
        done = self.run_tool()
        self.assertEqual(done.data["outcome"], "exec")
        self.assertIsNone(self.runner.pending_source(prepared))
        self.assertEqual(self.runner.pending_source(prepared, through=done.seq - 1), result_seq)
        self.assertIsNone(self.runner.pending_source(prepared, through=done.seq))
        self.assertTrue(self.runner.settled(prepared))
        timeout_key = "chat:30:answer:0:attempt:0:tool:1"
        timed_out = self.run_tool(key=timeout_key)
        pending = self.engine.journal.get(timeout_key + ":prepare")
        self.assertEqual(timed_out.data["outcome"], "timeout")
        self.assertFalse(self.runner.settled(pending))
        self.assertIsNone(self.runner.pending_source(pending))
        late_seq = self.emit_run(timed_out.data["request_seq"])
        self.async_runner.run(self.engine.aread_through(CMD_CHANNEL, late_seq))
        self.assertTrue(self.runner.settled(pending))
        self.assertEqual(self.run_tool(key=timeout_key), timed_out)
        self.assertEqual(self.runner.result(timed_out)["error_code"], "timeout")

    def test_fast_result_reserves_position_before_request_commit_confirmation(self):
        key = "chat:30:answer:0:attempt:0:tool:0"
        prepared = self.runner._prepare(key, self.call())
        observed = []
        def respond(message, payload):
            if payload.get("kind") != "cmd.run.request":
                return
            result_seq = self.emit_run(message.seq)
            self.assertIsNone(self.engine.journal.get(key + ":request:0:commit"))
            observed.append(self.runner.pending_source(prepared))
            self.assertEqual(observed[-1], result_seq)
        self.events.on_publish = respond
        done = self.run_tool()
        self.assertEqual(observed, [done.data["result_seq"]])

    def test_unconfirmed_request_search_skips_old_cmd_history_and_observes_late_publication(self):
        self.events.on_publish = lambda message, payload: self.emit_run(message.seq) \
            if payload.get("kind") == "cmd.run.request" else None
        for number in range(8):
            self.run_tool(key=f"chat:30:answer:0:attempt:0:tool:{number}")
        key = "chat:30:answer:0:attempt:0:tool:8"

        def crash_after_intent(message, payload):
            if payload.get("kind") == "effect.prepare" and payload["data"].get("kind") == "cmd.run.request":
                raise Crash()

        self.events.on_publish = crash_after_intent
        with self.assertRaises(Crash):
            self.run_tool(key=key)
        prepared = self.engine.journal.get(key + ":prepare")
        intent = self.engine.journal.get(key + ":request:0:prepare")
        self.events.on_publish = None
        original = self.engine.events_in
        visited = []

        def newer_events(channel, *, after=None):
            events = original(channel, after=after)
            self.assertEqual(after, intent.seq)
            self.assertTrue(all(event.seq > intent.seq for event in events))
            visited.append(len(events))
            return events

        with patch.object(self.engine, "events_in", side_effect=newer_events):
            self.assertIsNone(self.runner.pending_source(prepared))
        self.assertEqual(visited, [0])

        def crash_after_request(message, payload):
            if payload.get("kind") == "cmd.run.request":
                raise Crash()

        self.events.on_publish = crash_after_request
        with self.assertRaises(Crash):
            self.run_tool(key=key)
        self.events.on_publish = None
        request = self.events.messages("cmd.run.request", CMD_CHANNEL)[-1]
        self.assertIsNone(self.engine.journal.get(key + ":request:0:commit"))
        with patch.object(self.engine, "events_in", side_effect=newer_events):
            self.assertIsNone(self.runner.pending_source(prepared))
            result_seq = self.emit_run(request.seq)
            self.assertEqual(self.runner.pending_source(prepared), result_seq)
            self.assertTrue(self.runner.settled(prepared))
        self.assertEqual(visited, [0, 1, 2, 2])

    def test_cleanup_waits_for_real_terminal_after_timeout_and_sent_kill(self):
        done = self.run_tool()
        prepared = self.engine.journal.get(done.data["tool_key"] + ":prepare")
        self.assertEqual(done.data["outcome"], "timeout")
        self.conversation.cancelled = lambda: True
        async def exercise():
            waiting = asyncio.Event()
            conversation = self.conversation._target
            async def wait_actual(timeout=None, *, ready=None):
                waiting.set()
                await Conversation.wait(conversation, timeout, ready=ready)
            self.conversation.wait = wait_actual
            task = asyncio.create_task(self.runner._target.cleanup(prepared))
            try:
                await asyncio.wait_for(waiting.wait(), 2)
                self.assertEqual(len(self.events.messages("cmd.kill.request", CMD_CHANNEL)), 1)
                self.assertFalse(task.done())
                self.assertFalse(self.runner.settled(prepared))
                await self.engine.blocking(self.emit_run, done.data["request_seq"],
                                           status="KILLED", data=b"", exit_code=None)
                await asyncio.wait_for(task, 2)
                self.assertTrue(self.runner.settled(prepared))
                self.assertEqual(self.runner.result(done)["error_code"], "timeout")
                self.assertEqual(len(self.events.messages("tool.done", WAL_CHANNEL)), 1)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.async_runner.run(exercise())

    def test_concurrent_cancellation_paths_share_one_kill_identity(self):
        done = self.run_tool()
        prepared = self.engine.journal.get(done.data["tool_key"] + ":prepare")
        async def exercise():
            await asyncio.gather(self.runner._target._kill(prepared, done.data["request_seq"]),
                                 self.runner._target._kill(prepared, done.data["request_seq"]))
        self.async_runner.run(exercise())
        requests = self.events.messages("cmd.kill.request", CMD_CHANNEL)
        self.assertEqual(len(requests), 1)
        intent = self.engine.journal.get(done.data["tool_key"] + ":kill:prepare")
        self.assertEqual(intent.data["uuid"], requests[0].uuid)


if __name__ == "__main__":
    unittest.main()
