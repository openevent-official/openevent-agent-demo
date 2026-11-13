"""Current reply context survives source eviction without caching old history."""

import json
import unittest
from unittest.mock import patch

import test_chat_agent_session as fixture


class SourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_snapshot_keeps_a_large_input_queue_lazy_and_loads_only_selected_bodies(self):
        f = self.f
        inputs = [f.input(f"queued {number} " + "x" * 2000) for number in range(150)]
        index = f.session.index
        with patch.object(f.session.chat, "get_turn_content", side_effect=AssertionError("snapshot loaded user bodies")), \
                patch.object(index, "render", side_effect=AssertionError("snapshot rendered user bodies")), \
                patch.object(f.engine, "source_event", side_effect=AssertionError("snapshot reread old sources")):
            frozen = index.snapshot()
        self.assertEqual(len(frozen.events), len(inputs))
        self.assertEqual(index.users, {})
        self.assertEqual(index.rendered, {})

        later = f.input("arrived after snapshot")
        source = f.session.chat.get_turn_content
        loaded = []

        def selected_source(seq):
            self.assertIn(seq, {inputs[0], inputs[-1]}, "unselected queued body was read")
            loaded.append(seq)
            return source(seq)

        with patch.object(f.session.chat, "get_turn_content", side_effect=selected_source):
            self.assertEqual(frozen.events[0].event["input_id"], inputs[0])
            self.assertEqual(frozen.events[0].event["input_id"], inputs[0])
            self.assertEqual(frozen.events[-1].event["input_id"], inputs[-1])
        self.assertEqual(loaded, [inputs[0], inputs[-1]])
        self.assertEqual(set(index.users), {inputs[0], inputs[-1]})
        self.assertNotIn(later, [item.ref["seq"] for item in frozen.events])

    def test_quoted_old_text_stays_with_current_input_until_that_input_is_covered(self):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        text = "original answer " + "x" * 1000
        f.request_callback(lambda request, payload: f.end(request, payload,
            response_body=fixture.body("summary" if "tools" not in payload["body"] else text)))
        f.input("original question " + "q" * 800)
        old = f.run_answer()
        old_turn = f.session._turn(old)
        for number in range(11):
            f.input(f"question {number} " + "q" * 800)
            f.run_answer()
        index = f.session.index
        self.assertIsNotNone(index.snapshot().summary)
        self.assertGreater(index.covered[0], old_turn.terminal_seq)

        quoted = f.user.single_turn(turn_id="quote-old-answer",
            content=(fixture.TextPart("explain this answer"),), reply_to_seqs=[old_turn.terminal_seq])
        materialized = index.user(quoted)
        self.assertEqual(materialized["reply_context"][0]["text"], text)
        self.assertNotIn(old_turn.creation_seq, index.users)
        read = f.session.chat.get_turn_content

        def no_repeated_quote_load(seq):
            self.assertNotEqual(seq, quoted, "current quoted input was loaded again")
            return read(seq)

        with patch.object(f.session.chat, "get_turn_content", side_effect=no_repeated_quote_load):
            f.run_answer()
            self.assertEqual(index.user(quoted), materialized)
            self.assertEqual(index.user(quoted), materialized)
        self.assertIn(quoted, index.users)
        frozen = next(item for item in index.snapshot().events
                      if item.ref == {"type": "user", "seq": quoted})

        for number in range(20):
            f.input(f"advance summary {number} " + "q" * 800)
            f.run_answer()
            index.snapshot()
            if index.covered >= (quoted, 0):
                break
        self.assertGreaterEqual(index.covered, (quoted, 0))
        self.assertNotIn(quoted, index.users)
        # A frozen older request still has the same source, but does not put
        # this now-covered input back into the current context cache.
        self.assertEqual(index.user(quoted), materialized)
        self.assertEqual(frozen.event, materialized)
        self.assertNotIn(quoted, index.users)

    def test_reply_context_is_one_level_and_is_not_written_into_chat(self):
        f = self.f
        first = f.user.single_turn(turn_id="first", content=(fixture.TextPart("first body"),))
        second = f.user.single_turn(turn_id="second", content=(fixture.TextPart("second body"),),
                                    reply_to_seqs=[first])
        third = f.user.single_turn(turn_id="third", content=(fixture.TextPart("third body"),),
                                   reply_to_seqs=[second])
        second_event = f.session.index.user(second)
        third_event = f.session.index.user(third)
        self.assertEqual(second_event["reply_context"][0]["text"], "first body")
        self.assertEqual(third_event["reply_context"], [{"role": "user", "status": "completed",
            "text": "second body", "attachments": []}])
        stored = next(message for message in f.events.history if message.seq == third)
        payload = json.loads(stored.payload)
        self.assertEqual(payload["reply_to_seqs"], [second])
        self.assertEqual(payload["content"], [{"type": "text", "text": "third body"}])
        self.assertNotIn("reply_context", payload)


if __name__ == "__main__":
    unittest.main()
