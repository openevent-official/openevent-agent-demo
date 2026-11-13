"""Publication confirmation does not fetch bodies already read by the shared reader."""

import json
import unittest
from unittest.mock import patch

import test_chat_agent_session as fixture
import test_chat_agent_v3_flow as flow


class PublicationConfirmationTests(unittest.TestCase):
    def test_short_reply_only_loads_the_frozen_input_body(self):
        f = flow.EventFlowTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.input()
        f.request_callback(lambda request, payload: f.end(
            request, payload, response_body=fixture.body("answer")))

        with patch.object(f.engine, "source_event", wraps=f.engine.source_event) as source:
            answer = f.run_answer()

        self.assertEqual(f.visible_turn(fixture.AGENT, answer.data["turn_id"]).text, "answer")
        self.assertEqual(len(f.events.messages("infer.request", fixture.MODEL_CHANNEL)), 1)
        # The frozen input is deliberately materialized once. WAL and business
        # publication confirmation must not add any source-body round trips.
        source.assert_called_once_with(fixture.WAL_CHANNEL, answer.seq)
        self.assertEqual(json.loads(next(message.payload for message in f.events.history
                                        if message.seq == answer.seq))["kind"], "answer.prepare")


if __name__ == "__main__":
    unittest.main()
