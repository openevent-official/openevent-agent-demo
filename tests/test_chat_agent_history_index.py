"""History order follows immutable sources and independent summary cursors."""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openevent.cmd_sdk import CmdOutput, CmdRunResultInput

import test_chat_agent_session as sessions
from chat_model_agent.history import position
from chat_model_agent.state import ChatProjection, ModelView
from chat_model_agent.wal import Journal, encode


class HistoryIndexTests(unittest.TestCase):
    def setUp(self):
        self.f = sessions.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_backlog_and_recovered_inputs_do_not_replay_chat_prefixes(self):
        f = self.f
        for restarted in (False, True):
            for count in (60, 120):
                with self.subTest(restarted=restarted, inputs=count):
                    f.fresh()
                    inputs = [f.input(f"question {number}") for number in range(count)]
                    if restarted:
                        f.restart()
                    # Recovery/ingestion already built the projection. Rendering
                    # N inputs must not replay the first 1, 2, ..., N events again.
                    with patch.object(ChatProjection, "apply", side_effect=AssertionError("Chat history replayed")):
                        snapshot = f.session.index.snapshot()
                    self.assertEqual([item.event["input_id"] for item in snapshot.events], inputs)
                    self.assertEqual([item.event["text"] for item in snapshot.events],
                                     [f"question {number}" for number in range(count)])

    def test_decided_model_streams_are_not_replayed_after_history_is_rebuilt(self):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        ordinary_requests = []
        def respond(request, payload):
            compacting = "tools" not in payload["body"]
            if not compacting:
                ordinary_requests.append(request.seq)
            previous = f.head(request, payload)
            fragments = ["summary"] if compacting else ["x" * 80] * 16
            for index, text in enumerate(fragments):
                previous = f.append(request, payload, previous, sessions.chunk(text, done=index == len(fragments) - 1))
            f.end(request, payload, failure=not compacting and len(ordinary_requests) == 1)
        f.request_callback(respond)
        for number in range(8):
            f.input(f"question {number}")
            f.session._model_step()
        self.assertTrue(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail"))
        self.assertTrue(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="compaction.commit"))
        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                if restarted:
                    f.restart()
                # Cold recovery still reconstructs accepted text from its
                # sources once, using the existing assistant cache.
                expected = f.session.index.snapshot()
                f.session.index.summary(expected.summary)
                old = {event.seq for event in f.events.history if event.channel_id == sessions.MODEL_CHANNEL}
                apply = ModelView.apply
                def only_new_streams(view, event, *args):
                    self.assertNotIn(event.seq, old, "a decided model stream was replayed")
                    return apply(view, event, *args)
                with patch.object(ModelView, "apply", only_new_streams):
                    for _ in range(3):
                        self.assertEqual(f.session.index.snapshot(), expected)
                    later = f.input("next question")
                    following = f.session._next_answer()
                self.assertEqual(following.data["reply_to_seqs"], [later])
                f.session._model_step()

    def test_completed_wal_and_inputs_are_not_rescanned_between_snapshots(self):
        f = self.f
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=sessions.body("done")))
        for number in range(8):
            f.input(f"question {number}")
            f.session._model_step()
        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                if restarted:
                    f.restart()
                index = f.session.index
                expected = index.snapshot()
                select, pending_inputs = f.engine.journal.select, f.engine.pending_inputs

                def only_new_wal(**kwargs):
                    self.assertEqual(kwargs["channel"], sessions.CHAT_CHANNEL)
                    self.assertGreaterEqual(kwargs.get("after", 0), expected.read)
                    records = select(**kwargs)
                    self.assertTrue(all(record.seq > expected.read for record in records))
                    return records

                def only_new_inputs(channel, **kwargs):
                    self.assertGreaterEqual(kwargs.get("after", 0), expected.read)
                    return pending_inputs(channel, **kwargs)

                with patch.object(f.engine.journal, "select", side_effect=only_new_wal), \
                        patch.object(f.engine, "pending_inputs", side_effect=only_new_inputs), \
                        patch.object(index.session, "_turn", side_effect=AssertionError("completed answer rescanned")), \
                        patch.object(index, "tool_count", side_effect=AssertionError("completed attempt rescanned")):
                    for _ in range(3):
                        self.assertEqual(index.snapshot(), expected)
                    later = f.input("new question")
                    following = index.snapshot()
                self.assertEqual(following.consumed, expected.consumed)
                self.assertEqual(following.events[-1].event["input_id"], later)

    def test_frozen_answer_without_chat_turn_does_not_remain_pending(self):
        f = self.f
        f.input()
        answer = f.session._next_answer()
        with patch("chat_model_agent.session.body_size", return_value=1 << 40):
            f.session._run_answer(answer)
        self.assertIsNone(f.session._turn(answer))
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":freeze"))
        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                if restarted:
                    f.restart()
                index = f.session.index
                with patch.object(index.session, "_turn", side_effect=AssertionError("frozen answer rescanned")):
                    snapshot = index.snapshot()
                    self.assertEqual(index.snapshot(), snapshot)
                self.assertEqual(snapshot.processed, snapshot.read)

    def test_new_rejection_and_newer_snapshot_cannot_change_an_in_progress_snapshot(self):
        f = self.f
        source = f.input()
        index = f.session.index
        read, user = f.engine.scanned_through, index.user
        newer = []

        def reject_while_rendering(seq):
            if not newer:
                rejected = f.session.put("input.reject", f"chat:{sessions.CHAT_CHANNEL}:input:{source}:reject", {
                    "input_seq": source, "reason": "message_too_long", "turn_id": "rejected",
                    "snapshot": {"read_through_seq": read, "processed_through_seq": read}})
                self.assertGreater(rejected.seq, read)
                # The newer snapshot prunes the shared reference index while
                # the older one still renders its already-frozen input list.
                newer.append(index.snapshot())
            return user(seq)

        with patch.object(index, "user", side_effect=reject_while_rendering):
            snapshot = index.snapshot()
            # Frozen snapshots hold references; payloads are loaded by consumers.
            self.assertEqual([item.event["input_id"] for item in snapshot.events], [source])
        self.assertEqual(snapshot.read, read)
        self.assertEqual([item.event["input_id"] for item in snapshot.events], [source])
        self.assertEqual(newer[0].events, ())
        self.assertEqual(index.snapshot(), newer[0])
        f.restart()
        self.assertEqual(f.session.index.snapshot(), newer[0])

    def test_user_references_keep_terminal_content_after_later_activity_and_restart(self):
        f = self.f
        for error_end in (False, True):
            with self.subTest(error_end=error_end):
                f.fresh()
                ancestor = f.input("ancestor is not a direct reference")
                reference = f.session.chat.start_turn(turn_id="reference", reply_to_seqs=[ancestor],
                    content=(sessions.TextPart("draft"),),
                    object_keys=[sessions.ObjectKey(object_id=1, object_token="old-object")])
                reset_seq = reference.reset(content=(sessions.TextPart("final referenced content"),),
                    object_keys=[sessions.ObjectKey(object_id=2, object_token="final-object")])
                if error_end:
                    terminal = f.events.publish_auto_seq(principal=sessions.AGENT,
                        token=f.config.agent.token, channel_id=sessions.CHAT_CHANNEL, uuid=f.events.get_uuid(),
                        payload=json.dumps({"kind": "turn.end", "turn_id": "reference",
                            "start_seq": reference.start_seq, "pre_seq": reset_seq,
                            "error": "turn_too_long"}).encode()).seq
                else:
                    terminal = reference.complete()
                source = f.user.single_turn(turn_id="with-reference", content=(sessions.TextPart("question"),),
                    reply_to_seqs=[terminal])
                def metadata(keys):
                    return [{"name": f"object-{key['object_id']}", "type": "text/plain",
                             "nbytes": key["object_id"]} for key in keys]
                f.engine.objects.batch_metadata_reader = metadata
                expected = f.session.index.user(source)
                self.assertEqual(expected["reply_context"], [{"role": "assistant", "status": "completed",
                    "text": "" if error_end else "final referenced content",
                    "attachments": [] if error_end else [
                        {"name": "object-2", "type": "text/plain", "nbytes": 2, "object_id": 2}]}])
                later = f.session.chat.start_turn(turn_id="later", content=(sessions.TextPart("unrelated"),))
                later.reset(content=(sessions.TextPart("later replacement must not enter the old input"),))
                f.input("later user input must not enter the old event")
                for restarted in (False, True):
                    with self.subTest(restarted=restarted):
                        if restarted:
                            f.restart()
                        f.session.index.users.clear()
                        metadata_reads = []
                        def read_metadata(keys):
                            metadata_reads.extend(key["object_id"] for key in keys)
                            return metadata(keys)
                        f.engine.objects.batch_metadata_reader = read_metadata
                        read = f.session.chat.get_turn_content
                        def selected_content(seq):
                            self.assertFalse(f.engine.condition._is_owned(), "SDK read held the reader lock")
                            self.assertIn(seq, {source, terminal}, "unrelated Chat turn was loaded")
                            return read(seq)
                        with patch.object(f.session.chat, "get_turn_content", side_effect=selected_content) as loaded:
                            rebuilt = f.session.index.user(source)
                        self.assertEqual([call.args[0] for call in loaded.call_args_list], [source, terminal])
                        self.assertEqual(rebuilt, expected)
                        self.assertTrue(set(metadata_reads) <= (set() if error_end else {2}))

    def tools_response(self, count=2, *, content="starting", command="pwd"):
        return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": content, "tool_calls": [
                {"id": f"provider-{i}", "type": "function", "function": {
                    "name": "exec", "arguments": json.dumps({"command": command})}}
                for i in range(count)]}}]}

    def accept_tools(self, count=2, **kwargs):
        self.f.input("do work")
        response = self.tools_response(count, **kwargs)
        self.f.request_callback(lambda request, payload: self.f.end(request, payload, response_body=response))
        answer = self.f.run_answer()
        self.f.events.on_publish = None
        return answer, self.f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="tool.prepare")

    def emit_run(self, request_seq):
        return self.f.cmd.publish_run_result(sessions.CMD, "worker", sessions.CMD_CHANNEL, sessions.AGENT,
            CmdRunResultInput(prev_seq=request_seq, status="SUCCESS", timeout_ms=1000,
                exit_code=0,
                stdout=CmdOutput("stdout", 2, "utf-8", "ok"), stderr=CmdOutput("stderr", 0, "utf-8", "")))

    def test_same_response_events_have_stable_source_indexes_and_no_receipts(self):
        f = self.f
        answer, tools = self.accept_tools()
        accepted, = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept")
        snapshot = f.session.index.snapshot()
        self.assertEqual([item.event["type"] for item in snapshot.events], ["user", "assistant", "tool_call", "tool_call"])
        self.assertEqual([item.cursor for item in snapshot.events[1:]], [
            {"seq": accepted.data["terminal_seq"], "index": index} for index in range(3)])
        self.assertEqual([item.event["task_id"] for item in snapshot.events[2:]], [tool.data["task_id"] for tool in tools])
        self.assertEqual([item.trigger for item in snapshot.events], [True, False, False, False])
        self.assertEqual(snapshot.consumed, answer.data["input_through"])
        self.assertNotIn("provider-", json.dumps([item.event for item in snapshot.events]))
        later = f.input("explain while the command runs")
        next_answer = f.session._next_answer()
        self.assertEqual(next_answer.data["reply_to_seqs"], [later])
        next_answer = f.engine.journal.materialize(next_answer)
        self.assertNotIn("tool_result", [ref["type"] for ref in next_answer.data["events"]])

    def test_unaccepted_model_terminal_blocks_later_inputs_until_verdict(self):
        f = self.f
        f.input("first")
        prepared = f.session._next_answer()
        f.session.answer = prepared
        attempt = f.session._new_attempt(0)
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=sessions.body("accepted later")))
        f.session._request(attempt)
        terminal = f.events.messages("infer.end", sessions.MODEL_CHANNEL)[-1]
        later = f.input("second")
        snapshot = f.session.index.snapshot()
        self.assertGreaterEqual(snapshot.read, later)
        self.assertEqual(snapshot.processed, terminal.seq - 1)
        self.assertNotIn(later, [item.event.get("input_id") for item in snapshot.events])
        f.session._run_answer(prepared)
        snapshot = f.session.index.snapshot()
        self.assertEqual([item.event["type"] for item in snapshot.events], ["user", "assistant", "user"])
        self.assertEqual(snapshot.events[1].cursor["seq"], terminal.seq)
        self.assertEqual(snapshot.events[-1].event["input_id"], later)

    def test_cancelled_model_allows_new_answer_before_and_after_restart(self):
        f = self.f
        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                f.fresh()
                f.input("first question")
                f.request_callback(lambda request, payload: f.user.cancel_turn(
                    target_turn=sessions.TurnRef(sessions.AGENT, f.session.answer.data["turn_id"]),
                    start_seq=f.session._turn().creation_seq))
                answer = f.run_answer()
                f.session._finish_answer()
                self.assertIsNotNone(f.engine.journal.get(answer.key + ":cancel"))
                self.assertTrue(f.session._answer_finished(answer))
                self.assertEqual(len(f.events.messages("infer.cancel", sessions.MODEL_CHANNEL)), 1)
                self.assertFalse(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept"))
                self.assertFalse(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail"))
                f.events.on_publish = None
                later = f.input("new question after cancellation")
                if restarted:
                    f.restart()
                snapshot = f.session.index.snapshot()
                self.assertEqual(snapshot.processed, snapshot.read)
                self.assertEqual([item.event["type"] for item in snapshot.events],
                                 ["user", "user_cancelled", "user"])
                f.request_callback(lambda request, payload: f.end(
                    request, payload, response_body=sessions.body("new answer")))
                following = f.run_answer()
                self.assertEqual(following.data["reply_to_seqs"], [later])
                self.assertTrue(f.session._answer_finished(following))

    def test_earlier_model_terminal_is_resolved_before_cancellation(self):
        f = self.f
        f.input("first")
        answer = f.session._next_answer()
        f.session.answer = answer
        f.session._chat_effect(answer.key + ":start", "turn.start", source={"type": "placeholder"})
        attempt = f.session._new_attempt(0)
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=sessions.body("earlier result")))
        f.session._request(attempt)
        terminal = f.events.messages("infer.end", sessions.MODEL_CHANNEL)[-1]
        cancel_seq = f.user.cancel_turn(target_turn=sessions.TurnRef(sessions.AGENT, answer.data["turn_id"]),
            start_seq=f.session._turn(answer).creation_seq)
        later = f.input("second")
        self.assertLess(terminal.seq, cancel_seq)
        self.assertEqual(f.session.index.snapshot().processed, terminal.seq - 1)
        f.session._run_answer(answer)
        f.session._cancel_answer()
        accepted = f.engine.journal.get(attempt.key + ":accept")
        cancellation = f.engine.journal.get(answer.key + ":cancel")
        self.assertEqual(accepted.data["terminal_seq"], terminal.seq)
        self.assertEqual(cancellation.data["cancel_seq"], cancel_seq)
        snapshot = f.session.index.snapshot()
        self.assertEqual(snapshot.processed, snapshot.read)
        self.assertEqual([item.event["type"] for item in snapshot.events], ["user", "assistant", "user_cancelled", "user"])
        self.assertEqual([item.cursor["seq"] for item in snapshot.events[1:3]], [terminal.seq, cancel_seq])
        self.assertEqual(snapshot.events[-1].event["input_id"], later)

    def test_accepted_response_waits_for_all_tool_preparations_at_its_source(self):
        f = self.f
        f.input("first")
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=self.tools_response()))
        prepared = f.session._next_answer()
        with patch.object(f.session.tools, "_prepare", side_effect=sessions.Crash):
            with self.assertRaises(sessions.Crash):
                f.session._run_answer(prepared)
        accepted, = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept")
        later = f.input("second")
        snapshot = f.session.index.snapshot()
        self.assertEqual(snapshot.processed, accepted.data["terminal_seq"] - 1)
        self.assertNotIn(later, [item.event.get("input_id") for item in snapshot.events])
        f.session._run_answer(prepared)
        snapshot = f.session.index.snapshot()
        self.assertEqual([item.event["type"] for item in snapshot.events], ["user", "assistant", "tool_call", "tool_call", "user"])

    def test_cancelled_accepted_response_still_waits_for_tool_preparations(self):
        f = self.f
        f.input("first")
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=self.tools_response()))
        answer = f.session._next_answer()
        with patch.object(f.session.tools, "_prepare", side_effect=sessions.Crash):
            with self.assertRaises(sessions.Crash):
                f.session._run_answer(answer)
        accepted, = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept")
        cancel_seq = f.user.cancel_turn(target_turn=sessions.TurnRef(sessions.AGENT, answer.data["turn_id"]),
            start_seq=f.session._turn(answer).creation_seq)
        f.session.put("answer.cancel", answer.key + ":cancel", {"cancel_seq": cancel_seq})
        later = f.input("second")
        self.assertEqual(f.session.index.snapshot().processed, accepted.data["terminal_seq"] - 1)
        f.session._cancel_answer()
        snapshot = f.session.index.snapshot()
        self.assertEqual(snapshot.processed, snapshot.read)
        self.assertEqual([item.ref["type"] for item in snapshot.events[:5]],
                         ["user", "assistant", "tool_call", "tool_call", "cancel"])
        self.assertIn(later, [item.event.get("input_id") for item in snapshot.events])
        done = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="tool.done")
        self.assertEqual([record.data["outcome"] for record in done], ["cancelled", "cancelled"])
        self.assertFalse(f.events.messages("run.request", sessions.CMD_CHANNEL))

    def test_complete_tool_result_holds_its_original_position_during_wal_delay(self):
        f = self.f
        answer, tools = self.accept_tools(count=1)
        tool = f.engine.journal.materialize(tools[0])
        runner = f.session.tools
        params = runner._params(tool.data)
        request = f.async_runner.run(runner._request(tool, 0, params, lambda: False))
        before_result = f.input("while running")
        self.assertEqual(f.session.index.snapshot().events[-1].event["input_id"], before_result)
        f.events.now += 20
        result_seq = self.emit_run(request)
        f.events.now += 20
        later = f.input("after the result")
        snapshot = f.session.index.snapshot()
        self.assertGreaterEqual(snapshot.read, later)
        self.assertEqual(snapshot.processed, result_seq - 1)
        self.assertNotIn(later, [item.event.get("input_id") for item in snapshot.events])
        done = f.async_runner.run(runner._done(tool, "exec", request_seq=request, result_seq=result_seq))
        self.assertGreater(done.seq, later)
        snapshot = f.session.index.snapshot()
        result = next(item for item in snapshot.events if item.ref["type"] == "tool_result")
        self.assertEqual(result.cursor, {"seq": result_seq, "index": 0})
        self.assertTrue(result.trigger)
        self.assertEqual(result.targets, tuple(answer.data["reply_to_seqs"]))
        self.assertLess(snapshot.events.index(result), next(i for i, item in enumerate(snapshot.events)
                                                         if item.event.get("input_id") == later))
        self.assertEqual(result.event["task_id"], tool.data["task_id"])

    def test_user_cancel_uses_source_time_and_targets_without_becoming_trigger(self):
        f = self.f
        answer, tools = self.accept_tools(count=1)
        f.events.now += 100
        cancel_seq = f.user.cancel_turn(target_turn=sessions.TurnRef(sessions.AGENT, answer.data["turn_id"]),
            start_seq=f.session._turn(answer).creation_seq)
        f.events.now += 100
        later = f.input("another task")
        snapshot = f.session.index.snapshot()
        self.assertEqual(snapshot.processed, cancel_seq - 1)
        f.session.put("answer.cancel", answer.key + ":cancel", {"cancel_seq": cancel_seq})
        self.assertEqual(f.session.index.snapshot().processed, cancel_seq - 1)
        f.session._cancel_answer()
        snapshot = f.session.index.snapshot()
        cancelled = next(item for item in snapshot.events if item.ref["type"] == "cancel")
        self.assertEqual(cancelled.cursor, {"seq": cancel_seq, "index": 0})
        self.assertFalse(cancelled.trigger)
        self.assertEqual(cancelled.event["target_input_ids"], answer.data["reply_to_seqs"])
        self.assertEqual(cancelled.event["target_answer_id"], answer.data["turn_id"])
        tool_result = next(item for item in snapshot.events if item.ref["type"] == "tool_result")
        self.assertEqual(tool_result.cursor, {"seq": cancel_seq, "index": 1})
        self.assertEqual(snapshot.events[-1].event["input_id"], later)

    def test_result_before_cancel_remains_a_trigger_when_wal_confirmation_is_late(self):
        f = self.f
        answer, tools = self.accept_tools(count=1)
        tool = f.engine.journal.materialize(tools[0])
        runner = f.session.tools
        params = runner._params(tool.data)
        request = f.async_runner.run(runner._request(tool, 0, params, lambda: False))
        result_seq = self.emit_run(request)
        cancel_seq = f.user.cancel_turn(target_turn=sessions.TurnRef(sessions.AGENT, answer.data["turn_id"]),
            start_seq=f.session._turn(answer).creation_seq)
        f.session.put("answer.cancel", answer.key + ":cancel", {"cancel_seq": cancel_seq})
        done = f.async_runner.run(runner._done(tool, "exec", request_seq=request, result_seq=result_seq))
        self.assertLess(result_seq, cancel_seq)
        self.assertLess(cancel_seq, done.seq)
        result = next(item for item in f.session.index.snapshot().events if item.ref["type"] == "tool_result")
        self.assertEqual(result.cursor["seq"], result_seq)
        self.assertTrue(result.trigger, "late WAL persistence must not turn an existing result into cancellation cleanup")

    def test_late_cancel_fact_updates_existing_result_trigger_without_rescanning_wal(self):
        f = self.f
        answer, tools = self.accept_tools(count=1)
        tool = f.engine.journal.materialize(tools[0])
        runner = f.session.tools
        params = runner._params(tool.data)
        request = f.async_runner.run(runner._request(tool, 0, params, lambda: False))
        cancel_seq = f.user.cancel_turn(target_turn=sessions.TurnRef(sessions.AGENT, answer.data["turn_id"]),
            start_seq=f.session._turn(answer).creation_seq)
        result_seq = self.emit_run(request)
        f.async_runner.run(runner._done(tool, "exec", request_seq=request, result_seq=result_seq))
        before = f.session.index.snapshot()
        self.assertEqual(before.processed, cancel_seq - 1)
        result, = [item for item in before.all_events if item.ref["type"] == "tool_result"]
        self.assertTrue(result.trigger)
        f.session.put("answer.cancel", answer.key + ":cancel", {"cancel_seq": cancel_seq})
        after = f.session.index.snapshot()
        result, = [item for item in after.events if item.ref["type"] == "tool_result"]
        self.assertEqual(result.cursor["seq"], result_seq)
        self.assertFalse(result.trigger)
        self.assertEqual(after.processed, after.read)
        f.restart()
        self.assertEqual(f.session.index.snapshot(), after)

    def test_summary_can_cover_unconsumed_tool_events_across_same_response_batches(self):
        f = self.f
        f.config = replace(f.config, openevent=replace(f.config.openevent, max_payload_bytes=32768))
        f.restart(context_max_input_bytes=8000, context_compression_prompt="c" * 3500)
        answer, tools = self.accept_tools(count=4, content=None, command="x" * (f.session.budget.target - 250))
        old_consumed = dict(answer.data["input_through"])
        pending = f.input("continue with a new request")
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=sessions.body("s")))
        prepared = f.session._next_answer()
        batches = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="compaction.prepare")
        self.assertGreaterEqual(len(batches), 2)
        previous = None
        covered = []
        for batch in batches:
            batch = f.engine.journal.materialize(batch)
            self.assertEqual(batch.data["range"]["after"], previous)
            self.assertTrue(batch.data["events"])
            self.assertNotIn({"type": "user", "seq": pending}, batch.data["events"])
            if batch.data["summary"]:
                commit = f.engine.journal.get(batch.data["summary"])
                self.assertLessEqual(commit.seq, batch.data["snapshot"]["read_through_seq"])
            previous = batch.data["range"]["through"]
            covered.extend(batch.data["events"])
        self.assertEqual(len(covered), len({tuple(ref.items()) for ref in covered}))
        self.assertGreater(position(previous), position(old_consumed))
        self.assertGreater(position(prepared.data["input_through"]), position(previous))
        self.assertEqual(prepared.data["reply_to_seqs"], [pending])
        self.assertNotIn("history", prepared.data)
        self.assertEqual(f.session.index.summary(prepared.data["summary"])[1], previous)
        prepared = f.engine.journal.materialize(prepared)
        self.assertEqual([ref for ref in covered + prepared.data["events"] if ref["type"] == "tool_call"],
                         [{"type": "tool_call", "record": tool.key} for tool in tools])
        f.events.on_publish = None
        f.restart()
        restored = f.engine.journal.get(prepared.key)
        restored = f.engine.journal.materialize(restored)
        self.assertEqual(restored.data, prepared.data)
        self.assertEqual(f.session.index.summary(restored.data["summary"])[1], previous)
        self.assertEqual(f.session.index.snapshot().consumed, prepared.data["input_through"])


class JournalIndexTests(unittest.TestCase):
    def test_later_duplicate_keeps_first_source_and_kind_range_queries_stay_correct(self):
        journal = Journal(None, 10, "token", 22)
        def observe(seq, key, channel=30):
            return journal.observe(SimpleNamespace(seq=seq, principal=10,
                payload=encode("answer.cancel", key, channel, {"cancel_seq": 4})))
        observe(5, "answer-a")
        observe(20, "answer-b")
        observe(30, "other", 31)
        observe(40, "answer-a")
        self.assertEqual([record.seq for record in journal.select(channel=30)], [5, 20])
        self.assertEqual([record.key for record in journal.select(channel=30, kind="answer.cancel", after=5)], ["answer-b"])
        self.assertEqual(journal.get("answer-a").seq, 5)
        self.assertEqual([record.seq for record in journal.select(channel=30, through=10)], [5])


if __name__ == "__main__":
    unittest.main()
