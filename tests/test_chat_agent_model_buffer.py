"""Active model responses are consumed once; historical reads remain explicit."""

import unittest
from unittest.mock import patch

from async_helpers import wait_for_reader
from openevent.model_proxy_sdk import (
    InferAppendInput, InferEndInput, InferRequestInput, InferResultInput,
    create_client, publish_infer_append, publish_infer_end, publish_infer_request,
    publish_infer_result,
)

import test_chat_agent_session as fixture


class ModelBufferTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.engine = self.f.engine
        self.agent = create_client(self.f.events, "agent-token", max_retries=0)

    def request(self, stream):
        return publish_infer_request(self.agent, fixture.MODEL_CHANNEL, fixture.AGENT,
            InferRequestInput(stream_id=stream, method="POST", path="/v1/chat/completions",
                              body={"model": "model", "stream": True, "messages": []}))

    def head(self, stream, request):
        return publish_infer_result(self.f.provider, fixture.MODEL_CHANNEL, fixture.PROVIDER,
            fixture.AGENT, InferResultInput(stream_id=stream, prev_seq=request, status_code=200))

    def append(self, stream, request, previous, text):
        return publish_infer_append(self.f.provider, fixture.MODEL_CHANNEL, fixture.PROVIDER,
            fixture.AGENT, InferAppendInput(stream_id=stream, request_seq=request, prev_seq=previous,
                                           body=fixture.chunk(text)))

    def end(self, stream, request, *, body=None):
        fields = {"stream_id": stream, "request_seq": request,
                  "status_code": 200, "end_status": "completed"}
        if body is not None:
            fields["body"] = body
        return publish_infer_end(self.f.provider, fixture.MODEL_CHANNEL, fixture.PROVIDER,
            fixture.AGENT, InferEndInput(**fields))

    def consume_without_source_read(self, stream, **bounds):
        # The independent reader may still poll. Consumption must not issue an
        # additional targeted Fetch or parse the already ingested model payload.
        with patch.object(self.engine, "source_event",
                          side_effect=AssertionError("active response fetched again")), \
                patch("chat_model_agent.worker.parse_model",
                      side_effect=AssertionError("active response parsed again")):
            return self.engine.model_events(stream, consume=True, **bounds)

    def test_active_responses_are_parsed_once_and_released_when_consumed(self):
        stream = "active"
        self.engine.begin_model_stream(stream)
        request = self.request(stream)
        head = self.head(stream, request)
        append = self.append(stream, request, head, "first fragment")
        end = self.end(stream, request, body=fixture.body("complete answer"))
        self.assertEqual(set(self.engine.model_buffers[stream]), {head, append, end})
        events = self.consume_without_source_read(stream)
        self.assertEqual([event.seq for event in events], [request, head, append, end])
        self.assertEqual(events[2].payload.body, fixture.chunk("first fragment"))
        self.assertEqual(events[3].payload.body, fixture.body("complete answer"))
        self.assertEqual(self.engine.model_buffers[stream], {})
        self.assertTrue(self.engine.has_model_events(stream))
        self.assertEqual([event.seq for event in self.engine.model_headers(stream)],
                         [request, head, append, end])
        self.engine.end_model_stream(stream)
        self.assertNotIn(stream, self.engine.model_buffers)

    def test_consumption_respects_boundaries_and_keeps_future_responses(self):
        stream = "bounded"
        self.engine.begin_model_stream(stream)
        request = self.request(stream)
        head = self.head(stream, request)
        first = self.append(stream, request, head, "first")
        second = self.append(stream, request, first, "second")
        end = self.end(stream, request)
        self.assertEqual(self.consume_without_source_read(stream, after_seq=first, through_seq=head), [])
        self.assertEqual(set(self.engine.model_buffers[stream]), {head, first, second, end})
        events = self.consume_without_source_read(stream, after_seq=request, through_seq=first)
        self.assertEqual([event.seq for event in events], [head, first])
        self.assertEqual(set(self.engine.model_buffers[stream]), {second, end})
        events = self.consume_without_source_read(stream, after_seq=first, through_seq=end)
        self.assertEqual([event.seq for event in events], [second, end])
        self.assertEqual(events[0].payload.body, fixture.chunk("second"))
        self.assertEqual(self.engine.model_buffers[stream], {})

    def test_historical_read_does_not_consume_or_refill_active_buffer(self):
        stream = "history"
        self.engine.begin_model_stream(stream)
        request = self.request(stream)
        head = self.head(stream, request)
        append = self.append(stream, request, head, "history body")
        original = dict(self.engine.model_buffers[stream])
        with patch.object(self.engine, "source_event",
                          side_effect=AssertionError("buffered history fetched again")), \
                patch("chat_model_agent.worker.parse_model",
                      side_effect=AssertionError("buffered history parsed again")):
            historical = self.engine.model_events(stream, after_seq=request)
        self.assertEqual([event.seq for event in historical], [head, append])
        for event in historical:
            self.assertIs(event, original[event.seq])
        self.assertEqual(self.engine.model_buffers[stream], original)
        live = self.consume_without_source_read(stream, after_seq=request)
        self.assertEqual([event.seq for event in live], [head, append])
        for event, prior in zip(live, historical):
            self.assertIs(event, prior)
        self.assertEqual(self.engine.model_buffers[stream], {})
        with patch.object(self.engine, "source_event", wraps=self.engine.source_event) as source:
            reread = self.engine.model_events(stream, after_seq=request)
        self.assertEqual([(event.seq, event.payload.to_dict()) for event in reread],
                         [(event.seq, event.payload.to_dict()) for event in historical])
        self.assertEqual([call.args for call in source.call_args_list],
                         [(fixture.MODEL_CHANNEL, head), (fixture.MODEL_CHANNEL, append)])
        self.assertEqual(self.engine.model_buffers[stream], {}, "history must not repopulate the live buffer")

    def test_registration_does_not_cache_earlier_responses(self):
        stream = "resumed"
        request = self.request(stream)
        head = self.head(stream, request)
        previous = self.append(stream, request, head, "before registration")
        self.assertNotIn(stream, self.engine.model_buffers)
        self.engine.begin_model_stream(stream)
        self.assertEqual(self.engine.model_buffers[stream], {})
        latest = self.append(stream, request, previous, "after registration")
        self.assertEqual(set(self.engine.model_buffers[stream]), {latest})
        with patch.object(self.engine, "source_event", wraps=self.engine.source_event) as source:
            events = self.engine.model_events(stream, after_seq=request, consume=True)
        self.assertEqual([call.args for call in source.call_args_list],
                         [(fixture.MODEL_CHANNEL, head), (fixture.MODEL_CHANNEL, previous)])
        self.assertEqual([event.seq for event in events], [head, previous, latest])
        self.assertEqual(events[-1].payload.body, fixture.chunk("after registration"))
        self.assertEqual(self.engine.model_buffers[stream], {})

    def test_ending_registration_discards_unconsumed_data_and_ignores_late_responses(self):
        stream = "ended"
        self.engine.begin_model_stream(stream)
        request = self.request(stream)
        head = self.head(stream, request)
        self.engine.end_model_stream(stream)
        self.assertNotIn(stream, self.engine.model_buffers)
        late = self.append(stream, request, head, "late provider data")
        end = self.end(stream, request)
        self.assertNotIn(stream, self.engine.model_buffers)
        with patch.object(self.engine, "source_event", wraps=self.engine.source_event) as source:
            events = self.engine.model_events(stream, after_seq=request, consume=True)
        self.assertEqual([call.args for call in source.call_args_list],
                         [(fixture.MODEL_CHANNEL, seq) for seq in (head, late, end)])
        self.assertEqual(events[1].payload.body, fixture.chunk("late provider data"))
        self.assertNotIn(stream, self.engine.model_buffers)

    def test_interleaved_streams_consume_and_release_independently(self):
        for stream in ("first", "second"):
            self.engine.begin_model_stream(stream)
        first_request = self.request("first")
        second_request = self.request("second")
        first_head = self.head("first", first_request)
        second_head = self.head("second", second_request)
        first_append = self.append("first", first_request, first_head, "first stream")
        second_append = self.append("second", second_request, second_head, "second stream")
        second_before = dict(self.engine.model_buffers["second"])
        first = self.consume_without_source_read("first", after_seq=first_request)
        self.assertEqual([event.seq for event in first], [first_head, first_append])
        self.assertEqual(self.engine.model_buffers["second"], second_before)
        self.engine.end_model_stream("first")
        self.assertEqual(self.engine.model_buffers["second"], second_before)
        second = self.consume_without_source_read("second", after_seq=second_request)
        self.assertEqual([event.seq for event in second], [second_head, second_append])
        self.assertEqual(second[-1].payload.body, fixture.chunk("second stream"))
        self.assertEqual(self.engine.model_buffers["second"], {})

    def test_active_buffer_preserves_many_fragments_and_large_response_body(self):
        stream = "large"
        self.engine.begin_model_stream(stream)
        request = self.request(stream)
        head = self.head(stream, request)
        self.f.events.confirm_publications = False
        previous = head
        appends = []
        for index in range(1025):
            previous = self.append(stream, request, previous, str(index))
            appends.append(previous)
        large_text = "x" * (1024 * 1024)
        large = self.append(stream, request, previous, large_text)
        end = self.end(stream, request)
        wait_for_reader(self.engine, end)
        self.assertEqual(set(self.engine.model_buffers[stream]), {head, *appends, large, end})
        events = self.consume_without_source_read(stream, after_seq=request)
        self.assertEqual([event.seq for event in events], [head, *appends, large, end])
        self.assertEqual([event.payload.body for event in events[1:-2]],
                         [fixture.chunk(str(index)) for index in range(1025)])
        self.assertEqual(events[-2].payload.body, fixture.chunk(large_text))
        self.assertEqual(self.engine.model_buffers[stream], {})


if __name__ == "__main__":
    unittest.main()
