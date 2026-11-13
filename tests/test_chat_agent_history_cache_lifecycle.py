"""Disposable history caches do not keep rejected or summarized bodies alive."""

import unittest
from unittest.mock import patch

from async_helpers import SyncProxy
from chat_model_agent.history import position
import test_chat_agent_session as fixture


class HistoryCacheLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_rejected_reply_context_is_evicted_and_frozen_snapshot_reloads(self):
        f = self.f
        f.fresh(context_max_input_bytes=5000)
        reference = f.session.chat.start_turn(turn_id="long-reference",
            content=(fixture.TextPart("first " + "x" * f.session.budget.target),))
        reference.append(content=(fixture.TextPart("last"),))
        terminal = reference.complete()
        index = f.session.index
        for number in range(3):
            source = f.user.single_turn(turn_id=f"rejected-{number}",
                content=(fixture.TextPart("explain"),), reply_to_seqs=[terminal])
            before = index.snapshot()
            old = next(item.event for item in before.events if item.event.get("input_id") == source)
            self.assertIn(source, index.users)
            f.session._reject_inputs()
            self.assertNotIn(source, index.users, "rejection must release the admission body immediately")
            self.assertEqual(index.snapshot().events, ())
            self.assertEqual(index.users, {})
            self.assertEqual(index.user(source), old, "original reply context must remain reconstructible")
            self.assertNotIn(source, index.users, "historical reads must not re-cache rejected bodies")
            self.assertEqual([item.event for item in before.events], [old])
            self.assertEqual(index.users, {})
            self.assertEqual(index.snapshot().events, ())
        f.restart()
        self.assertEqual(f.session.index.snapshot().events, ())
        later = f.input("short accepted message")
        snapshot = f.session.index.snapshot()
        self.assertEqual([event.event["input_id"] for event in snapshot.events], [later])

    def test_rejection_during_render_cannot_repopulate_the_cache(self):
        f = self.f
        source = f.input("original")
        index = f.session.index
        original = f.session.chat.get_turn_content

        def reject_after_cache_miss(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            read = f.engine.scanned_through
            f.session.put("input.reject", f"chat:{fixture.CHAT_CHANNEL}:input:{source}:reject", {
                "input_seq": source, "reason": "message_too_long", "turn_id": "rejected",
                "snapshot": {"read_through_seq": read, "processed_through_seq": read}})
            index.reject_user(source)
            return snapshot

        with patch.object(f.session.chat, "get_turn_content", side_effect=reject_after_cache_miss):
            event = index.user(source)
        self.assertEqual(event["text"], "original")
        self.assertNotIn(source, index.users)

    def test_completed_tool_counts_expire_with_summary_and_compactions_are_not_cached(self):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        f.request_callback(lambda request, payload: f.end(request, payload,
            response_body=fixture.body("summary" if "tools" not in payload["body"] else "x" * 1000)))
        for number in range(12):
            f.input(f"question {number} " + "q" * 800)
            f.session._model_step()
        index = f.session.index
        self.assertIsNotNone(index.snapshot().summary)
        accepted = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="attempt.accept")
        covered = []
        for record in accepted:
            purpose = f.engine.journal.get(record.data["attempt"]).data["purpose"]
            if purpose == "compaction":
                self.assertNotIn(record.key, index.tool_counts)
            elif position({"seq": record.data["terminal_seq"], "index": 0}) <= index.covered:
                covered.append(record)
                self.assertNotIn(record.key, index.tool_counts)
        self.assertTrue(covered)
        for record in covered:
            self.assertEqual(index.tool_count(record), 0)
            self.assertNotIn(record.key, index.tool_counts, "old reads must not repopulate counts")

    def test_summarized_pending_answer_keeps_count_until_tool_cleanup(self):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        text = "x" * (f.session.budget.target - 600)
        f.input(text)
        output = fixture.body(text)
        output["choices"][0]["finish_reason"] = "tool_calls"
        output["choices"][0]["message"]["tool_calls"] = [{"id": "run", "type": "function",
            "function": {"name": "exec", "arguments": '{"command":"sleep 10"}'}}]
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=output))
        f.session._model_step()
        answer = f.engine.journal.latest(channel=fixture.CHAT_CHANNEL, kind="answer.prepare")
        accepted = f.engine.journal.latest(channel=fixture.CHAT_CHANNEL, kind="attempt.accept")
        tool = f.engine.journal.latest(channel=fixture.CHAT_CHANNEL, kind="tool.prepare")
        child = SyncProxy(f.session.children[answer.key], f.async_runner)
        f.input("next question")
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=fixture.body("short")))
        f.session._model_step()
        index = f.session.index
        index.snapshot()
        self.assertLessEqual((accepted.data["terminal_seq"], 0), index.covered)
        self.assertEqual(index.tool_counts[accepted.key], 1)
        with patch.object(index.session, "_accepted_assistant", side_effect=AssertionError("old body replayed")):
            self.assertEqual(f.session._answer_tools(answer), [tool])

        def finish_command(request, payload):
            if payload.get("kind") == "cmd.run.request":
                f.cmd.publish_run_result(fixture.CMD, "worker", fixture.CMD_CHANNEL, fixture.AGENT,
                    fixture.CmdRunResultInput(prev_seq=request.seq, status="SUCCESS", timeout_ms=1000,
                        exit_code=0, stdout=fixture.CmdOutput("stdout", 0, "utf-8", ""),
                        stderr=fixture.CmdOutput("stderr", 0, "utf-8", "")))

        f.events.on_publish = finish_command
        f.session._tool_step(tool)
        child._finish_answer()
        self.assertNotIn(answer.key, f.session.children)
        index.snapshot()
        self.assertNotIn(accepted.key, index.tool_counts)


if __name__ == "__main__":
    unittest.main()
