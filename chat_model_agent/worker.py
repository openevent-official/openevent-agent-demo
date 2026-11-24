"""Deployment reader and per-conversation coordinators.

The reader keeps running while a conversation publishes, prepares attachments, or
waits for a model/tool. Each conversation has exactly one side-effect writer.
"""
from __future__ import annotations

import logging
import asyncio
import json
import threading
import time
from bisect import bisect_right

from openevent.chat_sdk import parse_message as parse_chat
from openevent.cmd_sdk import parse_message as parse_cmd
from openevent.model_proxy_sdk import parse_message as parse_model
from openevent.sdk.proto import openevent_pb2

from .budget import ContextBudget
from .objects import ObjectIndex
from .rpc import InfrastructureError, Rpc
from .scheduling import AsyncCondition, RpcPool
from .source import EventHeader, parsed_header
from .state import ChatProjection, HistoryError, TurnState
from .wal import Journal, PROTOCOL

LOG = logging.getLogger(__name__)


class Stopped(Exception):
    pass


class ChatModelAgent:
    def __init__(self, config, events, *, clock=None):
        self.config, self.events = config, events
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.stop = threading.Event()
        self.condition = AsyncCondition(threading.RLock())
        self.rpc = Rpc(events, config.rpc)
        self.rpc_pool = RpcPool()
        self.journal = Journal(self.rpc, config.agent.principal, config.agent.token,
                               config.channels.wal, confirm=self.aconfirm, blocking=self.blocking,
                               load_event=lambda seq: self.source_event(config.channels.wal, seq))
        self.messages = {}
        self.parsed = {}
        self.channels = {}
        self.model_streams = {}
        self.model_buffers = {}
        self.activity_versions = {}
        self.model_owners = {}
        self.cmd_owners = {}
        self.cmd_uuids = {}
        self.cmd_results = {}
        self.input_attachments = {}
        self.cursor = 0
        self.scanned_through = -1
        self.recovery_high = None
        self.recovery_pending = set()
        self.recovery_cmds = {}
        self.chat_candidates = {}
        self.chats = {}
        self.started_sessions = set()
        self.tasks = set()
        self.reader_thread = None
        self.reader_stop = threading.Event()
        self.reader_error = None
        self.error = None
        self._closed = False
        self.model_slots = threading.BoundedSemaphore(config.agent.max_model_concurrency)
        self.objects = ObjectIndex(batch_metadata_reader=lambda keys: self.rpc.call(
            "BatchGetObjectMetadata", lambda: events.batch_get_object_metadata(
                [openevent_pb2.ObjectKey(**key) for key in keys])).metadata)

    def check_running(self):
        if self.stop.is_set():
            raise Stopped()

    async def blocking(self, operation, *args, **kwargs):
        return await self.rpc_pool.call(operation, *args, **kwargs)

    async def wait_async(self, predicate, timeout=None):
        return await self.condition.wait_async(predicate, timeout)

    def fail(self, error):
        with self.condition:
            if self.error is None:
                self.error = error
                LOG.error("Agent stopped: %s", error)
            self.request_stop()

    def request_stop(self):
        with self.condition:
            self.stop.set()
            self.condition.notify_all()

    def _channel(self, channel_id):
        response = self.rpc.call("GetChannel", lambda: self.events.get_channel(
            self.config.agent.principal, self.config.agent.token, channel_id))
        return response.channel

    def validate_channels(self):
        cfg = self.config
        for cid, protocol, members in (
            (cfg.channels.model, "llm.v1", {cfg.agent.principal, cfg.model_proxy.principal}),
            (cfg.channels.cmd, "cmd.v4", {cfg.agent.principal, cfg.cmd_worker.principal}),
            (cfg.channels.wal, PROTOCOL, {cfg.agent.principal}),
        ):
            channel = self._channel(cid)
            if (channel.protocol != protocol or set(channel.members) != members
                    or channel.visibility != openevent_pb2.VISIBILITY_PRIVATE):
                raise HistoryError(f"incompatible auxiliary Channel {cid}")
            self._register(cid)

    def _register(self, channel_id, *, chat=False):
        with self.condition:
            if chat:
                self.chats.setdefault(channel_id, ChatProjection())
            self.channels.setdefault(channel_id, [])

    def _chat_candidate(self, message):
        """Resolve discovery before committing system state under the reader lock."""
        payload = json.loads(message.payload)
        kind, cid = payload.get("kind"), int(message.channel_id)
        if kind == "system.channel_initialized":
            data = payload["data"]
            if data["protocol"] != "chat.v1":
                return
            member = self.config.agent.principal in data["members"]
            with self.condition:
                return self.chat_candidates.get(cid, False) or member
        elif kind == "system.member_added" and payload["data"]["member"] == self.config.agent.principal:
            with self.condition:
                if cid in self.chats or cid in self.chat_candidates:
                    return True
            if self._channel(cid).protocol == "chat.v1":
                return True

    def ingest(self, message):
        """Expose each projection and its confirmed position together."""
        seq, cid = int(message.seq), int(message.channel_id)
        system = int(message.principal) == 0
        candidate = self._chat_candidate(message) if system else None
        with self.condition:
            if system:
                if candidate is not None:
                    self.chat_candidates[cid] = candidate
                    if candidate:
                        self._register(cid, chat=True)
            else:
                self._ingest_business(message)
            self._advance_cursor(seq + 1)

    def _ingest_business(self, message):
        """Update business indexes while ingest holds the reader state lock."""
        seq, cid = int(message.seq), int(message.channel_id)
        if seq in self.messages:
            return
        owner = None
        if cid == self.config.channels.wal:
            parsed = self.journal.observe(message)
            owner = parsed.chat_channel_id
            if parsed.kind == "attempt.prepare":
                self.model_owners[parsed.data["stream_id"]] = owner
            elif parsed.kind == "effect.prepare" and parsed.data["module"] == "cmd":
                self.cmd_uuids[parsed.data["uuid"]] = owner
        elif cid == self.config.channels.model:
            parsed = parse_model(message)
            stream_id = parsed.payload.stream_id
            self.model_streams.setdefault(stream_id, []).append(seq)
            owner = self.model_owners.get(stream_id)
            if (stream_id in self.model_buffers
                    and parsed.payload.kind in {"infer.result", "infer.append", "infer.end"}):
                self.model_buffers[stream_id][seq] = parsed
            parsed = parsed_header(message, parsed, "model")
        elif cid == self.config.channels.cmd:
            parsed = parse_cmd(message)
            if parsed.payload.kind.endswith(".result"):
                self.cmd_results.setdefault((parsed.payload.kind, parsed.payload.prev_seq), seq)
                owner = self.cmd_owners.get(parsed.payload.prev_seq)
            else:
                owner = self.cmd_uuids.get(int(message.uuid))
                if owner is not None:
                    self.cmd_owners[seq] = owner
            parsed = parsed_header(message, parsed, "cmd")
            self._index_recovery_cmd(int(message.uuid), parsed)
        elif cid in self.chats:
            parsed = parse_chat(message)
            self.chats[cid].apply(parsed)
            owner = cid
            for key in parsed.object_keys:
                self.objects.register(key)
            if (parsed.principal == self.config.agent.user_principal
                    and parsed.payload["kind"] == "turn.single"
                    and self.chats[cid].by_creation[seq].has_content):
                sources = self.input_attachments.setdefault(cid, {})
                for ident in self.input_object_ids(cid, seq):
                    sources.setdefault(ident, []).append(seq)
            parsed = parsed_header(message, parsed, "chat")
        else:
            return
        if owner is not None:
            self.activity_versions[owner] = self.activity_versions.get(owner, 0) + 1
        self.messages[seq] = EventHeader.from_message(message)
        self.parsed[seq] = parsed
        self.channels.setdefault(cid, []).append(seq)

    def _advance_cursor(self, next_seq):
        """Called under condition, after all sources before next_seq are applied."""
        if next_seq > self.cursor:
            self.cursor, self.scanned_through = next_seq, next_seq - 1
            self.condition.notify_all()

    def _fetch_page(self, through=None):
        """Apply sources in order, then confirm the page's remaining scan range."""
        cursor = self.cursor
        page = self.rpc.call("Fetch", lambda: self.events.fetch(
            principal=self.config.agent.principal, token=self.config.agent.token,
            from_seq=cursor, limit=1000, channels=[], only_my_recipient=False))
        next_seq = int(page.next_seq)
        if next_seq < cursor:
            raise HistoryError("Fetch cursor moved backwards")
        for message in page.messages:
            if (int(message.seq) >= cursor
                    and (through is None or int(message.seq) <= through)):
                self.ingest(message)
        if through is not None:
            next_seq = min(next_seq, through + 1)
            if next_seq <= cursor and cursor <= through:
                raise InfrastructureError("Fetch did not advance to the requested recovery watermark")
        if next_seq <= cursor and cursor <= int(page.last_seq):
            raise InfrastructureError("Fetch did not advance through available history")
        with self.condition:
            self._advance_cursor(next_seq)
        return bool(page.messages) or next_seq <= int(page.last_seq)

    def _confirmation_ready(self, through):
        if self.scanned_through >= through:
            return True
        if self.reader_error is not None:
            raise InfrastructureError("Shared reader failed during publication confirmation") from self.reader_error
        if self.reader_stop.is_set():
            raise InfrastructureError("Shared reader stopped before publication confirmation")
        return False

    async def aread_through(self, channel_id, through):
        await self.condition.wait_async(lambda: self._confirmation_ready(through))

    async def aconfirm(self, channel_id, seq):
        """Wait for the shared reader and verify identity without reloading a body."""
        await self.aread_through(channel_id, seq)
        return self.event_header(channel_id, seq)

    def source_event(self, channel_id, seq):
        """Load one observed immutable event, without ingesting or caching it."""
        header = self.event_header(channel_id, seq)
        page = self.rpc.call("Fetch source", lambda: self.events.fetch(
            principal=self.config.agent.principal, token=self.config.agent.token,
            from_seq=seq, limit=1, channels=[channel_id], only_my_recipient=False))
        event = next((message for message in page.messages if int(message.seq) == seq), None)
        if event is None or EventHeader.from_message(event) != header:
            raise HistoryError(f"source event {seq} is absent or conflicts with observed history")
        return event

    def event_header(self, channel_id, seq):
        """Read identity and time without fetching a historical payload."""
        with self.condition:
            header = self.messages.get(seq)
            if header is None or header.channel_id != channel_id:
                raise HistoryError(f"committed event {seq} is absent from readable history")
            return header

    def parsed_event(self, channel_id, seq):
        """Materialize a source payload only for the operation that needs it."""
        raw = self.source_event(channel_id, seq)
        if channel_id == self.config.channels.model:
            return parse_model(raw)
        if channel_id == self.config.channels.cmd:
            return parse_cmd(raw)
        if channel_id == self.config.channels.wal:
            from .wal import parse
            return parse(raw)
        return parse_chat(raw)

    def events_in(self, channel_id, *, after=None):
        with self.condition:
            seqs = self.channels.get(channel_id, ())
            start = 0 if after is None else bisect_right(seqs, after)
            return [self.parsed[seq] for seq in seqs[start:]]

    def _index_recovery_cmd(self, uuid, event):
        candidates = self.recovery_cmds.get(uuid)
        if candidates is not None:
            candidates.append(event)

    def cmd_headers(self, uuid, after):
        with self.condition:
            candidates = self.recovery_cmds.get(uuid)
            if candidates is not None:
                return [event for event in candidates if event.seq > after]
        return [event for event in self.events_in(self.config.channels.cmd, after=after)
                if self.messages[event.seq].uuid == uuid]

    def finish_recovery(self, channel_id):
        with self.condition:
            self.recovery_pending.discard(channel_id)
            if not self.recovery_pending:
                self.recovery_cmds.clear()

    def model_headers(self, stream_id, after_seq=0, through_seq=None):
        """Read only stream identities/status; safe in cooperative predicates."""
        with self.condition:
            seqs = self.model_streams.get(stream_id, ())
            start = bisect_right(seqs, after_seq)
            end = len(seqs) if through_seq is None else bisect_right(seqs, through_seq)
            return [self.parsed[seq] for seq in seqs[start:end]]

    def has_model_events(self, stream_id, after_seq=0, through_seq=None):
        with self.condition:
            seqs = self.model_streams.get(stream_id, ())
            start = bisect_right(seqs, after_seq)
            return start < len(seqs) and (through_seq is None or seqs[start] <= through_seq)

    def begin_model_stream(self, stream_id):
        """Retain incoming responses until this attempt consumes them."""
        with self.condition:
            self.model_buffers[stream_id] = {}

    def end_model_stream(self, stream_id):
        with self.condition:
            self.model_buffers.pop(stream_id, None)

    def model_events(self, stream_id, after_seq=0, through_seq=None, *, consume=False):
        """Reuse live response events; load missing sources outside the reader lock."""
        with self.condition:
            headers = self.model_headers(stream_id, after_seq, through_seq)
            pending = self.model_buffers.get(stream_id, {})
            cached = [(pending.pop(header.seq, None) if consume else pending.get(header.seq))
                      for header in headers]
        return [event if event is not None else
                header if header.payload["kind"] in {"infer.request", "infer.cancel"} else
                self.parsed_event(self.config.channels.model, header.seq)
                for header, event in zip(headers, cached)]

    def cmd_result(self, request_seq, kind):
        """Return the first observed result for this exact Cmd request and kind."""
        with self.condition:
            seq = self.cmd_results.get((kind, request_seq))
            return self.parsed[seq] if seq is not None else None

    def current_turn(self, channel_id, principal, turn_id, *, include_content_seqs=False):
        """Read status cheaply; copy the effective chain only for writer recovery."""
        with self.condition:
            projection = self.chats.get(channel_id)
            turn = projection.turns.get((principal, turn_id)) if projection is not None else None
            return TurnState(turn.principal, turn.turn_id, turn.creation_seq, turn.last_seq,
                             turn.status, turn.terminal_seq, turn.cancelled_by,
                             tuple(turn.content_seqs) if include_content_seqs else ()) if turn is not None else None

    def pending_inputs(self, channel_id, *, after=0):
        """Return ready input positions and their watermark in one locked read."""
        with self.condition:
            projection = self.chats[channel_id]
            ready = [(turn.creation_seq, turn.terminal_seq)
                     for turn in projection.ready(self.config.agent.user_principal, after=after)]
            return ready, projection.through_seq

    def input_object_ids(self, channel_id, input_seq):
        """Read one immutable input's attachment IDs, including direct replies."""
        with self.condition:
            projection = self.chats[channel_id]
            turn = projection.by_creation[input_seq]
            return frozenset(key.object_id
                             for source in (turn, *(projection.by_terminal[seq] for seq in turn.reply_to_seqs))
                             for key in source.object_keys)

    def input_object_sources(self, channel_id, object_id, through):
        """Return this attachment's source positions within a frozen input bound."""
        with self.condition:
            seqs = self.input_attachments.get(channel_id, {}).get(object_id, ())
            return tuple(seqs[:bisect_right(seqs, through)])

    def activity(self, channel_id):
        with self.condition:
            return self.activity_versions.get(channel_id, 0)

    def chat_watermark(self, channel_id):
        with self.condition:
            return self.chats[channel_id].through_seq

    def recover(self):
        self.check_running()
        self.configured_budget = ContextBudget.from_config(self.config.agent)
        self.budget = ContextBudget.for_runtime(self.config, self.configured_budget)
        self.validate_channels()
        cfg = self.config
        high = int(self.rpc.call("GetStatus", lambda: self.events.get_status(
            cfg.agent.principal, cfg.agent.token)).max_seq)
        self.recovery_high = high
        while self.cursor <= high:
            self.check_running()
            self._fetch_page(through=high)
        self._validate_deployment()
        self.check_running()
        first_cmd_intent = None
        for chat in self.chats:
            # Each commit completes one prepare. Fully confirmed chats need no candidate index.
            if (self.journal.count(channel=chat, kind="effect.prepare")
                    == self.journal.count(channel=chat, kind="effect.commit")):
                continue
            for prepared in self.journal.select(channel=chat, kind="effect.prepare"):
                if (prepared.data["module"] == "cmd"
                        and self.journal.get(prepared.key.removesuffix(":prepare") + ":commit") is None):
                    self.recovery_cmds[prepared.data["uuid"]] = []
                    self.recovery_pending.add(chat)
                    if first_cmd_intent is None or prepared.seq < first_cmd_intent:
                        first_cmd_intent = prepared.seq
        if first_cmd_intent is not None:
            for event in self.events_in(cfg.channels.cmd, after=first_cmd_intent):
                self._index_recovery_cmd(self.messages[event.seq].uuid, event)
        return high

    def _deployment(self):
        cfg = self.config
        return {"deployment_id": cfg.deployment_id, "agent": cfg.agent.principal,
                "user": cfg.agent.user_principal, "model": cfg.channels.model,
                "cmd": cfg.channels.cmd, "wal": cfg.channels.wal,
                "model_principal": cfg.model_proxy.principal, "cmd_principal": cfg.cmd_worker.principal}

    def _validate_deployment(self):
        expected = self._deployment()
        for record in self.journal.select(channel=0, kind="deployment"):
            if (record.key != f"deployment:{self.config.deployment_id}"
                    or not record.same_data(expected)):
                raise HistoryError("WAL deployment binding conflicts with the current configuration")

    async def _finish_shutdown(self):
        # No coordinator may add business tasks after stop was set. The reader
        # remains alive while already submitted publications are confirmed.
        with self.condition:
            tasks = self.tasks - {asyncio.current_task()}
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self.condition:
            self.reader_stop.set()
            self.condition.notify_all()
        reader = self.reader_thread
        if reader is not None:
            reader.join()
        if self.error is not None:
            LOG.error("Shutdown handoff incomplete: an operation failed")
            return
        LOG.info("Agent stopped after confirming in-flight operations; business tasks may still run")

    def _start_reader(self):
        with self.condition:
            self.check_running()
            if self.reader_thread is None:
                self.reader_thread = threading.Thread(target=self._read, name="chat-reader", daemon=True)
                self.reader_thread.start()

    def _read(self):
        try:
            while not self.reader_stop.is_set():
                busy = self._fetch_page()
                if not busy:
                    self.reader_stop.wait(self.config.agent.idle_sleep_ms / 1000)
        except Exception as error:
            with self.condition:
                self.reader_error = error
                self.fail(error)

    async def _coordinate(self):
        from .session import Conversation
        try:
            self.check_running()
            await self.blocking(self.recover)
            self._start_reader()
            self.check_running()
            cfg = self.config
            await self.journal.put("deployment", f"deployment:{cfg.deployment_id}", 0, self._deployment())
            while True:
                await self.wait_async(lambda: self.stop.is_set()
                                      or self.chats.keys() - self.started_sessions)
                with self.condition:
                    self.check_running()
                    pending = tuple(self.chats.keys() - self.started_sessions)
                for cid in pending:
                    self.check_running()
                    session = Conversation(self, cid)
                    self.tasks.add(asyncio.create_task(session.run(), name=f"chat-{cid}"))
                    self.started_sessions.add(cid)
        except Stopped:
            pass
        except Exception as error:
            self.fail(error)

    async def aclose(self):
        if self._closed:
            return
        self.request_stop()
        try:
            await self._finish_shutdown()
        finally:
            self._closed = True
            with self.condition:
                self.model_buffers.clear()
            try:
                self.events.close()
            finally:
                self.rpc_pool.close()

    async def _run(self):
        try:
            coordinator = asyncio.create_task(self._coordinate(), name="chat-coordinator")
            with self.condition:
                self.tasks.add(coordinator)
            await self.wait_async(self.stop.is_set)
        except Exception as error:
            self.fail(error)
        finally:
            await self.aclose()
        if self.error is not None:
            raise self.error

    def run(self):
        asyncio.run(self._run())
