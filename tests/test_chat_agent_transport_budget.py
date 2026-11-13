"""Model batching adapts to message capacity without changing saved sources."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from openevent.model_proxy_sdk import _json as model_json, prepare_payload

from chat_model_agent import session as session_module
from chat_model_agent.budget import ContextBudget, body_size, event_size
from chat_model_agent.display_limits import publication_fits
import test_chat_agent_session as fixture


class TransportBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.restart(context_max_input_bytes=20000)

    def capacity(self, limit):
        f = self.f
        f.config = replace(f.config, openevent=replace(f.config.openevent, max_payload_bytes=limit))
        f.restart()
        f.session._check_recovery_budget()

    def complete(self, text="done"):
        f = self.f
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=fixture.body(
            "saved facts" if "tools" not in payload["body"] else text)))

    def records(self, kind):
        return self.f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind=kind)

    def test_message_capacity_drives_batching_and_compaction_below_configured_budget(self):
        f = self.f
        self.capacity(5000)
        configured = ContextBudget.from_config(f.config.agent)
        self.assertLess(f.session.budget.limit, configured.limit)
        self.assertLess(f.session.budget.target, configured.target)
        sources = [f.input("x" * (f.session.budget.target // 2)) for _ in range(12)]
        self.complete()
        consumed = []
        encodings, checked, sent = {}, {}, {}
        encode = model_json.dump_bytes
        fits = session_module.publication_fits
        publish = f.events.publish_auto_seq

        def encode_request(value):
            if isinstance(value, dict) and value.get("kind") == "infer.request":
                stream = value["stream_id"]
                encodings[stream] = encodings.get(stream, 0) + 1
            return encode(value)

        def check(config, channel, payload, **kwargs):
            if channel == fixture.MODEL_CHANNEL:
                checked[json.loads(payload)["stream_id"]] = payload
            return fits(config, channel, payload, **kwargs)

        def publish_request(**kwargs):
            if kwargs["channel_id"] == fixture.MODEL_CHANNEL:
                value = json.loads(kwargs["payload"])
                if value["kind"] == "infer.request":
                    sent[value["stream_id"]] = kwargs["payload"]
            return publish(**kwargs)

        with patch.object(model_json, "dump_bytes", new=encode_request), \
                patch.object(session_module, "publication_fits", new=check), \
                patch.object(f.events, "publish_auto_seq", side_effect=publish_request):
            for _ in range(20):
                answer = f.session._next_answer()
                if answer is None:
                    break
                consumed.extend(f.new_inputs(answer))
                f.session._run_answer(answer)
            else:
                self.fail("input backlog did not finish")
        self.assertEqual(consumed, sources)
        self.assertTrue(self.records("compaction.commit"))
        self.assertEqual(encodings, {stream: 1 for stream in sent})
        self.assertEqual(set(checked), set(sent))
        for stream, payload in sent.items():
            self.assertIs(payload, checked[stream])
        self.assertIsNone(f.session.prepared_request)
        for request in f.events.messages("infer.request", fixture.MODEL_CHANNEL):
            self.assertLessEqual(len(request.payload), 5000)
            self.assertLessEqual(body_size(json.loads(request.payload)["body"]), f.session.budget.limit)
        for prepared in self.records("answer.prepare") + self.records("compaction.prepare"):
            self.assertEqual(prepared.data["settings"]["context_max_input_bytes"], configured.limit)
            self.assertEqual(prepared.data["settings"]["context_history_max_bytes"], f.session.budget.target)

    def test_smaller_message_capacity_compacts_previously_admitted_larger_events(self):
        f = self.f
        source = f.input("x" * 3000)
        self.complete("y" * 3000)
        f.run_answer()
        self.capacity(6000)
        self.assertGreater(event_size(f.session.index.user(source)), f.session.budget.target)
        fresh = f.input("continue")
        self.complete()
        answer = f.run_answer()
        self.assertTrue(self.records("compaction.commit"))
        self.assertEqual(f.new_inputs(answer), [fresh])
        self.assertFalse(self.records("answer.freeze"))

    def test_small_capacity_still_compacts_when_full_baseline_no_longer_fits(self):
        f = self.f
        f.input("x" * 400)
        self.complete("y" * 400)
        f.run_answer()
        configured = ContextBudget.from_config(f.config.agent)
        with patch("openevent.model_proxy_sdk.publishing.time.time_ns", return_value=((1 << 64) - 1) * 1_000_000):
            envelope = len(prepare_payload(f.session._model_request({}, "f" * 36)).payload) - 2
        self.capacity(configured.fixed + 400 + envelope)
        target = (f.session.budget.limit - f.session.budget.fixed) // 2
        self.assertGreater(f.session.budget.target, target)
        self.assertGreater(target, 0)
        f.input("continue")
        self.complete()
        answer = f.run_answer()
        prepared, = self.records("compaction.prepare")
        self.assertEqual(prepared.data["settings"]["context_history_max_bytes"], target)
        self.assertIsNotNone(answer.data["summary"])
        self.assertFalse(self.records("answer.freeze"))

    def test_uncompressible_old_event_uses_local_failure_and_survives_restart(self):
        f = self.f
        f.input("x" * 8000)
        self.complete()
        f.run_answer()
        requests = len(f.events.messages("infer.request", fixture.MODEL_CHANNEL))
        self.capacity(5000)
        source = f.input("continue")
        answer = f.run_answer()
        self.assertEqual(f.new_inputs(answer), [source])
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":freeze"))
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), requests)
        notices = len(f.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT))
        f.events.on_publish = None
        f.restart()
        f.session._check_recovery_budget()
        self.assertIsNone(f.session._next_answer())
        self.assertEqual(len(f.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)), notices)

    def test_saved_summary_that_no_longer_fits_uses_local_failure(self):
        f = self.f
        text = "x" * (f.session.budget.target - 500)
        f.input(text)
        self.complete(text)
        f.run_answer()
        source = f.input("continue")

        def save_summary(event, payload):
            if event.channel_id == fixture.MODEL_CHANNEL and payload["kind"] == "infer.request":
                f.end(event, payload, response_body=fixture.body("s" * 6000))
            elif event.channel_id == fixture.WAL_CHANNEL and payload["kind"] == "compaction.commit":
                raise fixture.Crash()

        f.events.on_publish = save_summary
        with self.assertRaises(fixture.Crash):
            f.session._next_answer()
        self.assertEqual(len(self.records("compaction.commit")), 1)
        requests = len(f.events.messages("infer.request", fixture.MODEL_CHANNEL))
        f.events.on_publish = None
        self.capacity(5000)
        answer = f.run_answer()
        self.assertEqual(f.new_inputs(answer), [source])
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":freeze"))
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), requests)

    def test_capacity_below_minimum_model_request_freezes_without_rejecting_user(self):
        f = self.f
        self.capacity(1100)
        self.assertLess(f.session.budget.limit, f.session.budget.fixed)
        source = f.input("hello")
        answer = f.run_answer()
        self.assertEqual(f.new_inputs(answer), [source])
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":freeze"))
        self.assertFalse(self.records("input.reject"))
        self.assertFalse(f.events.messages("infer.request", fixture.MODEL_CHANNEL))
        self.assertFalse(f.events.messages("turn.start", fixture.CHAT_CHANNEL, fixture.AGENT))
        for event in f.events.history:
            if event.principal == fixture.AGENT:
                self.assertLessEqual(len(event.payload), 1100)

    def test_capacity_reduction_does_not_reject_an_already_sent_request(self):
        f = self.f
        f.input("x" * 3000)
        answer = f.session._next_answer()
        f.session.answer = answer
        attempt = f.session._new_attempt(0)
        f.session._chat_effect(answer.key + ":start", "turn.start", source={"type": "placeholder"})
        request_seq = f.session._request(attempt)
        request = next(event for event in f.events.history if event.seq == request_seq)
        self.capacity(2500)
        self.assertGreater(len(request.payload), 2500)
        f.end(request, json.loads(request.payload), response_body=fixture.body("already sent"))
        self.assertEqual(f.session._next_answer(), answer)
        f.session._run_answer(answer)
        self.assertIsNotNone(f.engine.journal.get(attempt.key + ":accept"))
        self.assertFalse(self.records("attempt.fail"))
        self.assertFalse(self.records("answer.freeze"))
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), 1)

    def test_unconfirmed_request_source_is_reconciled_without_preparing_new_bytes(self):
        f = self.f
        f.input("x" * 3000)
        answer = f.session._next_answer()
        f.session.answer = answer
        attempt = f.session._new_attempt(0)

        def crash_after_request(event, payload):
            if event.channel_id == fixture.MODEL_CHANNEL and payload["kind"] == "infer.request":
                raise fixture.Crash()

        f.events.on_publish = crash_after_request
        with self.assertRaises(fixture.Crash):
            f.session._request(attempt)
        request, = f.events.messages("infer.request", fixture.MODEL_CHANNEL)
        self.assertIsNone(f.engine.journal.get(attempt.key + ":request:commit"))
        f.events.on_publish = None
        self.capacity(2500)
        f.session.answer = f.engine.journal.get(answer.key)
        self.assertGreater(len(request.payload), 2500)
        with patch.object(session_module, "prepare_payload", side_effect=AssertionError("request was already sent")):
            self.assertEqual(f.session._request(attempt), request.seq)
        self.assertEqual(f.engine.journal.get(attempt.key + ":request:commit").data["seq"], request.seq)
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), 1)
        self.assertIsNone(f.session.prepared_request)

    def test_prepared_request_uses_its_own_prompt_after_configuration_changes(self):
        f = self.f
        source = f.input("hello")
        answer = f.session._next_answer()
        f.session.answer = answer
        request = f.session._model_request(f.session._model_body(), "f" * 36)
        with patch("openevent.model_proxy_sdk.publishing.time.time_ns", return_value=((1 << 64) - 1) * 1_000_000):
            payload = prepare_payload(request).payload
        f.config = replace(f.config, openevent=replace(f.config.openevent, max_payload_bytes=len(payload)))
        f.restart(system_prompt="new prompt" * 1000, context_max_input_bytes=50000)
        f.session._check_recovery_budget()
        self.assertEqual(f.session._next_answer(), answer)
        self.assertTrue(publication_fits(f.config, fixture.MODEL_CHANNEL, payload))
        self.assertLess(f.session.budget.limit, f.session.budget.fixed)
        self.complete()
        f.session._run_answer(answer)
        self.assertEqual(f.new_inputs(answer), [source])
        sent, = f.events.messages("infer.request", fixture.MODEL_CHANNEL)
        self.assertEqual(json.loads(sent.payload)["body"]["messages"][0]["content"], "help")
        self.assertFalse(self.records("attempt.fail"))
        self.assertFalse(self.records("answer.freeze"))


if __name__ == "__main__":
    unittest.main()
