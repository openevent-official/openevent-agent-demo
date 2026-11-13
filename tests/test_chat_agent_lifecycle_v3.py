"""Interleavings between selected model results, cancellation and WAL handoff."""
import asyncio
import json
import unittest
from unittest.mock import patch

from openevent.chat_sdk import TurnRef
from openevent.cmd_sdk import CmdOutput, CmdRunResultInput
from openevent.model_proxy_sdk import InferEndInput, publish_infer_end

from chat_model_agent.session import AnswerWorkflow, Conversation
from chat_model_agent.budget import ContextBudget
import test_chat_agent_session as support
from test_chat_agent_session import AGENT, PROVIDER, CMD, CHAT_CHANNEL, MODEL_CHANNEL, CMD_CHANNEL
from chat_model_agent.state import HistoryError


class LifecycleV3Tests(unittest.TestCase):
    # Reuse transport/setup, not the old suite's test methods.
    setUp = support.ConversationSDKTests.setUp
    stop_reader = support.ConversationSDKTests.stop_reader
    fresh = support.ConversationSDKTests.fresh
    restart = support.ConversationSDKTests.restart
    advance = support.ConversationSDKTests.advance
    input = support.ConversationSDKTests.input
    run_answer = support.ConversationSDKTests.run_answer
    end = support.ConversationSDKTests.end
    request_callback = support.ConversationSDKTests.request_callback

    def finish(self, request, payload, *, tools=False):
        message = {"role": "assistant", "content": None if tools else "finished"}
        if tools:
            message["tool_calls"] = [{"id": f"call-{i + 1}", "type": "function", "function": {
                "name": "exec", "arguments": json.dumps({"command": "true"})}}
                for i in range(int(tools))]
        return publish_infer_end(self.provider, MODEL_CHANNEL, PROVIDER, AGENT,
            InferEndInput(stream_id=payload["stream_id"], request_seq=request.seq,
                          status_code=200, end_status="completed", body={"choices": [{
                              "index": 0, "message": message,
                              "finish_reason": "tool_calls" if tools else "stop"}]}))

    def test_selected_model_output_survives_slow_final_display(self):
        self.input()
        deadlines = []
        def respond(message, payload):
            if payload.get("kind") == "infer.request":
                deadlines.append(message.ts_ms + self.config.agent.model_timeout_ms)
                self.finish(message, payload)
            elif payload.get("kind") == "turn.reset":
                self.events.now = deadlines[-1] + 1
        self.events.on_publish = respond
        prepared = self.run_answer()
        accepted = self.engine.journal.get(prepared.key + ":attempt:0:accept")
        self.assertIsNotNone(accepted)
        self.assertIsNone(self.engine.journal.get(prepared.key + ":attempt:0:fail"))
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)

    def test_cancellation_during_accept_handoff_waits_for_direct_tool_facts(self):
        self.input()
        self.events.on_publish = lambda request, payload: self.finish(request, payload, tools=True) if payload.get("kind") == "infer.request" else None

        async def exercise():
            reached, release = asyncio.Event(), asyncio.Event()
            original = AnswerWorkflow.put
            async def hold_accept(session, kind, key, data, **kwargs):
                if kind == "attempt.accept":
                    reached.set()
                    await release.wait()
                return await original(session, kind, key, data, **kwargs)
            session = self.session._target
            async def wait_actual(timeout=None, *, ready=None):
                await Conversation.wait(session, timeout, ready=ready)
            session.wait = wait_actual
            runner = None
            try:
                with patch.object(AnswerWorkflow, "put", hold_accept):
                    runner = asyncio.create_task(session.run())
                    await asyncio.wait_for(reached.wait(), 2)
                    prepared, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")
                    cancel_seq = await self.engine.blocking(self.user.cancel_turn,
                        target_turn=TurnRef(AGENT, prepared.data["turn_id"]),
                        start_seq=session._turn(prepared).creation_seq)
                    terminal, = self.events.messages("infer.end", MODEL_CHANNEL)
                    self.assertLess(terminal.seq, cancel_seq)
                    self.assertEqual(session._turn(prepared).terminal_seq, cancel_seq)
                    # Reading the cancellation cannot skip the earlier terminal
                    # while its acceptance is still waiting for confirmation.
                    await asyncio.sleep(0)
                    snapshot = await self.engine.blocking(session.index.snapshot)
                    self.assertEqual(snapshot.processed, terminal.seq - 1)
                    self.assertIsNone(self.engine.journal.get(prepared.key + ":attempt:0:accept"))
                    self.assertIsNone(self.engine.journal.get(prepared.key + ":cancel"))
                    self.assertFalse(session._answer_finished(prepared))
                    release.set()
                    await asyncio.wait_for(self.engine.wait_async(
                        lambda: session._answer_finished(prepared)), 2)
                    accepted = self.engine.journal.get(prepared.key + ":attempt:0:accept")
                    tool = self.engine.journal.get(prepared.key + ":attempt:0:tool:0:prepare")
                    result = self.engine.journal.get(prepared.key + ":attempt:0:tool:0:done")
                    self.assertIsNotNone(accepted)
                    self.assertEqual(result.data["outcome"], "cancelled")
                    self.assertLess(accepted.seq, tool.seq)
                    self.assertLess(tool.seq, result.seq)
                    cancellation = self.engine.journal.get(prepared.key + ":cancel")
                    self.assertEqual(cancellation.data["cancel_seq"], cancel_seq)
                    self.assertEqual(accepted.data["terminal_seq"], terminal.seq)
                    snapshot = await self.engine.blocking(session.index.snapshot)
                    ordered = [item.ref["type"] for item in snapshot.events]
                    self.assertEqual(ordered, ["user", "tool_call", "cancel", "tool_result"])
                    self.assertEqual(session._turn(prepared).status, "cancelled")
                    self.assertEqual(self.events.messages("cmd.run.request", CMD_CHANNEL), [])
            finally:
                release.set()
                self.engine.request_stop()
                if runner is not None:
                    await asyncio.wait_for(runner, 2)
        self.async_runner.run(exercise())

    def test_snapshot_does_not_use_a_tool_verdict_newer_than_its_read_watermark(self):
        self.input()
        self.events.on_publish = lambda request, payload: self.finish(request, payload, tools=True) if payload.get("kind") == "infer.request" else None
        answer = self.run_answer()
        prepared = self.engine.journal.get(answer.key + ":attempt:0:tool:0:prepare")
        prepared = self.engine.journal.materialize(prepared)
        tools = self.session.tools
        request_seq = self.async_runner.run(tools._request(
            prepared, 0, tools._params(prepared.data), lambda: False))
        result_seq = self.cmd.publish_run_result(CMD, "worker-token", CMD_CHANNEL, AGENT,
            CmdRunResultInput(prev_seq=request_seq, status="SUCCESS", timeout_ms=1000,
                exit_code=0,
                stdout=CmdOutput("out", 2, "utf-8", "ok"), stderr=CmdOutput("err", 0, "utf-8", "")))
        later_user = self.input("arrived after the complete result")
        read = self.engine.scanned_through
        original = self.session.index.user
        persisted = []
        def persist_while_rendering(seq):
            if not persisted:
                loaded = self.engine.journal.materialize(prepared)
                persisted.append(self.async_runner.run(tools.run(loaded, lambda: False)))
            return original(seq)
        with patch.object(self.session.index, "user", persist_while_rendering):
            snapshot = self.session.index.snapshot()
            # Body loading is deferred until the frozen snapshot is consumed.
            next(item for item in snapshot.events if item.ref["type"] == "user").event
        self.assertGreater(persisted[0].seq, read)
        self.assertEqual(snapshot.read, read)
        self.assertLess(snapshot.processed, result_seq)
        self.assertFalse(any(item.ref == {"type": "user", "seq": later_user} for item in snapshot.events))
        following = self.session.index.snapshot()
        self.assertTrue(any(item.ref == {"type": "tool_result", "record": persisted[0].key}
                            for item in following.events))
        self.assertTrue(any(item.ref == {"type": "user", "seq": later_user} for item in following.events))

    def test_slow_rejection_notice_does_not_block_an_independent_valid_input(self):
        fixed = ContextBudget.from_config(self.config.agent).fixed
        self.fresh(context_max_input_bytes=fixed + 2000)
        rejected_seq = self.input("x" * 5000)
        valid_seq = self.input("continue")
        self.events.on_publish = lambda request, payload: self.finish(request, payload) if payload.get("kind") == "infer.request" else None
        async def exercise():
            reached, release = asyncio.Event(), asyncio.Event()
            original = Conversation._chat_effect
            async def hold_notice(session, key, kind, **kwargs):
                if kwargs.get("source", {}).get("type") == "input_rejection":
                    reached.set()
                    await release.wait()
                return await original(session, key, kind, **kwargs)
            session = self.session._target
            async def wait_actual(timeout=None, *, ready=None):
                await Conversation.wait(session, timeout, ready=ready)
            session.wait = wait_actual
            runner = None
            try:
                with patch.object(Conversation, "_chat_effect", hold_notice):
                    runner = asyncio.create_task(session.run())
                    await asyncio.wait_for(reached.wait(), 2)
                    await asyncio.wait_for(self.engine.wait_async(lambda: bool(
                        self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept"))), 2)
                    answer, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")
                    self.assertEqual(answer.data["reply_to_seqs"], [valid_seq])
                    rejection, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="input.reject")
                    self.assertEqual(rejection.data["input_seq"], rejected_seq)
                    self.assertIsNone(self.engine.journal.get(rejection.key + ":notice:commit"))
                    release.set()
                    await asyncio.wait_for(self.engine.wait_async(lambda:
                        self.engine.journal.get(rejection.key + ":notice:commit") is not None), 2)
            finally:
                release.set()
                self.engine.request_stop()
                if runner is not None:
                    await asyncio.wait_for(runner, 2)
        self.async_runner.run(exercise())

    def test_dispatch_waits_for_every_tool_preparation_in_the_accepted_output(self):
        self.input()
        def respond(request, payload):
            if payload.get("kind") == "infer.request":
                self.finish(request, payload, tools=2 if len(self.events.messages("infer.request", MODEL_CHANNEL)) == 1 else False)
            elif payload.get("kind") == "cmd.run.request":
                self.cmd.publish_run_result(CMD, "worker-token", CMD_CHANNEL, AGENT,
                    CmdRunResultInput(prev_seq=request.seq, status="SUCCESS", timeout_ms=1000,
                        exit_code=0,
                        stdout=CmdOutput("out", 0, "utf-8", ""), stderr=CmdOutput("err", 0, "utf-8", "")))
        self.events.on_publish = respond
        async def exercise():
            reached, release = asyncio.Event(), asyncio.Event()
            original = AnswerWorkflow.put
            async def hold_second_tool(session, kind, key, data, **kwargs):
                if kind == "tool.prepare" and data["call_id"] == "call-2":
                    reached.set()
                    await release.wait()
                return await original(session, kind, key, data, **kwargs)
            session = self.session._target
            async def wait_actual(timeout=None, *, ready=None):
                await Conversation.wait(session, timeout, ready=ready)
            session.wait = wait_actual
            runner = None
            try:
                with patch.object(AnswerWorkflow, "put", hold_second_tool):
                    runner = asyncio.create_task(session.run())
                    await asyncio.wait_for(reached.wait(), 2)
                    await asyncio.sleep(.02)
                    self.assertEqual(self.events.messages("cmd.run.request", CMD_CHANNEL), [])
                    release.set()
                    await asyncio.wait_for(self.engine.wait_async(lambda: len(
                        self.engine.journal.select(channel=CHAT_CHANNEL, kind="tool.done")) == 2), 2)
                    preparations = self.engine.journal.select(channel=CHAT_CHANNEL, kind="tool.prepare")
                    requests = self.events.messages("cmd.run.request", CMD_CHANNEL)
                    self.assertLess(max(record.seq for record in preparations), min(request.seq for request in requests))
            finally:
                release.set()
                self.engine.request_stop()
                if runner is not None:
                    await asyncio.wait_for(runner, 2)
        self.async_runner.run(exercise())

    def test_recovery_budget_checks_known_events_behind_an_unsettled_source(self):
        self.input("start a command")
        self.events.on_publish = lambda request, payload: self.finish(request, payload, tools=True) if payload.get("kind") == "infer.request" else None
        first = self.run_answer()
        tool = self.engine.journal.get(first.key + ":attempt:0:tool:0:prepare")
        tool = self.engine.journal.materialize(tool)
        request_seq = self.async_runner.run(self.session.tools._request(
            tool, 0, self.session.tools._params(tool.data), lambda: False))
        self.input("another question while the command runs")
        result_sources = []
        def finish_both(request, payload):
            if payload.get("kind") != "infer.request":
                return
            result_sources.append(self.cmd.publish_run_result(CMD, "worker-token", CMD_CHANNEL, AGENT,
                CmdRunResultInput(prev_seq=request_seq, status="SUCCESS", timeout_ms=1000,
                    exit_code=0,
                    stdout=CmdOutput("out", 0, "utf-8", ""), stderr=CmdOutput("err", 0, "utf-8", ""))))
            self.end(request, payload, response_body=support.body("x" * 2000))
        self.events.on_publish = finish_both
        second = self.run_answer()
        self.session._finish_answer()
        self.assertTrue(self.session._answer_finished(second))
        snapshot = self.session.index.snapshot()
        self.assertLess(snapshot.processed, result_sources[0])
        self.assertFalse(any(item.ref == {"type": "assistant", "record": second.key + ":attempt:0:accept"}
                             for item in snapshot.events))
        fixed = ContextBudget.from_config(self.config.agent).fixed
        self.events.on_publish = None
        self.restart(context_max_input_bytes=fixed + 2000)
        with self.assertRaisesRegex(HistoryError, "required history event"):
            self.session._check_recovery_budget()

    def test_restart_after_final_compaction_failure_does_not_grant_new_attempts(self):
        self.fresh(context_max_input_bytes=12000)
        text = "x" * (self.session.budget.target - 600)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=support.body(text)))
        self.input(text)
        self.run_answer()
        self.session._finish_answer()
        self.input(text)
        def fail_then_crash(request, payload):
            if payload.get("kind") == "infer.request":
                self.end(request, payload, response_body=support.body(" "))
            elif (payload.get("kind") == "attempt.fail" and ":compaction:" in payload["key"]
                  and payload["key"].endswith(":attempt:1:fail")):
                raise support.Crash()
        self.events.on_publish = fail_then_crash
        with self.assertRaises(support.Crash):
            self.session._next_answer()
        self.assertEqual(len(self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")), 1)
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 3)
        self.events.on_publish = None
        self.restart()
        prepared = self.session._next_answer()
        self.session._run_answer(prepared)
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertTrue(self.session._answer_finished(prepared))
        self.assertIsNone(self.session._turn(prepared))
        self.assertEqual(len(self.engine.journal.select(channel=CHAT_CHANNEL, kind="compaction.prepare")), 1)
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 3)
        failure = self.engine.journal.get(prepared.key + ":attempt:0:fail")
        self.assertIn("History compression", failure.data["reason"])


if __name__ == "__main__":
    unittest.main()
