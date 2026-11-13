"""Independent answer executions share coordination, not disposable state."""

import asyncio
import unittest
from unittest.mock import patch

from openevent.chat_sdk import TurnRef

from async_helpers import SyncProxy
import test_chat_agent_session as fixture


class AnswerExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def turn(self, prepared):
        return self.f.visible_turn(fixture.AGENT, prepared.data["turn_id"])

    def pending_tool_answer(self):
        f = self.f
        output = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "old answer", "tool_calls": [{
                "id": "run", "type": "function",
                "function": {"name": "exec", "arguments": '{"command":"true"}'}}]}}]}
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=output))
        f.input("old question")
        prepared = f.session._next_answer()
        child = SyncProxy(f.session._child(prepared), f.async_runner)
        child._run_answer(prepared)
        self.assertFalse(f.session._answer_finished(prepared))
        self.assertEqual(self.turn(prepared).text, "old answer")
        return prepared, child

    def test_cancelling_old_answer_preserves_new_writer_and_model_buffers(self):
        f = self.f
        old, old_child = self.pending_tool_answer()
        f.input("new question")
        prepared = f.session._next_answer()
        child = SyncProxy(f.session._child(prepared), f.async_runner)
        pending = []

        def stream(request, payload):
            pending.append((request, payload))
            head = f.head(request, payload)
            f.append(request, payload, head, fixture.chunk("new answer survives", done=True))

        f.request_callback(stream)
        waits = []

        async def cancel_old_then_finish(timeout=None, *, ready=None):
            self.assertEqual(waits, [])
            waits.append(timeout)
            self.assertEqual(self.turn(prepared).text, "new answer survives")
            body, response, writer = child.model_body, child.model_response, child.active_writer
            self.assertIsNotNone(body)
            self.assertIsNotNone(response)
            self.assertIsNotNone(writer)
            f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, old.data["turn_id"]),
                               start_seq=old_child._turn().creation_seq)
            await old_child._target._cancel_answer()
            self.assertNotIn(old.key, f.session.children)
            self.assertIsNone(old_child.model_body)
            self.assertIsNone(old_child.model_response)
            self.assertIs(child.model_body, body)
            self.assertIs(child.model_response, response)
            self.assertIs(child.active_writer, writer)
            self.assertFalse(child.cancelled())
            self.assertEqual(child._turn().status, "streaming")
            self.assertEqual(f.events.messages("infer.cancel", fixture.MODEL_CHANNEL), [])
            f.end(*pending[0])

        f.session.wait = cancel_old_then_finish
        child._run_answer(prepared)
        self.assertEqual(len(waits), 1)
        self.assertEqual(old_child._turn().status, "cancelled")
        self.assertEqual(self.turn(prepared).text, "new answer survives")
        self.assertEqual(child._turn().status, "completed")
        self.assertTrue(f.session._answer_finished(old))
        self.assertTrue(f.session._answer_finished(prepared))
        self.assertIsNone(f.engine.journal.get(prepared.key + ":cancel"))
        self.assertEqual(f.events.messages("cmd.run.request", fixture.CMD_CHANNEL), [])
        self.assertEqual(f.session.children, {})

    def test_released_execution_can_finish_an_already_running_tool_coroutine(self):
        f = self.f
        prepared, proxy = self.pending_tool_answer()
        child = proxy._target
        tool, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")
        f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, prepared.data["turn_id"]),
                           start_seq=child._turn().creation_seq)

        async def finish_after_release():
            committed, resume = asyncio.Event(), asyncio.Event()
            original_put = child.put

            async def hold_tool_completion(kind, *args, **kwargs):
                result = await original_put(kind, *args, **kwargs)
                if kind == "tool.done":
                    committed.set()
                    await resume.wait()
                return result

            with patch.object(child, "put", side_effect=hold_tool_completion):
                task = asyncio.create_task(f.session._target._tool_step(tool))
                try:
                    await asyncio.wait_for(committed.wait(), 3)
                    await child._cancel_answer()
                    self.assertNotIn(prepared.key, f.session.children)
                    self.assertIsNone(child.model_body)
                    self.assertIsNone(child.model_response)
                    self.assertTrue(f.session._answer_finished(prepared))
                finally:
                    resume.set()
                    await asyncio.wait_for(task, 3)
            self.assertTrue(child.tools.settled(tool))
            before = len(f.events.history)
            await child._finish_answer()
            self.assertEqual(len(f.events.history), before)

        f.async_runner.run(finish_after_release())
        self.assertEqual(f.events.messages("cmd.run.request", fixture.CMD_CHANNEL), [])
        self.assertEqual(f.engine.journal.get(tool.data["tool_key"] + ":done").data["outcome"], "cancelled")


if __name__ == "__main__":
    unittest.main()
