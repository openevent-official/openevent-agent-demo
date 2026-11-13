"""Completed workflows release disposable bodies without losing recovery facts."""
import asyncio
import unittest
from unittest.mock import patch

from async_helpers import SyncProxy
import test_chat_agent_session as fixture
import test_chat_agent_v3_flow as flow


class CompletedCacheTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_completed_answers_release_children_and_are_not_recreated_by_the_scheduler(self):
        f = self.f
        f.request_callback(lambda request, payload: f.end(request, payload,
            response_body=fixture.body("answer " + "x" * 4096)))
        previous = []
        for index in range(20):
            f.input(f"question {index}")
            prepared = f.session._next_answer()
            child = SyncProxy(f.session._child(prepared), f.async_runner)
            previous.append(prepared.key)
            child._run_answer(prepared)
            self.assertTrue(f.session._answer_finished(prepared))
            self.assertIsNone(child.model_body)
            self.assertIsNone(child.model_response)
            self.assertEqual(f.session.children, {})
        original_child = f.session._child
        def pending_only(prepared):
            self.assertNotIn(prepared.key, previous)
            return original_child(prepared)
        f.input("one more question")
        with patch.object(f.session._target, "_child", side_effect=pending_only), f.background_session() as until:
            until(lambda: sum(f.session._answer_finished(answer) for answer in
                f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="answer.prepare")) == 21)
        f.events.on_publish = None
        f.restart()
        snapshot = f.session.index.snapshot()
        self.assertEqual(sum(item.ref["type"] == "assistant" for item in snapshot.events), 21)
        self.assertIsNone(f.session._next_answer())
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), 21)

    def test_tool_timeout_keeps_execution_but_not_request_body_until_cleanup_finishes(self):
        f = self.f
        f.input("run command")
        output = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "run", "type": "function",
                "function": {"name": "exec", "arguments": '{"command":"sleep 10"}'}}]}}]}
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=output))
        f.session._model_step()
        answer, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="answer.prepare")
        tool, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")
        child = SyncProxy(f.session.children[answer.key], f.async_runner)
        self.assertIsNone(child.model_body)
        self.assertIsNone(child.model_body_key)
        self.assertIsNone(child.model_response)
        def timeout():
            f.events.now += f.config.agent.cmd_result_timeout_ms + 1
        def finish_cleanup():
            done = f.engine.journal.get(tool.data["tool_key"] + ":done")
            self.assertEqual(done.data["outcome"], "timeout")
            self.assertIn(answer.key, f.session.children)
            self.assertIsNone(child.model_body)
            self.assertIsNone(child.model_response)
            self.assertFalse(f.session._answer_finished(answer))
            request, = f.events.messages("cmd.run.request", fixture.CMD_CHANNEL)
            f.cmd.publish_run_result(fixture.CMD, "worker-token", fixture.CMD_CHANNEL, fixture.AGENT,
                fixture.CmdRunResultInput(prev_seq=request.seq, status="SUCCESS", timeout_ms=1000,
                    exit_code=0, stdout=fixture.CmdOutput("stdout", 0, "utf-8", ""),
                    stderr=fixture.CmdOutput("stderr", 0, "utf-8", "")))
        f.waiting.extend((timeout, finish_cleanup))
        f.session._tool_step(tool)
        child._finish_answer()
        self.assertTrue(f.session._answer_finished(answer))
        self.assertNotIn(answer.key, f.session.children)
        self.assertIsNone(child.model_body)
        self.assertIsNone(child.model_response)

    def test_finally_failed_compaction_releases_its_body_and_recovers_without_retry(self):
        f = flow.EventFlowTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        text = f.seed_large_history()
        f.input(text)
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=fixture.body(" ")))
        f.session._model_step()
        compression, = f.records("compaction.prepare")
        answer = f.records("answer.prepare")[-1]
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":freeze"))
        self.assertNotIn(compression.key, f.session.children)
        self.assertNotIn(answer.key, f.session.children)
        count = len(f.requests())
        f.events.on_publish = None
        f.restart()
        self.assertIsNone(f.session._next_answer())
        self.assertEqual(len(f.requests()), count)
        self.assertEqual(len(f.records("compaction.prepare")), 1)

    def test_tool_coroutine_can_finish_after_the_coordinator_releases_its_child(self):
        f = self.f
        f.input("run command")
        output = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "run", "type": "function",
                "function": {"name": "exec", "arguments": '{"command":"true"}'}}]}}]}
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=output))
        f.session._model_step()
        answer, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="answer.prepare")
        tool, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")
        child = f.session.children[answer.key]
        def command(request, payload):
            if payload.get("kind") == "cmd.run.request":
                f.cmd.publish_run_result(fixture.CMD, "worker-token", fixture.CMD_CHANNEL, fixture.AGENT,
                    fixture.CmdRunResultInput(prev_seq=request.seq, status="SUCCESS", timeout_ms=1000,
                        exit_code=0, stdout=fixture.CmdOutput("stdout", 0, "utf-8", ""),
                        stderr=fixture.CmdOutput("stderr", 0, "utf-8", "")))
        f.events.on_publish = command
        async def run():
            committed, resume = asyncio.Event(), asyncio.Event()
            original = child.put
            async def delayed_put(kind, *args, **kwargs):
                result = await original(kind, *args, **kwargs)
                if kind == "tool.done":
                    committed.set()
                    await resume.wait()
                return result
            with patch.object(child, "put", side_effect=delayed_put):
                task = asyncio.create_task(f.session._target._tool_step(tool))
                try:
                    await asyncio.wait_for(committed.wait(), 3)
                    await child._finish_answer()
                    self.assertNotIn(answer.key, f.session.children)
                    self.assertIsNone(child.model_response)
                finally:
                    resume.set()
                await asyncio.wait_for(task, 3)
            self.assertTrue(child.tools.settled(tool))
        f.async_runner.run(run())


if __name__ == "__main__":
    unittest.main()
