"""Admission, bounded prefixes and compaction preserve source input facts."""
from dataclasses import replace
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_chat_agent_session as sessions
from chat_model_agent.budget import INPUT_REJECTION, body_size, event_size, history_size, build_compaction_body
from chat_model_agent.history import HistoryEvent, Snapshot, position
from chat_model_agent.prompt import user_event
from chat_model_agent.state import HistoryError


class InputBatchTests(unittest.TestCase):
    def setUp(self):
        self.f = sessions.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.config = replace(self.f.config, openevent=replace(
            self.f.config.openevent, max_payload_bytes=16384))
        self.f.restart(context_max_input_bytes=5000)

    def complete(self):
        self.f.request_callback(lambda request, payload: self.f.end(request, payload,
            response_body=sessions.body("older facts" if "tools" not in payload["body"] else "ok")))

    def input_refs(self, prepared):
        prepared = self.f.engine.journal.materialize(prepared)
        return [ref["seq"] for ref in prepared.data["events"] if ref["type"] == "user"]

    def requests(self, *, compression=False):
        return [json.loads(event.payload)["body"]
                for event in self.f.events.messages("infer.request", sessions.MODEL_CHANNEL)
                if ("tools" not in json.loads(event.payload)["body"]) == compression]

    def test_oversized_input_is_rejected_without_consuming_or_freezing(self):
        f = self.f
        first = f.input("first")
        rejected = f.input("x" * f.session.budget.target)
        last = f.input("last")
        f.session._reject_inputs()
        async def finish_notices():
            await asyncio.gather(*f.session.rejection_tasks.values())
        f.async_runner.run(finish_notices())
        rejection, = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="input.reject")
        self.assertEqual(rejection.data["input_seq"], rejected)
        self.assertEqual(f.session.index.snapshot().consumed, None)
        self.assertFalse(f.events.messages("infer.request", sessions.MODEL_CHANNEL))
        self.assertFalse(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="answer.prepare"))
        notices = f.events.messages("turn.single", sessions.CHAT_CHANNEL, principal=sessions.AGENT)
        self.assertEqual(len(notices), 1)
        notice = json.loads(notices[0].payload)
        self.assertEqual(notice["reply_to_seqs"], [rejected])
        self.assertEqual(notice["content"][0]["text"], INPUT_REJECTION)
        self.complete()
        prepared = f.run_answer()
        self.assertEqual(self.input_refs(prepared), [first, last])
        self.assertFalse(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="answer.freeze"))
        f.events.on_publish = None
        f.restart()
        f.session._reject_inputs()
        self.assertEqual(len(f.events.messages("turn.single", sessions.CHAT_CHANNEL, principal=sessions.AGENT)), 1)
        self.assertIsNone(f.session._next_answer())

    def test_one_complete_event_at_target_fits_with_its_real_encoded_overhead(self):
        f = self.f
        source_seq = len(f.events.history) + 1
        prototype = user_event(source_seq, f.events.now, "", [])
        text = "x" * (f.session.budget.target - event_size(prototype))
        source = f.input(text)
        self.assertEqual(source, source_seq)
        self.assertEqual(event_size(f.session.index.user(source)), f.session.budget.target)
        self.complete()
        f.run_answer()
        self.assertEqual(len(self.requests()), 1)
        self.assertLessEqual(body_size(self.requests()[0]), f.session.budget.limit)
        self.assertFalse(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="input.reject"))

    def test_escape_expansion_and_attachment_metadata_count_toward_admission(self):
        f = self.f
        text = "\x00" * (f.session.budget.target // 7 + 1)
        self.assertLess(len(text.encode()), f.session.budget.target)
        escaped = f.input(text)
        attached = f.input("a", object_keys=[sessions.ObjectKey(object_id=42, object_token="private")])
        f.engine.objects.batch_metadata_reader = lambda keys: [{
            "name": "x" * f.session.budget.target, "type": "text/plain", "nbytes": 1} for _ in keys]
        f.session._reject_inputs()
        self.assertEqual([record.data["input_seq"] for record in f.engine.journal.select(
            channel=sessions.CHAT_CHANNEL, kind="input.reject")], [escaped, attached])
        self.assertIsNone(f.session._next_answer())
        self.assertFalse(self.requests())

    def test_backlog_uses_contiguous_prefixes_and_only_one_ordinary_consume(self):
        f = self.f
        inputs = [f.input(f"task-{index}:" + "x" * 400) for index in range(14)]
        self.complete()
        consumed = []
        previous = None
        for _ in range(20):
            prepared = f.session._next_answer()
            if prepared is None:
                break
            fresh = [seq for seq in self.input_refs(prepared) if previous is None or seq > previous["seq"]]
            consumed.extend(fresh)
            self.assertEqual(prepared.data["reply_to_seqs"], fresh)
            previous = prepared.data["input_through"]
            f.session._run_answer(prepared)
        else:
            self.fail("backlog did not finish")
        self.assertEqual(consumed, inputs)
        self.assertGreater(len(self.requests()), 1)
        self.assertTrue(self.requests(compression=True))
        for request in self.requests() + self.requests(compression=True):
            self.assertLessEqual(body_size(request), f.session.budget.limit)
        for event in f.events.history:
            if event.principal == sessions.AGENT:
                self.assertLessEqual(len(event.payload), 16384)
        f.events.on_publish = None
        f.restart()
        self.assertIsNone(f.session._next_answer())

    def test_prepare_crash_preserves_sources_and_prompt_when_configuration_grows(self):
        f = self.f
        source = f.input("a" * 400)
        def crash(event, value):
            if event.channel_id == sessions.WAL_CHANNEL and value["kind"] == "answer.prepare":
                raise sessions.Crash()
        f.events.on_publish = crash
        with self.assertRaises(sessions.Crash):
            f.session._next_answer()
        prepared, = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="answer.prepare")
        self.assertNotIn("history", prepared.data)
        f.events.on_publish = None
        f.restart(context_max_input_bytes=5100, system_prompt="new prompt")
        self.assertEqual(f.session._next_answer(), prepared)
        self.complete()
        f.session._run_answer(prepared)
        request, = self.requests()
        self.assertEqual(request["messages"][0]["content"], "help")
        self.assertEqual([event["input_id"] for event in json.loads(request["messages"][1]["content"])["events"]], [source])
        self.assertEqual(prepared.data["settings"]["context_max_input_bytes"], 5000)

    def test_smaller_target_cannot_resume_an_unfinished_larger_stage(self):
        f = self.f
        f.input("hello")
        prepared = f.session._next_answer()
        f.restart(context_max_input_bytes=4900)
        with self.assertRaisesRegex(HistoryError, "larger history target"):
            f.session._check_recovery_budget()
        self.assertEqual(f.session._next_answer(), prepared)
        self.assertFalse(self.requests())

    def test_rejected_new_message_cannot_wake_failure_or_old_backlog(self):
        f = self.f
        f.restart(max_model_attempts=1)
        inputs = [f.input("x" * 400) for _ in range(12)]
        f.request_callback(lambda request, payload: f.end(request, payload, failure=True))
        prepared = f.run_answer()
        self.assertLess(len(self.input_refs(prepared)), len(inputs))
        frozen = f.engine.journal.get(prepared.key + ":freeze")
        self.assertIsNotNone(frozen)
        f.events.on_publish = None
        f.restart()
        rejected = f.input("x" * f.session.budget.target)
        self.assertIsNone(f.session._next_answer())
        self.assertIsNone(f.engine.journal.get(prepared.key + ":wake"))
        self.assertEqual(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="input.reject")[-1].data["input_seq"], rejected)
        fresh = f.input("try again")
        self.complete()
        next_answer = f.session._next_answer()
        self.assertIsNotNone(next_answer)
        self.assertEqual(f.engine.journal.get(prepared.key + ":wake").data["input_seq"], fresh)
        self.assertGreater(fresh, frozen.data["notice_seq"])
        self.assertNotIn(rejected, self.input_refs(next_answer))

    def test_compaction_targets_all_old_history_and_preserves_pending_user(self):
        f = self.f
        old = f.input("x" * (f.session.budget.target - 150))
        self.complete()
        f.run_answer()
        fresh = f.input("new question")
        prepared = f.session._next_answer()
        self.assertTrue(self.requests(compression=True))
        self.assertEqual(self.input_refs(prepared), [fresh])
        summary, after = f.session.index.summary(prepared.data["summary"])
        self.assertIsNotNone(summary)
        self.assertLess(position(after), position(prepared.data["input_through"]))
        for request in self.requests(compression=True):
            events = json.loads(request["messages"][1]["content"])["events"]
            self.assertNotIn(fresh, [event.get("input_id") for event in events])
            self.assertTrue(events)
        prepared = f.engine.journal.materialize(prepared)
        old_events = [f.session.index.render(ref) for ref in prepared.data["events"] if ref != {"type": "user", "seq": fresh}]
        self.assertLessEqual(history_size(summary, old_events), f.session.budget.target)
        self.assertNotEqual(old, fresh)

    def test_many_short_events_do_not_reencode_every_prefix(self):
        f = self.f
        f.config = replace(f.config, openevent=replace(f.config.openevent, max_payload_bytes=400000))
        f.restart(context_max_input_bytes=100000)
        events = tuple(HistoryEvent({"seq": seq, "index": 0}, {"type": "user", "seq": seq},
            user_event(seq, 1000, '中文\\\n"' * 30, []), True, (seq,)) for seq in range(1, 129))
        snapshot = Snapshot(129, 129, events, events, None, None)
        original = json.dumps

        def measure(operation):
            encoded_bytes = 0
            def counting_encode(*args, **kwargs):
                nonlocal encoded_bytes
                value = original(*args, **kwargs)
                encoded_bytes += len(value.encode("utf-8"))
                return value
            with patch("json.dumps", side_effect=counting_encode):
                result = operation()
            return result, encoded_bytes

        prepared, ordinary_work = measure(lambda: f.session._batch(snapshot))
        self.assertEqual(prepared[1]["events"], [item.ref for item in events])
        self.assertEqual(prepared[1]["reply_to_seqs"], list(range(1, 129)))

        incoming = HistoryEvent({"seq": 129, "index": 0}, {"type": "user", "seq": 129},
            user_event(129, 1000, "new question", []), True, (129,))
        old = replace(snapshot, events=events + (incoming,), all_events=events + (incoming,),
                      consumed=events[-1].cursor)
        body = build_compaction_body(f.session._settings("compaction"), None, [item.event for item in events])
        self.assertGreater(history_size(None, [item.event for item in events]), f.session.budget.target)
        self.assertLessEqual(body_size(body), f.session.budget.limit)
        child = SimpleNamespace(_run_compaction=AsyncMock(return_value="stop after selection"))
        with patch.object(f.session._target, "_child", return_value=child):
            _, compaction_work = measure(lambda: f.session._compress(old))
        compressed, = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="compaction.prepare")
        compressed = f.engine.journal.materialize(compressed)
        self.assertEqual(compressed.data["events"], [item.ref for item in events])
        # Count real JSON encoder output, including WAL/Chat previews and both
        # Provider layers. A few full passes are fine; N complete prefixes are not.
        for work in (ordinary_work, compaction_work):
            self.assertLess(work, 12 * body_size(body))


if __name__ == "__main__":
    unittest.main()
