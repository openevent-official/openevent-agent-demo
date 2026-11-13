import json
import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openevent.sdk import openevent_pb2

from chat_model_agent.config import parse_config
from chat_model_agent.objects import ObjectIndexError
from chat_model_agent.rpc import InfrastructureError
from chat_model_agent.source import EventHeader
from chat_model_agent.state import ChatProjection, HistoryError, ModelView
from chat_model_agent.wal import Record
from chat_model_agent.worker import ChatModelAgent, Stopped


BUILD = Path(__file__).resolve().parents[1] / "build" / "tests"


class WorkerSDKTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=BUILD)
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config = parse_config({
            "version": "chat-agent.v1", "deployment_id": "demo",
            "openevent": {"target": "events:9527"},
            "agent": {"principal": 10, "user_principal": 11, "token": "private-token",
                      "system_prompt": "", "context_compression_prompt": "compress", "context_max_input_bytes": 1048576, "model": "model", "work_root": str(root / "work")},
            "model_proxy": {"principal": 12}, "cmd_worker": {"principal": 13},
            "channels": {"model": 20, "cmd": 21, "wal": 22},
            "rpc": {"max_retries": 0},
        })
        self.channels = {
            20: self.channel(20, "llm.v1", [10, 12]),
            21: self.channel(21, "cmd.v3", [10, 13]),
            22: self.channel(22, "agent.wal.v5", [10]),
            30: self.channel(30, "chat.v1", [10, 11], creator=11, name="chat-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
        }
        self.message = openevent_pb2.EventMessage(
            seq=5, uuid=5, channel_id=30, principal=11, ts_ms=1000,
            payload=json.dumps({"kind": "turn.single", "turn_id": "input", "content": [], "reply_to_seqs": []}).encode(),
            object_keys=[openevent_pb2.ObjectKey(object_id=99, object_token="object-private-token")],
        )
        self.history = [self.initialized(index, channel) for index, channel in enumerate(self.channels.values(), 1)] + [self.message]
        self.fetch_calls = []
        self.client = SimpleNamespace(get_channel=lambda principal, token, channel_id: SimpleNamespace(channel=self.channels[channel_id]),
                                      get_status=lambda principal, token: SimpleNamespace(max_seq=10),
                                      list_channels=lambda *args, **kwargs: self.fail("Agent must not list Channels"),
                                      fetch=self.fetch, close=lambda: None)
        self.agent = ChatModelAgent(self.config, self.client)
        ingest = self.agent.ingest
        def observed_source(message):
            # Direct-ingest tests still represent durable OpenEvent sources.
            self.history = [item for item in self.history if item.seq != message.seq] + [message]
            self.history.sort(key=lambda item: item.seq)
            ingest(message)
        self.agent.ingest = observed_source
        self.addCleanup(self.stop_reader)
        self.addAsyncCleanup(self.agent.aclose)

    def channel(self, channel_id, protocol, members, *, creator=10, name="aux"):
        return openevent_pb2.ChannelInfo(channel_id=channel_id, protocol=protocol, members=members,
                                         creator=creator, name=name, visibility=openevent_pb2.VISIBILITY_PRIVATE)

    def fetch(self, principal, token, from_seq, limit, only_my_recipient=False, channels=()):
        self.fetch_calls.append((from_seq, channels))
        self.assertTrue(channels == [] or len(channels) == 1)
        self.assertFalse(only_my_recipient)
        messages = [message for message in self.history if message.seq >= from_seq
                    and (not channels or message.channel_id in channels)]
        if channels:
            messages = messages[:limit]
        return openevent_pb2.FetchResponse(messages=messages, next_seq=11, last_seq=10)

    def stop_reader(self):
        self.agent.reader_stop.set()
        if self.agent.reader_thread is not None:
            self.agent.reader_thread.join(1)
            self.assertFalse(self.agent.reader_thread.is_alive())

    def initialized(self, seq, channel):
        return self.system_message(seq, channel.channel_id, "system.channel_initialized", {
            "protocol": channel.protocol, "members": list(channel.members), "creator": channel.creator,
            "name": channel.name, "description": channel.description, "visibility": "private"})

    def system_message(self, seq, cid, kind, data):
        return openevent_pb2.EventMessage(seq=seq, uuid=seq, channel_id=cid, principal=0, ts_ms=1000,
            payload=json.dumps({"kind": kind, "data": {"schema_version": 1, **data}}).encode())

    def test_auxiliary_and_discovered_channels_use_real_sdk_constants(self):
        self.agent.recover()
        self.assertEqual(set(self.agent.channels), {20, 21, 22, 30})
        self.assertEqual(self.agent.cursor, 11)
        self.assertEqual(self.agent.scanned_through, 10)
        self.assertEqual(self.fetch_calls, [(0, [])])
        self.assertEqual([turn.creation_seq for turn in self.agent.chats[30].ready(11)], [5])

    def test_recovery_restores_references_in_memory_without_a_persistent_index(self):
        self.assertEqual(self.agent.recover(), 10)
        key = {"object_id": 99, "object_token": "object-private-token"}
        self.assertEqual(self.agent.objects.get(99), key)
        self.assertFalse(self.agent.started_sessions)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        recovered = ChatModelAgent(self.config, self.client)
        with self.assertRaisesRegex(ObjectIndexError, "not registered"):
            recovered.objects.get(99)
        recovered.recover()
        self.assertEqual(recovered.objects.get(99), key)

    async def test_conversations_start_after_history_and_references_are_recovered(self):
        def start(channel_id):
            self.assertEqual(channel_id, 30)
            self.assertEqual((self.agent.cursor, self.agent.scanned_through), (11, 10))
            self.assertEqual(self.agent.objects.get(99)["object_token"], "object-private-token")
            self.assertIn(channel_id, self.agent.sessions)
            self.agent.request_stop()
        with patch.object(self.agent, "_read"), \
                patch("chat_model_agent.session.Conversation"), \
                patch.object(self.agent.journal, "put"), \
                patch.object(self.agent, "_start_session", side_effect=start) as started:
            await self.agent._coordinate()
        self.stop_reader()
        self.assertIsNone(self.agent.error)
        started.assert_called_once_with(30)

    async def test_all_recovered_conversations_exist_before_reader_or_wal_starts(self):
        channel = self.channel(31, "chat.v1", [10, 11])
        self.history.append(self.initialized(6, channel))
        built = {}
        def construct(engine, channel_id):
            self.assertIs(engine, self.agent)
            session = object()
            built[channel_id] = session
            return session
        def reader():
            self.assertEqual(set(built), {30, 31})
            self.assertEqual(self.agent.sessions, built)
        def publish(*args):
            reader()
            self.agent.request_stop()
        with patch("chat_model_agent.session.Conversation", side_effect=construct), \
                patch.object(self.agent, "_start_reader", side_effect=reader) as read, \
                patch.object(self.agent.journal, "put", side_effect=publish) as put, \
                patch.object(self.agent, "_start_session") as start:
            await self.agent._coordinate()
        read.assert_called_once()
        put.assert_called_once()
        start.assert_not_called()
        self.assertIsNone(self.agent.error)

    def test_live_ingest_registers_references_without_waiting_for_model_preparation(self):
        self.agent.recover()
        message = self.single_message(12, 30)
        message.object_keys.add(object_id=100, object_token="new-reference")
        self.agent.ingest(message)
        self.assertEqual(self.agent.objects.get(100),
                         {"object_id": 100, "object_token": "new-reference"})
        self.agent.ingest(message)
        self.assertEqual(self.agent.channels[30], [5, 12])

    def test_source_reads_leave_only_headers_and_do_not_advance_or_cache(self):
        self.agent.recover()
        self.chat_event(12, "turn.single", turn_id="large", content=[{"type": "text", "text": "large " * 10000}],
                        reply_to_seqs=[])
        self.assertFalse(hasattr(self.agent.messages[12], "payload"))
        self.assertNotIn("content", self.agent.parsed[12].payload)
        self.assertFalse(hasattr(self.agent.chats[30].by_creation[12], "_text_parts"))
        state = (self.agent.cursor, self.agent.scanned_through, self.agent.chat_watermark(30),
                 dict(self.agent.activity_versions))
        original_fetch = self.client.fetch

        def fetch_unlocked(**kwargs):
            self.assertFalse(self.agent.condition._is_owned())
            self.assertEqual((kwargs["from_seq"], kwargs["channels"], kwargs["limit"]), (12, [30], 1))
            return original_fetch(**kwargs)

        with patch.object(self.client, "fetch", side_effect=fetch_unlocked) as fetch:
            first = self.agent.source_event(30, 12)
            second = self.agent.source_event(30, 12)
        self.assertEqual(first, second)
        self.assertEqual(fetch.call_count, 2, "historical source reads must not populate a cache")
        self.assertEqual(state, (self.agent.cursor, self.agent.scanned_through,
                                self.agent.chat_watermark(30), dict(self.agent.activity_versions)))
        self.assertFalse(hasattr(self.agent.messages[12], "payload"))
        self.assertNotIn("content", self.agent.parsed[12].payload)

    def test_model_headers_and_state_do_not_keep_or_expand_bodies(self):
        self.agent.recover()
        message = openevent_pb2.EventMessage(seq=12, uuid=12, channel_id=20, principal=10, ts_ms=12000,
            payload=json.dumps({"kind": "infer.request", "stream_id": "large", "ts_ms": 12000,
                "method": "POST", "path": "/v1/chat/completions", "body": {"prompt": "large " * 10000}}).encode())
        response = openevent_pb2.EventMessage(seq=13, uuid=13, channel_id=20, principal=12, ts_ms=13000,
            payload=json.dumps({"kind": "infer.result", "stream_id": "large", "ts_ms": 13000,
                "prev_seq": 12, "status_code": 200, "body": {}}).encode())
        with patch("openevent.model_proxy_sdk._json.body_copy",
                   side_effect=AssertionError("metadata expanded a model body")):
            self.agent.ingest(message)
            self.agent.ingest(response)
            with patch.object(self.client, "fetch", side_effect=AssertionError("metadata read a source payload")):
                self.assertTrue(self.agent.has_model_events("large"))
                headers = self.agent.model_headers("large")
                indexed = self.agent.recovery_index(20)[("infer.request", "large", None)]
                view = ModelView(12, "large")
                for item in headers:
                    view.apply(item, 10, 12)
                self.assertIs(view.terminal, headers[1])
            source = self.agent.parsed_event(20, 13)
            view = ModelView(12, "large")
            view.apply(source, 10, 12)
            self.assertIs(view.terminal, source)
        self.assertEqual(indexed, headers[:1])
        for item in headers:
            self.assertTrue(item.payload.has_body)
            self.assertIsNone(item.payload.body)
        self.assertFalse(hasattr(self.agent.messages[12], "payload"))
        self.assertEqual(self.agent.parsed_event(20, 12).payload.to_dict()["body"],
                         {"prompt": "large " * 10000})
        self.assertEqual(source.payload.body, {})

    def chat_event(self, seq, kind, **fields):
        self.agent._register(30, chat=True)
        message = openevent_pb2.EventMessage(
            seq=seq, uuid=seq, channel_id=30, principal=11, ts_ms=seq * 1000,
            payload=json.dumps({"kind": kind, "turn_id": "input", **fields}).encode(),
            object_keys=[] if kind == "turn.end" else [openevent_pb2.ObjectKey(object_id=seq, object_token=f"object-{seq}")],
        )
        self.agent.ingest(message)

    def test_writer_state_stays_isolated_from_later_updates(self):
        self.chat_event(1, "turn.start", content=[{"type": "text", "text": "first"}], reply_to_seqs=[])
        with patch.object(self.agent, "source_event", side_effect=AssertionError("state loaded a body")):
            turn = self.agent.current_turn(30, 11, "input", include_content_seqs=True)
            self.assertIsNone(self.agent.current_turn(30, 11, "missing"))
            self.assertIsNone(self.agent.current_turn(31, 11, "input"))
        self.chat_event(2, "turn.append", pre_seq=1, content=[{"type": "text", "text": " appended"}])
        self.assertEqual((turn.turn_id, turn.creation_seq, turn.last_seq, turn.status, turn.content_seqs),
                         ("input", 1, 1, "streaming", (1,)))
        with self.assertRaises(FrozenInstanceError):
            turn.last_seq = 999
        current = self.agent.current_turn(30, 11, "input", include_content_seqs=True)
        self.assertEqual((current.last_seq, current.content_seqs), (2, (1, 2)))
        self.chat_event(3, "turn.reset", pre_seq=2, content=[])
        self.assertEqual(self.agent.current_turn(30, 11, "input", include_content_seqs=True).content_seqs, (3,))
        self.assertEqual(current.content_seqs, (1, 2))

    def test_current_turn_contains_no_text_or_attachment_copies(self):
        self.chat_event(1, "turn.start", content=[{"type": "text", "text": "first"}], reply_to_seqs=[])
        self.chat_event(2, "turn.append", pre_seq=1, content=[{"type": "text", "text": " appended"}])
        class UnreadablePositions(list):
            def __iter__(self):
                raise AssertionError("ordinary status query copied the growing content chain")
        with patch.object(self.agent, "source_event", side_effect=AssertionError("turn body loaded")), \
                patch.object(self.agent.chats[30].by_creation[1], "content_seqs", UnreadablePositions([1, 2])):
            state = self.agent.current_turn(30, 11, "input")
        self.assertEqual((state.principal, state.turn_id, state.creation_seq, state.last_seq,
                          state.status, state.terminal_seq, state.cancelled_by, state.content_seqs),
                         (11, "input", 1, 2, "streaming", None, None, ()))
        self.assertFalse(hasattr(state, "text"))
        self.assertFalse(hasattr(state, "object_keys"))
        self.agent.ingest(openevent_pb2.EventMessage(
            seq=3, uuid=3, channel_id=30, principal=10, ts_ms=3000,
            payload=json.dumps({"kind": "turn.cancel", "start_seq": 1, "target_turn": {
                "principal": 11, "turn_id": "input"}}).encode()))
        current = self.agent.current_turn(30, 11, "input")
        self.assertEqual((current.status, current.terminal_seq, current.cancelled_by, current.content_seqs),
                         ("cancelled", 3, 10, ()))
        self.assertEqual(state.status, "streaming")

    def test_terminal_reply_attachment_index_survives_reset_and_restart(self):
        self.chat_event(2, "turn.single", turn_id="ancestor",
                        content=[{"type": "text", "text": "older"}], reply_to_seqs=[])
        self.chat_event(3, "turn.start", turn_id="reference",
                        content=[{"type": "text", "text": "draft"}], reply_to_seqs=[2])
        self.chat_event(4, "turn.reset", turn_id="reference", pre_seq=3,
                        content=[{"type": "text", "text": "replacement"}])
        self.chat_event(5, "turn.end", turn_id="reference", start_seq=3, pre_seq=4, content_seqs=[4])
        self.chat_event(6, "turn.single", content=[{"type": "text", "text": "question"}], reply_to_seqs=[5])
        self.assertEqual(self.agent.input_object_ids(30, 6), {4, 6})
        self.assertEqual(self.agent.input_object_sources(30, 4, 6), (6,))
        self.assertEqual(self.agent.input_object_sources(30, 2, 6), (2,))
        self.assertIs(self.agent.chats[30].by_terminal[5], self.agent.chats[30].by_creation[3])
        self.chat_event(7, "turn.single", turn_id="next",
                        content=[{"type": "text", "text": "next input"}], reply_to_seqs=[])
        self.history = [self.initialized(1, self.channels[30]),
                        *(message for message in self.history if message.seq in self.agent.messages)]
        restarted = ChatModelAgent(self.config, self.client)
        restarted.recover()
        self.assertEqual(restarted.input_object_ids(30, 6), {4, 6})
        self.assertEqual(restarted.input_object_sources(30, 4, 6), (6,))
        self.assertEqual(restarted.current_turn(30, 11, "reference").content_seqs, ())
        # Objects erased from the current turn remain immutable source objects.
        self.assertEqual(restarted.objects.get(3)["object_token"], "object-3")

    def test_error_end_removes_reply_attachment_sources(self):
        self.chat_event(1, "turn.start", content=[{"type": "text", "text": "draft"}], reply_to_seqs=[])
        self.chat_event(2, "turn.end", start_seq=1, pre_seq=1, error="turn_too_long")
        self.chat_event(3, "turn.single", turn_id="reply", content=[], reply_to_seqs=[2])
        self.assertEqual(self.agent.input_object_ids(30, 3), {3})
        self.assertEqual(self.agent.input_object_sources(30, 1, 3), ())
        self.assertFalse(self.agent.chats[30].by_terminal[2].has_content)

    def test_pending_inputs_skip_consumed_ready_positions_without_copying_history(self):
        self.agent._register(30, chat=True)
        def ingest(seq, kind, *, principal=11, object_keys=(), **fields):
            self.agent.ingest(openevent_pb2.EventMessage(
                seq=seq, uuid=seq, channel_id=30, principal=principal, ts_ms=seq * 1000,
                payload=json.dumps({"kind": kind, **fields}).encode(), object_keys=object_keys))
        content = [{"type": "text", "text": "input"}]
        for seq, kind, turn_id in ((1, "turn.single", "consumed"),
                                   (2, "turn.start", "finishes-later"), (3, "turn.single", "ready"),
                                   (4, "turn.single", "empty"), (5, "turn.start", "cancelled")):
            ingest(seq, kind, turn_id=turn_id, content=[] if turn_id == "empty" else content, reply_to_seqs=[])
        ingest(6, "turn.cancel", start_seq=5, target_turn={"principal": 11, "turn_id": "cancelled"})
        ingest(7, "turn.single", principal=10, turn_id="agent-comment", content=content, reply_to_seqs=[])
        ingest(8, "turn.single", turn_id="attachment", content=[], reply_to_seqs=[],
               object_keys=[openevent_pb2.ObjectKey(object_id=99, object_token="attachment-token")])
        with patch.object(self.agent, "source_event", side_effect=AssertionError("history loaded")), \
                patch.object(ChatProjection, "apply", side_effect=AssertionError("history replayed")):
            self.assertEqual(self.agent.pending_inputs(30, after=1), ([(3, 3), (8, 8)], 8))
        ingest(9, "turn.end", turn_id="finishes-later", start_seq=2, pre_seq=2, content_seqs=[2])
        ready, watermark = self.agent.pending_inputs(30, after=1)
        self.assertEqual((ready, watermark), ([(3, 3), (8, 8), (2, 9)], 9))
        ready.clear()
        self.assertEqual(self.agent.pending_inputs(30, after=8), ([(2, 9)], 9))

    def test_model_events_are_stream_local_incremental_and_keep_chain_validation(self):
        def ingest(seq, stream, kind, **fields):
            message = openevent_pb2.EventMessage(
                seq=seq, uuid=seq, channel_id=20, principal=10 if kind == "infer.request" else 12,
                ts_ms=seq * 1000,
                payload=json.dumps({"kind": kind, "stream_id": stream, "ts_ms": seq * 1000, **fields}).encode(),
            )
            self.agent.ingest(message)
            return message
        for seq, stream in ((10, "a"), (11, "b")):
            ingest(seq, stream, "infer.request", method="POST", path="/v1/chat/completions",
                   body={"model": "model", "messages": [], "stream": True})
        ingest(12, "a", "infer.result", prev_seq=10, status_code=200)
        ingest(13, "b", "infer.result", prev_seq=11, status_code=200)
        append = ingest(14, "a", "infer.append", request_seq=10, prev_seq=12, body={"choices": []})
        ingest(15, "b", "infer.append", request_seq=11, prev_seq=13, body={"choices": []})
        ingest(16, "a", "infer.end", request_seq=10, status_code=200, end_status="completed")
        self.agent.ingest(append)
        with patch.object(self.agent, "events_in", side_effect=AssertionError("shared channel scanned")):
            first = self.agent.model_events("a", through_seq=12)
            second = self.agent.model_events("a", after_seq=12, through_seq=14)
            last = self.agent.model_events("a", after_seq=14)
        self.assertEqual([event.seq for event in first], [10, 12])
        self.assertEqual([event.seq for event in second], [14])
        self.assertEqual([event.seq for event in last], [16])
        self.assertEqual([event.payload.has_body for event in self.agent.model_headers("a")],
                         [True, False, True, False])
        self.assertFalse(self.agent.model_events("a", after_seq=16))
        self.assertFalse(self.agent.model_events("a", after_seq=14, through_seq=12))
        self.assertFalse(self.agent.model_events("missing"))
        view = ModelView(10, "a")
        for event in first + second + last:
            view.apply(event, 10, 12)
        self.assertEqual([event.seq for event in view.chunks], [14])
        self.assertEqual(view.terminal.seq, 16)

    def test_recovery_reads_past_empty_page_using_next_seq(self):
        pages = [openevent_pb2.FetchResponse(next_seq=4, last_seq=10),
                 openevent_pb2.FetchResponse(next_seq=11, last_seq=10)]
        with patch.object(self.client, "fetch", side_effect=pages) as fetch:
            self.agent.recover()
        self.assertEqual([call.kwargs["from_seq"] for call in fetch.call_args_list], [0, 4])
        self.assertEqual((self.agent.cursor, self.agent.scanned_through), (11, 10))

    def test_reader_fetches_all_readable_channels_even_without_registered_sessions(self):
        def fetch(**kwargs):
            self.fetch_calls.append((kwargs["from_seq"], kwargs["channels"]))
            self.agent.reader_stop.set()
            return openevent_pb2.FetchResponse(next_seq=11, last_seq=10)
        with patch.object(self.client, "fetch", side_effect=fetch):
            self.agent._read()
        self.assertIsNone(self.agent.error)
        self.assertEqual(self.fetch_calls, [(0, [])])
        self.assertEqual((self.agent.cursor, self.agent.scanned_through), (11, 10))
        self.assertFalse(self.agent.chats)

    def single_message(self, seq, cid):
        return openevent_pb2.EventMessage(
            seq=seq, uuid=seq, channel_id=cid, principal=11, ts_ms=seq * 1000,
            payload=json.dumps({"kind": "turn.single", "turn_id": f"input-{seq}",
                                "content": [], "reply_to_seqs": []}).encode())

    def test_reader_only_sleeps_after_an_empty_page_at_the_tail(self):
        self.agent._register(30, chat=True)
        pages = [
            openevent_pb2.FetchResponse(messages=[self.single_message(1, 30)], next_seq=2, last_seq=5),
            openevent_pb2.FetchResponse(next_seq=4, last_seq=5),
            openevent_pb2.FetchResponse(messages=[self.single_message(5, 30)], next_seq=6, last_seq=5),
            openevent_pb2.FetchResponse(next_seq=6, last_seq=5),
        ]
        def idle(timeout):
            self.assertFalse(pages, "messages and unread pages must be drained before the idle delay")
            self.agent.reader_stop.set()
        with patch.object(self.client, "fetch", side_effect=lambda **kwargs: pages.pop(0)) as fetched, \
                patch.object(self.agent.reader_stop, "wait", side_effect=idle) as waited:
            self.agent._read()
        self.assertIsNone(self.agent.error)
        self.assertEqual(fetched.call_count, 4)
        waited.assert_called_once_with(self.config.agent.idle_sleep_ms / 1000)
        self.assertEqual(self.agent.channels[30], [1, 5])

    def test_short_pages_and_unrelated_business_advance_one_global_cursor(self):
        self.agent._register(30, chat=True)
        unrelated = openevent_pb2.EventMessage(seq=2, channel_id=99, principal=14, payload=b"not Chat JSON")
        pages = [
            openevent_pb2.FetchResponse(messages=[unrelated], next_seq=3, last_seq=10),
            openevent_pb2.FetchResponse(next_seq=5, last_seq=10),
            openevent_pb2.FetchResponse(messages=[self.message], next_seq=11, last_seq=10),
        ]
        with patch.object(self.client, "fetch", side_effect=pages) as fetched:
            self.agent._fetch_page()
            self.agent._fetch_page()
            self.agent._fetch_page()
        self.assertEqual([call.kwargs["from_seq"] for call in fetched.call_args_list], [0, 3, 5])
        self.assertTrue(all(call.kwargs["channels"] == [] for call in fetched.call_args_list))
        self.assertEqual(self.agent.channels[30], [5])
        self.assertNotIn(99, self.agent.channels)
        self.assertEqual((self.agent.cursor, self.agent.scanned_through), (11, 10))

    async def test_unchanged_empty_tail_does_not_wake_idle_chats_but_stop_does(self):
        from chat_model_agent.session import Conversation

        self.agent.cursor, self.agent.scanned_through = 11, 10
        scheduling_checks = []
        async def next_answer():
            scheduling_checks.append(None)
            return None
        async def ready():
            return None
        tasks = []
        with patch("chat_model_agent.session.create_chat"), patch("chat_model_agent.session.create_model"), \
                patch("chat_model_agent.tool_runner.create_client"), \
                patch.object(self.agent, "chat_watermark", wraps=self.agent.chat_watermark) as watermark:
            for channel_id in range(100, 220):
                self.agent._register(channel_id, chat=True)
                conversation = Conversation(self.agent, channel_id)
                conversation._reconcile = ready
                conversation._check_recovery_budget = ready
                conversation._reject_inputs = ready
                conversation._next_answer = next_answer
                task = asyncio.create_task(conversation.run())
                tasks.append(task)
                self.agent.tasks.add(task)
            async def initial_checks():
                while len(scheduling_checks) < 120 and not self.agent.stop.is_set():
                    await asyncio.sleep(0.001)
            await asyncio.wait_for(initial_checks(), 3)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(len(scheduling_checks), 120)
            baseline = watermark.call_count
            for _ in range(20):
                self.assertFalse(self.agent._fetch_page())
                # A notification would first enqueue wake callbacks, then tasks.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            self.assertEqual(watermark.call_count, baseline)
            self.assertEqual(len(scheduling_checks), 120)
            self.assertTrue(all(not task.done() for task in tasks))
            self.assertEqual((self.agent.cursor, self.agent.scanned_through), (11, 10))
            self.agent.request_stop()
            await asyncio.wait_for(asyncio.gather(*tasks), 1)
        self.assertTrue(all(task.done() for task in tasks))
        self.assertIsNone(self.agent.error)

    async def test_empty_and_unrelated_pages_still_wake_readback_when_the_watermark_advances(self):
        unrelated = openevent_pb2.EventMessage(seq=7, channel_id=99, principal=14, payload=b"not Chat JSON")
        pages = [openevent_pb2.FetchResponse(next_seq=5, last_seq=4),
                 openevent_pb2.FetchResponse(messages=[unrelated], next_seq=8, last_seq=7)]
        for page in pages:
            with self.subTest(messages=len(page.messages)):
                confirmation = asyncio.create_task(self.agent.aread_through(30, page.last_seq))
                await asyncio.sleep(0)
                self.assertFalse(confirmation.done())
                with patch.object(self.client, "fetch", return_value=page):
                    self.agent._fetch_page()
                await asyncio.wait_for(confirmation, 1)
                self.assertEqual(self.agent.scanned_through, page.last_seq)
                self.assertFalse(self.agent.messages)

    def test_live_discovery_and_first_input_share_the_same_page_without_rescanning(self):
        self.agent.cursor, self.agent.scanned_through = 11, 10
        created = self.initialized(11, self.channels[30])
        first = self.single_message(12, 30)
        with patch.object(self.client, "fetch", return_value=openevent_pb2.FetchResponse(
                messages=[created, first], next_seq=13, last_seq=12)) as fetch, \
                patch.object(self.agent, "_start_session") as start:
            self.agent._fetch_page()
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["from_seq"], 11)
        self.assertEqual(self.agent.channels[30], [12])
        start.assert_not_called()

    def test_recovery_watermark_leaves_later_messages_for_the_same_reader(self):
        later = self.single_message(12, 30)
        page = openevent_pb2.FetchResponse(messages=[*self.history, later], next_seq=13, last_seq=12)
        with patch.object(self.client, "fetch", return_value=page):
            self.agent.recover()
        self.assertEqual(self.agent.channels[30], [5])
        self.assertEqual((self.agent.cursor, self.agent.scanned_through), (11, 10))
        with patch.object(self.client, "fetch", return_value=openevent_pb2.FetchResponse(
                messages=[later], next_seq=13, last_seq=12)) as fetched:
            self.agent._fetch_page()
        self.assertEqual(fetched.call_args.kwargs["from_seq"], 11)
        self.assertEqual(self.agent.channels[30], [5, 12])

    def test_discovery_only_requires_chat_protocol_and_own_membership(self):
        for index, changes in enumerate(({"visibility": openevent_pb2.VISIBILITY_PUBLIC},
                {"description": "another application"}, {"creator": 88}, {"name": "arbitrary"},
                {"members": [10, 77, 88]}, {"members": [10]}), 1):
            channel = self.channel(30 + index, "chat.v1", [10, 11])
            for field, value in changes.items():
                if field == "members":
                    del channel.members[:]
                    channel.members.extend(value)
                else:
                    setattr(channel, field, value)
            self.agent.ingest(self.initialized(index, channel))
            self.assertIn(channel.channel_id, self.agent.chats)
        for index, protocol, members in ((7, "other.v1", [10]), (8, "chat.v1", [11, 77])):
            channel = self.channel(30 + index, protocol, members)
            self.agent.ingest(self.initialized(index, channel))
            self.assertNotIn(channel.channel_id, self.agent.chats)

    def test_own_join_fetches_missing_protocol_once_before_dispatching_first_input(self):
        join = self.system_message(4, 30, "system.member_added", {"member": 10})
        page = openevent_pb2.FetchResponse(messages=[join, self.message], next_seq=11, last_seq=10)
        original = self.client.get_channel
        def lookup(*args):
            self.assertFalse(self.agent.condition._is_owned(), "discovery RPC held the reader state lock")
            return original(*args)
        with patch.object(self.client, "get_channel", side_effect=lookup) as get_channel, \
                patch.object(self.client, "fetch", return_value=page):
            self.agent._fetch_page()
            self.agent.ingest(self.system_message(12, 30, "system.member_added", {"member": 10}))
        get_channel.assert_called_once_with(10, "private-token", 30)
        self.assertEqual(self.agent.channels[30], [5])

    def test_known_chat_join_uses_minimal_cached_protocol_and_other_members_do_not_register(self):
        channel = self.channel(30, "chat.v1", [11, 77])
        with patch.object(self.client, "get_channel") as get_channel:
            self.agent.ingest(self.initialized(1, channel))
            self.agent.ingest(self.system_message(2, 30, "system.member_added", {"member": 88}))
            self.assertNotIn(30, self.agent.chats)
            self.agent.ingest(self.system_message(3, 30, "system.member_added", {"member": 10}))
        get_channel.assert_not_called()
        self.assertIn(30, self.agent.chats)

    def test_system_events_are_never_sent_to_any_business_parser(self):
        with patch("chat_model_agent.worker.parse_chat") as chat, \
                patch("chat_model_agent.worker.parse_cmd") as cmd, \
                patch("chat_model_agent.worker.parse_model") as model, \
                patch.object(self.agent.journal, "observe") as wal:
            for seq, cid in enumerate((0, 20, 21, 22, 99), 1):
                self.agent.ingest(self.system_message(seq, cid, "system.member_added", {"member": 77}))
        for parser in (chat, cmd, model, wal):
            parser.assert_not_called()
        self.assertFalse(self.agent.messages)

    async def test_confirmation_waits_for_global_watermark_without_its_own_fetch(self):
        self.agent._register(30, chat=True)
        with patch.object(self.client, "fetch", wraps=self.client.fetch) as fetch, \
                patch.object(self.agent, "blocking", side_effect=AssertionError("confirmation occupied an RPC worker")):
            confirmation = asyncio.create_task(self.agent.aconfirm(30, 5))
            await asyncio.sleep(0)
            self.assertFalse(confirmation.done())
            fetch.assert_not_called()
            self.agent._start_reader()
            self.assertEqual(await asyncio.wait_for(confirmation, 1), EventHeader.from_message(self.message))
            self.assertTrue(fetch.call_args_list)
            self.assertTrue(all(call.kwargs["channels"] == [] for call in fetch.call_args_list))

    async def test_reader_failure_wakes_a_waiting_publication_confirmation(self):
        confirmation = asyncio.create_task(self.agent.aread_through(30, 5))
        await asyncio.sleep(0)
        self.assertFalse(confirmation.done())
        with patch.object(self.client, "fetch", side_effect=RuntimeError("unavailable")):
            self.agent._start_reader()
            with self.assertRaisesRegex(InfrastructureError, "Shared reader failed"):
                await asyncio.wait_for(confirmation, 1)
        self.assertTrue(self.agent.stop.is_set())
        self.assertIsInstance(self.agent.error, InfrastructureError)

    async def test_scanned_but_absent_event_is_an_error_without_rescanning(self):
        self.agent.scanned_through, self.agent.cursor = 10, 11
        with patch.object(self.client, "fetch") as fetch:
            with self.assertRaisesRegex(HistoryError, "absent"):
                await self.agent.aconfirm(30, 5)
        fetch.assert_not_called()

    async def test_confirmation_rejects_an_event_from_another_channel_without_fetching(self):
        self.agent.recover()
        with patch.object(self.client, "fetch") as fetch:
            with self.assertRaisesRegex(HistoryError, "absent"):
                await self.agent.aconfirm(20, self.message.seq)
        fetch.assert_not_called()

    async def test_async_wait_registers_before_a_reader_can_notify(self):
        observed, updated = threading.Event(), threading.Event()
        def predicate():
            observed.set()
            return updated.is_set()
        def reader():
            if not observed.wait(1):
                return
            with self.agent.condition:
                updated.set()
                self.agent.condition.notify_all()
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            self.assertTrue(await asyncio.wait_for(self.agent.wait_async(predicate), 1))
        finally:
            thread.join(1)
        self.assertFalse(thread.is_alive())

    async def test_async_wait_does_not_repeat_a_successful_capacity_acquisition(self):
        available = threading.BoundedSemaphore(1)
        checks = []
        def acquire():
            checks.append(None)
            return available.acquire(blocking=False)
        self.assertTrue(await self.agent.wait_async(acquire))
        self.assertEqual(len(checks), 1)
        available.release()

    async def test_async_confirmation_survives_stop_until_reader_confirms(self):
        confirmation = asyncio.create_task(self.agent.aread_through(30, 50))
        await asyncio.sleep(0)
        self.agent.request_stop()
        await asyncio.sleep(0)
        self.assertFalse(confirmation.done())
        with self.agent.condition:
            self.agent._advance_cursor(51)
        await asyncio.wait_for(confirmation, 1)

    async def test_rpc_pool_is_bounded_and_shutdown_drains_submitted_work(self):
        agent = ChatModelAgent(self.config, self.client)
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        lock = threading.Lock()
        active = 0
        def blocked():
            nonlocal active
            with lock:
                active += 1
                if active == 4:
                    loop.call_soon_threadsafe(entered.set)
            release.wait()
        tasks = [asyncio.create_task(agent.blocking(blocked), name=f"blocked-{i}") for i in range(4)]
        agent.tasks.update(tasks)
        dispatched = []
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            queued = asyncio.create_task(agent.blocking(lambda: dispatched.append(True)), name="queued")
            agent.tasks.add(queued)
            await asyncio.sleep(0)
            self.assertEqual(len(agent.rpc_pool.threads), 4)
            self.assertTrue(all(thread.daemon for thread in agent.rpc_pool.threads))
            with patch.object(self.client, "close") as close:
                closing = asyncio.create_task(agent.aclose())
                await asyncio.sleep(0)
                self.assertFalse(closing.done())
                close.assert_not_called()
                self.assertFalse(queued.cancelled())
                release.set()
                await asyncio.wait_for(closing, 1)
                await agent.aclose()
                close.assert_called_once()
            self.assertIsNone(agent.error)
            self.assertTrue(all(task.done() and not task.cancelled() for task in [*tasks, queued]))
        finally:
            release.set()
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await agent.aclose()
            for thread in agent.rpc_pool.threads:
                thread.join(1)
                self.assertFalse(thread.is_alive())
        self.assertEqual(dispatched, [True])

    async def test_queued_effect_checks_stop_when_a_busy_rpc_worker_becomes_available(self):
        from chat_model_agent.session import Conversation

        self.agent.recover()
        self.client.timeout_ms = self.config.rpc.timeout_ms
        def unexpected_rpc(*args, **kwargs):
            self.fail("queued publication must not reach the SDK")
        self.client.get_uuid = self.client.get_seq_by_uuid = self.client.publish_auto_seq = unexpected_rpc
        conversation = Conversation(self.agent, 30)
        intent = {"module": "model", "kind": "infer.request", "stream_id": "queued"}
        self.agent.journal._store(Record(
            11, "effect.prepare", "queued:prepare", 30, {"channel": 20, **intent}))
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        lock = threading.Lock()
        active = 0
        def blocked():
            nonlocal active
            with lock:
                active += 1
                if active == 4:
                    loop.call_soon_threadsafe(entered.set)
            release.wait()
        blockers = [asyncio.create_task(self.agent.blocking(blocked)) for _ in range(4)]
        effect = None
        published = []
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with patch.object(self.agent.rpc_pool, "submit", wraps=self.agent.rpc_pool.submit) as submit:
                effect = asyncio.create_task(conversation.effect(
                    "queued", 20, lambda event: False, lambda: published.append(True), intent))
                await asyncio.sleep(0)
                submit.assert_called_once()
                self.assertFalse(effect.done())
            self.agent.request_stop()
            # Let the queued dispatch execute before shutdown cancels any tasks.
            release.set()
            with self.assertRaises(Stopped):
                await asyncio.wait_for(effect, 1)
            await asyncio.gather(*blockers)
            self.assertEqual(published, [])
            self.assertIsNone(self.agent.journal.get("queued:commit"))
        finally:
            release.set()
            await asyncio.gather(*blockers, *([effect] if effect is not None else []), return_exceptions=True)

    async def test_shutdown_keeps_reader_alive_until_inflight_confirmation_finishes(self):
        self.agent._register(30, chat=True)
        finished = asyncio.Event()
        async def confirm():
            await self.agent.aconfirm(30, 5)
            finished.set()
        task = asyncio.create_task(confirm(), name="pending-confirmation")
        self.agent.tasks.add(task)
        self.agent._start_reader()
        self.agent.request_stop()
        await self.agent._finish_shutdown()
        self.assertTrue(finished.is_set())
        self.assertTrue(task.done())
        self.assertTrue(self.agent.reader_stop.is_set())
        self.assertFalse(self.agent.reader_thread.is_alive())
        self.assertIsNone(self.agent.error)

    def test_recovery_rejects_other_deployment_or_changed_binding_before_dispatch(self):
        for key, changes in (("deployment:other", {"deployment_id": "other"}),
                             ("deployment:demo", {"model": 999})):
            with self.subTest(key=key, changes=changes):
                agent = ChatModelAgent(self.config, self.client)
                agent.journal._store(Record(1, "deployment", key, 0, {**agent._deployment(), **changes}))
                with self.assertRaisesRegex(HistoryError, "deployment binding"):
                    agent.recover()
                self.assertFalse(agent.started_sessions)

    def test_recovery_accepts_matching_persisted_binding(self):
        self.agent.journal._store(Record(
            1, "deployment", "deployment:demo", 0, self.agent._deployment()))
        self.assertEqual(self.agent.recover(), 10)

    def stop_before_dispatch(self):
        self.agent.request_stop()

    def test_shutdown_without_work_needs_no_additional_wal_publication(self):
        with patch.object(self.agent, "recover", side_effect=self.stop_before_dispatch), \
                patch.object(self.agent.journal, "put") as put, \
                patch("chat_model_agent.worker.LOG.info") as logged:
            self.agent.run()
        put.assert_not_called()
        logged.assert_called_once()

    def test_recovery_failure_still_closes_event_client(self):
        with patch.object(self.agent, "recover", side_effect=HistoryError("invalid history")), \
                patch.object(self.client, "close") as closed:
            with self.assertRaisesRegex(HistoryError, "invalid history"):
                self.agent.run()
        self.assertFalse(self.agent.started_sessions)
        closed.assert_called_once()

    def test_shutdown_waits_for_startup_rpc_without_dispatching(self):
        agent = ChatModelAgent(self.config, self.client)
        entered, release = threading.Event(), threading.Event()
        failures = []
        def blocked(*args):
            entered.set()
            release.wait()
        def run():
            try:
                agent.run()
            except Exception as error:
                failures.append(error)
        with patch.object(agent, "recover", side_effect=blocked), \
                patch.object(agent.journal, "put"), patch("chat_model_agent.worker.LOG.info") as logged:
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                agent.request_stop()
                self.assertTrue(agent.stop.is_set())
                self.assertTrue(thread.is_alive())
                logged.assert_not_called()
                release.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                logged.assert_called_once()
            finally:
                release.set()
                thread.join(1)
                for background in agent.rpc_pool.threads:
                    background.join(1)

    def test_shutdown_waits_for_recovered_chat_client_without_starting_next(self):
        config = self.config
        self.channels[31] = self.channel(31, "chat.v1", [10, 11])
        self.history.append(self.initialized(6, self.channels[31]))
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        failures, chat_lookups = [], []
        def get_channel(principal, token, channel_id):
            if channel_id in (30, 31):
                chat_lookups.append(channel_id)
                entered.set()
                release.wait()
            return SimpleNamespace(channel=self.channels[channel_id])
        def unexpected_publication(*args, **kwargs):
            self.fail("no publication may start during recovered client construction")
        client = SimpleNamespace(**{**vars(self.client), "get_channel": get_channel,
                                   "timeout_ms": config.rpc.timeout_ms,
                                   "get_uuid": unexpected_publication,
                                   "get_seq_by_uuid": unexpected_publication,
                                   "publish_auto_seq": unexpected_publication})
        agent = ChatModelAgent(config, client)
        def run():
            try:
                agent.run()
            except Exception as error:
                failures.append(error)
        def stop():
            agent.request_stop()
            stopped.set()
        with patch.object(agent, "_start_reader") as read, \
                patch.object(agent, "_start_session") as start, \
                patch.object(agent.journal, "put") as put, \
                patch.object(client, "close") as closed:
            thread = threading.Thread(target=run, daemon=True)
            stopper = threading.Thread(target=stop, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(1), "real Chat SDK construction did not reach GetChannel")
                stopper.start()
                self.assertTrue(stopped.wait(1), "GetChannel blocked the shutdown request")
                self.assertTrue(agent.stop.is_set())
                self.assertTrue(thread.is_alive())
                closed.assert_not_called()
                release.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                closed.assert_called_once()
            finally:
                release.set()
                if stopper.ident is not None:
                    stopper.join(1)
                thread.join(1)
                for background in agent.rpc_pool.threads:
                    background.join(1)
                    self.assertFalse(background.is_alive())
            self.assertEqual(chat_lookups, [30], "shutdown must not construct the next conversation")
            self.assertEqual(agent.sessions, {})
            self.assertFalse(agent.started_sessions)
            read.assert_not_called()
            put.assert_not_called()
            start.assert_not_called()

    def test_recovery_stopped_in_rpc_does_not_dispatch(self):
        agent = ChatModelAgent(self.config, self.client)
        def status(principal, token):
            agent.request_stop()
            return SimpleNamespace(max_seq=10)
        from chat_model_agent.worker import Stopped
        with patch.object(self.client, "get_status", side_effect=status):
            with self.assertRaises(Stopped):
                agent.recover()
        self.assertFalse(agent.started_sessions)


if __name__ == "__main__":
    unittest.main()
