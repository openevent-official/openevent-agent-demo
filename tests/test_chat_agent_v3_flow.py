"""End-to-end event scheduling, admission and compaction with real module SDKs."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
import unittest

from openevent.cmd_sdk import CmdOutput, CmdRunResultInput
from openevent.chat_sdk import TurnRef

from async_helpers import SyncProxy
from chat_model_agent.budget import INPUT_REJECTION, body_size, failure_reason, history_size
from chat_model_agent.errors import FailureCode
from chat_model_agent.session import Conversation
import test_chat_agent_session as fixture


class EventFlowTests(unittest.TestCase):
    # Share only fixture operations; do not inherit tests for the old workflow.
    setUp = fixture.ConversationSDKTests.setUp
    stop_reader = fixture.ConversationSDKTests.stop_reader
    fresh = fixture.ConversationSDKTests.fresh
    restart = fixture.ConversationSDKTests.restart
    advance = fixture.ConversationSDKTests.advance
    input = fixture.ConversationSDKTests.input
    head = fixture.ConversationSDKTests.head
    append = fixture.ConversationSDKTests.append
    end = fixture.ConversationSDKTests.end
    visible_turn = fixture.ConversationSDKTests.visible_turn
    request_callback = fixture.ConversationSDKTests.request_callback

    def records(self, kind):
        return self.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind=kind)

    def finished_answers(self):
        return [answer for answer in self.records("answer.prepare") if self.session._answer_finished(answer)]

    def requests(self):
        return self.events.messages("infer.request", fixture.MODEL_CHANNEL)

    @staticmethod
    def model_input(request):
        return json.loads(json.loads(request.payload)["body"]["messages"][1]["content"])

    def run_answer(self, prepared=None):
        prepared = prepared or self.session._next_answer()
        self.assertIsNotNone(prepared)
        child = SyncProxy(self.session._child(prepared), self.async_runner)
        child._run_answer(prepared)
        child._finish_answer()
        return prepared

    def seed_large_history(self):
        self.fresh(context_max_input_bytes=12000)
        text = "x" * (self.session.budget.target - 600)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=fixture.body(text)))
        self.input(text)
        prepared = self.run_answer()
        self.assertTrue(self.session._answer_finished(prepared))
        return text

    @contextmanager
    def running(self):
        # Use production waiting for background concurrency; the deterministic
        # fixture's one-shot advance callback is for direct calls only.
        self.session.wait = Conversation.wait.__get__(self.session._target, Conversation)

        async def start():
            return asyncio.create_task(self.session.run())

        task = self.async_runner.run(start())

        def until(predicate):
            with self.engine.condition:
                self.assertTrue(self.engine.condition.wait_for(
                    lambda: predicate() or self.engine.error is not None, 3),
                    "conversation did not reach the expected state")
            if self.engine.error is not None:
                raise self.engine.error

        try:
            yield until
        finally:
            self.engine.request_stop()

            async def finish():
                await asyncio.wait_for(task, 3)

            self.async_runner.run(finish())

    def test_overlong_input_is_rejected_while_an_earlier_model_call_is_running(self):
        pending = []
        self.request_callback(lambda request, payload: pending.append((request, payload)))
        first = self.input("A")
        with self.running() as until:
            until(lambda: len(pending) == 1)
            oversized = self.input("B" * self.session.budget.target * 2)
            until(lambda: bool(self.records("input.reject")))
            until(lambda: bool(self.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)))
            self.assertEqual(len(self.requests()), 1)
            self.assertEqual(len(self.records("answer.prepare")), 1)
            self.assertEqual(self.records("input.reject")[0].data["input_seq"], oversized)
            notice, = self.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)
            payload = json.loads(notice.payload)
            self.assertEqual(payload["reply_to_seqs"], [oversized])
            self.assertEqual(payload["content"], [{"type": "text", "text": INPUT_REJECTION}])
            self.end(*pending[0], response_body=fixture.body("A finished"))
            until(lambda: bool(self.finished_answers()))
            self.assertEqual(self.records("answer.prepare")[0].data["reply_to_seqs"], [first])
            self.assertEqual(self.records("answer.freeze"), [])
            self.assertEqual(self.events.messages("infer.cancel", fixture.MODEL_CHANNEL), [])

    def test_running_tool_does_not_block_a_new_user_and_its_result_triggers_another_call(self):
        model_inputs, command_requests = [], []

        def publish(request, payload):
            if request.channel_id == fixture.MODEL_CHANNEL and payload["kind"] == "infer.request":
                model_inputs.append(self.model_input(request))
                if len(model_inputs) == 1:
                    response = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                        "role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
                            "function": {"name": "exec", "arguments": json.dumps({"command": "sleep 1"})}}]}}]}
                else:
                    response = fixture.body(f"answer {len(model_inputs)}")
                self.end(request, payload, response_body=response)
            elif request.channel_id == fixture.CMD_CHANNEL and payload["kind"] == "cmd.run.request":
                command_requests.append(request)

        self.events.on_publish = publish
        first = self.input("start a command")
        with self.running() as until:
            until(lambda: len(command_requests) == 1)
            second = self.input("continue talking while it runs")
            until(lambda: len(model_inputs) == 2)
            until(lambda: len(self.finished_answers()) == 1)
            answers = self.records("answer.prepare")
            self.assertEqual(answers[0].data["reply_to_seqs"], [first])
            self.assertEqual(answers[1].data["reply_to_seqs"], [second])
            self.assertFalse(self.session._answer_finished(answers[0]))
            self.assertIn("tool_call", [event["type"] for event in model_inputs[1]["events"]])
            self.assertNotIn("tool_result", [event["type"] for event in model_inputs[1]["events"]])
            command = command_requests[0]
            self.cmd.publish_run_result(fixture.CMD, "worker-token", fixture.CMD_CHANNEL, fixture.AGENT,
                CmdRunResultInput(prev_seq=command.seq, status="SUCCESS", timeout_ms=1000,
                    exit_code=0,
                    stdout=CmdOutput("stdout", 0, "utf-8", ""), stderr=CmdOutput("stderr", 0, "utf-8", "")))
            until(lambda: len(model_inputs) == 3)
            until(lambda: len(self.finished_answers()) == 3)
            self.assertEqual(len(command_requests), 1)
            self.assertEqual(self.records("answer.prepare")[2].data["reply_to_seqs"], [first])
            result, = [event for event in model_inputs[2]["events"] if event["type"] == "tool_result"]
            call, = [event for event in model_inputs[1]["events"] if event["type"] == "tool_call"]
            self.assertEqual(result["task_id"], call["task_id"])
            turn = self.visible_turn(fixture.AGENT, answers[0].data["turn_id"])
            self.assertEqual(turn.text, "Tool processing has ended.")

    def test_compression_covers_only_old_events_and_makes_room_for_the_next_user(self):
        text = self.seed_large_history()
        next_user = self.input(text)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=fixture.body(
            "preserved history" if "tools" not in payload["body"] else "new answer")))
        prepared = self.run_answer()
        commits = self.records("compaction.commit")
        self.assertEqual(len(commits), 1)
        self.assertEqual(prepared.data["summary"], commits[0].key)
        compression = self.model_input(self.requests()[1])
        normal = self.model_input(self.requests()[2])
        self.assertNotIn(next_user, [event.get("input_id") for event in compression["events"]])
        self.assertEqual(normal["summary"], "preserved history")
        self.assertEqual([event["input_id"] for event in normal["events"] if event["type"] == "user"], [next_user])
        self.assertLessEqual(history_size("preserved history", []), self.session.budget.target)
        for request in self.requests():
            self.assertLessEqual(body_size(json.loads(request.payload)["body"]), self.session.budget.limit)

    def test_cancelling_an_older_tool_reply_does_not_cancel_the_new_model_call(self):
        held, inputs = [], []

        def publish(request, payload):
            if request.channel_id == fixture.CMD_CHANNEL and payload["kind"] == "cmd.kill.request":
                self.cmd.publish_run_result(fixture.CMD, "worker-token", fixture.CMD_CHANNEL, fixture.AGENT,
                    CmdRunResultInput(prev_seq=payload["target_seq"], status="KILLED", timeout_ms=1000,
                        stdout=CmdOutput("stdout", 0, "utf-8", ""), stderr=CmdOutput("stderr", 0, "utf-8", "")))
                return
            if request.channel_id != fixture.MODEL_CHANNEL or payload["kind"] != "infer.request":
                return
            inputs.append(self.model_input(request))
            if len(inputs) == 1:
                response = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "tool_calls": [{"id": "run", "type": "function",
                        "function": {"name": "exec", "arguments": '{"command":"sleep 30"}'}}]}}]}
                self.end(request, payload, response_body=response)
            elif len(inputs) == 2:
                held.append((request, payload))
            else:
                self.end(request, payload, response_body=fixture.body("after cancellation"))

        self.events.on_publish = publish
        first = self.input("run a command")
        with self.running() as until:
            until(lambda: bool(self.events.messages("cmd.run.request", fixture.CMD_CHANNEL)))
            old = self.records("answer.prepare")[0]
            self.input("independent new question")
            until(lambda: bool(held))
            self.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, old.data["turn_id"]),
                                  start_seq=self.session._turn(old).creation_seq)
            until(lambda: self.session._answer_finished(old))
            self.assertEqual(len(self.events.messages("cmd.kill.request", fixture.CMD_CHANNEL)), 1)
            self.assertEqual(self.events.messages("infer.cancel", fixture.MODEL_CHANNEL), [])
            self.end(*held[0], response_body=fixture.body("new answer survives"))
            until(lambda: len(self.finished_answers()) == 2)
            self.assertEqual(len(self.requests()), 2)
            self.input("next question")
            until(lambda: len(self.finished_answers()) == 3)
            cancellation, = [event for event in inputs[-1]["events"] if event["type"] == "user_cancelled"]
            self.assertEqual(cancellation["target_answer_id"], old.data["turn_id"])
            self.assertEqual(cancellation["target_input_ids"], [first])
            self.assertEqual(len(self.requests()), 3)

    def test_committed_summary_survives_a_crash_without_repeating_compression(self):
        text = self.seed_large_history()
        self.input(text)

        def crash_after_summary(request, payload):
            if request.channel_id == fixture.MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(request, payload, response_body=fixture.body("saved summary"))
            elif request.channel_id == fixture.WAL_CHANNEL and payload["kind"] == "compaction.commit":
                raise fixture.Crash()

        self.events.on_publish = crash_after_summary
        with self.assertRaises(fixture.Crash):
            self.session._next_answer()
        committed, = self.records("compaction.commit")
        self.events.on_publish = None
        self.restart()
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=fixture.body("recovered")))
        prepared = self.run_answer()
        self.assertEqual(prepared.data["summary"], committed.key)
        self.assertEqual(len(self.records("compaction.prepare")), 1)
        self.assertEqual(len(self.requests()), 3)
        self.assertEqual(self.model_input(self.requests()[-1])["summary"], "saved summary")

    def test_compression_keeps_working_when_the_first_summary_leaves_history_over_target(self):
        self.fresh(context_max_input_bytes=12000)
        text = "x" * (self.session.budget.target - 600)
        response = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": text, "tool_calls": [{"id": "long-command", "type": "function",
                "function": {"name": "exec", "arguments": json.dumps({"command": text})}}]}}]}
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=response))
        self.input(text)
        self.run_answer()
        compressed = []

        def complete(request, payload):
            if "tools" not in payload["body"]:
                compressed.append(self.model_input(request))
                value = "s" * (self.session.budget.target - 50) if len(compressed) == 1 else "small summary"
            else:
                value = "new answer"
            self.end(request, payload, response_body=fixture.body(value))

        self.request_callback(complete)
        self.input("new input")
        prepared = self.run_answer()
        self.assertEqual(len(compressed), 2)
        commits = self.records("compaction.commit")
        self.assertEqual(len(commits), 2)
        self.assertEqual(prepared.data["summary"], commits[-1].key)
        self.assertEqual(compressed[1]["summary"], "s" * (self.session.budget.target - 50))
        batches = self.records("compaction.prepare")
        self.assertEqual(batches[1].data["range"]["after"], batches[0].data["range"]["through"])
        self.assertEqual([event["type"] for event in compressed[1]["events"]], ["tool_call"])
        self.assertEqual(self.model_input(self.requests()[-1])["summary"], "small summary")
        self.assertTrue(all(body_size(json.loads(request.payload)["body"]) <= self.session.budget.limit
                            for request in self.requests()))

    def test_failed_compression_reports_its_cause_and_restart_does_not_retry_the_paused_input(self):
        text = self.seed_large_history()
        self.input(text)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=fixture.body(" ")))
        failed_answer = self.run_answer()
        self.assertEqual(len(self.requests()), 3)  # Seed ordinary request and two compression attempts.
        self.assertIsNotNone(self.engine.journal.get(failed_answer.key + ":freeze"))
        self.assertTrue(self.session._answer_finished(failed_answer))
        self.assertIsNone(self.session._turn(failed_answer))
        notices = self.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)
        self.assertEqual(len(notices), 1)
        self.assertIn("History compression", json.loads(notices[0].payload)["content"][0]["text"])
        self.assertEqual(len(self.events.messages("turn.start", fixture.CHAT_CHANNEL, fixture.AGENT)), 1)
        self.restart()
        self.assertIsNone(self.session._next_answer())
        self.assertEqual(len(self.requests()), 3)
        self.input("too large" * self.session.budget.target)
        self.assertIsNone(self.session._next_answer())
        self.assertEqual(len(self.records("input.reject")), 1)
        self.assertEqual(self.records("answer.wake"), [])
        self.assertEqual(len(self.requests()), 3)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=fixture.body(
            "successful summary" if "tools" not in payload["body"] else "retried")))
        self.input("try again")
        self.run_answer()
        self.assertEqual(len(self.records("answer.wake")), 1)
        self.assertEqual(len(self.records("compaction.prepare")), 2)

    def test_local_failure_recovery_keeps_frozen_budget_and_never_starts_a_model_request(self):
        text = self.seed_large_history()
        self.input(text)

        def crash_after_local_prepare(request, payload):
            if request.channel_id == fixture.MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(request, payload, response_body=fixture.body(" "))
            elif (request.channel_id == fixture.WAL_CHANNEL and payload["kind"] == "answer.prepare"
                  and payload["key"].endswith(":answer:1")):
                raise fixture.Crash()

        self.events.on_publish = crash_after_local_prepare
        with self.assertRaises(fixture.Crash):
            self.session._next_answer()
        prepared = self.records("answer.prepare")[-1]
        self.assertEqual(len(self.requests()), 3)
        self.assertEqual(prepared.data["settings"]["context_max_input_bytes"], 12000)
        self.events.on_publish = None
        self.restart(context_max_input_bytes=24000)
        resumed = self.run_answer()
        self.assertEqual(resumed, prepared)
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertTrue(self.session._answer_finished(prepared))
        self.assertIsNone(self.session._turn(prepared))
        self.assertEqual(len(self.requests()), 3)
        self.assertEqual(len(self.events.messages("turn.start", fixture.CHAT_CHANNEL, fixture.AGENT)), 1)
        failed = self.engine.journal.get(prepared.key + ":attempt:0:fail")
        self.assertFalse(failed.data["retryable"])
        self.assertIn("History compression", failed.data["reason"])

    def test_recovered_unsent_compression_reports_local_failure_without_stopping_agent(self):
        original_config = self.config
        for crash_kind in ("compaction.prepare", "effect.prepare"):
            with self.subTest(crash_kind=crash_kind):
                self.config = original_config
                self.seed_large_history()
                self.input("next question")

                def crash_after_prepare(message, payload):
                    if (message.channel_id == fixture.WAL_CHANNEL and payload["kind"] == crash_kind
                            and ":compaction:" in payload["key"]):
                        raise fixture.Crash()

                self.events.on_publish = crash_after_prepare
                with self.assertRaises(fixture.Crash):
                    self.session._next_answer()
                batch, = self.records("compaction.prepare")
                intent = self.engine.journal.get(batch.key + ":attempt:0:request:prepare")
                self.assertEqual(intent is not None, crash_kind == "effect.prepare")
                requests = len(self.requests())
                self.events.on_publish = None
                self.config = replace(self.config, openevent=replace(self.config.openevent, max_payload_bytes=5000))
                self.restart()
                with self.running() as until:
                    until(lambda: bool(self.records("answer.freeze")))
                    self.assertIsNone(self.engine.error)
                    self.assertFalse(self.engine.stop.is_set())
                    failed = self.engine.journal.get(batch.key + ":attempt:0:fail")
                    self.assertFalse(failed.data["retryable"])
                    self.assertEqual(failed.data["source_seq"], batch.seq)
                    self.assertEqual(failed.data["reason"], "The history compression request exceeds the size limit")
                    self.assertEqual(len(self.requests()), requests)
                    self.assertFalse(self.events.messages("infer.cancel", fixture.MODEL_CHANNEL))
                    self.assertFalse(self.records("compaction.commit"))
                    notice, = self.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)
                    self.assertIn(failed.data["reason"], json.loads(notice.payload)["content"][0]["text"])

    def test_successful_compression_does_not_lend_its_old_error_to_a_later_failure(self):
        text = self.seed_large_history()
        self.input(text)
        compression_attempts = []

        def complete(request, payload):
            if "tools" not in payload["body"]:
                compression_attempts.append(request.seq)
                answer = " " if len(compression_attempts) == 1 else "s" * 3000
            else:
                answer = "ready"
            self.end(request, payload, response_body=fixture.body(answer))

        self.request_callback(complete)
        successful = self.run_answer()
        self.assertTrue(self.session._answer_finished(successful))
        self.assertEqual(len(self.records("compaction.commit")), 1)
        old_failure, = self.records("attempt.fail")
        self.assertEqual(old_failure.data["reason"], "History compression returned an invalid summary")

        requests = len(self.requests())
        self.events.on_publish = None
        self.config = replace(self.config, openevent=replace(self.config.openevent, max_payload_bytes=5000))
        self.restart()
        self.session._check_recovery_budget()
        self.input("continue")
        prepared = self.run_answer()
        failed = self.engine.journal.get(prepared.key + ":attempt:0:fail")
        self.assertEqual(failed.data["reason"], failure_reason(FailureCode.REQUEST_TOO_LARGE))
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertEqual(len(self.requests()), requests)
        notice, = self.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)
        self.assertIn(failed.data["reason"], json.loads(notice.payload)["content"][0]["text"])

    def test_completed_fallback_does_not_lend_its_compression_error_to_a_later_failure(self):
        self.fresh(context_max_input_bytes=12000)
        text = "x" * (self.session.budget.target // 2 + 300)
        self.input(text)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=fixture.body(text)))
        self.run_answer()
        self.input("continue")
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=fixture.body(
            " " if "tools" not in payload["body"] else "ready")))
        fallback = self.run_answer()
        self.assertTrue(self.session._answer_finished(fallback))
        self.assertEqual(len(self.records("attempt.fail")), 2)
        self.assertFalse(self.records("compaction.commit"))
        self.assertFalse(self.records("answer.freeze"))

        requests = len(self.requests())
        self.events.on_publish = None
        self.config = replace(self.config, openevent=replace(self.config.openevent, max_payload_bytes=2000))
        self.restart()
        self.session._check_recovery_budget()
        self.input("continue again")
        prepared = self.run_answer()
        failed = self.engine.journal.get(prepared.key + ":attempt:0:fail")
        self.assertEqual(failed.data["reason"], failure_reason(FailureCode.REQUEST_TOO_LARGE))
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertEqual(len(self.requests()), requests)
        notice, = self.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)
        self.assertIn(failed.data["reason"], json.loads(notice.payload)["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
