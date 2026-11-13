"""Long model retry waits remain cancellable and leave model capacity available."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
import threading
import unittest
from unittest.mock import patch

from openevent.chat_sdk import TextPart, TurnRef

import test_chat_agent_scheduling as scheduling
import test_chat_agent_session as sessions


class ModelRetrySchedulingTests(unittest.TestCase):
    add_chat = scheduling.CooperativeSchedulingTests.add_chat
    running = scheduling.CooperativeSchedulingTests.running

    def setUp(self):
        scheduling.CooperativeSchedulingTests.setUp(self)
        self.fixture.config = replace(self.fixture.config, agent=replace(
            self.fixture.config.agent, model_retry_interval_ms=60000))

    @contextmanager
    def observe_retry_wait(self, engine):
        entered = threading.Event()
        wait_async = engine.wait_async

        async def observe(predicate, timeout=None):
            if (timeout is not None and timeout > 3
                    and engine.journal.select(kind="attempt.fail", channel=sessions.CHAT_CHANNEL)):
                # This callback runs after the real condition registers its
                # waiter and yields; the test then cancels, stops or adds work.
                asyncio.get_running_loop().call_soon(entered.set)
            return await wait_async(predicate, timeout)

        with patch.object(engine, "wait_async", observe):
            yield entered

    def requests(self):
        return self.events.messages("infer.request", sessions.MODEL_CHANNEL)

    def test_user_cancel_interrupts_retry_wait_without_a_second_request(self):
        self.fixture.request_callback(lambda request, payload: self.fixture.end(request, payload, failure=True))
        with self.running(model_capacity=1) as (engine, wait_until):
            with self.observe_retry_wait(engine) as waiting:
                self.fixture.user.single_turn(turn_id="cancel-during-retry", content=(TextPart("question"),))
                self.assertTrue(waiting.wait(3), "model retry did not enter the condition wait")
                answer, = engine.journal.select(kind="answer.prepare", channel=sessions.CHAT_CHANNEL)
                turn = engine.current_turn(sessions.CHAT_CHANNEL, sessions.AGENT, answer.data["turn_id"])
                self.fixture.user.cancel_turn(
                    target_turn=TurnRef(sessions.AGENT, answer.data["turn_id"]), start_seq=turn.creation_seq)
                wait_until(lambda: engine.sessions[sessions.CHAT_CHANNEL]._answer_finished(answer))
                self.assertIsNotNone(engine.journal.get(answer.key + ":cancel"))
                self.assertIsNone(engine.journal.get(answer.key + ":attempt:1"))
                self.assertIsNone(engine.journal.get(answer.key + ":freeze"))
                self.assertEqual(len(self.requests()), 1)

    def test_shutdown_interrupts_retry_wait_without_a_second_request(self):
        self.fixture.request_callback(lambda request, payload: self.fixture.end(request, payload, failure=True))
        with self.running(model_capacity=1) as (engine, wait_until):
            with self.observe_retry_wait(engine) as waiting:
                self.fixture.user.single_turn(turn_id="stop-during-retry", content=(TextPart("question"),))
                self.assertTrue(waiting.wait(3), "model retry did not enter the condition wait")
                engine.request_stop()
                wait_until(lambda: engine._closed)
                self.assertEqual(len(self.requests()), 1)
                self.assertEqual(len(engine.journal.select(kind="attempt.prepare", channel=sessions.CHAT_CHANNEL)), 1)
                self.assertFalse(engine.journal.select(kind="answer.freeze", channel=sessions.CHAT_CHANNEL))

    def test_retry_wait_leaves_the_single_model_slot_available_to_another_chat(self):
        other_user = self.add_chat(31)

        def respond(request, payload):
            content = json.loads(payload["body"]["messages"][1]["content"])
            text = [event["text"] for event in content["events"] if event["type"] == "user"][-1]
            if text == "retry later":
                self.fixture.end(request, payload, failure=True)
            else:
                self.fixture.end(request, payload, response_body=sessions.body("other chat finished"))

        self.fixture.request_callback(respond)
        with self.running(model_capacity=1) as (engine, wait_until):
            with self.observe_retry_wait(engine) as waiting:
                self.fixture.user.single_turn(turn_id="waiting-retry", content=(TextPart("retry later"),))
                self.assertTrue(waiting.wait(3), "model retry did not enter the condition wait")
                other_user.single_turn(turn_id="other-chat", content=(TextPart("quick reply"),))

                def other_chat_finished():
                    answers = engine.journal.select(kind="answer.prepare", channel=31)
                    return bool(answers) and engine.sessions[31]._answer_finished(answers[0])

                wait_until(other_chat_finished)
                self.assertEqual(len(self.requests()), 2)
                waiting_answer, = engine.journal.select(kind="answer.prepare", channel=sessions.CHAT_CHANNEL)
                self.assertFalse(engine.sessions[sessions.CHAT_CHANNEL]._answer_finished(waiting_answer))
                self.assertIsNone(engine.journal.get(waiting_answer.key + ":attempt:1"))
                self.assertEqual(len(engine.journal.select(kind="attempt.accept", channel=31)), 1)


if __name__ == "__main__":
    unittest.main()
