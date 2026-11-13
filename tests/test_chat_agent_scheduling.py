"""Real SDK workflows share bounded threads across independent conversations."""

from contextlib import contextmanager
from dataclasses import replace
import json
import threading
import time
import unittest

from openevent.chat_sdk import TextPart, TurnRef, create_client

from chat_model_agent.worker import ChatModelAgent
import test_chat_agent_session as sessions


class CooperativeSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sessions.ConversationSDKTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.stop_reader()
        self.events = self.fixture.events
        self.events.engine = None

    def add_chat(self, channel_id):
        channel = self.events.channel(channel_id, "chat.v1", [sessions.AGENT, sessions.USER])
        self.events.channels[channel_id] = channel
        self.events.publish_auto_seq(
            principal=0, token="", channel_id=channel_id, uuid=self.events.get_uuid(),
            payload=json.dumps({"kind": "system.channel_initialized", "data": {
                "schema_version": 1, "protocol": "chat.v1", "members": list(channel.members),
                "creator": channel.creator, "name": channel.name, "description": "",
                "visibility": "private"}}).encode())
        return create_client(self.events, principal=sessions.USER, token="user-token",
                             channel_id=channel_id, max_retries=0)

    @contextmanager
    def running(self, *, model_capacity=2):
        config = replace(self.fixture.config, agent=replace(
            self.fixture.config.agent, max_model_concurrency=model_capacity))
        engine = ChatModelAgent(config, self.events, clock=lambda: self.events.now)
        self.fixture.engine = engine
        self.events.engine = engine
        failures = []
        def run():
            try:
                engine.run()
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=run, daemon=True, name="test-agent-process")
        thread.start()
        def wait_until(predicate):
            deadline = time.monotonic() + 3
            ready = lambda: bool(failures) or engine.error is not None or predicate()
            with engine.condition:
                while not ready():
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, "conversation state did not advance")
                    # Session startup has no notification contract.
                    engine.condition.wait_for(ready, min(0.02, remaining))
            self.assertFalse(failures)
            self.assertIsNone(engine.error)
        try:
            wait_until(lambda: sessions.CHAT_CHANNEL in engine.started_sessions)
            yield engine, wait_until
        finally:
            engine.request_stop()
            thread.join(3)
            self.assertFalse(thread.is_alive(), "Agent shutdown did not complete")
            self.assertFalse(failures)

    def test_hundreds_of_idle_chats_do_not_add_threads(self):
        with self.running() as (engine, wait_until):
            baseline = {thread.ident for thread in threading.enumerate()}
            for channel_id in range(100, 220):
                self.add_chat(channel_id)
            wait_until(lambda: len(engine.started_sessions) == 121)
            self.assertEqual({thread.ident for thread in threading.enumerate()}, baseline)
            self.assertFalse(self.events.messages("infer.request", sessions.MODEL_CHANNEL))

    def test_model_and_tool_waits_leave_other_chats_running_and_one_chat_stays_serial(self):
        user_a = self.fixture.user
        user_b, user_c = self.add_chat(31), self.add_chat(32)
        requests = {}
        def respond(message, payload):
            if message.channel_id != sessions.MODEL_CHANNEL or payload["kind"] != "infer.request":
                return
            text = [event["text"] for event in json.loads(payload["body"]["messages"][1]["content"])["events"] if event["type"] == "user"][-1]
            requests[text] = message, payload
            if text == "waiting tool":
                call = {"id": "pending-command", "type": "function", "function": {
                    "name": "exec", "arguments": json.dumps({"command": "printf pending"})}}
                self.fixture.end(message, payload, response_body={"choices": [{"index": 0,
                    "message": {"role": "assistant", "content": "working", "tool_calls": [call]},
                    "finish_reason": "tool_calls"}]})
            elif text in {"quick reply", "queued input"}:
                self.fixture.end(message, payload, response_body=sessions.body(text + " done"))
        self.events.on_publish = respond
        with self.running() as (engine, wait_until):
            def finished(channel):
                session = engine.sessions.get(channel)
                return [] if session is None else [answer for answer in
                    engine.journal.select(kind="answer.prepare", channel=channel)
                    if session._answer_finished(answer)]
            first = user_a.single_turn(turn_id="a-first", content=(TextPart("waiting model"),))
            wait_until(lambda: "waiting model" in requests)
            user_b.single_turn(turn_id="b-first", content=(TextPart("waiting tool"),))
            wait_until(lambda: bool(self.events.messages("cmd.run.request", sessions.CMD_CHANNEL)))
            second = user_a.single_turn(turn_id="a-second", content=(TextPart("queued input"),))
            user_c.single_turn(turn_id="c-first", content=(TextPart("quick reply"),))
            wait_until(lambda: bool(finished(32)))
            self.assertNotIn("queued input", requests)
            self.assertFalse(finished(30))
            self.assertFalse(finished(31))
            request, payload = requests["waiting model"]
            self.fixture.end(request, payload, response_body=sessions.body("first done"))
            wait_until(lambda: len(finished(30)) == 2)
            answers = engine.journal.select(kind="answer.prepare", channel=30)
            self.assertEqual([record.data["reply_to_seqs"] for record in answers], [[first], [second]])
            self.assertEqual(len(self.events.messages("cmd.run.request", sessions.CMD_CHANNEL)), 1)

    def test_chat_waiting_for_model_capacity_can_be_cancelled_without_releasing_capacity(self):
        queued_user = self.add_chat(31)
        with self.running(model_capacity=1) as (engine, wait_until):
            self.fixture.user.single_turn(turn_id="occupy-model", content=(TextPart("hold model"),))
            wait_until(lambda: len(self.events.messages("infer.request", sessions.MODEL_CHANNEL)) == 1)
            queued_user.single_turn(turn_id="waiting-for-model", content=(TextPart("queue model"),))
            wait_until(lambda: bool(engine.journal.select(kind="answer.prepare", channel=31)))
            answer, = engine.journal.select(kind="answer.prepare", channel=31)
            wait_until(lambda: engine.current_turn(31, sessions.AGENT, answer.data["turn_id"]) is not None)
            queued_user.cancel_turn(target_turn=TurnRef(sessions.AGENT, answer.data["turn_id"]),
                start_seq=engine.current_turn(31, sessions.AGENT, answer.data["turn_id"]).creation_seq)
            wait_until(lambda: bool(engine.journal.select(kind="answer.cancel", channel=31)))
            self.assertEqual(len(self.events.messages("infer.request", sessions.MODEL_CHANNEL)), 1)
            self.assertFalse(self.events.messages("infer.cancel", sessions.MODEL_CHANNEL))
