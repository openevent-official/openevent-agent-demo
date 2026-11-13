"""Reply completion follows terminal Chat state and settled direct work."""

import json
import unittest
from unittest.mock import patch

from openevent.chat_sdk import TurnRef
from openevent.cmd_sdk import CmdOutputReadResultInput

import test_chat_agent_session as fixture


class AnswerCompletionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def prepare(self, calls=()):
        f = self.f
        output = fixture.body("answer")
        if calls:
            output["choices"][0]["finish_reason"] = "tool_calls"
            output["choices"][0]["message"]["tool_calls"] = [
                {"id": ident, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}
                for ident, name, arguments in calls]
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=output))
        f.input()
        prepared = f.session._next_answer()
        return prepared, f.session._child(prepared)

    def test_restart_finishes_model_cancellation_after_user_cancel_fact_was_committed(self):
        f = self.f
        prepared, child = self.prepare()
        requests = []

        def cancel_then_crash(message, payload):
            if payload.get("kind") == "infer.request":
                requests.append(message)
                f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, prepared.data["turn_id"]),
                                   start_seq=child._turn().creation_seq)
            elif payload.get("kind") == "answer.cancel":
                raise fixture.Crash()

        f.events.on_publish = cancel_then_crash
        with self.assertRaises(fixture.Crash):
            f.async_runner.run(child._run_answer(prepared))
        self.assertEqual(child._turn().status, "cancelled")
        self.assertIsNotNone(f.engine.journal.get(prepared.key + ":cancel"))
        self.assertFalse(f.session._answer_finished(prepared))
        self.assertEqual(f.events.messages("infer.cancel", fixture.MODEL_CHANNEL), [])
        f.events.on_publish = None
        f.restart()
        self.assertFalse(f.session._answer_finished(prepared))
        with f.background_session() as until:
            until(lambda: f.session._answer_finished(prepared) and prepared.key not in f.session.children)
        cancelled, = f.events.messages("infer.cancel", fixture.MODEL_CHANNEL)
        self.assertEqual(json.loads(cancelled.payload)["request_seq"], requests[0].seq)
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), 1)
        self.assertEqual(f.events.messages("answer.done", fixture.WAL_CHANNEL), [])
        self.assertEqual(f.session.children, {})

    def test_cancelled_answer_waits_for_every_accepted_tool_to_be_registered(self):
        f = self.f
        prepared, child = self.prepare((
            ("first", "exec", {"command": "true"}),
            ("second", "exec", {"command": "true"}),
        ))
        original_prepare = child.tools._prepare

        async def interrupt_second(key, call):
            if call.id == "second":
                raise fixture.Crash()
            return await original_prepare(key, call)

        with patch.object(child.tools, "_prepare", side_effect=interrupt_second):
            with self.assertRaises(fixture.Crash):
                f.async_runner.run(child._run_answer(prepared))
            f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, prepared.data["turn_id"]),
                               start_seq=child._turn().creation_seq)
            with self.assertRaises(fixture.Crash):
                f.async_runner.run(child._cancel_answer())
            tool, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")
            f.session._tool_step(tool)
            self.assertTrue(child.tools.settled(tool))
            self.assertEqual(f.engine.journal.get(tool.data["tool_key"] + ":done").data["outcome"], "cancelled")
            self.assertIsNotNone(f.engine.journal.get(prepared.key + ":cancel"))
            self.assertFalse(f.session._answer_finished(prepared))
            f.async_runner.run(child._finish_answer())
            self.assertIn(prepared.key, f.session.children)
        f.async_runner.run(child._cancel_answer())
        self.assertEqual(len(f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")), 2)
        self.assertTrue(f.session._answer_finished(prepared))
        self.assertNotIn(prepared.key, f.session.children)
        self.assertEqual(f.events.messages("cmd.run.request", fixture.CMD_CHANNEL), [])

    def test_cancelled_reply_with_timed_out_read_keeps_execution_until_actual_result_arrives(self):
        f = self.f
        prepared, child = self.prepare((("read", "read_stdout", {"exec_id": 100, "nbytes": 2}),))
        f.async_runner.run(child._run_answer(prepared))
        tool, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")
        waits = []

        async def advance(timeout=None, *, ready=None):
            waits.append(timeout)
            if len(waits) == 1:
                f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, prepared.data["turn_id"]),
                                   start_seq=child._turn().creation_seq)
                await child._cancel_answer()
                f.events.now += f.config.agent.cmd_result_timeout_ms + 1
                return
            self.assertEqual(len(waits), 2)
            self.assertEqual(child._turn().status, "cancelled")
            self.assertEqual(f.engine.journal.get(tool.data["tool_key"] + ":done").data["outcome"], "timeout")
            self.assertFalse(child.tools.settled(tool))
            self.assertFalse(f.session._answer_finished(prepared))
            await child._finish_answer()
            self.assertIn(prepared.key, f.session.children)
            self.assertIsNone(child.model_body)
            request, = f.events.messages("cmd.output.read.request", fixture.CMD_CHANNEL)
            payload = json.loads(request.payload)
            f.cmd.publish_output_read_result(fixture.CMD, "worker-token", fixture.CMD_CHANNEL, fixture.AGENT,
                CmdOutputReadResultInput(prev_seq=request.seq, status="SUCCESS", content="ok", bytes=2,
                    eof=True, content_encoding="utf-8", target_seq=payload["target_seq"],
                    stream=payload["stream"], offset=payload["offset"], nbytes=payload["nbytes"]))

        f.session.wait = advance
        f.session._tool_step(tool)
        f.async_runner.run(child._finish_answer())
        self.assertEqual(len(waits), 2)
        self.assertTrue(f.session._answer_finished(prepared))
        self.assertNotIn(prepared.key, f.session.children)
        self.assertIsNone(child.model_body)
        self.assertEqual(f.events.messages("answer.done", fixture.WAL_CHANNEL), [])


if __name__ == "__main__":
    unittest.main()
