"""Compacted snapshots omit old bodies while retaining their source facts."""

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from openevent.cmd_sdk import CmdOutputReadResultInput

from chat_model_agent.budget import event_size
from chat_model_agent.history import position
from chat_model_agent.state import HistoryError
import test_chat_agent_session as fixture


class CompactedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def records(self, kind):
        return self.f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind=kind)

    def seed_large_answer(self, *, attachment=False, pending_read=False):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        f.engine.objects.batch_metadata_reader = lambda keys: [{
            "name": "source.txt", "type": "text/plain", "nbytes": 7} for _ in keys]
        text = "x" * (f.session.budget.target - 600)
        keys = [fixture.ObjectKey(object_id=42, object_token="source-token")] if attachment else []
        f.input(text, object_keys=keys)
        output = fixture.body(text)
        if pending_read:
            output["choices"][0]["finish_reason"] = "tool_calls"
            output["choices"][0]["message"]["tool_calls"] = [{
                "id": "read", "type": "function", "function": {
                    "name": "read_stdout", "arguments": '{"exec_id":100,"nbytes":2}'}}]
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=output))
        return f.run_answer()

    def compact_for_next_input(self):
        f = self.f
        f.input("next question")
        before = f.session.index.snapshot()
        f.request_callback(lambda request, payload: f.end(
            request, payload, response_body=fixture.body("remember old facts")))
        following = f.session._next_answer()
        self.assertIsNotNone(following.data["summary"])
        return before, following

    def emit_read_result(self, tool):
        f = self.f
        tool = f.engine.journal.materialize(tool)
        child = f.session._child(f.engine.journal.get(tool.data["answer"]))
        params = {**child.tools._params(tool.data), "nbytes": 2}
        request = f.async_runner.run(child.tools._request(tool, 0, params, lambda: False))
        return f.cmd.publish_output_read_result(
            fixture.CMD, "worker-token", fixture.CMD_CHANNEL, fixture.AGENT,
            CmdOutputReadResultInput(prev_seq=request, status="SUCCESS", content="ok", bytes=2,
                eof=True, content_encoding="utf-8", target_seq=params["target_seq"],
                stream=params["stream"], offset=params["offset"], nbytes=2))

    def test_summary_cut_in_one_response_keeps_later_tool_indexes_without_rendering_covered_bodies(self):
        f = self.f
        f.config = replace(f.config, openevent=replace(f.config.openevent, max_payload_bytes=32768))
        f.restart(context_max_input_bytes=8000, context_compression_prompt="c" * 3500)
        command = "x" * (f.session.budget.target - 250)
        output = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": f"run-{index}", "type": "function", "function": {
                    "name": "exec", "arguments": json.dumps({"command": command})}}
                for index in range(4)]}}]}
        f.input("do work")
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=output))
        f.run_answer()
        accepted, = self.records("attempt.accept")
        f.input("next question")
        before = f.session.index.snapshot()

        def commit_first_batch(request, payload):
            if request.channel_id == fixture.MODEL_CHANNEL and payload["kind"] == "infer.request":
                f.end(request, payload, response_body=fixture.body("summary"))
            elif request.channel_id == fixture.WAL_CHANNEL and payload["kind"] == "compaction.commit":
                raise fixture.Crash()

        f.events.on_publish = commit_first_batch
        with self.assertRaises(fixture.Crash):
            f.session._next_answer()
        f.events.on_publish = None
        batch, = self.records("compaction.prepare")
        through = batch.data["range"]["through"]
        self.assertEqual(through["seq"], accepted.data["terminal_seq"])
        self.assertGreaterEqual(through["index"], 1)
        self.assertLess(through["index"], 4)
        covered = [item.ref for item in before.all_events if position(item.cursor) <= position(through)]
        covered.append({"type": "assistant", "record": accepted.key})
        expected = tuple(item for item in before.all_events if position(item.cursor) > position(through))
        f.restart()
        index = f.session.index
        render, user = index.render, index.user

        def render_uncovered(ref):
            self.assertNotIn(ref, covered, "snapshot rebuilt a summarized event body")
            return render(ref)

        def user_uncovered(seq):
            self.assertGreater(seq, through["seq"], "snapshot rebuilt a summarized user body")
            return user(seq)

        with patch.object(index, "render", side_effect=render_uncovered), \
                patch.object(index, "user", side_effect=user_uncovered):
            snapshot = index.snapshot()
        self.assertEqual(snapshot.events, expected)
        self.assertEqual(snapshot.all_events, expected)
        self.assertEqual(snapshot.events[0].cursor, {
            "seq": through["seq"], "index": through["index"] + 1})

    def test_old_refs_frozen_request_and_attachment_authority_survive_summary_and_restart(self):
        f = self.f
        old = self.seed_large_answer(attachment=True)
        original_body = f.session._model_body()
        original_refs = [(item.ref, item.event) for item in f.session.index.snapshot().events]
        _, following = self.compact_for_next_input()
        f.events.on_publish = None
        f.restart()
        f.engine.objects.batch_metadata_reader = lambda keys: [{
            "name": "source.txt", "type": "text/plain", "nbytes": 7} for _ in keys]
        index = f.session.index
        snapshot = index.snapshot()
        _, through = index.summary(snapshot.summary)
        self.assertTrue(all(position(item.cursor) > position(through) for item in snapshot.all_events))
        self.assertEqual([index.render(ref) for ref, _ in original_refs],
                         [event for _, event in original_refs])
        self.assertEqual(f.session._child(old)._model_body(), original_body)
        self.assertEqual(index.object_ids(following, {42}), frozenset({42}))
        self.assertEqual(f.session._next_answer(), following)
        f.request_callback(lambda request, payload: f.end(
            request, payload, response_body=fixture.body("recovered next answer")))
        f.session._model_step()
        self.assertTrue(f.session._answer_finished(following))

    def test_summarized_pending_tool_keeps_result_barrier_and_later_result_event(self):
        f = self.f
        old = self.seed_large_answer(pending_read=True)
        tool, = self.records("tool.prepare")
        _, following = self.compact_for_next_input()
        _, through = f.session.index.summary(following.data["summary"])
        accepted = f.engine.journal.get(tool.data["tool_key"].rsplit(":tool:", 1)[0] + ":accept")
        self.assertGreaterEqual(position(through), (accepted.data["terminal_seq"], 1))
        f.events.on_publish = None
        f.restart()
        result_seq = self.emit_read_result(tool)
        later = f.input("new body behind the unconfirmed result")
        snapshot = f.session.index.snapshot()
        self.assertEqual(snapshot.processed, result_seq - 1)
        self.assertNotIn(later, [item.event.get("input_id") for item in snapshot.events])
        self.assertIn(later, [item.event.get("input_id") for item in snapshot.all_events])
        self.assertNotIn({"type": "tool_call", "record": tool.key}, [item.ref for item in snapshot.all_events])
        f.session._tool_step(tool)
        snapshot = f.session.index.snapshot()
        result, = [item for item in snapshot.events if item.ref["type"] == "tool_result"]
        self.assertEqual(result.cursor, {"seq": result_seq, "index": 0})
        self.assertEqual(result.event["task_id"], tool.data["task_id"])
        self.assertTrue(result.trigger)
        self.assertEqual(result.targets, tuple(old.data["reply_to_seqs"]))
        self.assertLess(snapshot.events.index(result), next(
            index for index, item in enumerate(snapshot.events) if item.event.get("input_id") == later))
        self.assertEqual(len(f.events.messages("cmd.output.read.request", fixture.CMD_CHANNEL)), 1)

    def test_recovery_budget_checks_required_assistant_behind_old_tool_result_barrier(self):
        f = self.f
        self.seed_large_answer(pending_read=True)
        tool, = self.records("tool.prepare")
        _, following = self.compact_for_next_input()
        result_seq = self.emit_read_result(tool)
        # This answer was already admitted before the older tool's result
        # arrived, so its independent model stage may finish behind the barrier.
        f.request_callback(lambda request, payload: f.end(request, payload,
            response_body=fixture.body("y" * (f.session.budget.target - 300))))
        f.async_runner.run(f.session._child(following)._run_answer(following))
        self.assertTrue(f.session._answer_finished(following))
        accepted, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL,
            kind="attempt.accept", prefix=following.key + ":")
        ref = {"type": "assistant", "record": accepted.key}
        snapshot = f.session.index.snapshot()
        self.assertEqual(snapshot.processed, result_seq - 1)
        self.assertNotIn(ref, [item.ref for item in snapshot.events])
        required, = [item for item in snapshot.all_events if item.ref == ref]
        self.assertGreater(required.cursor["seq"], result_seq)
        target = event_size(required.event) - 1
        reduced_limit = f.session.budget.fixed + 2 * target
        f.events.on_publish = None
        f.restart(context_max_input_bytes=reduced_limit)
        self.assertLess(f.session.budget.target, event_size(required.event))
        with self.assertRaisesRegex(HistoryError, "required history event"):
            f.session._check_recovery_budget()

    def test_committed_summary_does_not_change_frozen_snapshot_bodies(self):
        f = self.f
        self.seed_large_answer()
        original = f.session.index.snapshot()
        bodies = [item.event for item in original.events]
        before, following = self.compact_for_next_input()
        commit = f.engine.journal.get(following.data["summary"])
        self.assertGreater(commit.seq, before.read)
        self.assertIsNone(before.summary)
        self.assertEqual([item.event for item in original.events], bodies)
        self.assertEqual([item.event for item in before.events[:-1]], bodies)
        self.assertEqual(before.events[-1].event["text"], "next question")
        self.assertLess(len(f.session.index.snapshot().all_events), len(before.all_events))


if __name__ == "__main__":
    unittest.main()
