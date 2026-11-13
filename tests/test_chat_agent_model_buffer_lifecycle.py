"""Active model responses survive interleaved history reads and leave no buffer."""

import unittest
from unittest.mock import patch

from openevent.chat_sdk import TurnRef

from chat_model_agent.rpc import InfrastructureError
from chat_model_agent.worker import Stopped
import test_chat_agent_session as fixture
import test_chat_agent_v3_flow as flow


class ModelBufferLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = flow.EventFlowTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_immediate_response_is_registered_before_request_and_released_on_success(self):
        f = self.f
        f.input()
        response_seqs = []

        def respond(request, payload):
            stream = payload["stream_id"]
            self.assertEqual(set(f.engine.model_buffers), {stream})
            head = f.head(request, payload)
            part = f.append(request, payload, head, fixture.chunk("immediate answer", done=True))
            end = f.end(request, payload)
            response_seqs.extend((head, part, end))
            self.assertEqual(set(f.engine.model_buffers[stream]), set(response_seqs))
            self.assertNotIn(request.seq, f.engine.model_buffers[stream])

        source_event = f.engine.source_event
        def source(channel, seq):
            if channel == fixture.MODEL_CHANNEL:
                self.assertNotIn(seq, response_seqs, "live response was fetched again")
            return source_event(channel, seq)

        f.request_callback(respond)
        with patch.object(f.engine, "source_event", side_effect=source):
            answer = f.run_answer()
        self.assertEqual(f.visible_turn(fixture.AGENT, answer.data["turn_id"]).text, "immediate answer")
        self.assertEqual(f.engine.model_buffers, {})

    def test_failed_attempt_is_released_before_retry_starts(self):
        f = self.f
        f.fresh(max_model_attempts=2)
        f.input()
        streams = []

        def respond(request, payload):
            stream = payload["stream_id"]
            self.assertEqual(set(f.engine.model_buffers), {stream})
            self.assertNotIn(stream, streams)
            streams.append(stream)
            f.end(request, payload, failure=len(streams) == 1,
                  response_body=None if len(streams) == 1 else fixture.body("retried answer"))

        f.request_callback(respond)
        answer = f.run_answer()
        self.assertEqual(len(streams), 2)
        self.assertEqual(len(f.records("attempt.fail")), 1)
        self.assertEqual(len(f.records("attempt.accept")), 1)
        self.assertEqual(f.visible_turn(fixture.AGENT, answer.data["turn_id"]).text, "retried answer")
        self.assertEqual(f.engine.model_buffers, {})

    def test_cancellation_discards_buffered_events_after_the_cancel_boundary(self):
        f = self.f
        f.input()
        answer = f.session._next_answer()

        def respond(request, payload):
            head = f.head(request, payload)
            first = f.append(request, payload, head, fixture.chunk("before cancellation"))

            def cancel_then_finish():
                turn = f.session._turn(answer)
                f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, answer.data["turn_id"]),
                                   start_seq=turn.creation_seq)
                late = f.append(request, payload, first, fixture.chunk("late", done=True))
                end = f.end(request, payload)
                self.assertTrue({late, end}.issubset(f.engine.model_buffers[payload["stream_id"]]))

            f.waiting.append(cancel_then_finish)

        f.request_callback(respond)
        f.run_answer(answer)
        self.assertEqual(len(f.records("answer.cancel")), 1)
        self.assertEqual(f.records("attempt.accept"), [])
        self.assertEqual(f.records("attempt.fail"), [])
        self.assertEqual(f.engine.model_buffers, {})

    def test_timeout_releases_the_active_stream(self):
        f = self.f
        f.fresh(max_model_attempts=1, model_timeout_ms=100)
        f.input()

        def respond(request, payload):
            head = f.head(request, payload)
            f.append(request, payload, head, fixture.chunk("unfinished"))
            f.events.now = request.ts_ms + f.config.agent.model_timeout_ms

        f.request_callback(respond)
        f.run_answer()
        self.assertEqual(len(f.records("attempt.timeout")), 1)
        self.assertEqual(len(f.records("answer.freeze")), 1)
        self.assertEqual(f.engine.model_buffers, {})

    def test_exception_and_stop_release_unconsumed_response_events(self):
        f = self.f
        for outcome in ("crash", "rpc_failure", "stop"):
            with self.subTest(outcome=outcome):
                f.fresh()
                f.input()

                def respond(request, payload):
                    head = f.head(request, payload)
                    part = f.append(request, payload, head, fixture.chunk("pending"))
                    self.assertIn(part, f.engine.model_buffers[payload["stream_id"]])
                    if outcome == "stop":
                        f.engine.request_stop()
                    elif outcome == "crash":
                        raise fixture.Crash()
                    else:
                        raise RuntimeError("test publication handoff failed")

                expected = {"crash": fixture.Crash, "rpc_failure": InfrastructureError, "stop": Stopped}[outcome]
                f.request_callback(respond)
                with self.assertRaises(expected):
                    f.run_answer()
                self.assertEqual(f.engine.model_buffers, {})

    def test_historical_read_peeks_without_advancing_or_rewinding_live_response(self):
        f = self.f
        f.input()
        answer = f.session._next_answer()
        child = f.session._child(answer)

        def respond(request, payload):
            stream = payload["stream_id"]
            head = f.head(request, payload)
            first = f.append(request, payload, head, fixture.chunk("first "))

            def inspect_then_finish():
                live = child.model_response
                self.assertEqual(live.cursor, first)
                second = f.append(request, payload, first, fixture.chunk("second", done=True))
                buffered = dict(f.engine.model_buffers[stream])
                historical = child._model_response(request.seq, stream, second)
                historical.completion()
                self.assertIsNot(historical, live)
                self.assertEqual("".join(part.text for part in historical.fragments), "first second")
                self.assertIs(child.model_response, live)
                self.assertEqual(live.cursor, first)
                self.assertEqual(f.engine.model_buffers[stream], buffered)
                earlier = child._model_response(request.seq, stream, head)
                self.assertIsNot(earlier, live)
                self.assertEqual(live.cursor, first)
                self.assertEqual(f.engine.model_buffers[stream], buffered)
                self.assertIs(child._model_response(request.seq, stream, first), live)
                f.end(request, payload)

            f.waiting.append(inspect_then_finish)

        f.request_callback(respond)
        f.run_answer(answer)
        self.assertEqual(f.visible_turn(fixture.AGENT, answer.data["turn_id"]).text, "first second")
        self.assertEqual(f.engine.model_buffers, {})

    def test_restart_reads_old_sources_but_consumes_new_responses_from_buffer(self):
        f = self.f
        f.input()
        original = []

        def respond(request, payload):
            head = f.head(request, payload)
            first = f.append(request, payload, head, fixture.chunk("before restart "))
            original.append((request, payload, head, first))
            def crash():
                raise fixture.Crash()
            f.waiting.append(crash)

        f.request_callback(respond)
        with self.assertRaises(fixture.Crash):
            f.run_answer()
        self.assertEqual(f.engine.model_buffers, {})
        f.events.on_publish = None
        f.restart()
        self.assertEqual(f.engine.model_buffers, {})
        request, payload, head, first = original[0]
        newer, fetched = [], []

        def finish():
            self.assertIn(payload["stream_id"], f.engine.model_buffers)
            newer.append(f.append(request, payload, first, fixture.chunk("after restart", done=True)))
            newer.append(f.end(request, payload))

        source_event = f.engine.source_event
        def source(channel, seq):
            if channel == fixture.MODEL_CHANNEL:
                fetched.append(seq)
                self.assertNotIn(seq, newer, "new response ignored its active buffer")
            return source_event(channel, seq)

        f.waiting.append(finish)
        with patch.object(f.engine, "source_event", side_effect=source):
            answer = f.run_answer()
        self.assertTrue({head, first}.issubset(fetched))
        self.assertEqual(len(f.requests()), 1)
        self.assertEqual(f.visible_turn(fixture.AGENT, answer.data["turn_id"]).text,
                         "before restart after restart")
        self.assertEqual(f.engine.model_buffers, {})

    def test_compaction_retry_and_commit_release_their_streams(self):
        f = self.f
        f.seed_large_history()
        f.input("next question")
        streams = []

        def respond(request, payload):
            self.assertNotIn("tools", payload["body"])
            stream = payload["stream_id"]
            self.assertEqual(set(f.engine.model_buffers), {stream})
            streams.append(stream)
            if len(streams) == 1:
                f.end(request, payload, failure=True)
            else:
                head = f.head(request, payload)
                f.append(request, payload, head, fixture.chunk("retained summary", done=True))
                f.end(request, payload)

        f.request_callback(respond)
        prepared = f.session._next_answer()
        self.assertEqual(len(streams), 2)
        self.assertEqual(len(f.records("compaction.commit")), 1)
        self.assertEqual(f.session.index.summary(prepared.data["summary"])[0], "retained summary")
        self.assertEqual(f.engine.model_buffers, {})


if __name__ == "__main__":
    unittest.main()
