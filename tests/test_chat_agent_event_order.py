"""Business outcomes follow durable source order during execution and replay."""
import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from openevent.chat_sdk import TextPart, TurnRef
from openevent.sdk import openevent_pb2

from chat_model_agent.budget import ContextBudget, event_size, failure_reason
from chat_model_agent.display_limits import display_fits
from chat_model_agent.errors import FailureCode
from chat_model_agent.session import UserCancelled
import test_chat_agent_session as support


class EventOrderTests(unittest.TestCase):
    def setUp(self):
        self.f = support.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def copy_history(self):
        f = self.f
        return tuple(message.SerializeToString() for message in f.events.history), f.events.next_uuid

    def restore_history(self, saved):
        f = self.f
        f.stop_reader()
        f.events.on_publish = None
        records, f.events.next_uuid = saved
        f.events.history = [openevent_pb2.EventMessage.FromString(raw) for raw in records]
        f.restart()
        self.assertEqual(tuple(message.SerializeToString() for message in f.events.history), records)

    def cancel_answer(self):
        f = self.f
        answer = f.session.answer
        return f.user.cancel_turn(target_turn=TurnRef(support.AGENT, answer.data["turn_id"]),
            start_seq=f.session._turn(answer).creation_seq)

    @staticmethod
    def tool_response():
        return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "Inspect the log", "tool_calls": [{
                "id": "inspect", "type": "function", "function": {
                    "name": "exec", "arguments": json.dumps({"command": "printf inspected"})}}]}}]}

    def test_cancel_is_confirmed_with_its_projection_before_the_page_finishes(self):
        f = self.f
        f.input()
        answer = f.session._next_answer()
        f.session.answer = answer
        attempt = f.session._new_attempt(0)
        f.session._chat_effect(answer.key + ":start", "turn.start", source={"type": "placeholder"})
        request_seq = f.session._request(attempt)
        request = next(message for message in f.events.history if message.seq == request_seq)
        payload = json.loads(request.payload)
        head = f.head(request, payload)
        f.append(request, payload, head, support.chunk("hello"))
        response = f.session._model_response(request_seq, payload["stream_id"])
        response.completion()

        ingested, release = threading.Event(), threading.Event()
        ingest = f.engine.ingest
        def pause_after_cancel(message):
            ingest(message)
            if json.loads(message.payload).get("kind") == "turn.cancel":
                ingested.set()
                if not release.wait(3):
                    raise AssertionError("cancel page was not released")
        errors = []
        def publish_cancel():
            try:
                self.cancel_answer()
            except Exception as error:
                errors.append(error)
        with patch.object(f.engine, "ingest", side_effect=pause_after_cancel):
            publisher = threading.Thread(target=publish_cancel, daemon=True)
            publisher.start()
            try:
                self.assertTrue(ingested.wait(2))
                self.assertLessEqual(f.session._turn().terminal_seq, f.engine.scanned_through)
                self.assertTrue(f.session.cancelled())
                with self.assertRaises(UserCancelled):
                    f.session._display_content(attempt, response)
            finally:
                release.set()
                publisher.join(2)
        self.assertFalse(publisher.is_alive())
        self.assertEqual(errors, [])
        f.run_calls()
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":cancel"))
        self.assertFalse(f.engine.journal.get(attempt.key + ":accept"))
        self.assertFalse(f.engine.journal.get(attempt.key + ":fail"))
        f.input("continue")
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=support.body("ready")))
        following = f.run_calls()
        self.assertNotEqual(following.key, answer.key)
        self.assertTrue(f.session._answer_finished(following))
        self.assertIsNone(f.engine.error)
        self.assertFalse(f.engine.stop.is_set())

    def test_final_failure_then_cancel_has_same_outcome_from_identical_history(self):
        f = self.f
        f.fresh(max_model_attempts=1)
        f.input("inspect the log")
        saved = []

        def respond(message, payload):
            if message.channel_id == support.MODEL_CHANNEL and payload["kind"] == "infer.request":
                f.end(message, payload, failure=True)
            if (message.channel_id == support.WAL_CHANNEL and payload["kind"] == "effect.prepare"
                    and payload["key"].endswith(":failure-cancel:prepare")):
                self.cancel_answer()
                saved.append(self.copy_history())

        f.events.on_publish = respond
        answer = f.run_calls()
        self.assertEqual(len(saved), 1)
        f.events.on_publish = None
        f.run_calls()  # Finish cancellation cleanup as well as failure closure.

        def outcome():
            failure = f.engine.journal.get(answer.key + ":attempt:0:fail")
            cancellation = f.engine.journal.get(answer.key + ":cancel")
            freeze = f.engine.journal.get(answer.key + ":freeze")
            self.assertIsNotNone(failure)
            self.assertIsNotNone(cancellation)
            self.assertIsNotNone(freeze)
            self.assertLess(failure.data["source_seq"], cancellation.data["cancel_seq"])
            notices = f.events.messages("turn.single", support.CHAT_CHANNEL, support.AGENT)
            self.assertEqual(len(notices), 1)
            self.assertEqual(freeze.data["notice_seq"], notices[0].seq)
            self.assertFalse(f.engine.journal.get(answer.key + ":wake"))
            return failure.data, json.loads(notices[0].payload)

        live = outcome()
        self.restore_history(saved[0])
        f.run_calls()
        self.assertEqual(outcome(), live)
        self.assertEqual(tuple(message.SerializeToString() for message in f.events.history[:len(saved[0][0])]),
                         saved[0][0])

    def test_model_terminal_before_cancel_is_kept_before_and_after_restart(self):
        f = self.f
        f.input("inspect the log")
        saved, sources = [], []

        def respond(request, payload):
            f.end(request, payload, response_body=self.tool_response())
            terminal = f.events.messages("infer.end", support.MODEL_CHANNEL)[-1].seq
            cancelled = self.cancel_answer()
            sources.append((terminal, cancelled))
            saved.append(self.copy_history())

        f.request_callback(respond)
        answer = f.run_calls()
        self.assertEqual(len(saved), 1)
        terminal, cancelled = sources[0]
        self.assertLess(terminal, cancelled)

        def outcome():
            accepted = f.engine.journal.get(answer.key + ":attempt:0:accept")
            self.assertIsNotNone(accepted)
            self.assertEqual(accepted.data["terminal_seq"], terminal)
            tool = f.engine.journal.get(answer.key + ":attempt:0:tool:0:prepare")
            self.assertIsNotNone(tool)
            done = f.engine.journal.get(tool.data["tool_key"] + ":done")
            self.assertEqual(done.data["outcome"], "cancelled")
            self.assertFalse(f.events.messages("cmd.run.request", support.CMD_CHANNEL))
            self.assertFalse(f.engine.journal.get(answer.key + ":freeze"))
            events = f.session.index.snapshot().events
            self.assertEqual([item.ref["type"] for item in events],
                             ["user", "assistant", "tool_call", "cancel", "tool_result"])
            return accepted.data, [(item.cursor, item.ref["type"]) for item in events]

        live = outcome()
        self.restore_history(saved[0])
        f.run_calls()
        self.assertEqual(outcome(), live)

    def test_submitted_failure_notice_is_not_lost_or_repeated_on_restart(self):
        f = self.f
        f.fresh(max_model_attempts=1)
        f.input("inspect the log")
        saved = []

        def respond(message, payload):
            if message.channel_id == support.MODEL_CHANNEL and payload["kind"] == "infer.request":
                f.end(message, payload, failure=True)
            elif (message.channel_id == support.WAL_CHANNEL and payload["kind"] == "effect.prepare"
                    and payload["key"].endswith(":failure-cancel:prepare")):
                self.cancel_answer()
            elif (message.channel_id == support.CHAT_CHANNEL and message.principal == support.AGENT
                    and payload["kind"] == "turn.single"):
                saved.append(self.copy_history())

        f.events.on_publish = respond
        answer = f.run_calls()
        self.assertEqual(len(saved), 1)
        notice, = f.events.messages("turn.single", support.CHAT_CHANNEL, support.AGENT)
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":freeze"))
        self.restore_history(saved[0])
        self.assertIsNone(f.engine.journal.get(answer.key + ":notice:commit"))
        self.assertIsNone(f.engine.journal.get(answer.key + ":freeze"))
        f.run_calls()
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":cancel"))
        self.assertEqual(f.engine.journal.get(answer.key + ":freeze").data["notice_seq"], notice.seq)
        self.assertEqual([message.seq for message in f.events.messages(
            "turn.single", support.CHAT_CHANNEL, support.AGENT)], [notice.seq])

    def test_invalid_fragment_and_cancel_are_decided_by_source_order(self):
        f = self.f
        for cancel_first in (False, True):
            with self.subTest(cancel_first=cancel_first):
                f.fresh(max_model_attempts=1)
                f.input("inspect the log")
                sources, saved = [], []

                def respond(request, payload):
                    head = f.head(request, payload)
                    if cancel_first:
                        self.cancel_answer()
                    source = f.append(request, payload, head, {
                        "choices": [{"index": 0, "delta": {"content": 7}, "finish_reason": None}]})
                    sources.append(source)
                    if not cancel_first:
                        self.cancel_answer()
                    f.end(request, payload)
                    saved.append(self.copy_history())

                f.request_callback(respond)
                answer = f.run_calls()

                def check():
                    failure = f.engine.journal.get(answer.key + ":attempt:0:fail")
                    freeze = f.engine.journal.get(answer.key + ":freeze")
                    if cancel_first:
                        self.assertIsNone(failure)
                        self.assertIsNone(freeze)
                    else:
                        self.assertEqual(failure.data["source_seq"], sources[0])
                        self.assertIsNotNone(freeze)

                check()
                self.restore_history(saved[0])
                f.run_calls()
                check()

    def test_cancel_before_model_terminal_never_accepts_later_output(self):
        f = self.f
        f.input("inspect the log")
        saved, sources = [], []

        def respond(request, payload):
            cancelled = self.cancel_answer()
            f.end(request, payload, response_body=self.tool_response())
            terminal = f.events.messages("infer.end", support.MODEL_CHANNEL)[-1].seq
            sources.append((cancelled, terminal))
            saved.append(self.copy_history())

        f.request_callback(respond)
        answer = f.run_calls()
        self.assertLess(*sources[0])

        def check():
            self.assertIsNotNone(f.engine.journal.get(answer.key + ":cancel"))
            self.assertFalse(f.engine.journal.get(answer.key + ":attempt:0:accept"))
            self.assertFalse(f.engine.journal.get(answer.key + ":attempt:0:fail"))
            self.assertFalse(f.engine.journal.get(answer.key + ":freeze"))
            self.assertFalse(f.engine.journal.select(channel=support.CHAT_CHANNEL, kind="tool.prepare"))
            self.assertFalse(f.events.messages("turn.single", support.CHAT_CHANNEL, support.AGENT))

        check()
        self.restore_history(saved[0])
        f.run_calls()
        check()

    def test_persisted_terminal_is_accepted_when_restart_clock_is_past_deadline(self):
        f = self.f
        f.input("inspect the log")
        saved = []

        def respond(request, payload):
            f.end(request, payload, response_body=support.body("inspection completed"))
            saved.append(self.copy_history())

        f.request_callback(respond)
        answer = f.run_calls()
        accepted = f.engine.journal.get(answer.key + ":attempt:0:accept")
        self.assertIsNotNone(accepted)
        expected = dict(accepted.data)
        self.restore_history(saved[0])
        f.events.now += f.config.agent.model_timeout_ms * 2
        f.run_calls()
        accepted = f.engine.journal.get(answer.key + ":attempt:0:accept")
        self.assertEqual(accepted.data, expected)
        self.assertFalse(f.engine.journal.get(answer.key + ":attempt:0:timeout"))
        self.assertFalse(f.engine.journal.get(answer.key + ":attempt:0:fail"))
        self.assertEqual(len(f.events.messages("infer.request", support.MODEL_CHANNEL)), 1)

    def test_model_terminal_and_timeout_use_committed_order_during_publication(self):
        f = self.f
        for terminal_first in (True, False):
            with self.subTest(terminal_first=terminal_first):
                f.fresh(max_model_attempts=1)
                f.input("inspect the log")
                requests, saved = [], []

                def finish():
                    request, payload = requests[0]
                    f.end(request, payload, response_body=support.body("inspection completed"))

                def respond(message, payload):
                    if message.channel_id == support.MODEL_CHANNEL and payload["kind"] == "infer.request":
                        requests.append((message, payload))
                        f.events.now = message.ts_ms + f.config.agent.model_timeout_ms
                    elif payload["kind"] == "attempt.timeout" and not terminal_first:
                        finish()

                original = f.session._target.put

                async def publish_timeout(kind, key, data, **kwargs):
                    if kind == "attempt.timeout" and terminal_first:
                        await f.engine.blocking(finish)
                    record = await original(kind, key, data, **kwargs)
                    if kind == "attempt.timeout":
                        saved.append(self.copy_history())
                    return record

                f.events.on_publish = respond
                with patch.object(f.session._target, "put", new=publish_timeout):
                    answer = f.run_calls()
                self.assertEqual(len(saved), 1)

                def check():
                    timeout = f.engine.journal.get(answer.key + ":attempt:0:timeout")
                    self.assertIsNotNone(timeout)
                    terminal, = f.events.messages("infer.end", support.MODEL_CHANNEL)
                    accepted = f.engine.journal.get(answer.key + ":attempt:0:accept")
                    failure = f.engine.journal.get(answer.key + ":attempt:0:fail")
                    if terminal_first:
                        self.assertLess(terminal.seq, timeout.seq)
                        self.assertIsNotNone(accepted)
                        self.assertIsNone(failure)
                        self.assertIsNone(f.engine.journal.get(answer.key + ":freeze"))
                    else:
                        self.assertLess(timeout.seq, terminal.seq)
                        self.assertIsNone(accepted)
                        self.assertEqual(failure.data["source_seq"], timeout.seq)
                        self.assertIsNotNone(f.engine.journal.get(answer.key + ":freeze"))
                    self.assertEqual(len(f.events.messages("attempt.timeout", support.WAL_CHANNEL)), 1)
                    self.assertEqual(len(f.events.messages("infer.request", support.MODEL_CHANNEL)), 1)

                check()
                self.restore_history(saved[0])
                f.run_calls()
                check()

    def test_cancel_before_failed_model_terminal_does_not_freeze(self):
        f = self.f
        f.fresh(max_model_attempts=1)
        f.input("inspect the log")

        def respond(request, payload):
            self.cancel_answer()
            f.end(request, payload, failure=True)

        f.request_callback(respond)
        answer = f.run_calls()
        self.assertIsNotNone(f.engine.journal.get(answer.key + ":cancel"))
        self.assertFalse(f.engine.journal.get(answer.key + ":attempt:0:fail"))
        self.assertFalse(f.engine.journal.get(answer.key + ":freeze"))
        self.assertFalse(f.events.messages("turn.single", support.CHAT_CHANNEL, support.AGENT))

    def test_input_arriving_after_admission_cannot_wake_before_its_own_check(self):
        f = self.f
        fixed = ContextBudget.from_config(f.config.agent).fixed
        f.fresh(context_max_input_bytes=fixed + 2000, max_model_attempts=1)
        original = f.input("x" * 750)
        f.request_callback(lambda request, payload: f.end(request, payload, failure=True))
        answer = f.run_calls()
        f.events.on_publish = None
        freeze = f.engine.journal.get(answer.key + ":freeze")
        pending, arrived = f.session._pending_answers, []

        def receive_after_check():
            result = pending()
            if not arrived:
                arrived.append(f.user.single_turn(turn_id="late-reference", content=(TextPart("continue"),),
                    reply_to_seqs=[original, freeze.data["notice_seq"]]))
            return result

        with patch.object(f.session, "_pending_answers", side_effect=receive_after_check):
            f.session._next_answer()
        seq, = arrived
        self.assertGreater(event_size(f.session.index.user(seq)), f.session.budget.target)
        self.assertFalse(f.engine.journal.get(answer.key + ":wake"))
        f.session._next_answer()
        self.assertIsNotNone(f.engine.journal.get(f"chat:{support.CHAT_CHANNEL}:input:{seq}:reject"))
        self.assertFalse(f.engine.journal.get(answer.key + ":wake"))
        accepted = f.input("continue without the references")
        f.session._next_answer()
        wake = f.engine.journal.get(answer.key + ":wake")
        self.assertEqual(wake.data["input_seq"], accepted)

    def test_oversized_display_source_survives_later_cancel_and_restart(self):
        f = self.f
        for variant in ("stream", "body", "stream_then_invalid_chunk"):
            with self.subTest(variant=variant):
                f.fresh(max_model_attempts=1)
                f.input("inspect the log")
                text = "x" * (20 * 1024)
                sources, saved = [], []

                def respond(request, payload):
                    if variant == "body":
                        sources.append(f.end(request, payload, response_body=support.body(text)))
                    else:
                        head = f.head(request, payload)
                        source = f.append(request, payload, head,
                            support.chunk(text, done=variant == "stream"))
                        sources.append(source)
                        if variant == "stream_then_invalid_chunk":
                            f.append(request, payload, source, {
                                "choices": [{"index": 0, "delta": {"content": 7}, "finish_reason": None}]})
                        f.end(request, payload)
                    # These are real SDK publications within the deployment's
                    # actual payload cap; only the Agent check below is scaled.
                    self.assertTrue(all(len(message.payload) < f.config.openevent.max_payload_bytes
                        for message in f.events.messages("infer.append", support.MODEL_CHANNEL)
                        + f.events.messages("infer.end", support.MODEL_CHANNEL)))

                original = f.session._target.put

                async def cancel_before_verdict(kind, key, data, **kwargs):
                    if kind == "attempt.fail":
                        await f.engine.blocking(self.cancel_answer)
                        saved.append(self.copy_history())
                    return await original(kind, key, data, **kwargs)

                f.request_callback(respond)
                def limited_display(config, *args):
                    display_config = replace(config, openevent=replace(config.openevent,
                        max_payload_bytes=16 * 1024))
                    return display_fits(display_config, *args)

                # Scale only this module's display check, still using real JSON
                # encoding and envelope validation rather than an SDK failure.
                with patch("chat_model_agent.session.display_fits", side_effect=limited_display):
                    with patch.object(f.session._target, "put", new=cancel_before_verdict):
                        answer = f.run_calls()
                    self.assertEqual(len(saved), 1)

                    def check():
                        failure = f.engine.journal.get(answer.key + ":attempt:0:fail")
                        self.assertEqual(failure.data["source_seq"], sources[0])
                        self.assertEqual(failure.data["reason"], failure_reason(FailureCode.DISPLAY_TOO_LARGE))
                        self.assertIsNotNone(f.engine.journal.get(answer.key + ":freeze"))
                        self.assertFalse(f.engine.journal.get(answer.key + ":attempt:0:accept"))
                        self.assertEqual(len(f.events.messages("turn.single", support.CHAT_CHANNEL, support.AGENT)), 1)
                        self.assertFalse(f.events.messages("turn.reset", support.CHAT_CHANNEL, support.AGENT))
                        self.assertFalse(f.events.messages("turn.append", support.CHAT_CHANNEL, support.AGENT))

                    check()
                    self.restore_history(saved[0])
                    f.run_calls()
                    check()
