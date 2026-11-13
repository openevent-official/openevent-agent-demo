"""Model retries respect their configured delay without changing frozen input."""

import asyncio
from contextlib import contextmanager
import json
import time
import unittest
from unittest.mock import patch

import test_chat_agent_session as support
import test_chat_agent_v3_flow as flow


class ModelRetryTests(unittest.TestCase):
    def setUp(self):
        self.f = flow.EventFlowTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    @contextmanager
    def advance_retry_timers(self):
        f = self.f
        waits = []
        original = f.engine.wait_async

        async def wait(predicate, timeout=None):
            if timeout is None:
                return await original(predicate, timeout)
            self.assertTrue(f.records("attempt.fail"))
            self.assertFalse(predicate())
            waits.append(timeout)
            f.events.now += round(timeout * 1000)
            return False

        with patch.object(f.engine, "wait_async", side_effect=wait):
            yield waits

    def test_answer_and_compaction_use_the_same_interval_after_failure(self):
        f = self.f
        for purpose in ("answer", "compaction"):
            with self.subTest(purpose=purpose):
                if purpose == "compaction":
                    f.seed_large_history()
                else:
                    f.fresh()
                f.restart(model_retry_interval_ms=2500)
                f.input("next question")
                requests = []

                def respond(request, payload):
                    requests.append(request)
                    f.end(request, payload, failure=len(requests) == 1,
                          response_body=None if len(requests) == 1 else support.body("finished"))

                f.request_callback(respond)
                with self.advance_retry_timers() as waits:
                    prepared = f.session._next_answer()
                    if purpose == "answer":
                        f.run_answer(prepared)
                self.assertEqual(waits, [2.5])
                self.assertEqual(len(requests), 2)
                failed, = f.records("attempt.fail")
                failure_time = f.engine.event_header(support.WAL_CHANNEL, failed.seq).ts_ms
                self.assertEqual(requests[1].ts_ms - failure_time, 2500)
                first_body, second_body = [json.loads(request.payload)["body"] for request in requests]
                self.assertEqual(first_body, second_body)

    def test_recovery_uses_remaining_time_and_current_configuration(self):
        f = self.f
        for elapsed, interval, expected in ((600, 2000, [1.4]), (2500, 2000, []), (600, 1000, [0.4])):
            with self.subTest(elapsed=elapsed, interval=interval):
                f.fresh(model_retry_interval_ms=5000)
                f.input()

                def crash_after_failure(message, payload):
                    if payload["kind"] == "infer.request":
                        f.end(message, payload, failure=True)
                    elif payload["kind"] == "attempt.fail":
                        raise support.Crash()

                f.events.on_publish = crash_after_failure
                with self.assertRaises(support.Crash):
                    f.run_answer()
                f.events.on_publish = None
                f.events.now += elapsed
                f.restart(model_retry_interval_ms=interval)
                f.request_callback(lambda request, payload: f.end(
                    request, payload, response_body=support.body("recovered")))
                with self.advance_retry_timers() as waits:
                    f.run_answer()
                self.assertEqual(waits, expected)
                self.assertEqual(len(f.requests()), 2)
                self.assertEqual([record.data["number"] for record in f.records("attempt.prepare")], [0, 1])

    def test_prepared_retry_resumes_without_waiting_again_when_interval_increases(self):
        f = self.f
        f.fresh(model_retry_interval_ms=1000)
        f.input()

        def crash_after_preparing_retry(message, payload):
            if payload["kind"] == "infer.request":
                f.end(message, payload, failure=True)
            elif payload["kind"] == "attempt.prepare" and payload["data"]["number"] == 1:
                raise support.Crash()

        f.events.on_publish = crash_after_preparing_retry
        with self.advance_retry_timers() as waits:
            with self.assertRaises(support.Crash):
                f.run_answer()
        self.assertEqual(waits, [1.0])
        self.assertEqual(len(f.requests()), 1)
        f.events.on_publish = None
        f.events.now += 100
        f.restart(model_retry_interval_ms=60000)
        f.request_callback(lambda request, payload: f.end(
            request, payload, response_body=support.body("prepared retry recovered")))
        with self.advance_retry_timers() as waits:
            f.run_answer()
        self.assertEqual(waits, [])
        self.assertEqual(len(f.requests()), 2)
        self.assertEqual([record.data["number"] for record in f.records("attempt.prepare")], [0, 1])

    def test_success_nonretryable_failure_and_last_attempt_do_not_wait(self):
        f = self.f
        for outcome in ("success", "nonretryable", "exhausted"):
            with self.subTest(outcome=outcome):
                f.fresh(model_retry_interval_ms=60000, max_model_attempts=1 if outcome == "exhausted" else 3)
                f.input()
                response = ({"error": {"code": "context_length_exceeded"}} if outcome == "nonretryable"
                            else support.body("done"))
                f.request_callback(lambda request, payload: f.end(
                    request, payload, failure=outcome == "exhausted", response_body=response))
                with self.advance_retry_timers() as waits:
                    f.run_answer()
                self.assertEqual(waits, [])
                self.assertEqual(len(f.requests()), 1)
                self.assertEqual(bool(f.records("answer.freeze")), outcome != "success")

    def test_unrelated_notifications_cannot_shorten_a_real_retry_wait(self):
        f = self.f
        f.restart(model_retry_interval_ms=100)
        f.input()
        requests, failures, timers = [], [], []

        def respond(message, payload):
            if payload["kind"] == "infer.request":
                requests.append(time.monotonic())
                f.end(message, payload, failure=len(requests) == 1,
                      response_body=None if len(requests) == 1 else support.body("done"))
            elif payload["kind"] == "attempt.fail":
                failures.append(time.monotonic())

        async def notify_repeatedly():
            for _ in range(5):
                await asyncio.sleep(0.01)
                with f.engine.condition:
                    f.engine.condition.notify_all()

        original = f.engine.wait_async

        async def wait(predicate, timeout=None):
            if timeout is not None:
                timers.append(timeout)
                asyncio.create_task(notify_repeatedly())
            return await original(predicate, timeout)

        f.events.on_publish = respond
        with patch.object(f.engine, "wait_async", side_effect=wait):
            f.run_answer()
        self.assertEqual(timers, [0.1])
        self.assertEqual(len(requests), 2)
        self.assertGreaterEqual(requests[1] - failures[0], 0.095)


if __name__ == "__main__":
    unittest.main()
