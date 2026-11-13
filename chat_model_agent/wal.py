"""agent.wal.v5 records. Payload contracts live in docs/AGENT_WAL_PROTOCOL*.md."""
from __future__ import annotations

import json
import inspect
import threading
import hashlib
from copy import deepcopy
from bisect import bisect_right
from dataclasses import dataclass, field, replace

PROTOCOL = "agent.wal.v5"
KINDS = frozenset({"deployment", "input.reject", "answer.prepare", "attempt.prepare",
                   "attempt.accept", "attempt.fail", "attempt.timeout", "effect.prepare", "effect.commit", "effect.error",
                   "tool.prepare", "tool.done", "tool.timeout", "answer.cancel", "answer.freeze",
                   "answer.wake", "compaction.prepare", "compaction.commit"})


class WalError(ValueError):
    pass


@dataclass(frozen=True)
class Record:
    seq: int
    kind: str
    key: str
    chat_channel_id: int
    data: dict
    fingerprint: bytes | None = field(default=None, repr=False, compare=False)
    deferred: bool = field(default=False, repr=False, compare=False)

    def same_data(self, other):
        """Compare complete fact contents without loading an indexed payload."""
        fingerprint = self.fingerprint or _fingerprint(self.data)
        if isinstance(other, Record):
            return fingerprint == (other.fingerprint or _fingerprint(other.data))
        return type(other) is dict and fingerprint == _fingerprint(other)


def _fingerprint(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, allow_nan=False,
        sort_keys=True, separators=(",", ":")).encode()).digest()


def encode(kind, key, chat_channel_id, data):
    value = {"version": 5, "kind": kind, "key": key,
             "chat_channel_id": chat_channel_id, "data": data}
    validate(value)
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def validate(value):
    if type(value) is not dict or set(value) != {"version", "kind", "key", "chat_channel_id", "data"}:
        raise WalError("invalid WAL fields")
    if type(value["version"]) is not int or value["version"] != 5 or not isinstance(value["kind"], str) or value["kind"] not in KINDS:
        raise WalError("unsupported WAL version or kind")
    if not isinstance(value["key"], str) or not value["key"]:
        raise WalError("WAL key must be nonempty")
    if type(value["chat_channel_id"]) is not int or value["chat_channel_id"] < 0:
        raise WalError("invalid WAL Chat Channel")
    if type(value["data"]) is not dict:
        raise WalError("WAL data must be an object")


def parse(message):
    value = json.loads(message.payload)
    validate(value)
    return Record(int(message.seq), value["kind"], value["key"], value["chat_channel_id"], value["data"])


class Journal:
    def __init__(self, rpc, principal, token, channel_id, *, confirm=None, blocking=None,
                 load_event=None):
        self.rpc, self.principal, self.token, self.channel_id = rpc, principal, token, channel_id
        self.confirm = confirm
        self.blocking = blocking or self._direct
        self.load_event = load_event
        self.records = {}
        self.ordered = {}
        self.lock = threading.RLock()

    def observe(self, message):
        if int(message.principal) != self.principal:
            raise WalError("foreign WAL writer")
        record = self._index_record(parse(message))
        self._store(record)
        return record

    def _index_record(self, record):
        if self.load_event is None or record.fingerprint is not None:
            return record
        data = {key: value for key, value in record.data.items()
                if key not in {"events", "arguments", "request"}}
        deferred = len(data) != len(record.data)
        if isinstance(data.get("settings"), dict):
            settings = {key: value for key, value in data["settings"].items()
                        if key not in {"system_prompt", "context_compression_prompt"}}
            deferred |= len(settings) != len(data["settings"])
            data["settings"] = settings
        return replace(record, data=data, deferred=deferred,
                       fingerprint=record.fingerprint or _fingerprint(record.data))

    def materialize(self, record):
        """Load one temporary complete record, outside locks/backgrounded by caller."""
        if not record.deferred:
            return (record if record.fingerprint is None else
                    replace(record, data=deepcopy(record.data), fingerprint=None))
        # This operation does not publish or feed the source into the reader.
        message = self.load_event(record.seq)
        if int(message.principal) != self.principal:
            raise WalError("foreign WAL writer")
        loaded = parse(message)
        if ((loaded.seq, loaded.kind, loaded.key, loaded.chat_channel_id)
                != (record.seq, record.kind, record.key, record.chat_channel_id)):
            raise WalError(f"WAL source conflicts with indexed fact: {record.key}")
        return loaded

    def _store(self, record):
        record = self._index_record(record)
        with self.lock:
            old = self.records.get(record.key)
            if old and ((old.kind, old.chat_channel_id) != (record.kind, record.chat_channel_id)
                        or not old.same_data(record)):
                raise WalError(f"conflicting WAL key {record.key}")
            if old is not None:
                return old
            # The sole reader observes facts in source-seq order. New keys
            # append once; later identical records retain the first source.
            for kind in (None, record.kind):
                self.ordered.setdefault((record.chat_channel_id, kind), []).append(record)
            self.records[record.key] = record
            return record

    def get(self, key):
        with self.lock:
            return self.records.get(key)

    def latest(self, *, channel, kind, through=None):
        with self.lock:
            records = self.ordered.get((channel, kind), ())
            end = len(records) if through is None else bisect_right(records, through, key=lambda item: item.seq)
            return records[end - 1] if end else None

    def count(self, *, channel, kind):
        with self.lock:
            return len(self.ordered.get((channel, kind), ()))

    def select(self, *, channel, kind=None, prefix=None, after=0, through=None):
        with self.lock:
            records = self.ordered.get((channel, kind), [])
            start = bisect_right(records, after, key=lambda item: item.seq)
            end = len(records) if through is None else bisect_right(records, through, key=lambda item: item.seq)
            selected = records[start:end]
            return selected if prefix is None else [record for record in selected if record.key.startswith(prefix)]

    @staticmethod
    async def _direct(operation, *args, **kwargs):
        """Allow small in-memory test transports without creating an executor."""
        return operation(*args, **kwargs)

    async def put(self, kind, key, channel, data):
        old = self.get(key)
        if old is not None:
            if (old.kind, old.chat_channel_id) != (kind, channel) or not old.same_data(data):
                raise WalError(f"WAL key reused with different intent: {key}")
            return old
        payload = encode(kind, key, channel, data)
        uuid = await self.blocking(self.rpc.call, "GetUUID", self.rpc.client.get_uuid)
        seq = await self.blocking(self.rpc.publish, uuid, lambda: self.rpc.client.publish_auto_seq(
            principal=self.principal, token=self.token, channel_id=self.channel_id,
            uuid=uuid, payload=payload, recipients=()))
        record = Record(seq, kind, key, channel, json.loads(payload)["data"])
        if self.confirm is not None:
            # The reader observes the publication before any dependent effect.
            # Never hold the journal lock while waiting for that reader.
            confirmation = self.confirm(self.channel_id, seq)
            if inspect.isawaitable(confirmation):
                await confirmation
        else:
            return self._store(record)
        with self.lock:
            old = self.records.get(key)
            if old is None:
                raise WalError(f"confirmed WAL publication has no matching record: {key}")
            if ((old.kind, old.chat_channel_id) != (record.kind, record.chat_channel_id)
                    or not old.same_data(record)):
                raise WalError(f"conflicting WAL key {key}")
        return old
