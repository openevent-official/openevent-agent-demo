"""Conversation integration tests using real module SDKs and in-memory events."""
from collections import UserDict
from contextlib import contextmanager
from dataclasses import replace
import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import grpc

from async_helpers import AsyncRunner, SyncProxy, wait_for_reader

from openevent.chat_sdk import ObjectKey, TextPart, TurnRef, TurnWriter, create_client as create_chat
from openevent.cmd_sdk import CmdOutput, CmdOutputReadResultInput, CmdRunResultInput, create_client as create_cmd
from openevent.model_proxy_sdk import (
    InferAppendInput, InferEndInput, InferRequestInput, InferResultInput, create_client as create_model,
    publish_infer_append, publish_infer_end, publish_infer_request, publish_infer_result,
)
from openevent.sdk import openevent_pb2

from chat_model_agent.config import parse_config
from chat_model_agent.prompt import CompletionAccumulator
from chat_model_agent.rpc import InfrastructureError
from chat_model_agent.session import Conversation, FAILURE_NOTICE, ModelResponse, PLACEHOLDER
from chat_model_agent.state import HistoryError
from chat_model_agent.worker import ChatModelAgent, Stopped


BUILD = Path(__file__).resolve().parents[1] / "build" / "tests"
AGENT, USER, PROVIDER, CMD = 10, 11, 12, 13
MODEL_CHANNEL, CMD_CHANNEL, WAL_CHANNEL, CHAT_CHANNEL = 20, 21, 22, 30


class Crash(BaseException):
    """Simulated process death after a server commit, before local handoff."""


class MemoryRpcError(grpc.RpcError):
    def __init__(self, status):
        self.status = status

    def code(self):
        return self.status


class MemoryEvents:
    def __init__(self):
        self.timeout_ms = 10000
        self.now = 1_790_000_000_000
        self.history = []
        self.next_uuid = 100
        self.engine = None
        self.on_publish = None
        self.confirm_publications = True
        self.fetch_calls = []
        self.lock = threading.RLock()
        self.channels = {
            MODEL_CHANNEL: self.channel(MODEL_CHANNEL, "llm.v1", [AGENT, PROVIDER]),
            CMD_CHANNEL: self.channel(CMD_CHANNEL, "cmd.v3", [AGENT, CMD]),
            WAL_CHANNEL: self.channel(WAL_CHANNEL, "agent.wal.v5", [AGENT]),
            CHAT_CHANNEL: self.channel(CHAT_CHANNEL, "chat.v1", [AGENT, USER], creator=USER,
                                       name="chat-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        }
        for channel in self.channels.values():
            self.history.append(openevent_pb2.EventMessage(
                seq=len(self.history) + 1, uuid=self.get_uuid(), channel_id=channel.channel_id,
                principal=0, ts_ms=self.now,
                payload=json.dumps({"kind": "system.channel_initialized", "data": {
                    "schema_version": 1, "protocol": channel.protocol, "members": list(channel.members),
                    "creator": channel.creator, "name": channel.name, "description": channel.description,
                    "visibility": "private"}}).encode()))

    @staticmethod
    def channel(channel_id, protocol, members, *, creator=AGENT, name="aux"):
        return openevent_pb2.ChannelInfo(channel_id=channel_id, protocol=protocol, members=members,
                                         creator=creator, name=name, visibility=openevent_pb2.VISIBILITY_PRIVATE)

    def get_uuid(self):
        with self.lock:
            self.next_uuid += 1
            return self.next_uuid

    def close(self):
        pass

    def get_seq_by_uuid(self, uuid):
        with self.lock:
            for message in self.history:
                if message.uuid == uuid:
                    return message.seq
        raise MemoryRpcError(grpc.StatusCode.NOT_FOUND)

    def get_status(self, principal, token):
        return SimpleNamespace(max_seq=len(self.history))

    def get_channel(self, principal, token, channel_id):
        return SimpleNamespace(channel=self.channels[channel_id])

    def list_channels(self, principal, token, filter=None):
        return SimpleNamespace(channels=list(self.channels.values()))

    def fetch(self, principal, token, from_seq, limit, channels=(), only_my_recipient=False):
        self.fetch_calls.append((from_seq, tuple(channels), only_my_recipient))
        eligible = [message for message in self.history
                    if message.seq >= from_seq and (not channels or message.channel_id in channels)]
        page = eligible[:limit]
        next_seq = page[-1].seq + 1 if len(eligible) > len(page) else len(self.history) + 1
        return openevent_pb2.FetchResponse(messages=page, next_seq=next_seq, last_seq=len(self.history))

    def publish_auto_seq(self, *, principal, token, channel_id, uuid, payload,
                         recipients=(), object_keys=(), **kwargs):
        with self.lock:
            if any(message.uuid == uuid for message in self.history):
                raise MemoryRpcError(grpc.StatusCode.ALREADY_EXISTS)
            message = openevent_pb2.EventMessage(
                seq=len(self.history) + 1, uuid=uuid, channel_id=channel_id, principal=principal,
                ts_ms=self.now, payload=payload, recipients=recipients, object_keys=object_keys,
            )
            self.history.append(message)
        if self.engine is not None and self.confirm_publications:
            # Use the same independent Fetch reader as production. Keeping this
            # fixture deterministic must never project a publish directly.
            wait_for_reader(self.engine, message.seq)
        if self.on_publish:
            self.on_publish(message, json.loads(payload))
        return SimpleNamespace(seq=message.seq)

    def messages(self, kind, channel=None, principal=None):
        return [message for message in self.history
                if (channel is None or message.channel_id == channel)
                and (principal is None or message.principal == principal)
                and json.loads(message.payload).get("kind") == kind]

    def start_seq(self, principal, turn_id):
        return next(message.seq for message in self.history
                    if message.principal == principal
                    and (payload := json.loads(message.payload)).get("kind") in {"turn.single", "turn.start"}
                    and payload["turn_id"] == turn_id)


def body(text="answer", *, refusal=None):
    message = {"role": "assistant", "content": text}
    if refusal is not None:
        message["refusal"] = refusal
    return {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}


def chunk(text, *, done=False):
    return {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop" if done else None}]}


class ConversationSDKTests(unittest.TestCase):
    def setUp(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=BUILD)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config = parse_config({
            "version": "chat-agent.v1", "deployment_id": "session-test",
            "openevent": {"target": "events:9527"},
            "agent": {"principal": AGENT, "user_principal": USER, "token": "agent-token",
                      "system_prompt": "help", "context_compression_prompt": "compress", "context_max_input_bytes": 1048576, "model": "model", "work_root": str(root / "work"),
                      "idle_sleep_ms": 1, "model_retry_interval_ms": 1},
            "model_proxy": {"principal": PROVIDER}, "cmd_worker": {"principal": CMD},
            "channels": {"model": MODEL_CHANNEL, "cmd": CMD_CHANNEL, "wal": WAL_CHANNEL},
            "rpc": {"max_retries": 0},
        })
        self.async_runner = AsyncRunner()
        self.addCleanup(self.async_runner.close)
        self.fresh()

    def stop_reader(self):
        engine = getattr(self, "engine", None)
        if engine is not None:
            self.async_runner.run(engine.aclose())
            if engine.reader_thread is not None:
                engine.reader_thread.join(1)
                self.assertFalse(engine.reader_thread.is_alive())

    def fresh(self, **agent_settings):
        self.stop_reader()
        self.events = MemoryEvents()
        self.user = create_chat(self.events, principal=USER, token="user-token", channel_id=CHAT_CHANNEL, max_retries=0)
        self.provider = create_model(self.events, "provider-token", max_retries=0)
        self.cmd = create_cmd(self.events)
        self.user_count = 0
        self.waiting = []
        self.restart(**agent_settings)

    def assert_source_reads_are_targeted(self):
        for seq, channels, recipients in self.events.fetch_calls:
            self.assertFalse(recipients)
            if channels:
                self.assertEqual(len(channels), 1)
                self.assertGreater(seq, 0)
                self.assertIn(channels[0], self.events.channels)

    def restart(self, **agent_settings):
        self.stop_reader()
        self.config = replace(self.config, agent=replace(self.config.agent, **agent_settings))
        self.events.engine = None
        self.engine = ChatModelAgent(self.config, self.events, clock=lambda: self.events.now)
        self.engine.recover()
        idle_wait = self.engine.reader_stop.wait
        polling = patch.object(self.engine.reader_stop, "wait", side_effect=lambda timeout: idle_wait(min(timeout, 0.001)))
        polling.start()
        self.addCleanup(polling.stop)
        self.engine._start_reader()
        self.addCleanup(self.stop_reader)
        self.events.engine = self.engine
        self.session = SyncProxy(Conversation(self.engine, CHAT_CHANNEL), self.async_runner)
        self.session.wait = self.advance

    async def advance(self, timeout=None, *, ready=None):
        if not self.waiting:
            self.fail("conversation waited without a scheduled provider/user event")
        self.waiting.pop(0)()

    def input(self, text="question", *, start=False, object_keys=()):
        self.user_count += 1
        method = self.user.start_turn if start else self.user.single_turn
        return method(turn_id=f"user-{self.user_count}", content=(TextPart(text),) if text else (),
                      object_keys=object_keys)

    def run_answer(self):
        prepared = self.session._next_answer()
        self.assertIsNotNone(prepared)
        self.session._run_answer(prepared)
        return prepared

    def run_calls(self):
        """Drive this answer and result-triggered continuations; retain later users."""
        prepared = None
        for _ in range(30):
            self.session._reconcile()
            owners = self.engine.journal.select(channel=self.session.cid, kind="answer.prepare")
            for owner in owners:
                self.session.answer = owner
                if self.session.cancelled():
                    self.session._cancel_answer()
                self.session._finish_answer()
                if self.session.cancelled():
                    self.session._cancel_answer()
            for tool in self.engine.journal.select(channel=self.session.cid, kind="tool.prepare"):
                if self.engine.journal.get(tool.data["tool_key"] + ":done") is None:
                    self.session._tool_step(tool)
            following = self.session._next_answer()
            if following is None:
                return prepared or (owners[-1] if owners else None)
            prepared = following
            self.session._run_answer(prepared)
            if self.engine.journal.get(prepared.key + ":freeze"):
                return prepared
            executed = False
            for tool in self.engine.journal.select(channel=self.session.cid, kind="tool.prepare"):
                if self.engine.journal.get(tool.data["tool_key"] + ":done") is None:
                    self.session._tool_step(tool)
                    executed = True
            self.session._finish_answer()
            if self.session.cancelled():
                self.session._cancel_answer()
            if not executed:
                return prepared
        self.fail("fixture produced more than 30 consecutive tool turns")

    def new_inputs(self, record):
        record = self.engine.journal.materialize(record)
        earlier = self.engine.journal.select(channel=record.chat_channel_id, kind="answer.prepare", through=record.seq - 1)
        after = earlier[-1].data["input_through"]["seq"] if earlier else 0
        return [ref["seq"] for ref in record.data["events"] if ref["type"] == "user" and ref["seq"] > after]

    def request_callback(self, operation):
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                operation(message, payload)
        self.events.on_publish = callback

    def head(self, request, payload):
        return publish_infer_result(self.provider, MODEL_CHANNEL, PROVIDER, AGENT,
                                    InferResultInput(stream_id=payload["stream_id"], prev_seq=request.seq, status_code=200))

    def append(self, request, payload, previous, content):
        return publish_infer_append(self.provider, MODEL_CHANNEL, PROVIDER, AGENT,
                                    InferAppendInput(stream_id=payload["stream_id"], request_seq=request.seq,
                                                     prev_seq=previous, body=content))

    def end(self, request, payload, *, response_body=None, failure=False):
        fields = {"stream_id": payload["stream_id"], "request_seq": request.seq,
                  "status_code": 500 if failure else 200, "end_status": "failed" if failure else "completed"}
        if response_body is not None:
            fields["body"] = response_body
        elif failure:
            fields["body"] = {"error": {"code": "PROVIDER_ERROR", "message": "provider failed"}}
        return publish_infer_end(self.provider, MODEL_CHANNEL, PROVIDER, AGENT, InferEndInput(**fields))

    def visible_turn(self, principal, turn_id):
        """Inspect output for tests without restoring a production history projection."""
        turn = self.engine.chats[CHAT_CHANNEL].turns[(principal, turn_id)]
        if turn.terminal_seq is not None:
            content = self.session.chat.get_turn_content(turn.terminal_seq)
            text = "".join(part.text for part in content.content)
            keys = [attachment.object_key for attachment in content.attachments]
        else:
            # A streaming turn has no SDK terminal read yet. Assert its current
            # emitted content using the effective positions from the live state.
            sources = {event.seq: event for event in self.events.history}
            text = "".join(part["text"] for seq in turn.content_seqs
                           for part in json.loads(sources[seq].payload).get("content", ()))
            keys = [key for seq in turn.content_seqs for key in sources[seq].object_keys]
        return SimpleNamespace(**{**vars(turn), "text": text, "object_keys": keys})

    def visible_turns(self):
        return [self.visible_turn(*key) for key in self.engine.chats[CHAT_CHANNEL].turns]

    def answer_turns(self):
        return [turn for turn in self.visible_turns()
                if turn.principal == AGENT and not turn.text.startswith("Processing failed: ")]

    def finished_answers(self):
        return [answer for answer in self.engine.journal.select(channel=self.session.cid, kind="answer.prepare")
                if self.session._answer_finished(answer)]

    @contextmanager
    def background_session(self):
        def wait_until(predicate):
            with self.engine.condition:
                self.assertTrue(self.engine.condition.wait_for(predicate, 1), "business state did not advance")
        async def start():
            return asyncio.create_task(self.session.run())
        task = self.async_runner.run(start())
        try:
            yield wait_until
        finally:
            self.engine.request_stop()
            async def finish():
                await asyncio.wait_for(task, 1)
            self.async_runner.run(finish())
            self.assertIsNone(self.engine.error)

    @contextmanager
    def observe_business_wait(self, callback):
        wait = self.session.wait
        async def observe(timeout=None, *, ready=None):
            if ready is None or not ready():
                callback(timeout)
            return await wait(timeout, ready=ready)
        with patch.object(self.session, "wait", side_effect=observe):
            yield

    def test_idle_session_does_not_rescan_history_for_other_channels_or_notifications(self):
        other_channel = CHAT_CHANNEL + 1
        self.events.channels[other_channel] = self.events.channel(other_channel, "chat.v1", [AGENT, USER])
        self.engine._register(other_channel, chat=True)
        other_user = create_chat(self.events, principal=USER, token="user-token",
                                 channel_id=other_channel, max_retries=0)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body()))
        with patch.object(self.engine, "pending_inputs", wraps=self.engine.pending_inputs) as pending, \
                patch.object(self.engine.journal, "select", wraps=self.engine.journal.select) as select, \
                self.background_session() as wait_until:
            wait_until(lambda: pending.call_count > 0)
            other_user.single_turn(turn_id="other", content=(TextPart("other conversation"),))
            self.assertFalse(self.events.messages("infer.request", MODEL_CHANNEL))
            with self.engine.condition:
                self.engine.condition.notify_all()
            self.assertFalse(self.events.messages("infer.request", MODEL_CHANNEL))
            self.input()
            wait_until(lambda: bool(self.finished_answers()))
            self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
            self.assertEqual(len(self.finished_answers()), 1)

    def test_input_arriving_between_idle_check_and_wait_is_processed(self):
        next_answer = self.session._next_answer
        inserted = []
        async def input_after_check():
            prepared = await next_answer()
            if prepared is None and not inserted:
                inserted.append(self.input("arrived during idle check"))
            return prepared
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body()))
        with patch.object(self.session, "_next_answer", side_effect=input_after_check), \
                self.background_session() as wait_until:
            wait_until(lambda: bool(self.finished_answers()))
            self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
            prepared, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")
            self.assertEqual(self.new_inputs(prepared), inserted)
            self.assertTrue(self.session._answer_finished(prepared))

    def test_scheduling_preserves_input_watermark_when_more_input_arrives_before_prepare(self):
        first = self.input("first input")
        added = []
        put = self.session.put
        def input_before_prepare(kind, key, data):
            if kind == "answer.prepare" and not added:
                added.append(self.input("next input"))
            return put(kind, key, data)
        with patch.object(self.session.chat, "get_turn_content", wraps=self.session.chat.get_turn_content) as reads, \
                patch.object(self.session, "put", side_effect=input_before_prepare):
            prepared = self.session._next_answer()
        self.assertEqual([call.args[0] for call in reads.call_args_list], [first])
        self.assertEqual(self.new_inputs(prepared), [first])
        self.assertEqual(prepared.data["snapshot"]["read_through_seq"], first)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body()))
        self.session._run_answer(prepared)
        following = self.session._next_answer()
        self.assertEqual(self.new_inputs(following), added)
        self.assertGreaterEqual(following.data["snapshot"]["read_through_seq"], added[0])

    def test_recovered_frozen_session_wakes_and_processes_queued_input_without_waiting_again(self):
        self.restart(max_model_attempts=1)
        self.input("original")
        queued = []
        def fail(request, payload):
            queued.append(self.input("queued before failure"))
            self.end(request, payload, failure=True)
        self.request_callback(fail)
        failed = self.run_calls()
        self.events.on_publish = None
        self.restart()
        queued_while_running = []
        def complete(request, payload):
            if not queued_while_running:
                queued_while_running.append(self.input("queued during recovery answer"))
            self.end(request, payload, response_body=body())
        self.request_callback(complete)
        with self.background_session() as wait_until:
            self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
            self.input("")
            self.assertIsNone(self.engine.journal.get(failed.key + ":wake"))
            fresh = self.input("resume")
            wait_until(lambda: len(self.finished_answers()) == 3)
            answers = self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")
            self.assertEqual([self.new_inputs(record) for record in answers[1:]],
                             [[queued[0], fresh], queued_while_running])
            self.assertTrue(all(self.session._answer_finished(record) for record in answers[1:]))
            self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 3)


    def test_restart_admits_queued_input_once_at_each_call_boundary(self):
        # Interrupt either the committed Chat end before its effect handoff or
        # the next answer's preparation, then recover the exact queued inputs.
        for crash_at in ("turn.end", "answer.prepare"):
            with self.subTest(crash_at=crash_at):
                self.fresh()
                first = self.input("A")
                queued, captured = [], []
                call = {"id": "first", "type": "function", "function": {
                    "name": "exec", "arguments": '{"command":"true"}'}}
                response = {"choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "working", "tool_calls": [call]},
                    "finish_reason": "tool_calls"}]}

                def publish(message, payload):
                    kind = payload["kind"]
                    if kind == "infer.request":
                        self.end(message, payload, response_body=response)
                    elif kind == "cmd.run.request":
                        queued.append(self.input("B"))
                        self.cmd.publish_run_result(CMD, "cmd-token", CMD_CHANNEL, AGENT,
                            CmdRunResultInput(prev_seq=message.seq, status="SUCCESS", exit_code=0,
                                timeout_ms=300000,
                                stdout=CmdOutput(file="out", bytes=0, content_encoding="utf-8", content=""),
                                stderr=CmdOutput(file="err", bytes=0, content_encoding="utf-8", content="")))
                    elif kind == crash_at and queued:
                        raise Crash()

                self.events.on_publish = publish
                with self.assertRaises(Crash):
                    self.run_calls()
                self.events.on_publish = None
                self.restart()
                late = self.input("C")
                def finish(request, payload):
                    captured.append(payload["body"]["messages"])
                    self.end(request, payload, response_body=body("done"))
                self.request_callback(finish)
                recovered = self.run_answer()
                expected = queued if crash_at == "answer.prepare" else [*queued, late]
                self.assertEqual(self.new_inputs(recovered), expected)
                self.assertEqual(recovered.data["reply_to_seqs"], expected)
                self.assertEqual([item["role"] for item in captured[0]],
                                 ["system", "user"])
                if crash_at == "answer.prepare":
                    following = self.run_answer()
                    self.assertEqual(self.new_inputs(following), [late])
                self.assertEqual([seq for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")
                                  for seq in self.new_inputs(record)], [first, *queued, late])
                self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)
                self.assertIsNone(self.session._next_answer())
                self.restart()
                self.assertIsNone(self.session._next_answer())

    def test_restart_recovers_committed_chat_end_without_a_completion_record(self):
        self.input()
        def publish(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, response_body=body("finished"))
            elif message.channel_id == CHAT_CHANNEL and payload["kind"] == "turn.end":
                raise Crash()
        self.events.on_publish = publish
        with self.assertRaises(Crash):
            self.run_calls()
        answer, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")
        self.assertIsNone(self.engine.journal.get(answer.key + ":end:commit"))
        turn, = self.answer_turns()
        self.assertEqual((turn.text, turn.status), ("finished", "completed"))
        self.events.on_publish = None
        self.restart()
        self.run_calls()
        self.assertTrue(self.session._answer_finished(answer))
        self.assertEqual(self.engine.journal.get(answer.key + ":end:commit").data["seq"], turn.terminal_seq)
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("turn.end", CHAT_CHANNEL, AGENT)), 1)
        self.assertIsNone(self.session._next_answer())


    def test_business_publish_can_return_before_reader_projects_it(self):
        self.events.confirm_publications = False
        source = self.input()
        self.async_runner.run(self.engine.aread_through(CHAT_CHANNEL, source))
        self.session.wait = Conversation.wait.__get__(self.session)
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body()))
        entered, published, release = threading.Event(), threading.Event(), threading.Event()
        ingest, publish = self.engine.ingest, self.events.publish_auto_seq
        failures = []
        def delayed_projection(message):
            if message.channel_id == CHAT_CHANNEL and json.loads(message.payload)["kind"] == "turn.start":
                entered.set()
                release.wait(1)
            return ingest(message)
        def observe_publish(**kwargs):
            result = publish(**kwargs)
            if kwargs["channel_id"] == CHAT_CHANNEL and json.loads(kwargs["payload"])["kind"] == "turn.start":
                published.set()
            return result
        def run():
            try:
                self.run_calls()
            except Exception as error:
                failures.append(error)
        with patch.object(self.engine, "ingest", side_effect=delayed_projection), \
                patch.object(self.events, "publish_auto_seq", side_effect=observe_publish):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(published.wait(1))
                self.assertTrue(entered.wait(1))
                start, = self.events.messages("turn.start", CHAT_CHANNEL, AGENT)
                self.assertNotIn(start.seq, self.engine.messages)
                release.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
            finally:
                release.set()
                thread.join(1)
        self.assertFalse(failures)
        self.assertEqual(len(self.finished_answers()), 1)
        self.assertEqual(self.answer_turns()[0].text, "answer")
        self.assert_source_reads_are_targeted()

    def test_agent_messages_are_not_inputs_and_references_preserve_roles(self):
        previous_user = self.user.start_turn(turn_id="user-comment", content=(TextPart("user comment"),)).complete()
        own_seq = self.session.chat.single_turn(turn_id="own-comment", content=(TextPart("agent comment"),))
        self.assertIsNone(self.session._next_answer())
        user_seq = self.user.single_turn(turn_id="question-with-references", content=(TextPart("compare these"),),
                                        reply_to_seqs=[previous_user, own_seq])
        requests = []
        def respond(request, payload):
            requests.append(payload["body"])
            self.end(request, payload, response_body=body())
        self.request_callback(respond)
        prepared = self.run_calls()
        self.assertEqual(self.new_inputs(prepared), [user_seq])
        event = json.loads(requests[0]["messages"][-1]["content"])["events"][0]
        self.assertEqual([reference["role"] for reference in event["reply_context"]], ["user", "assistant"])
        self.assertEqual([reference["text"] for reference in event["reply_context"]],
                         ["user comment", "agent comment"])

    def test_stream_replaces_placeholder_then_appends_without_repeating_prefix(self):
        self.input()
        def streaming(request, payload):
            head = self.head(request, payload)
            first = self.append(request, payload, head, chunk("hel"))
            def finish():
                self.append(request, payload, first, chunk("lo", done=True))
                self.end(request, payload)
            self.waiting.append(finish)
        self.request_callback(streaming)
        prepared = self.run_calls()
        turn, = self.answer_turns()
        self.assertEqual((turn.text, turn.status), ("hello", "completed"))
        self.assertEqual(len(self.events.messages("turn.start", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(len(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(len(self.events.messages("turn.append", CHAT_CHANNEL, AGENT)), 1)
        self.assertTrue(self.session._answer_finished(prepared))

    def test_stream_processes_events_received_during_display_without_waiting(self):
        self.fresh(idle_sleep_ms=10000)
        self.session.wait = Conversation.wait.__get__(self.session)
        self.input()
        source = {}
        def publish(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                previous = self.head(message, payload)
                source.update(request=message, payload=payload,
                              previous=self.append(message, payload, previous, chunk("hel")))
            elif message.channel_id == CHAT_CHANNEL and payload["kind"] == "turn.reset":
                self.append(source["request"], source["payload"], source["previous"], chunk("lo", done=True))
                self.end(source["request"], source["payload"])
        self.events.on_publish = publish
        def unexpected_wait(timeout):
            self.fail("already received model events must be processed before waiting")
        with self.observe_business_wait(unexpected_wait):
            self.run_calls()
        turn, = self.answer_turns()
        self.assertEqual((turn.text, turn.status), ("hello", "completed"))

    def test_small_stream_batch_uses_its_own_timer_with_long_idle_delay(self):
        self.fresh(idle_sleep_ms=10000)
        self.session.wait = Conversation.wait.__get__(self.session)
        self.input()
        source = {}
        def publish(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                previous = self.head(message, payload)
                source.update(request=message, payload=payload,
                              previous=self.append(message, payload, previous, chunk("hel")))
            elif message.channel_id == CHAT_CHANNEL and payload["kind"] == "turn.reset":
                self.append(source["request"], source["payload"], source["previous"], chunk("lo"))
            elif message.channel_id == CHAT_CHANNEL and payload["kind"] == "turn.append":
                self.end(source["request"], source["payload"], response_body=body("hello"))
        self.events.on_publish = publish
        waits = []
        def timed_wait(timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, .05)
            waits.append(timeout)
        with self.observe_business_wait(timed_wait):
            self.run_calls()
        self.assertTrue(waits)
        turn, = self.answer_turns()
        self.assertEqual((turn.text, turn.status), ("hello", "completed"))
        self.assertEqual(len(self.events.messages("turn.append", CHAT_CHANNEL, AGENT)), 1)

    def test_waiting_for_model_capacity_is_woken_by_cancellation(self):
        self.fresh(idle_sleep_ms=10000, max_model_concurrency=1)
        self.session.wait = Conversation.wait.__get__(self.session)
        self.input()
        self.assertTrue(self.engine.model_slots.acquire(blocking=False))
        try:
            with self.background_session() as wait_until:
                wait_until(lambda: bool(self.events.messages("turn.start", CHAT_CHANNEL, AGENT)))
                answer = self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")[-1]
                self.user.cancel_turn(target_turn=TurnRef(AGENT, answer.data["turn_id"]), start_seq=self.events.start_seq(AGENT, answer.data["turn_id"]))
                wait_until(lambda: bool(self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.cancel")))
                self.assertFalse(self.events.messages("infer.request", MODEL_CHANNEL))
                self.assertEqual(len(self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.cancel")), 1)
        finally:
            self.engine.model_slots.release()

    def test_stream_chunks_are_accumulated_once_across_idle_waits_and_display(self):
        self.input()
        def streaming(request, payload):
            head = self.head(request, payload)
            first = self.append(request, payload, head, chunk("hel"))
            def finish():
                self.append(request, payload, first, chunk("lo", done=True))
                self.end(request, payload)
            self.waiting.extend([lambda: None, lambda: None, finish])
        self.request_callback(streaming)
        with patch.object(CompletionAccumulator, "add_chunk", autospec=True,
                          side_effect=CompletionAccumulator.add_chunk) as add_chunk:
            self.run_calls()
        self.assertEqual(add_chunk.call_count, 2)
        self.assertEqual([call.args[1] for call in add_chunk.call_args_list],
                         [chunk("hel"), chunk("lo", done=True)])
        turn, = self.answer_turns()
        self.assertEqual((turn.text, turn.status), ("hello", "completed"))
        self.assertEqual(len(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(len(self.events.messages("turn.append", CHAT_CHANNEL, AGENT)), 1)

    def test_stream_batches_do_not_revisit_confirmed_writes_or_scan_channel_history(self):
        for count in (16, 32, 64):
            with self.subTest(batches=count):
                self.fresh()
                self.input()
                fragment_visits = [0]
                class CountedFragments(list):
                    def __iter__(self):
                        for fragment in super().__iter__():
                            fragment_visits[0] += 1
                            yield fragment
                    def __getitem__(self, index):
                        result = super().__getitem__(index)
                        return CountedFragments(result) if isinstance(index, slice) else result
                completion = ModelResponse.completion
                def track_fragments(response, **kwargs):
                    accumulator = completion(response, **kwargs)
                    if not isinstance(response.fragments, CountedFragments):
                        response.fragments = CountedFragments(response.fragments)
                    return accumulator
                def streaming(request, payload):
                    previous = self.head(request, payload)
                    previous = self.append(request, payload, previous, chunk("x" * 4096))
                    def next_chunk():
                        nonlocal previous
                        previous = self.append(request, payload, previous, chunk("x" * 4096))
                    self.waiting.extend([next_chunk] * (count - 1))
                    self.waiting.append(lambda: self.end(
                        request, payload, response_body=body("x" * (4096 * count))))
                self.request_callback(streaming)
                with patch.object(self.session, "_chat_effect", wraps=self.session._chat_effect) as effect, \
                        patch.object(self.engine.journal, "select", wraps=self.engine.journal.select) as select, \
                        patch.object(self.engine, "events_in", wraps=self.engine.events_in) as events_in, \
                        patch.object(ModelResponse, "completion", track_fragments):
                    self.run_calls()
                batches = [call for call in effect.call_args_list if ":text:" in call.args[0]]
                restores = [call for call in select.call_args_list
                            if call.kwargs.get("prefix", "").endswith(":text:")]
                self.assertEqual(len(batches), count)
                self.assertEqual(len(restores), 1)
                events_in.assert_not_called()
                # Each fragment contributes once to its batch and once to the
                # final display comparison, not to every subsequent batch.
                self.assertLessEqual(fragment_visits[0], count * 2)
                turn, = self.answer_turns()
                self.assertEqual((turn.text, turn.status), ("x" * (4096 * count), "completed"))
                self.assertEqual(len(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)), 1)
                self.assertEqual(len(self.events.messages("turn.append", CHAT_CHANNEL, AGENT)), count - 1)

    def test_restart_after_stream_batch_publish_restores_event_position_and_preserves_boundaries(self):
        self.input()
        part = "界" * 1365 + "x"  # Exactly 4096 UTF-8 bytes, fewer Unicode characters.
        def streaming(request, payload):
            previous = self.head(request, payload)
            previous = self.append(request, payload, previous, chunk(part))
            def next_chunk():
                nonlocal previous
                previous = self.append(request, payload, previous, chunk(part))
            self.waiting.extend([next_chunk] * 5)
            self.waiting.append(lambda: self.end(request, payload, response_body=body(part * 6)))
        def crash_after_append(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                streaming(message, payload)
            if message.channel_id == CHAT_CHANNEL and message.principal == AGENT and payload["kind"] == "turn.append":
                raise Crash()
        self.events.on_publish = crash_after_append
        with self.assertRaises(Crash):
            self.run_calls()
        batches = self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.prepare", prefix=self.session.display_prefix)
        fragments = self.events.messages("infer.append", MODEL_CHANNEL)
        self.assertEqual([(record.data["source"]["start_seq"], record.data["source"]["end_seq"])
                          for record in batches], [(item.seq, item.seq) for item in fragments])
        self.assertIsNone(self.engine.journal.get(batches[-1].key.removesuffix(":prepare") + ":commit"))
        self.events.on_publish = None
        self.restart()
        with patch.object(self.session, "_chat_effect", wraps=self.session._chat_effect) as effect:
            self.run_calls()
        visits = [call for call in effect.call_args_list if ":text:" in call.args[0]]
        self.assertEqual(len(visits), 6)
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)), 1)
        appends = self.events.messages("turn.append", CHAT_CHANNEL, AGENT)
        self.assertEqual(len(appends), 5)
        self.assertTrue(all(json.loads(message.payload)["content"][0]["text"] == part for message in appends))
        turn, = self.answer_turns()
        self.assertEqual((turn.text, turn.status), (part * 6, "completed"))

    def test_complete_json_without_append_is_displayed_once(self):
        self.input()
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body("full JSON")))
        self.run_calls()
        turn, = self.answer_turns()
        self.assertEqual((turn.text, turn.status), ("full JSON", "completed"))
        self.assertEqual(len(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)), 1)
        self.assertFalse(self.events.messages("turn.append", CHAT_CHANNEL, AGENT))

    def test_large_fragments_stay_whole_and_batches_end_before_the_next_fragment_exceeds_target(self):
        self.input()
        parts = ["界" * 2000, "a" * 2000, "b" * 2200, " \n\t", "文" * 1700]
        sequences = []
        def streaming(request, payload):
            previous = self.head(request, payload)
            previous = self.append(request, payload, previous, chunk(parts[0]))
            sequences.append(previous)
            def finish():
                nonlocal previous
                for index, part in enumerate(parts[1:], 1):
                    previous = self.append(request, payload, previous, chunk(part, done=index == len(parts) - 1))
                    sequences.append(previous)
                self.end(request, payload)
            self.waiting.append(finish)
        self.request_callback(streaming)
        self.run_calls()
        writes = sorted(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)
                        + self.events.messages("turn.append", CHAT_CHANNEL, AGENT), key=lambda item: item.seq)
        self.assertEqual([json.loads(item.payload)["content"][0]["text"] for item in writes],
                         [parts[0], parts[1], parts[2] + parts[3], parts[4]])
        records = [record for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.prepare")
                   if (record.data.get("source") or {}).get("type") == "model"]
        self.assertEqual([(record.data["source"]["start_seq"], record.data["source"]["end_seq"])
                          for record in records], [(sequences[0], sequences[0]), (sequences[1], sequences[1]),
                                                  (sequences[2], sequences[3]), (sequences[4], sequences[4])])
        for record in records:
            self.assertEqual(set(record.data["source"]), {"type", "attempt", "field", "start_seq", "end_seq"})
        self.assertEqual(self.answer_turns()[0].text, "".join(parts))
        accept, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept")
        self.assertGreater(accept.seq, max(self.engine.journal.get(record.key.removesuffix(":prepare") + ":commit").seq
                                           for record in records))

    def test_content_range_ignores_metadata_and_other_streams_and_recovers_whole_text(self):
        other_stream = "other-stream"
        other_seq = publish_infer_request(self.session.model, MODEL_CHANNEL, AGENT,
                                         InferRequestInput(stream_id=other_stream, method="POST",
                                                           path="/v1/chat/completions", body={}))
        other_request = self.events.history[other_seq - 1]
        other_payload = json.loads(other_request.payload)
        other_head = self.head(other_request, other_payload)
        self.input()
        sources = []
        metadata = []
        def streaming(request, payload):
            previous = self.head(request, payload)
            previous = self.append(request, payload, previous, chunk("prefix"))
            def finish():
                nonlocal previous
                previous = self.append(request, payload, previous, chunk("left"))
                sources.append(previous)
                previous = self.append(request, payload, previous, {"choices": []})
                metadata.append(previous)
                self.append(other_request, other_payload, other_head, chunk("must not appear"))
                previous = self.append(request, payload, previous, chunk("right", done=True))
                sources.append(previous)
                self.end(request, payload)
            self.waiting.append(finish)
        self.request_callback(streaming)
        self.run_calls()
        records = [record for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.prepare")
                   if record.data.get("kind") == "turn.append"]
        batch, = records
        self.assertEqual((batch.data["source"]["start_seq"], batch.data["source"]["end_seq"]), tuple(sources))
        self.events.on_publish = None
        self.restart()
        self.assertEqual(self.session._source_text(batch.data["source"]), "leftright")
        for boundary in ("start_seq", "end_seq"):
            with self.subTest(boundary=boundary):
                invalid = {**batch.data["source"], boundary: metadata[0]}
                with self.assertRaisesRegex(HistoryError, "endpoints"):
                    self.session._source_text(invalid)
        self.assertEqual(self.answer_turns()[0].text, "prefixleftright")

    def test_new_small_fragments_do_not_restart_the_batch_timer(self):
        self.input()
        now = [0.0]
        observed = []
        source = {}
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                previous = self.head(message, payload)
                source.update(request=message, payload=payload,
                              previous=self.append(message, payload, previous, chunk("first")))
                def add_at(moment, text):
                    now[0] = moment
                    source["previous"] = self.append(message, payload, source["previous"], chunk(text))
                self.waiting.extend([lambda: add_at(0.0, "a"), lambda: add_at(.02, "b"),
                                     lambda: now.__setitem__(0, .05)])
            elif message.channel_id == CHAT_CHANNEL and payload["kind"] == "turn.append":
                observed.append((now[0], payload["content"][0]["text"]))
                self.end(source["request"], source["payload"], response_body=body("firstab"))
        self.events.on_publish = callback
        self.session.wait = lambda timeout=None, ready=None: None if ready and ready() else self.advance(timeout, ready=ready)
        with patch("chat_model_agent.session.time", SimpleNamespace(monotonic=lambda: now[0])):
            self.run_calls()
        self.assertEqual(observed, [(.05, "ab")])

    def test_prepared_batch_keeps_its_event_range_when_new_fragments_arrive_during_restart(self):
        self.input()
        source = {}
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                previous = self.head(message, payload)
                previous = self.append(message, payload, previous, chunk("first"))
                source.update(request=message, payload=payload, previous=previous)
                def add_batch():
                    source["previous"] = self.append(message, payload, source["previous"], chunk("b" * 2000))
                    source["previous"] = self.append(message, payload, source["previous"], chunk("c" * 2096))
                self.waiting.append(add_batch)
            if (message.channel_id == WAL_CHANNEL and payload["kind"] == "effect.prepare"
                    and payload["data"]["kind"] == "turn.append"):
                raise Crash()
        self.events.on_publish = callback
        with self.assertRaises(Crash):
            self.run_calls()
        prepared = next(record for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.prepare")
                        if record.data.get("kind") == "turn.append")
        original_source = dict(prepared.data["source"])
        self.assertFalse(self.events.messages("turn.append", CHAT_CHANNEL, AGENT))
        self.events.on_publish = None
        tail = self.append(source["request"], source["payload"], source["previous"], chunk("last", done=True))
        self.end(source["request"], source["payload"])
        self.restart()
        self.run_calls()
        self.assertEqual(self.engine.journal.get(prepared.key).data["source"], original_source)
        writes = self.events.messages("turn.append", CHAT_CHANNEL, AGENT)
        self.assertEqual([json.loads(item.payload)["content"][0]["text"] for item in writes],
                         ["b" * 2000 + "c" * 2096, "last"])
        latest = [record for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.prepare")
                  if record.data.get("kind") == "turn.append"][-1]
        self.assertEqual((latest.data["source"]["start_seq"], latest.data["source"]["end_seq"]), (tail, tail))
        self.assert_source_reads_are_targeted()

    def test_final_display_is_one_unit_and_recovers_before_accept_without_repeating(self):
        for crash_at in ("prepare", "publish", "commit", "accept"):
            with self.subTest(crash_at=crash_at):
                self.fresh()
                self.input()
                text = "界" * 2000
                def callback(message, payload):
                    if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                        self.end(message, payload, response_body=body(text))
                    crash = ((crash_at == "publish" and message.channel_id == CHAT_CHANNEL and payload["kind"] == "turn.reset")
                             or (message.channel_id == WAL_CHANNEL and
                                 ((crash_at in {"prepare", "commit"} and payload["key"].endswith(":final:" + crash_at))
                                  or (crash_at == "accept" and payload["kind"] == "attempt.accept"))))
                    if crash:
                        raise Crash()
                self.events.on_publish = callback
                with self.assertRaises(Crash):
                    self.run_calls()
                final = next(record for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.prepare")
                             if record.key.endswith(":final:prepare"))
                source = dict(final.data["source"])
                self.events.on_publish = None
                self.restart()
                self.run_calls()
                writes = self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)
                self.assertEqual(len(writes), 1)
                self.assertEqual(json.loads(writes[0].payload)["content"][0]["text"], text)
                self.assertFalse(self.events.messages("turn.append", CHAT_CHANNEL, AGENT))
                self.assertEqual(self.engine.journal.get(final.key).data["source"], source)
                request, = self.events.messages("infer.request", MODEL_CHANNEL)
                terminal, = self.events.messages("infer.end", MODEL_CHANNEL)
                self.assertEqual((source["field"], source["start_seq"], source["end_seq"]),
                                 ("display", request.seq, terminal.seq))
                accepted, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept")
                commit = self.engine.journal.get(final.key.removesuffix(":prepare") + ":commit")
                self.assertGreater(accepted.seq, commit.seq)

    def test_display_committed_before_crash_is_accepted_after_original_deadline(self):
        self.restart(model_timeout_ms=1000, max_model_attempts=1)
        self.input()
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, response_body=body("displayed before restart"))
            if message.channel_id == WAL_CHANNEL and payload["key"].endswith(":final:commit"):
                raise Crash()
        self.events.on_publish = callback
        with self.assertRaises(Crash):
            self.run_calls()
        request, = self.events.messages("infer.request", MODEL_CHANNEL)
        self.events.now = request.ts_ms + 2000
        self.events.on_publish = None
        self.restart()
        prepared = self.run_calls()
        accepted, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept")
        terminal, = self.events.messages("infer.end", MODEL_CHANNEL)
        self.assertEqual(accepted.data["terminal_seq"], terminal.seq)
        self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail"))
        self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.timeout"))
        self.assertIsNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertTrue(self.session._answer_finished(prepared))
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(self.answer_turns()[0].text, "displayed before restart")



    def test_selected_acceptance_survives_rpc_queue_passing_deadline(self):
        self.restart(model_timeout_ms=1000, max_model_attempts=1)
        self.input()
        call = {"id": "work", "type": "function", "function": {
            "name": "exec", "arguments": json.dumps({"command": "printf unexpected"})}}
        response = {"choices": [{"index": 0, "message": {
            "role": "assistant", "content": "displayed before queueing", "tool_calls": [call]},
            "finish_reason": "tool_calls"}]}
        arm, queued, release = threading.Event(), threading.Event(), threading.Event()
        entered = [threading.Event() for _ in range(self.engine.rpc_pool.size)]
        blockers = []
        get_uuid, submit = self.events.get_uuid, self.engine.rpc_pool.submit
        def occupy_worker(ready):
            ready.set()
            self.assertTrue(release.wait(3), "test did not release the RPC workers")
        def occupy_after_accept_uuid():
            value = get_uuid()
            if arm.is_set() and not blockers:
                # Three workers enter these blockers immediately; the fourth
                # starts its blocker as soon as this GetUUID RPC returns.
                blockers.extend(submit(occupy_worker, ready) for ready in entered)
            return value
        def observe_queue(operation, *args, **kwargs):
            future = submit(operation, *args, **kwargs)
            allocating_uuid = operation == self.engine.rpc.call and args and args[0] == "GetUUID"
            if blockers and not allocating_uuid and not queued.is_set():
                queued.set()
            return future
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, response_body=response)
            elif message.channel_id == WAL_CHANNEL and payload["key"].endswith(":final:commit"):
                arm.set()
            elif message.channel_id == CMD_CHANNEL and payload["kind"] == "cmd.run.request":
                self.fail("an expired acceptance must not execute its tool")
        self.events.on_publish = callback
        async def execute():
            prepared = await self.session._next_answer()
            await self.session._run_answer(prepared)
            return prepared
        with patch.object(self.events, "get_uuid", side_effect=occupy_after_accept_uuid), \
                patch.object(self.engine.rpc_pool, "submit", side_effect=observe_queue):
            running = asyncio.run_coroutine_threadsafe(execute(), self.async_runner.loop)
            try:
                self.assertTrue(queued.wait(3), "acceptance publication did not enter the RPC queue")
                self.assertTrue(all(ready.wait(1) for ready in entered))
                self.assertFalse(running.done())
                request, = self.events.messages("infer.request", MODEL_CHANNEL)
                self.assertLess(self.events.now, request.ts_ms + 1000)
                self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept"))
                self.events.now = request.ts_ms + 1000
            finally:
                release.set()
            prepared = running.result(3)
            for blocker in blockers:
                blocker.result(1)
        self.assertEqual(len(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept")), 1)
        self.assertEqual(len(self.engine.journal.select(channel=CHAT_CHANNEL, kind="tool.prepare")), 1)
        self.assertFalse(self.events.messages("cmd.run.request", CMD_CHANNEL))
        self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail"))
        self.assertIsNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertEqual(self.answer_turns()[0].text, "displayed before queueing")

    def test_confirmed_acceptance_is_preserved_when_restart_occurs_after_deadline(self):
        self.restart(model_timeout_ms=1000, max_model_attempts=1)
        self.input()
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, response_body=body("accepted before restart"))
            if message.channel_id == WAL_CHANNEL and payload["kind"] == "attempt.accept":
                raise Crash()
        self.events.on_publish = callback
        with self.assertRaises(Crash):
            self.run_calls()
        accepted, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept")
        request, = self.events.messages("infer.request", MODEL_CHANNEL)
        self.events.now = request.ts_ms + 2000
        self.events.on_publish = None
        self.restart()
        prepared = self.run_calls()
        self.assertEqual(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept"), [accepted])
        self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail"))
        self.assertTrue(self.session._answer_finished(prepared))
        self.assertEqual(len(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(self.answer_turns()[0].status, "completed")

    def test_complete_tool_only_json_replaces_provisional_text_with_placeholder_before_accept(self):
        self.input()
        call = {"id": "work", "type": "function", "function": {
            "name": "exec", "arguments": json.dumps({"command": "printf done"})}}
        full = {"choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                                                     "tool_calls": [call]}, "finish_reason": "tool_calls"}]}
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                previous = self.head(message, payload)
                self.append(message, payload, previous, chunk("provisional text"))
                self.waiting.append(lambda: self.end(message, payload, response_body=full))
            if message.channel_id == WAL_CHANNEL and payload["kind"] == "attempt.accept":
                self.assertEqual(self.answer_turns()[0].text, PLACEHOLDER)
                raise Crash()
        self.events.on_publish = callback
        with self.assertRaises(Crash):
            self.run_calls()
        self.assertEqual([json.loads(item.payload)["content"][0]["text"]
                          for item in self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)],
                         ["provisional text", PLACEHOLDER])
        final = next(record for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.prepare")
                     if record.key.endswith(":final:prepare"))
        self.assertEqual(final.data["source"], {"type": "placeholder"})
        self.assertFalse(self.events.messages("cmd.run.request", CMD_CHANNEL))

    def test_hard_message_limit_ends_batches_on_whole_fragment_boundaries(self):
        self.input()
        def streaming(request, payload):
            previous = self.head(request, payload)
            previous = self.append(request, payload, previous, chunk("first"))
            def finish():
                second = self.append(request, payload, previous, chunk("a" * 1500))
                self.append(request, payload, second, chunk("b" * 1500, done=True))
                self.end(request, payload)
            self.waiting.append(finish)
        self.request_callback(streaming)
        with patch("chat_model_agent.session.display_fits", side_effect=lambda *args: len(args[-1].encode("utf-8")) <= 2000):
            self.run_calls()
        self.assertEqual([json.loads(item.payload)["content"][0]["text"]
                          for item in self.events.messages("turn.append", CHAT_CHANNEL, AGENT)],
                         ["a" * 1500, "b" * 1500])

    def test_oversized_indivisible_output_fails_without_accepting_or_executing_tools(self):
        for variant in ("fragment", "final", "refusal"):
            with self.subTest(variant=variant):
                self.fresh(max_model_attempts=1)
                self.input()
                text = "界" * 2000
                def respond(request, payload):
                    if variant == "fragment":
                        previous = self.head(request, payload)
                        self.append(request, payload, previous, chunk(text))
                    elif variant == "refusal":
                        self.end(request, payload, response_body=body(None, refusal=text))
                    else:
                        call = {"id": "should-not-run", "type": "function", "function": {
                            "name": "exec", "arguments": json.dumps({"command": "printf unexpected"})}}
                        self.end(request, payload, response_body={"choices": [{"index": 0, "message": {
                            "role": "assistant", "content": text, "tool_calls": [call]}, "finish_reason": "tool_calls"}]})
                self.request_callback(respond)
                with patch("chat_model_agent.session.display_fits", side_effect=lambda *args: len(args[-1].encode("utf-8")) <= 4500):
                    prepared = self.run_calls()
                self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
                self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept"))
                self.assertFalse(self.events.messages("cmd.run.request", CMD_CHANNEL))
                self.assertFalse(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT))
                self.assertFalse(self.events.messages("turn.append", CHAT_CHANNEL, AGENT))
                self.assertEqual(self.answer_turns()[0].text, PLACEHOLDER)

    def test_generic_publish_rejection_is_not_reclassified_as_a_model_size_failure(self):
        import grpc
        from openevent.chat_sdk import FailureInfo, PublishFailedError
        for code in (grpc.StatusCode.INVALID_ARGUMENT, grpc.StatusCode.RESOURCE_EXHAUSTED):
            with self.subTest(code=code):
                self.fresh()
                self.input()
                self.request_callback(lambda request, payload: self.end(request, payload, response_body=body("answer")))
                error = PublishFailedError(FailureInfo("PublishAutoSeq", "request_rejected", code,
                                                      "PublishAutoSeq failed after 1 attempts"), 9000, uncertain=False)
                with patch.object(TurnWriter, "reset", side_effect=error):
                    with self.assertRaises(InfrastructureError):
                        self.run_calls()
                self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail"))
                self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept"))
                self.assertFalse(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT))
                evidence, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.error")
                self.assertEqual(evidence.data["state"], "NOT_COMMITTED")

    def test_sdk_publication_error_preserves_ids_without_spending_a_model_attempt(self):
        from openevent.model_proxy_sdk.errors import CommitState, ResultPublishError
        self.input()
        failure = ResultPublishError(commit_state=CommitState.UNKNOWN, event_uuid=9876)
        with patch("chat_model_agent.session.publish_infer_request", side_effect=failure):
            with self.assertRaises(InfrastructureError):
                self.run_calls()
        known, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.error")
        attempt, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.prepare")
        self.assertEqual(dict(known.data), {
            "effect_key": attempt.key + ":request", "state": "UNKNOWN", "uuid": 9876})
        self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail"))
        self.restart()
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body("recovered")))
        self.run_calls()
        request, = self.events.messages("infer.request", MODEL_CHANNEL)
        self.assertEqual(json.loads(request.payload)["stream_id"], attempt.data["stream_id"])

    def test_sdk_committed_request_is_recovered_after_uuid_query_failure_without_republishing(self):
        self.input()
        self.session.model = replace(self.session.model, max_retries=1, retry_interval_ms=1)
        publish = self.events.publish_auto_seq
        query = self.events.get_seq_by_uuid
        attempts = []

        def lose_committed_response(**kwargs):
            if json.loads(kwargs["payload"])["kind"] == "infer.request":
                attempts.append(kwargs["uuid"])
                publish(**kwargs)
                # First publication commits but loses its response. Retrying
                # the same UUID reaches MemoryEvents' ALREADY_EXISTS branch.
                raise MemoryRpcError(grpc.StatusCode.UNAVAILABLE)
            return publish(**kwargs)

        def unavailable_query(uuid):
            if attempts and uuid == attempts[0]:
                raise MemoryRpcError(grpc.StatusCode.UNAVAILABLE)
            return query(uuid)

        with patch.object(self.events, "publish_auto_seq", side_effect=lose_committed_response), \
                patch.object(self.events, "get_seq_by_uuid", side_effect=unavailable_query) as lookup:
            with self.assertRaises(InfrastructureError):
                self.run_calls()
        request, = self.events.messages("infer.request", MODEL_CHANNEL)
        self.assertEqual(attempts, [request.uuid, request.uuid])
        self.assertEqual([call.args for call in lookup.call_args_list], [(request.uuid,), (request.uuid,)])
        evidence, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.error")
        attempt, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.prepare")
        self.assertEqual(dict(evidence.data), {
            "effect_key": attempt.key + ":request", "state": "COMMITTED", "uuid": request.uuid})
        self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail"))
        self.restart()
        self.end(request, json.loads(request.payload), response_body=body("recovered committed request"))
        prepared = self.run_calls()
        self.assertTrue(self.session._answer_finished(prepared))
        self.assertEqual(self.events.messages("infer.request", MODEL_CHANNEL), [request])
        commit = self.engine.journal.get(evidence.data["effect_key"] + ":commit")
        self.assertEqual((commit.data["seq"], commit.data["uuid"]), (request.seq, request.uuid))
        self.assertEqual(self.answer_turns()[0].text, "recovered committed request")

    def test_confirmed_publish_seq_is_saved_before_confirmation_can_fail(self):
        self.input()
        original = self.engine.aconfirm
        async def confirmation_failure(channel, seq):
            if channel == CHAT_CHANNEL:
                raise InfrastructureError("publication confirmation unavailable")
            return await original(channel, seq)
        with patch.object(self.engine, "aconfirm", side_effect=confirmation_failure):
            with self.assertRaises(InfrastructureError):
                self.run_calls()
        confirmed, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.commit")
        start, = self.events.messages("turn.start", CHAT_CHANNEL, AGENT)
        self.assertEqual(confirmed.data["seq"], start.seq)
        self.restart()
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body("after confirmation recovery")))
        self.run_calls()
        self.assertEqual(len(self.events.messages("turn.start", CHAT_CHANNEL, AGENT)), 1)

    def test_explicit_refusal_replaces_streamed_preface_without_retry_or_freeze(self):
        self.input()
        def refusing(request, payload):
            head = self.head(request, payload)
            first = self.append(request, payload, head, chunk("preface"))
            def finish():
                refusal = {"choices": [{"index": 0, "delta": {"refusal": "declined"}, "finish_reason": "stop"}]}
                self.append(request, payload, first, refusal)
                self.end(request, payload)
            self.waiting.append(finish)
        self.request_callback(refusing)
        prepared = self.run_calls()
        turn, = self.answer_turns()
        self.assertEqual((turn.text, turn.status), ("declined", "completed"))
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertIsNone(self.engine.journal.get(prepared.key + ":freeze"))

    def test_failed_attempt_resets_same_turn_before_retry_with_identical_model_input(self):
        self.input(object_keys=[ObjectKey(object_id=42, object_token="private-object-token")])
        metadata_reads = []
        def read_metadata(keys):
            metadata_reads.extend((key["object_id"], key["object_token"]) for key in keys)
            return [{"name": "input.txt", "type": "text/plain", "nbytes": 12} for _ in keys]
        self.engine.objects.batch_metadata_reader = read_metadata
        requests = []
        def respond(request, payload):
            requests.append(payload)
            if len(requests) == 1:
                previous = self.head(request, payload)
                self.append(request, payload, previous, chunk("unfinished previous attempt"))
                self.waiting.append(lambda: self.end(request, payload, failure=True))
            else:
                self.end(request, payload, response_body=body("recovered"))
        self.request_callback(respond)
        self.run_calls()
        self.assertEqual(metadata_reads, [(42, "private-object-token")])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["body"], requests[1]["body"])
        self.assertNotEqual(requests[0]["stream_id"], requests[1]["stream_id"])
        self.assertEqual(len(self.events.messages("turn.start", CHAT_CHANNEL, AGENT)), 1)
        resets = [json.loads(message.payload)["content"][0]["text"]
                  for message in self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)]
        self.assertEqual(resets, ["unfinished previous attempt", PLACEHOLDER, "recovered"])
        self.assertEqual(self.answer_turns()[0].text, "recovered")

    def test_retry_after_restart_reconstructs_identical_model_input_without_admitting_new_messages(self):
        first = self.input("preserve this input")
        requests, queued = [], []
        def fail_then_crash(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                requests.append(payload["body"])
                queued.append(self.input("arrived during the failed attempt"))
                self.end(message, payload, failure=True)
            elif message.channel_id == WAL_CHANNEL and payload["kind"] == "attempt.fail":
                raise Crash()
        self.events.on_publish = fail_then_crash
        with self.assertRaises(Crash):
            self.run_calls()
        self.events.on_publish = None
        self.restart(system_prompt="a changed configuration")
        def succeed(request, payload):
            requests.append(payload["body"])
            self.end(request, payload, response_body=body("after restart"))
        self.request_callback(succeed)
        self.run_calls()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(self.answer_turns()[0].text, "after restart")
        prepared, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")
        self.assertEqual(self.new_inputs(prepared), [first])
        self.assertEqual(prepared.data["settings"]["io_version"], 3)
        following = self.run_answer()
        self.assertEqual(self.new_inputs(following), queued)
        self.assertEqual(following.data["reply_to_seqs"], queued)
        self.assertEqual([event["text"] for event in json.loads(requests[2]["messages"][1]["content"])["events"] if event["type"] == "user"],
                         ["preserve this input", "arrived during the failed attempt"])
        self.assertIsNone(self.session._next_answer())

    def test_restart_rejects_other_model_io_versions_before_reinterpreting_input(self):
        for version in (1, 999):
            with self.subTest(io_version=version):
                self.fresh()
                self.input()
                # Persist an input prepared under a different interpretation.
                # Current code must not silently rebuild it with the new schema.
                with patch("chat_model_agent.session.MODEL_IO_VERSION", version):
                    prepared = self.session._next_answer()
                self.assertEqual(prepared.data["settings"]["io_version"], version)
                self.restart()
                count = len(self.events.history)
                recovered = self.session._next_answer()
                self.assertEqual(recovered, prepared)
                with self.assertRaisesRegex(HistoryError, "unsupported frozen model I/O version"):
                    self.session._run_answer(recovered)
                self.assertEqual(len(self.events.history), count)
                self.assertFalse(self.events.messages("infer.request", MODEL_CHANNEL))


    def test_command_carries_frozen_input_and_reply_attachments_across_recovery(self):
        for crash_at in (None, "attempt.accept", "tool.prepare", "cmd.run.request"):
            with self.subTest(crash_at=crash_at):
                self.fresh()
                first = ObjectKey(object_id=41, object_token="first-private-token")
                second = ObjectKey(object_id=42, object_token="second-private-token")
                unused = ObjectKey(object_id=43, object_token="unused-private-token")
                agent = create_chat(self.events, principal=AGENT, token="agent-token", channel_id=CHAT_CHANNEL,
                                    max_retries=0)
                first_seq = agent.single_turn(turn_id="past-answer", content=[TextPart("first attachment")],
                                              object_keys=[first, unused])
                self.user.single_turn(turn_id="with-reply", content=[TextPart("read both")],
                                      object_keys=[second], reply_to_seqs=[first_seq])
                def read_metadata(keys):
                    return [{"name": "input.txt", "type": "text/plain", "nbytes": 2} for _ in keys]
                self.engine.objects.batch_metadata_reader = read_metadata
                call = {"id": "read-files", "type": "function", "function": {
                    "name": "exec", "arguments": json.dumps({"command": "openevent-read-object 42; openevent-read-object 41",
                                                              "object_ids": [42, 41, 42]})}}
                tool_body = {"choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "reading", "tool_calls": [call]},
                    "finish_reason": "tool_calls"}]}
                interrupted = False
                late_input = False

                def callback(message, payload):
                    nonlocal interrupted, late_input
                    kind = payload["kind"]
                    if kind == "infer.request":
                        first_request = len(self.events.messages("infer.request", MODEL_CHANNEL)) == 1
                        self.end(message, payload, response_body=tool_body if first_request else body("done"))
                    elif kind == "cmd.run.request":
                        self.cmd.publish_run_result(CMD, "cmd-token", CMD_CHANNEL, AGENT, CmdRunResultInput(
                            prev_seq=message.seq, status="SUCCESS", exit_code=0, timeout_ms=300000,
                            stdout=CmdOutput(file="out", bytes=2, content_encoding="utf-8", content="ok"),
                            stderr=CmdOutput(file="err", bytes=0, content_encoding="utf-8", content="")))
                    if kind == "attempt.accept" and not late_input:
                        late_input = True
                        self.input("next task", object_keys=[ObjectKey(object_id=99, object_token="later-private-token")])
                    if kind == crash_at and not interrupted:
                        interrupted = True
                        raise Crash()

                self.events.on_publish = callback
                if crash_at is not None:
                    with self.assertRaises(Crash):
                        self.run_calls()
                    self.restart()
                    self.engine.objects.batch_metadata_reader = read_metadata
                self.run_calls()
                request, = self.events.messages("cmd.run.request", CMD_CHANNEL)
                self.assertEqual([(key.object_id, key.object_token) for key in request.object_keys],
                                 [(42, "second-private-token"), (41, "first-private-token")])
                prepared, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="tool.prepare")
                prepared = self.engine.journal.materialize(prepared)
                self.assertEqual(prepared.data["arguments"]["object_ids"], [42, 41])
                self.assertNotIn("object_ids", prepared.data)
                next_request = json.loads(self.events.messages("infer.request", MODEL_CHANNEL)[1].payload)
                invocation, = [event for event in json.loads(next_request["body"]["messages"][1]["content"])["events"]
                               if event["type"] == "tool_call"]
                self.assertEqual(invocation["name"], call["function"]["name"])
                self.assertEqual(invocation["arguments"], {**json.loads(call["function"]["arguments"]), "object_ids": [42, 41]})
                for message in (*self.events.messages("infer.request", MODEL_CHANNEL),
                                *self.events.messages("cmd.run.request", CMD_CHANNEL),
                                *(event for event in self.events.history if event.channel_id == WAL_CHANNEL)):
                    self.assertNotIn(b"private-token", message.payload)

    def test_large_attachment_context_carries_only_each_commands_explicit_selection(self):
        # Every Chat message stays within the per-message reference limit,
        # while their combined model input legitimately exceeds that limit.
        keys = [ObjectKey(object_id=ident, object_token=f"private-token-{ident}")
                for ident in range(1, 1026)]
        self.input("first batch", object_keys=keys[:600])
        self.input("second batch", object_keys=keys[600:])
        self.engine.objects.batch_metadata_reader = lambda keys: [
            {"name": "input.txt", "type": "text/plain", "nbytes": 2} for _ in keys]
        arguments = [{"command": "pwd"}, {"command": "pwd", "object_ids": []},
                     {"command": "openevent-read-object 1025", "object_ids": [1025]}]
        calls = [{"id": f"call-{index}", "type": "function", "function": {
                    "name": "exec", "arguments": json.dumps(value)}} for index, value in enumerate(arguments)]
        response = {"choices": [{"index": 0, "message": {
            "role": "assistant", "content": None, "tool_calls": calls}, "finish_reason": "tool_calls"}]}
        def callback(message, payload):
            if payload["kind"] == "infer.request":
                first_request = len(self.events.messages("infer.request", MODEL_CHANNEL)) == 1
                self.end(message, payload, response_body=response if first_request else body("done"))
            elif payload["kind"] == "cmd.run.request":
                self.cmd.publish_run_result(CMD, "cmd-token", CMD_CHANNEL, AGENT, CmdRunResultInput(
                    prev_seq=message.seq, status="SUCCESS", exit_code=0, timeout_ms=300000,
                    stdout=CmdOutput(file="out", bytes=2, content_encoding="utf-8", content="ok"),
                    stderr=CmdOutput(file="err", bytes=0, content_encoding="utf-8", content="")))
        self.events.on_publish = callback
        prepared = self.run_calls()
        first_request = json.loads(self.events.messages("infer.request", MODEL_CHANNEL)[0].payload)
        supplied = [attachment for message in first_request["body"]["messages"] if message["role"] == "user"
                    for event in json.loads(message["content"])["events"] for attachment in event["attachments"]]
        self.assertEqual(len(supplied), 1025)
        runs = self.events.messages("cmd.run.request", CMD_CHANNEL)
        self.assertEqual([[key.object_id for key in request.object_keys] for request in runs], [[], [], [1025]])
        self.assertEqual(runs[-1].object_keys[0].object_token, "private-token-1025")
        preparations = self.engine.journal.select(channel=CHAT_CHANNEL, kind="tool.prepare")
        preparations = [self.engine.journal.materialize(record) for record in preparations]
        self.assertEqual([record.data["arguments"].get("object_ids", []) for record in preparations],
                         [[], [], [1025]])
        for record in preparations:
            self.assertNotIn("object_ids", record.data)
        self.assertTrue(self.session._answer_finished(prepared))
        self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail"))







    def test_exhaustion_notifies_once_and_only_new_valid_user_completion_unfreezes(self):
        self.restart(max_model_attempts=1)
        self.input()
        queued = []
        def fail_with_queued_input(request, payload):
            queued.append(self.input("queued before failure"))
            self.end(request, payload, failure=True)
        self.request_callback(fail_with_queued_input)
        prepared = self.run_calls()
        freeze = self.engine.journal.get(prepared.key + ":freeze")
        self.assertIsNotNone(freeze)
        notices = self.events.messages("turn.single", CHAT_CHANNEL, AGENT)
        self.assertEqual(len(notices), 1)
        self.assertEqual(json.loads(notices[0].payload)["content"][0]["text"], FAILURE_NOTICE)
        self.assertIsNone(self.session._next_answer())
        self.input("")
        unfinished = self.input("not complete", start=True)
        self.assertIsNone(self.session._next_answer())
        self.user.cancel_turn(target_turn=TurnRef(USER, unfinished.turn_id), start_seq=self.events.start_seq(USER, unfinished.turn_id))
        self.assertIsNone(self.session._next_answer())
        fresh = self.input("fresh request")
        next_answer = self.session._next_answer()
        self.assertEqual(self.new_inputs(next_answer), [queued[0], fresh])
        self.assertEqual(self.engine.journal.get(prepared.key + ":wake").data["input_seq"], fresh)
        self.assertEqual(len(self.events.messages("turn.single", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)


    def test_restart_reconciles_committed_chat_start_without_second_placeholder(self):
        self.input()
        def crash(message, payload):
            if message.channel_id == CHAT_CHANNEL and message.principal == AGENT and payload["kind"] == "turn.start":
                raise Crash()
        self.events.on_publish = crash
        with self.assertRaises(Crash):
            self.run_calls()
        self.assertEqual(len(self.events.messages("turn.start", CHAT_CHANNEL, AGENT)), 1)
        self.events.on_publish = None
        self.restart()
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body("after restart")))
        prepared = self.run_calls()
        self.assertEqual(len(self.events.messages("turn.start", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(self.answer_turns()[0].text, "after restart")
        count = len(self.events.history)
        self.restart()
        self.assertIsNone(self.session._next_answer())
        self.assertEqual(len(self.events.history), count)
        self.assertTrue(self.session._answer_finished(prepared))

    def test_recovery_scans_each_source_once_and_checks_only_matching_identities(self):
        for count in (8, 16, 32):
            with self.subTest(answers=count):
                self.fresh()
                self.request_callback(lambda request, payload: self.end(
                    request, payload, response_body=body("x" * 12288)))
                for _ in range(count):
                    self.input()
                    self.run_calls()
                self.restart()
                effects = self.engine.journal.select(kind="effect.prepare", channel=CHAT_CHANNEL)
                before = len(self.events.history)
                with patch.object(self.engine, "events_in", wraps=self.engine.events_in) as history, \
                        patch.object(self.session, "_matches_intent", wraps=self.session._matches_intent) as match:
                    self.session._reconcile()
                self.assertCountEqual([call.args[0] for call in history.call_args_list],
                                      [CHAT_CHANNEL])
                self.assertEqual(match.call_count, len(effects))
                self.assertEqual(len(self.events.history), before)

    def test_conversation_wal_queries_stay_scoped_before_and_after_restart(self):
        class NoWalScan(UserDict):
            def __init__(self, records):
                # Delegate mutations to the original dictionary; the reader's
                # per-channel index must stay synchronized during this test.
                self.data = records

            def __iter__(self):
                raise AssertionError("conversation iterated WAL records outside its channel")

            def values(self):
                raise AssertionError("conversation scanned WAL records outside its channel")

        class NoHistoryScan(list):
            def __iter__(self):
                raise AssertionError("conversation iterated another channel's WAL index")

            def __getitem__(self, key):
                raise AssertionError("conversation read another channel's WAL index")

        contexts = []
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.fresh()
                sessions = {}
                self.request_callback(lambda request, payload: self.end(
                    request, payload, response_body=body("first answer")))
                for cid in range(CHAT_CHANNEL, CHAT_CHANNEL + 4):
                    if cid != CHAT_CHANNEL:
                        self.add_chat_channel(cid)
                    self.session = SyncProxy(Conversation(self.engine, cid), self.async_runner)
                    sessions[cid] = self.session
                    user = create_chat(self.events, principal=USER, token="user-token", channel_id=cid,
                                       max_retries=0)
                    user.single_turn(turn_id=f"input-{cid}", content=(TextPart(f"question in chat {cid}"),))
                    self.run_calls()
                if restart:
                    self.restart()
                else:
                    self.session = sessions[CHAT_CHANNEL]
                journal = self.engine.journal
                own_records = journal.select(channel=CHAT_CHANNEL)
                self.assertGreater(len(journal.records), len(own_records))
                requests = []
                def respond(request, payload):
                    requests.append(payload["body"]["messages"])
                    self.end(request, payload, response_body=body("follow-up answer"))
                self.request_callback(respond)
                unrelated = {scope: NoHistoryScan(records) for scope, records in journal.ordered.items()
                             if scope[0] != CHAT_CHANNEL}
                with patch.object(journal, "records", NoWalScan(journal.records)), \
                        patch.dict(journal.ordered, unrelated), \
                        patch.object(journal, "select", wraps=journal.select) as select:
                    before = len(self.events.history)
                    self.assertIsNone(self.session._next_answer())
                    self.assertEqual(len(self.events.history), before)
                    input_seq = self.input("follow-up question")
                    prepared = self.run_calls()
                    self.assertEqual(self.new_inputs(prepared), [input_seq])
                    self.assertTrue(self.session._answer_finished(prepared))
                    self.assertIsNone(self.session._next_answer())
                    self.assertTrue(select.call_args_list)
                    self.assertTrue(all(call.kwargs.get("channel") == CHAT_CHANNEL
                                        for call in select.call_args_list))
                self.assertEqual(len(requests), 1)
                messages = requests[0]
                self.assertEqual([message["role"] for message in messages],
                                 ["system", "user"])
                self.assertIn(f"question in chat {CHAT_CHANNEL}", messages[1]["content"])
                for cid in range(CHAT_CHANNEL + 1, CHAT_CHANNEL + 4):
                    self.assertNotIn(f"question in chat {cid}", json.dumps(messages))
                self.assertEqual(self.answer_turns()[-1].text, "follow-up answer")
                self.assertEqual(self.answer_turns()[-1].status, "completed")
                events = json.loads(messages[1]["content"])["events"]
                contexts.append([(event["type"], event.get("text")) for event in events])
        self.assertEqual(contexts[0], contexts[1])

    def test_recovery_shares_model_and_cmd_indexes_across_conversations_then_releases_them(self):
        channels = list(range(CHAT_CHANNEL, CHAT_CHANNEL + 4))
        for cid in channels[1:]:
            self.add_chat_channel(cid)
        call = {"id": "exec", "type": "function", "function": {
            "name": "exec", "arguments": json.dumps({"command": "true"})}}
        tool_body = {"choices": [{"index": 0, "message": {
            "role": "assistant", "content": "working", "tool_calls": [call]},
            "finish_reason": "tool_calls"}]}

        def respond(message, payload):
            if payload["kind"] == "infer.request":
                response = body("done") if json.loads(payload["body"]["messages"][1]["content"])["events"][-1]["type"] == "tool_result" else tool_body
                self.end(message, payload, response_body=response)
            elif payload["kind"] == "cmd.run.request":
                self.cmd.publish_run_result(CMD, "cmd-token", CMD_CHANNEL, AGENT, CmdRunResultInput(
                    prev_seq=message.seq, status="SUCCESS", exit_code=0, timeout_ms=300000,
                    stdout=CmdOutput(file="out", bytes=0, content_encoding="utf-8", content=""),
                    stderr=CmdOutput(file="err", bytes=0, content_encoding="utf-8", content="")))
        self.events.on_publish = respond
        for cid in channels:
            self.session = SyncProxy(Conversation(self.engine, cid), self.async_runner)
            user = create_chat(self.events, principal=USER, token="user-token", channel_id=cid, max_retries=0)
            user.single_turn(turn_id=f"input-{cid}", content=(TextPart("hello"),))
            self.run_calls()
        self.events.on_publish = None
        history_calls = []
        events_in = ChatModelAgent.events_in
        def observe(engine, cid):
            events = events_in(engine, cid)
            history_calls.append((cid, len(events)))
            return events
        with patch.object(ChatModelAgent, "events_in", new=observe):
            self.restart()
            self.assertEqual(self.engine.recovery_pending, set(channels))
            shared = dict(self.engine.recovery_indexes)
            before = len(self.events.history)
            for cid in channels:
                session = SyncProxy(Conversation(self.engine, cid), self.async_runner)
                self.assertIs(session._recovery_index(MODEL_CHANNEL, "model"), shared[MODEL_CHANNEL])
                self.assertIs(session._recovery_index(CMD_CHANNEL, "cmd"), shared[CMD_CHANNEL])
                session._reconcile()
            self.assertEqual(len(self.events.history), before)
        for cid in (MODEL_CHANNEL, CMD_CHANNEL):
            self.assertEqual([size for channel, size in history_calls if channel == cid],
                             [len(self.engine.channels[cid])])
        self.assertFalse(self.engine.recovery_pending)
        self.assertFalse(self.engine.recovery_indexes)

    def add_chat_channel(self, cid):
        channel = self.events.channel(cid, "chat.v1", [AGENT, USER])
        self.events.channels[cid] = channel
        self.events.publish_auto_seq(principal=0, token="", channel_id=cid,
            uuid=self.events.get_uuid(), payload=json.dumps({"kind": "system.channel_initialized", "data": {
                "schema_version": 1, "protocol": "chat.v1", "members": list(channel.members),
                "creator": channel.creator, "name": channel.name, "description": "",
                "visibility": "private"}}).encode())

    def test_shared_recovery_index_tracks_new_facts_and_new_live_conversations(self):
        self.add_chat_channel(CHAT_CHANNEL + 1)
        self.input()
        def crash_after_request(request, payload):
            raise Crash()
        self.request_callback(crash_after_request)
        with self.assertRaises(Crash):
            self.run_calls()
        self.events.on_publish = None
        self.restart()
        request, = self.events.messages("infer.request", MODEL_CHANNEL)
        attempt, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.prepare")
        self.assertIsNone(self.engine.journal.get(attempt.key + ":request:commit"))
        self.session._reconcile()
        self.assertEqual(self.engine.journal.get(attempt.key + ":request:commit").data["seq"], request.seq)
        self.assertEqual(self.engine.recovery_pending, {CHAT_CHANNEL + 1})
        shared = self.engine.recovery_indexes[MODEL_CHANNEL]
        self.session._cancel_model(attempt)
        cancellation, = self.events.messages("infer.cancel", MODEL_CHANNEL)
        identity = ("infer.cancel", attempt.data["stream_id"], request.seq)
        self.assertEqual([event.seq for event in shared[identity]], [cancellation.seq])
        other = SyncProxy(Conversation(self.engine, CHAT_CHANNEL + 1), self.async_runner)
        other._reconcile()
        self.assertFalse(self.engine.recovery_indexes)
        self.add_chat_channel(CHAT_CHANNEL + 2)
        self.session = SyncProxy(Conversation(self.engine, CHAT_CHANNEL + 2), self.async_runner)
        user = create_chat(self.events, principal=USER, token="user-token", channel_id=CHAT_CHANNEL + 2,
                           max_retries=0)
        user.single_turn(turn_id="new-live-input", content=(TextPart("hello"),))
        self.request_callback(lambda message, payload: self.end(message, payload, response_body=body("live")))
        prepared = self.run_calls()
        self.assertTrue(self.session._answer_finished(prepared))
        self.assertFalse(self.engine.recovery_indexes)

    def test_shared_recovery_index_detects_duplicate_fact_ingested_after_recovery(self):
        self.input()
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body("done")))
        self.run_calls()
        self.events.on_publish = None
        self.restart()
        request, = self.events.messages("infer.request", MODEL_CHANNEL)
        self.events.publish_auto_seq(principal=AGENT, token="agent-token", channel_id=MODEL_CHANNEL,
                                     uuid=self.events.get_uuid(), payload=request.payload)
        with self.assertRaisesRegex(HistoryError, "multiple matching source events"):
            self.session._reconcile()

    def test_recovery_preserves_source_identity_and_commit_conflict_checks(self):
        for conflict, message in (
                ("duplicate", "multiple matching source events"),
                ("seq", "effect commit conflicts"),
                ("uuid", "effect UUID conflicts"),
                ("evidence-uuid", "SDK reported a committed effect")):
            with self.subTest(conflict=conflict):
                self.fresh()
                self.input()
                self.request_callback(lambda request, payload: self.end(
                    request, payload, response_body=body("done")))
                self.run_calls()
                request, = self.events.messages("infer.request", MODEL_CHANNEL)
                original = next(record for record in self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.prepare")
                                if record.data["kind"] == "infer.request")
                self.session.put("effect.prepare", "conflict:prepare", original.data)
                if conflict == "duplicate":
                    self.events.on_publish = None
                    self.events.publish_auto_seq(principal=AGENT, token="agent-token", channel_id=MODEL_CHANNEL,
                                                 uuid=self.events.get_uuid(), payload=request.payload)
                elif conflict in {"seq", "uuid"}:
                    self.session.put("effect.commit", "conflict:commit", {
                        "seq": request.seq + (conflict == "seq"),
                        "uuid": request.uuid + (conflict == "uuid")})
                else:
                    # Test the Agent's own WAL association, not an SDK that
                    # reports the wrong publication UUID.
                    self.session.put("effect.error", "conflict:error:1", {
                        "effect_key": "conflict", "state": "COMMITTED",
                        "uuid": request.uuid + 1})
                self.restart()
                before = len(self.events.history)
                with self.assertRaisesRegex(HistoryError, message):
                    self.session._reconcile()
                self.assertEqual(len(self.events.history), before)

    def test_recovery_matches_chat_publisher_cancel_target_and_expected_tail(self):
        agent = create_chat(self.events, principal=AGENT, token="agent-token",
                            channel_id=CHAT_CHANNEL, max_retries=0)
        own = agent.start_turn(turn_id="shared")
        other = self.user.start_turn(turn_id="shared")
        own.append(content=(TextPart("first"),))
        second = own.append(content=(TextPart("second"),))
        other.append(content=(TextPart("other publisher"),))
        own_cancel = agent.cancel_turn(target_turn=TurnRef(AGENT, "shared"), start_seq=self.events.start_seq(AGENT, "shared"))
        agent.cancel_turn(target_turn=TurnRef(USER, "shared"), start_seq=self.events.start_seq(USER, "shared"))
        appends = self.events.messages("turn.append", CHAT_CHANNEL, AGENT)
        second_tail = json.loads(appends[1].payload)["pre_seq"]
        for key, kind, pre_seq in (("append", "turn.append", second_tail),
                                   ("cancel", "turn.cancel", None)):
            self.session.put("effect.prepare", key + ":prepare", {
                "channel": CHAT_CHANNEL, "module": "chat", "kind": kind,
                "turn_id": "shared", "pre_seq": pre_seq})
        self.restart()
        self.session._reconcile()
        self.assertEqual(self.engine.journal.get("append:commit").data["seq"], second)
        self.assertEqual(self.engine.journal.get("cancel:commit").data["seq"], own_cancel)
        self.session.put("effect.prepare", "wildcard:prepare", {
            "channel": CHAT_CHANNEL, "module": "chat", "kind": "turn.append",
            "turn_id": "shared", "pre_seq": None})
        self.restart()
        with self.assertRaisesRegex(HistoryError, "multiple matching source events"):
            self.session._reconcile()

    def crash_after_model_request(self, *, timeout):
        self.restart(model_timeout_ms=timeout, max_model_attempts=1)
        self.input()
        def crash(request, payload):
            raise Crash()
        self.request_callback(crash)
        with self.assertRaises(Crash):
            self.run_calls()
        self.events.on_publish = None
        request, = self.events.messages("infer.request", MODEL_CHANNEL)
        self.events.now = request.ts_ms + 2000
        return request, json.loads(request.payload)

    def assert_no_timeout_snapshot(self):
        def keys(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    yield key
                    yield from keys(nested)
            elif isinstance(value, list):
                for nested in value:
                    yield from keys(nested)
        names = set(keys([record.data for channel in (0, CHAT_CHANNEL)
                          for record in self.engine.journal.select(channel=channel)]))
        self.assertFalse(any("timeout" in name or "deadline" in name for name in names), names)

    def test_restart_shorter_current_timeout_uses_original_top_level_timestamp(self):
        request, payload = self.crash_after_model_request(timeout=10000)
        self.restart(model_timeout_ms=1000)
        prepared = self.run_calls()
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("infer.cancel", MODEL_CHANNEL)), 1)
        self.assertIn("timed out", self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail")[0].data["reason"])
        self.assert_no_timeout_snapshot()

    def test_restart_longer_current_timeout_can_finish_existing_request_without_republishing(self):
        request, payload = self.crash_after_model_request(timeout=1000)
        self.restart(model_timeout_ms=10000)
        self.waiting.append(lambda: self.end(request, payload, response_body=body("still in time")))
        prepared = self.run_calls()
        self.assertTrue(self.session._answer_finished(prepared))
        self.assertEqual(self.answer_turns()[0].text, "still in time")
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertFalse(self.events.messages("infer.cancel", MODEL_CHANNEL))
        self.assert_no_timeout_snapshot()

    def streaming_responder(self, request, payload):
        head = self.head(request, payload)
        first = self.append(request, payload, head, chunk("hel"))
        def finish():
            self.append(request, payload, first, chunk("lo", done=True))
            self.end(request, payload)
        self.waiting.append(finish)

    def test_restart_at_every_success_wal_boundary_keeps_one_reply_and_original_batches(self):
        self.input()
        self.request_callback(self.streaming_responder)
        self.run_calls()
        boundaries = [(message.seq, json.loads(message.payload)) for message in self.events.history
                      if message.channel_id == WAL_CHANNEL and message.principal == AGENT]
        for crash_seq, record in boundaries:
            with self.subTest(kind=record["kind"], key=record["key"]):
                self.fresh()
                self.input()
                def crash_at_record(message, payload):
                    if message.seq == crash_seq:
                        raise Crash()
                    if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                        self.streaming_responder(message, payload)
                self.events.on_publish = crash_at_record
                with self.assertRaises(Crash):
                    self.run_calls()
                self.request_callback(self.streaming_responder)
                self.restart()
                self.run_calls()
                turn, = self.answer_turns()
                self.assertEqual((turn.text, turn.status), ("hello", "completed"))
                for kind in ("turn.start", "turn.reset", "turn.append", "turn.end"):
                    self.assertEqual(len(self.events.messages(kind, CHAT_CHANNEL, AGENT)), 1, kind)
                self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
                self.assertIsNone(self.session._next_answer())

    def test_restart_at_every_failure_wal_boundary_preserves_single_notice_and_freeze(self):
        self.restart(max_model_attempts=1)
        self.input()
        self.request_callback(lambda request, payload: self.end(request, payload, failure=True))
        self.run_calls()
        boundaries = [(message.seq, json.loads(message.payload)) for message in self.events.history
                      if message.channel_id == WAL_CHANNEL and message.principal == AGENT]
        for crash_seq, record in boundaries:
            with self.subTest(kind=record["kind"], key=record["key"]):
                self.fresh(max_model_attempts=1)
                self.input()
                def crash_at_record(message, payload):
                    if message.seq == crash_seq:
                        raise Crash()
                    if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                        self.end(message, payload, failure=True)
                self.events.on_publish = crash_at_record
                with self.assertRaises(Crash):
                    self.run_calls()
                self.request_callback(lambda request, payload: self.end(request, payload, failure=True))
                self.restart()
                pending = self.session._next_answer()
                if pending is not None:
                    self.session._run_answer(pending)
                self.assertEqual(len(self.events.messages("turn.start", CHAT_CHANNEL, AGENT)), 1)
                self.assertEqual(len(self.events.messages("turn.cancel", CHAT_CHANNEL, AGENT)), 1)
                notices = self.events.messages("turn.single", CHAT_CHANNEL, AGENT)
                self.assertEqual(len(notices), 1)
                self.assertEqual(json.loads(notices[0].payload)["content"][0]["text"], FAILURE_NOTICE)
                self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
                self.assertIsNone(self.session._next_answer())

    def test_user_cancel_wins_before_prepared_model_request_reset_or_end_is_published(self):
        for target in ("infer.request", "turn.reset", "turn.end"):
            with self.subTest(target=target):
                self.fresh()
                self.input()
                cancelled = []
                def callback(message, payload):
                    if (message.channel_id == WAL_CHANNEL and payload["kind"] == "effect.prepare"
                            and payload["data"]["kind"] == target and not cancelled):
                        cancelled.append(self.user.cancel_turn(target_turn=TurnRef(
                            AGENT, self.session.answer.data["turn_id"]), start_seq=self.events.start_seq(AGENT, self.session.answer.data["turn_id"])))
                    if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                        self.end(message, payload, response_body=body("answer"))
                self.events.on_publish = callback
                prepared = self.run_calls()
                self.assertEqual(len(cancelled), 1)
                self.assertIsNotNone(self.engine.journal.get(prepared.key + ":cancel"))
                self.assertTrue(self.session._answer_finished(prepared))
                self.assertEqual(self.session._turn(prepared).terminal_seq, cancelled[0])
                channel = MODEL_CHANNEL if target == "infer.request" else CHAT_CHANNEL
                self.assertFalse(self.events.messages(target, channel, AGENT))
                self.assertFalse(self.events.messages("turn.end", CHAT_CHANNEL, AGENT))
                self.assertFalse(self.events.messages("turn.single", CHAT_CHANNEL, AGENT))

    def test_cancel_before_retry_reset_does_not_submit_second_model_request(self):
        self.input()
        def callback(message, payload):
            if (message.channel_id == WAL_CHANNEL and payload["kind"] == "effect.prepare"
                    and payload["key"].endswith(":placeholder:prepare")):
                self.user.cancel_turn(target_turn=TurnRef(AGENT, self.session.answer.data["turn_id"]), start_seq=self.events.start_seq(AGENT, self.session.answer.data["turn_id"]))
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, failure=True)
        self.events.on_publish = callback
        prepared = self.run_calls()
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":cancel"))
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertFalse(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT))

    def test_cancel_during_recovered_input_preparation_does_not_publish_model_request(self):
        self._interrupt_recovered_input_preparation(stop=False)

    def test_stop_during_recovered_input_preparation_leaves_model_request_for_restart(self):
        self._interrupt_recovered_input_preparation(stop=True)

    def test_shutdown_confirms_inflight_business_and_wal_before_closing(self):
        for phase in ("business", "wal"):
            with self.subTest(phase=phase):
                self._stop_during_publication(phase)

    def test_shutdown_wal_failure_is_not_reported_as_safe(self):
        self._stop_during_publication("wal", fail=True)

    def _stop_during_publication(self, phase, *, fail=False):
        self.fresh()
        self.input()
        self.stop_reader()
        self.engine = ChatModelAgent(self.config, self.events, clock=lambda: self.events.now)
        self.events.engine = self.engine
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        failures = []

        def on_publish(message, payload):
            if payload["kind"] == ("turn.start" if phase == "business" else "effect.commit"):
                entered.set()
                release.wait()
                if fail:
                    raise InfrastructureError("WAL confirmation failed")

        def run():
            try:
                self.engine.run()
            except Exception as error:
                failures.append(error)

        self.events.on_publish = on_publish
        with patch.object(self.events, "close", side_effect=closed.set, create=True), \
                patch("chat_model_agent.worker.LOG.info") as logged:
            runner = threading.Thread(target=run, daemon=True)
            runner.start()
            try:
                self.assertTrue(entered.wait(1))
                self.engine.request_stop()
                self.assertFalse(closed.wait(0.02))
                logged.assert_not_called()
                release.set()
                runner.join(1)
                self.assertFalse(runner.is_alive())
                self.assertTrue(closed.is_set())
                if fail:
                    self.assertEqual(len(failures), 1)
                    self.assertIsInstance(failures[0], InfrastructureError)
                    logged.assert_not_called()
                else:
                    self.assertFalse(failures)
                    start, = self.events.messages("turn.start", CHAT_CHANNEL, AGENT)
                    commit, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="effect.commit")
                    self.assertEqual(commit.data["seq"], start.seq)
                    logged.assert_called_once()
                self.assertFalse(self.events.messages("shutdown", WAL_CHANNEL))
                self.assertFalse(self.events.messages("infer.request", MODEL_CHANNEL))
                self.assertFalse(self.events.messages("turn.cancel", CHAT_CHANNEL))
            finally:
                self.engine.request_stop()
                release.set()
                runner.join(1)

    def _interrupt_recovered_input_preparation(self, *, stop):
        self.input("read attachment", object_keys=[ObjectKey(object_id=42, object_token="object-token")])
        metadata = {"name": "input.txt", "type": "text/plain", "nbytes": 12}
        self.engine.objects.batch_metadata_reader = lambda keys: [metadata for _ in keys]
        def crash_after_attempt_prepare(message, payload):
            if message.channel_id == WAL_CHANNEL and payload["key"].endswith(":start:commit"):
                raise Crash()
        self.events.on_publish = crash_after_attempt_prepare
        with self.assertRaises(Crash):
            self.run_calls()
        self.assertFalse(self.events.messages("infer.request", MODEL_CHANNEL))
        attempt, = self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.prepare")
        self.events.on_publish = None
        self.restart()
        def interrupt_during_metadata(keys):
            if stop:
                self.engine.request_stop()
            else:
                self.user.cancel_turn(target_turn=TurnRef(AGENT, self.session.answer.data["turn_id"]), start_seq=self.events.start_seq(AGENT, self.session.answer.data["turn_id"]))
            return [metadata for _ in keys]
        self.engine.objects.batch_metadata_reader = interrupt_during_metadata
        if stop:
            with self.assertRaises(Stopped):
                self.run_calls()
        else:
            prepared = self.run_calls()
            self.assertIsNotNone(self.engine.journal.get(prepared.key + ":cancel"))
        self.assertFalse(self.events.messages("infer.request", MODEL_CHANNEL))
        self.assertFalse(self.events.messages("infer.cancel", MODEL_CHANNEL))
        self.assertFalse(self.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.fail"))
        self.assertFalse(self.events.messages("turn.single", CHAT_CHANNEL, AGENT))
        self.restart()
        self.engine.objects.batch_metadata_reader = lambda keys: [metadata for _ in keys]
        if stop:
            self.request_callback(lambda request, payload: self.end(request, payload, response_body=body("resumed")))
            self.run_calls()
            request, = self.events.messages("infer.request", MODEL_CHANNEL)
            self.assertEqual(json.loads(request.payload)["stream_id"], attempt.data["stream_id"])
            self.assertEqual(self.answer_turns()[0].text, "resumed")
            self.assertEqual(len(self.events.messages("turn.start", CHAT_CHANNEL, AGENT)), 1)
        else:
            self.assertIsNone(self.session._next_answer())
            self.assertFalse(self.events.messages("infer.request", MODEL_CHANNEL))

    def test_prepared_failure_closure_is_not_revived_by_larger_retry_budget_on_restart(self):
        self.restart(max_model_attempts=1)
        self.input()
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, failure=True)
            if (message.channel_id == WAL_CHANNEL and payload["kind"] == "effect.prepare"
                    and payload["key"].endswith(":failure-cancel:prepare")):
                raise Crash()
        self.events.on_publish = callback
        with self.assertRaises(Crash):
            self.run_calls()
        self.events.on_publish = None
        self.restart(max_model_attempts=5)
        prepared = self.run_calls()
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("turn.single", CHAT_CHANNEL, AGENT)), 1)

    def test_answer_attempt_budget_stays_frozen_before_failure_closure_is_prepared(self):
        self.restart(max_model_attempts=1)
        self.input()
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, failure=True)
            if message.channel_id == WAL_CHANNEL and payload["kind"] == "attempt.fail":
                raise Crash()
        self.events.on_publish = callback
        with self.assertRaises(Crash):
            self.run_calls()
        self.assertFalse([record for record in self.engine.journal.select(channel=CHAT_CHANNEL)
                          if record.key.endswith(":failure-cancel:prepare")])
        self.events.on_publish = None
        self.restart(max_model_attempts=5)
        prepared = self.run_calls()
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)
        self.assertEqual(self.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")[0].data["max_model_attempts"], 1)

    def test_notice_committed_before_freeze_record_survives_restart_and_preserves_wakeup_input(self):
        self.restart(max_model_attempts=1)
        self.input()
        fresh_inputs = []
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, failure=True)
            if message.channel_id == CHAT_CHANNEL and message.principal == AGENT and payload["kind"] == "turn.single":
                fresh_inputs.append(self.input("sent after the failure notice"))
                raise Crash()
        self.events.on_publish = callback
        with self.assertRaises(Crash):
            self.run_calls()
        self.events.on_publish = None
        self.restart()
        failed_answer = self.run_calls()
        self.assertIsNotNone(self.engine.journal.get(failed_answer.key + ":freeze"))
        next_answer = self.session._next_answer()
        self.assertEqual(self.new_inputs(next_answer), fresh_inputs)
        self.assertEqual(len(self.events.messages("turn.single", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 1)

    def test_one_writer_is_reused_until_reply_completion_and_released_before_the_next_reply(self):
        writers = []
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.streaming_responder(message, payload)
            if (message.channel_id == WAL_CHANNEL and payload["kind"] == "effect.prepare"
                    and payload["data"]["module"] == "chat"
                    and payload["data"]["kind"] in {"turn.reset", "turn.append", "turn.end"}):
                writers.append(self.session.active_writer)
        self.events.on_publish = callback
        previous = None
        for _ in range(2):
            self.input()
            start = len(writers)
            self.run_calls()
            current = writers[start:]
            self.assertEqual(len(current), 3)
            self.assertIsNotNone(current[0])
            self.assertTrue(all(writer is current[0] for writer in current))
            self.assertIsNot(current[0], previous)
            self.assertIsNone(self.session.active_writer)
            previous = current[0]

    def test_unresolved_chat_publication_keeps_the_same_writer(self):
        import grpc
        from openevent.chat_sdk import TurnWriterStateError
        class Unavailable(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAVAILABLE
            def details(self):
                return "reply publication interrupted"
        self.input()
        self.request_callback(lambda request, payload: self.end(request, payload, response_body=body("reply")))
        publish = self.events.publish_auto_seq
        captured = []
        def interrupted_publish(**kwargs):
            if json.loads(kwargs["payload"])["kind"] == "turn.reset":
                captured.append(self.session.active_writer)
                raise Unavailable()
            return publish(**kwargs)
        with patch.object(self.events, "publish_auto_seq", side_effect=interrupted_publish):
            with self.assertRaises(InfrastructureError):
                self.run_calls()
        writer, = captured
        self.assertIs(self.session.active_writer, writer)
        self.assertIs(self.session._writer(writer.turn_id), writer)
        # Retaining the SDK object also preserves its unresolved-write guard.
        with self.assertRaises(TurnWriterStateError):
            writer.append(content=(TextPart("must not bypass the unresolved write"),))

    def test_cancel_during_recovered_writer_construction_ends_call(self):
        self.input()
        def crash_after_accept(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                self.end(message, payload, response_body=body("already displayed"))
            if message.channel_id == WAL_CHANNEL and payload["kind"] == "attempt.accept":
                raise Crash()
        self.events.on_publish = crash_after_accept
        with self.assertRaises(Crash):
            self.run_calls()
        self.events.on_publish = None
        self.restart()
        def construct_after_cancel(client, **kwargs):
            self.user.cancel_turn(target_turn=TurnRef(AGENT, kwargs["turn_id"]), start_seq=self.events.start_seq(AGENT, kwargs["turn_id"]))
            return TurnWriter(client, **kwargs)
        with patch("chat_model_agent.session.TurnWriter", side_effect=construct_after_cancel):
            prepared = self.run_calls()
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":cancel"))
        self.assertEqual(len(self.events.messages("turn.reset", CHAT_CHANNEL, AGENT)), 1)
        self.assertEqual(self.answer_turns()[0].status, "cancelled")
        self.assert_source_reads_are_targeted()

    def test_constructing_writer_for_agent_own_cancelled_turn_is_a_history_error(self):
        self.session.chat.start_turn(turn_id="agent-stopped", content=(TextPart(PLACEHOLDER),))
        self.session.chat.cancel_turn(target_turn=TurnRef(AGENT, "agent-stopped"), start_seq=self.events.start_seq(AGENT, "agent-stopped"))
        with self.assertRaises(HistoryError):
            self.session._writer("agent-stopped")

    def test_unrelated_sdk_rpc_error_is_not_hidden_by_user_cancellation(self):
        self.session.chat.start_turn(turn_id="user-stopped", content=(TextPart(PLACEHOLDER),))
        self.user.cancel_turn(target_turn=TurnRef(AGENT, "user-stopped"), start_seq=self.events.start_seq(AGENT, "user-stopped"))
        def failed_rpc():
            raise OSError("connection lost")
        with patch.object(self.events, "get_status", side_effect=AssertionError("must not reconcile a non-state error")):
            with self.assertRaises(InfrastructureError) as caught:
                self.engine.rpc.sdk("Chat publish", failed_rpc)
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_completed_tool_answer_is_not_reset_or_rerun_when_followup_failure_recovers(self):
        self.restart(max_model_attempts=1)
        self.input("run command")
        calls = [{"id": "call_exec", "type": "function", "function": {
            "name": "exec", "arguments": json.dumps({"command": "printf done"})}}]
        first_body = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "working", "tool_calls": calls},
                                    "finish_reason": "tool_calls"}]}
        crash_enabled = [True]
        followups = []
        def callback(message, payload):
            if message.channel_id == MODEL_CHANNEL and payload["kind"] == "infer.request":
                if len(self.events.messages("infer.request", MODEL_CHANNEL)) == 1:
                    self.end(message, payload, response_body=first_body)
                else:
                    followups.append(payload["body"]["messages"])
                    self.end(message, payload, failure=True)
            elif message.channel_id == CMD_CHANNEL and payload["kind"] == "cmd.run.request":
                self.cmd.publish_run_result(CMD, "cmd-token", CMD_CHANNEL, AGENT, CmdRunResultInput(
                    prev_seq=message.seq, status="SUCCESS", exit_code=0, timeout_ms=300000,
                    started_at_ms=self.events.now, ts_ms=self.events.now,
                    stdout=CmdOutput(file="private.stdout", bytes=4, content_encoding="utf-8", content="done"),
                    stderr=CmdOutput(file="private.stderr", bytes=0, content_encoding="utf-8", content="")))
            elif message.channel_id == WAL_CHANNEL and payload["kind"] == "attempt.fail" and crash_enabled[0]:
                raise Crash()
        self.events.on_publish = callback
        with self.assertRaises(Crash):
            self.run_calls()
        completed = [turn for turn in self.answer_turns() if turn.status == "completed"]
        self.assertEqual(len(completed), 1)
        first_turn = completed[0]
        initial_chat_events = [message.seq for message in self.events.history
                               if message.channel_id == CHAT_CHANNEL and message.principal == AGENT
                               and json.loads(message.payload).get("turn_id") == first_turn.turn_id]
        self.assertEqual([item["role"] for item in followups[0]], ["system", "user"])
        result = next(event["result"] for event in json.loads(followups[0][1]["content"])["events"] if event["type"] == "tool_result")
        self.assertEqual((result["exit_code"], result["stdout_bytes"], result["stderr_bytes"]), (0, 4, 0))
        crash_enabled[0] = False
        self.restart()
        prepared = self.run_calls()
        self.assertIsNotNone(self.engine.journal.get(prepared.key + ":freeze"))
        self.assertEqual(len(self.events.messages("cmd.run.request", CMD_CHANNEL)), 1)
        self.assertEqual(len(self.events.messages("infer.request", MODEL_CHANNEL)), 2)
        self.assertEqual(initial_chat_events, [message.seq for message in self.events.history
                         if message.channel_id == CHAT_CHANNEL and message.principal == AGENT
                         and json.loads(message.payload).get("turn_id") == first_turn.turn_id])
        restored = self.visible_turn(AGENT, first_turn.turn_id)
        self.assertEqual((restored.text, restored.status), ("working", "completed"))


if __name__ == "__main__":
    unittest.main()
