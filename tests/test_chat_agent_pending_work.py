"""Completed history leaves the scheduler without losing recoverable work."""

import asyncio
import json
import unittest
from unittest.mock import patch

from openevent.chat_sdk import TextPart, TurnRef
from openevent.cmd_sdk import CmdOutputReadResultInput

from chat_model_agent.history import position
from chat_model_agent.session import Conversation
import test_chat_agent_session as fixture


class PendingWorkTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def records(self, kind):
        return self.f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind=kind)

    def respond(self, text="answer"):
        self.f.request_callback(lambda request, payload: self.f.end(
            request, payload, response_body=fixture.body(text)))

    def finish_input(self, text="question"):
        self.f.input(text)
        self.f.session._model_step()
        return self.records("answer.prepare")[-1]

    def live_wait(self):
        session = self.f.session._target
        session.wait = Conversation.wait.__get__(session, Conversation)

    def test_stop_during_scheduler_read_does_not_start_a_new_answer(self):
        f = self.f
        f.input("leave this input for restart")
        session = f.session._target
        pending = session._pending_answers

        def stop_after_read():
            result = pending()
            f.engine.request_stop()
            return result

        async def run():
            await session.run()
            await asyncio.gather(*f.engine.tasks, return_exceptions=True)

        with patch.object(session, "_pending_answers", side_effect=stop_after_read):
            f.async_runner.run(run())
        self.assertTrue(f.engine.stop.is_set())
        self.assertIsNone(f.engine.error)
        self.assertEqual(self.records("answer.prepare"), [])
        self.assertEqual(f.events.messages("infer.request", fixture.MODEL_CHANNEL), [])

    def test_live_scheduler_does_not_recheck_completed_history_for_each_new_input(self):
        f = self.f
        self.respond()
        old = {self.finish_input(f"history {index}").key for index in range(24)}
        self.live_wait()
        f.input("start live scheduling")
        with f.background_session() as until:
            until(lambda: len(f.events.messages("turn.end", fixture.CHAT_CHANNEL)) == 25
                  and not f.session.children)
            original = f.session._answer_finished

            def pending_only(answer):
                self.assertNotIn(answer.key, old, "scheduler revisited a completed historical answer")
                return original(answer)

            with patch.object(f.session._target, "_answer_finished", side_effect=pending_only):
                for index in range(3):
                    f.input(f"new input {index}")
                    until(lambda: len(f.events.messages("turn.end", fixture.CHAT_CHANNEL)) == 26 + index
                          and not f.session.children)
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), 28)

    def test_restart_recovers_old_unfinished_model_cancel_beside_newer_completed_answer(self):
        f = self.f
        f.input("cancel the first request")
        answer = f.session._next_answer()
        child = f.session._child(answer)
        requests = []

        def cancel_then_crash(message, payload):
            if payload.get("kind") == "infer.request":
                requests.append(message)
                f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, answer.data["turn_id"]),
                                   start_seq=child._turn().creation_seq)
            elif payload.get("kind") == "answer.cancel":
                raise fixture.Crash()

        f.events.on_publish = cancel_then_crash
        with self.assertRaises(fixture.Crash):
            f.async_runner.run(child._run_answer(answer))
        self.assertFalse(f.events.messages("infer.cancel", fixture.MODEL_CHANNEL))
        self.respond("newer answer completed")
        newer = self.finish_input("a later question")
        self.assertTrue(f.session._answer_finished(newer))
        self.assertFalse(f.session._answer_finished(answer))
        f.events.on_publish = None
        f.restart()
        self.live_wait()
        with f.background_session() as until:
            until(lambda: bool(f.events.messages("infer.cancel", fixture.MODEL_CHANNEL))
                  and not f.session.children)
        cancelled, = f.events.messages("infer.cancel", fixture.MODEL_CHANNEL)
        self.assertEqual(json.loads(cancelled.payload)["request_seq"], requests[0].seq)
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), 2)
        self.assertTrue(f.session._answer_finished(answer))
        self.assertTrue(f.session._answer_finished(newer))

    def test_restart_keeps_old_timed_out_read_until_late_source_result_is_cleaned_up(self):
        self.check_late_read_cleanup(cancelled=False)

    def test_restart_releases_cancelled_read_after_late_result_without_another_close(self):
        self.check_late_read_cleanup(cancelled=True)

    def check_late_read_cleanup(self, *, cancelled):
        f = self.f
        output = fixture.body("reading output")
        output["choices"][0]["finish_reason"] = "tool_calls"
        output["choices"][0]["message"]["tool_calls"] = [{
            "id": "read", "type": "function", "function": {
                "name": "read_stdout", "arguments": '{"exec_id":100,"nbytes":2}'}}]
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=output))
        answer = self.finish_input("read the existing command output")
        child = f.session._child(answer)
        tool, = self.records("tool.prepare")
        tool = f.engine.journal.materialize(tool)
        params = {**child.tools._params(tool.data), "nbytes": 2}
        request = f.async_runner.run(child.tools._request(tool, 0, params, lambda: False))
        f.events.now += f.config.agent.cmd_result_timeout_ms + 1
        f.async_runner.run(child.tools._timeout(tool, request))
        done = f.async_runner.run(child.tools._done(tool, "timeout", request_seq=request))
        if cancelled:
            f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, answer.data["turn_id"]),
                               start_seq=child._turn().creation_seq)
            f.async_runner.run(child._cancel_answer())
        self.respond("newer answer completed")
        newer = self.finish_input("continue while the old read is being cleaned up")
        self.assertTrue(f.session._answer_finished(newer))
        self.assertFalse(f.session._answer_finished(answer))
        f.events.on_publish = None
        f.restart()
        self.live_wait()
        with f.background_session() as until:
            until(lambda: answer.key in f.session.children)
            self.assertFalse(f.session._answer_finished(answer))
            self.assertEqual(len(f.events.messages("cmd.output.read.request", fixture.CMD_CHANNEL)), 1)
            f.cmd.publish_output_read_result(fixture.CMD, "worker-token", fixture.CMD_CHANNEL,
                fixture.AGENT, CmdOutputReadResultInput(prev_seq=request, status="SUCCESS", content="ok",
                    bytes=2, eof=True, content_encoding="utf-8", target_seq=params["target_seq"],
                    stream=params["stream"], offset=params["offset"], nbytes=2))
            until(lambda: answer.key not in f.session.children
                  and f.session._turn(answer).status == ("cancelled" if cancelled else "completed"))
        self.assertEqual(f.engine.journal.get(done.key).data["outcome"], "timeout")
        self.assertEqual(len(self.records("tool.done")), 1)
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), 2)
        self.assertEqual(len(f.events.messages("cmd.output.read.request", fixture.CMD_CHANNEL)), 1)

    def test_compacted_body_caches_are_evicted_but_frozen_request_and_explicit_quote_rebuild(self):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        text = "x" * (f.session.budget.target - 600)
        self.respond(text)
        answer = self.finish_input(text)
        user_seq = answer.data["reply_to_seqs"][0]
        accepted, = self.records("attempt.accept")
        original_request, = f.events.messages("infer.request", fixture.MODEL_CHANNEL)
        original_body = json.loads(original_request.payload)["body"]
        original_snapshot = f.session.index.snapshot()
        original_refs = [(item.ref, item.event) for item in original_snapshot.events]
        original_turn = f.session._turn(answer)
        self.respond("summary")
        following = self.finish_input("continue after compressing old history")
        snapshot = f.session.index.snapshot()
        _, covered = f.session.index.summary(snapshot.summary)
        self.assertGreaterEqual(position(covered), (accepted.data["terminal_seq"], 0))
        self.assertNotIn(user_seq, list(f.session.index.users))
        self.assertNotIn(accepted.key, list(f.session.assistants))
        self.assertTrue(f.session._answer_finished(following))

        # Eviction removes derived bodies only; the original immutable sources
        # must still reconstruct frozen requests and an explicit later quote.
        self.assertEqual([f.session.index.render(ref) for ref, _ in original_refs],
                         [event for _, event in original_refs])
        self.assertEqual(f.session._child(answer)._model_body(), original_body)
        quoted = f.user.single_turn(turn_id="quote-compacted-reply", reply_to_seqs=[original_turn.terminal_seq],
                                   content=(TextPart("quote the old answer"),))
        self.assertEqual(f.session.index.user(quoted)["reply_context"][0]["text"], text)
        f.session.index.snapshot()
        self.assertNotIn(user_seq, list(f.session.index.users))
        self.assertNotIn(accepted.key, list(f.session.assistants))


if __name__ == "__main__":
    unittest.main()
