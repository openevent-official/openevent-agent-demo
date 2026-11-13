"""Streaming compaction measures new text once and rebuilds it on recovery."""
import unittest
from unittest.mock import patch

import test_chat_agent_session as fixture
import test_chat_agent_v3_flow as flow
from chat_model_agent import session as session_module
from chat_model_agent.budget import summary_size


class CompactionStreamTests(unittest.TestCase):
    def setUp(self):
        self.f = flow.EventFlowTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def seed(self):
        self.f.seed_large_history()
        self.f.input("next question")

    def test_many_fragments_measure_linear_text_volume_and_keep_the_exact_summary(self):
        f = self.f
        for count in (128, 256):
            with self.subTest(count=count):
                self.seed()
                part = '中"\\\n'
                work = []
                def measure(text):
                    work.append(len(text))
                    return summary_size(text)
                def stream(request, payload):
                    previous = f.head(request, payload)
                    for index in range(count):
                        def append(index=index):
                            nonlocal previous
                            previous = f.append(request, payload, previous,
                                fixture.chunk(part, done=index == count - 1))
                        f.waiting.extend((append, lambda: None))
                    f.waiting.append(lambda: f.end(request, payload))
                f.request_callback(stream)
                with patch.object(session_module, "summary_size", side_effect=measure):
                    prepared = f.session._next_answer()
                summary, _ = f.session.index.summary(prepared.data["summary"])
                self.assertEqual(summary, part * count)
                self.assertLessEqual(sum(work), 3 * len(summary))
                self.assertEqual(len(f.records("compaction.commit")), 1)
                self.assertFalse(any(child.answer.kind == "compaction.prepare"
                                     for child in f.session.children.values()))

    def test_restart_measures_replayed_fragments_once_and_keeps_original_request(self):
        f = self.f
        self.seed()
        first, second = '中\\\n"' * 20, '😀\t' * 20
        pending = []
        def stream(request, payload):
            head = f.head(request, payload)
            previous = f.append(request, payload, head, fixture.chunk(first))
            pending.append((request, payload, previous))
            def crash():
                raise fixture.Crash()
            f.waiting.append(crash)
        f.request_callback(stream)
        with self.assertRaises(fixture.Crash):
            f.session._next_answer()
        compression, = f.records("compaction.prepare")
        self.assertIsNotNone(f.session.children[compression.key].model_response)
        f.events.on_publish = None
        f.restart()
        request, payload, previous = pending[0]
        def finish():
            f.append(request, payload, previous, fixture.chunk(second, done=True))
            f.end(request, payload)
        f.waiting.append(finish)
        prepared = f.session._next_answer()
        self.assertEqual(f.session.index.summary(prepared.data["summary"])[0], first + second)
        self.assertEqual(len(f.requests()), 2)  # Original answer and original compaction.
        self.assertNotIn(compression.key, f.session.children)

    def test_oversized_stream_and_complete_body_are_rejected_without_poisoning_retry(self):
        f = self.f
        for streamed in (True, False):
            with self.subTest(streamed=streamed):
                self.seed()
                attempts = []
                def respond(request, payload):
                    attempts.append(request)
                    if len(attempts) == 1:
                        text = '"' * f.session.budget.target
                        if streamed:
                            head = f.head(request, payload)
                            f.append(request, payload, head, fixture.chunk(text))
                        else:
                            f.end(request, payload, response_body=fixture.body(text))
                    else:
                        head = f.head(request, payload)
                        f.append(request, payload, head, fixture.chunk("usable summary", done=True))
                        f.end(request, payload)
                f.request_callback(respond)
                prepared = f.session._next_answer()
                self.assertEqual(len(attempts), 2)
                self.assertEqual(f.session.index.summary(prepared.data["summary"])[0], "usable summary")
                failure, = f.records("attempt.fail")
                self.assertIn("summary that exceeds", failure.data["reason"])
                self.assertEqual(len(f.events.messages("infer.cancel", fixture.MODEL_CHANNEL)), int(streamed))


if __name__ == "__main__":
    unittest.main()
