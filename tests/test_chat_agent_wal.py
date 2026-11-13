"""WAL wire version and immutable fact reconciliation."""

import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from chat_model_agent.wal import Journal, PROTOCOL, WalError, encode, parse


class WalTests(unittest.IsolatedAsyncioTestCase):
    def message(self, *, version=5, seq=1, principal=10, data=None, kind="answer.cancel",
                key="chat:30:answer:1:cancel", channel=30):
        payload = json.loads(encode(kind, key, channel, {} if data is None else data))
        payload["version"] = version
        return SimpleNamespace(seq=seq, principal=principal, payload=json.dumps(payload).encode())

    def test_writes_and_reads_version_five(self):
        self.assertEqual(PROTOCOL, "agent.wal.v5")
        payload = encode("answer.cancel", "chat:30:answer:1:cancel", 30, {})
        self.assertEqual(json.loads(payload)["version"], 5)
        record = parse(SimpleNamespace(seq=12, payload=payload))
        self.assertEqual((record.seq, record.kind, record.chat_channel_id, record.data),
                         (12, "answer.cancel", 30, {}))

    def test_does_not_accept_another_version_or_boolean_version(self):
        for version in (1, 2, 3, 4, True, "5"):
            with self.subTest(version=version), self.assertRaises(WalError):
                parse(self.message(version=version))

    def test_removed_answer_done_is_rejected_on_write_and_read(self):
        with self.assertRaisesRegex(WalError, "unsupported WAL version or kind"):
            encode("answer.done", "chat:30:answer:1:done", 30, {})
        message = self.message()
        payload = json.loads(message.payload)
        payload.update(kind="answer.done", key="chat:30:answer:1:done")
        message.payload = json.dumps(payload).encode()
        with self.assertRaisesRegex(WalError, "unsupported WAL version or kind"):
            parse(message)

    def test_v5_records_cover_rejection_and_compression_without_old_version_adapter(self):
        for kind in ("input.reject", "compaction.prepare", "compaction.commit"):
            with self.subTest(kind=kind):
                record = parse(self.message(kind=kind, data={"source": "unchanged"}))
                self.assertEqual((record.kind, record.data), (kind, {"source": "unchanged"}))
        with self.assertRaises(WalError):
            encode("compaction.done", "new", 30, {})

    def test_duplicate_fact_keeps_first_source_and_rejects_conflicts(self):
        journal = Journal(None, 10, "token", 22)
        journal.observe(self.message(seq=9))
        journal.observe(self.message(seq=12))
        journal.observe(self.message(seq=15))
        earliest = journal.get("chat:30:answer:1:cancel")
        self.assertEqual(earliest.seq, 9)
        self.assertEqual(journal.select(channel=30), [earliest])
        self.assertIs(journal.select(channel=30)[0], earliest)
        self.assertEqual(journal.count(channel=30, kind="answer.cancel"), 1)
        self.assertIs(journal.latest(channel=30, kind="answer.cancel"), earliest)
        for seq, change in enumerate(({"data": {"different": True}}, {"channel": 31}, {"kind": "answer.prepare"}), 16):
            with self.subTest(change=change), self.assertRaisesRegex(WalError, "conflicting WAL key"):
                journal.observe(self.message(seq=seq, **change))
            self.assertIs(journal.get(earliest.key), earliest)
            self.assertEqual(journal.select(channel=30), [earliest])
            self.assertEqual(journal.select(channel=31), [])
            self.assertEqual(journal.select(channel=30, kind="answer.cancel"), [earliest])
        with self.assertRaisesRegex(WalError, "foreign WAL writer"):
            journal.observe(self.message(seq=19, principal=11))

    def test_channel_selection_avoids_scanning_unrelated_conversations(self):
        journal = Journal(None, 10, "token", 22)
        journal.observe(self.message(seq=5, key="chat:30:answer:2:cancel"))
        journal.observe(self.message(seq=10))
        journal.observe(self.message(seq=20, kind="deployment", key="deployment:test", channel=0))
        for channel in range(31, 131):
            journal.observe(self.message(seq=channel, key=f"chat:{channel}:answer:1:cancel", channel=channel))

        class NoGlobalScan(dict):
            def forbidden(self, *args):
                raise AssertionError("a channel query scanned the complete WAL")
            __iter__ = keys = items = values = forbidden

        journal.records = NoGlobalScan(journal.records)
        self.assertEqual([record.seq for record in journal.select(channel=30)], [5, 10])
        self.assertEqual([record.seq for record in journal.select(channel=31)], [31])
        self.assertEqual([record.seq for record in journal.select(channel=0)], [20])
        self.assertEqual(journal.select(channel=999), [])
        self.assertEqual([record.seq for record in journal.select(
            channel=30, kind="answer.cancel", prefix="chat:30:answer:1:")], [10])
        self.assertEqual(journal.select(channel=30, kind="answer.prepare"), [])

    def test_channel_queries_keep_seq_order_and_combined_filters(self):
        journal = Journal(None, 10, "token", 22)
        for message in (
                self.message(seq=3, key="chat:30:answer:2:cancel"),
                self.message(seq=7, key="chat:31:answer:1:cancel", channel=31),
                self.message(seq=9),
                self.message(seq=11, kind="answer.prepare", key="chat:30:answer:3"),
                self.message(seq=20, kind="deployment", key="deployment:test", channel=0)):
            journal.observe(message)
        self.assertEqual([record.seq for record in journal.select(channel=30)], [3, 9, 11])
        self.assertEqual([record.seq for record in journal.select(channel=31)], [7])
        self.assertEqual([record.seq for record in journal.select(channel=0, kind="deployment")], [20])
        self.assertEqual([record.seq for record in journal.select(channel=30, kind="answer.cancel")], [3, 9])
        self.assertEqual([record.seq for record in journal.select(channel=30, prefix="chat:30:")], [3, 9, 11])
        self.assertEqual([record.seq for record in journal.select(
            channel=30, kind="answer.cancel", prefix="chat:30:", after=3, through=9)], [9])
        self.assertEqual([record.seq for record in journal.select(
            channel=30, kind="answer.prepare", prefix="chat:30:")], [11])
        self.assertEqual(journal.select(channel=30, kind="answer.cancel", prefix="chat:30:answer:3"), [])
        self.assertEqual(journal.select(channel=30, prefix=""), journal.select(channel=30))
        self.assertEqual(journal.count(channel=30, kind="answer.cancel"), 2)
        self.assertEqual(journal.latest(channel=30, kind="answer.cancel").seq, 9)
        self.assertEqual(journal.latest(channel=30, kind="answer.cancel", through=8).seq, 3)
        self.assertEqual(journal.latest(channel=30, kind="answer.cancel", through=9).seq, 9)
        self.assertIsNone(journal.latest(channel=30, kind="answer.cancel", through=2))
        self.assertEqual(journal.latest(channel=30, kind="answer.prepare").seq, 11)
        self.assertEqual(journal.count(channel=999, kind="answer.prepare"), 0)
        self.assertIsNone(journal.latest(channel=999, kind="answer.prepare"))

    def test_selection_requires_an_explicit_channel(self):
        journal = Journal(None, 10, "token", 22)
        with self.assertRaises(TypeError):
            journal.select(kind="deployment")

    async def test_put_without_confirmation_is_visible_in_channel_queries(self):
        client = Mock()
        client.get_uuid.side_effect = [123, 124]
        client.publish_auto_seq.side_effect = [SimpleNamespace(seq=19), SimpleNamespace(seq=21)]
        rpc = SimpleNamespace(client=client, call=lambda name, method: method(),
                              publish=lambda uuid, method: method().seq)
        journal = Journal(rpc, 10, "token", 22)
        first = await journal.put("answer.cancel", "chat:30:answer:1:cancel", 30, {})
        deployment = await journal.put("deployment", "deployment:test", 0, {})
        self.assertIs(await journal.put("answer.cancel", first.key, 30, {}), first)
        self.assertEqual(journal.select(channel=30), [first])
        self.assertEqual(journal.select(channel=0), [deployment])
        self.assertEqual(journal.select(channel=30, kind="answer.cancel"), [first])
        self.assertEqual(journal.select(channel=0, kind="deployment"), [deployment])
        self.assertEqual(client.publish_auto_seq.call_count, 2)
        journal.observe(self.message(seq=23))
        self.assertEqual(journal.select(channel=30), [first])
        self.assertIs(journal.select(channel=30)[0], journal.get(first.key))
        self.assertEqual(journal.select(channel=30, kind="answer.cancel"), [journal.get(first.key)])

    async def test_put_waits_for_reader_confirmation_without_holding_its_lock(self):
        published = []
        client = Mock()
        client.get_uuid.return_value = 123
        client.publish_auto_seq.side_effect = lambda **kwargs: published.append(kwargs) or SimpleNamespace(seq=19)
        rpc = SimpleNamespace(client=client, call=lambda name, method: method(),
                              publish=lambda uuid, method: method().seq)

        def confirm(channel, seq):
            self.assertEqual((channel, seq), (22, 19))
            self.assertIsNone(journal.get("chat:30:answer:1:cancel"))
            message = SimpleNamespace(seq=seq, principal=10, payload=published[0]["payload"])
            reader = threading.Thread(target=journal.observe, args=(message,), daemon=True)
            reader.start()
            reader.join(timeout=1)
            self.assertFalse(reader.is_alive(), "WAL confirmation held the reader's journal lock")

        journal = Journal(rpc, 10, "token", 22, confirm=confirm)
        result = await journal.put("answer.cancel", "chat:30:answer:1:cancel", 30, {})
        self.assertIs(result, journal.get("chat:30:answer:1:cancel"))
        self.assertEqual(result.seq, 19)
        self.assertEqual(journal.select(channel=30), [result])

    async def test_put_rejects_confirmation_that_did_not_observe_the_record(self):
        rpc = SimpleNamespace(client=Mock(), call=lambda name, method: 123,
                              publish=lambda uuid, method: 19)
        journal = Journal(rpc, 10, "token", 22, confirm=lambda channel, seq: None)
        with self.assertRaisesRegex(WalError, "no matching record"):
            await journal.put("answer.cancel", "chat:30:answer:1:cancel", 30, {})
        self.assertEqual(journal.select(channel=30), [])

    def test_index_retains_small_fields_but_reads_payloads_without_caching(self):
        data = {"turn_id": "answer", "input_through": {"seq": 8, "index": 0},
                "reply_to_seqs": [8], "events": [{"type": "user", "seq": 8}],
                "settings": {"model": "model", "context_max_input_bytes": 100000,
                             "system_prompt": "prompt" * 10000,
                             "context_compression_prompt": "summary" * 10000}}
        message = self.message(kind="answer.prepare", key="chat:30:answer:0", data=data)
        reads = []

        def load(seq):
            reads.append(seq)
            # Source I/O must never happen with the Journal lock held.
            acquired = []
            def take_lock():
                with journal.lock:
                    acquired.append(True)
            thread = threading.Thread(target=take_lock, daemon=True)
            thread.start()
            thread.join(timeout=1)
            self.assertEqual(acquired, [True], "source read held the WAL index lock")
            return message

        journal = Journal(None, 10, "token", 22, load_event=load)
        record = journal.observe(message)
        self.assertIs(journal.get(record.key), record)
        self.assertIs(journal.latest(channel=30, kind="answer.prepare"), record)
        self.assertIs(type(record.data), dict)
        self.assertIs(type(record.data["settings"]), dict)
        self.assertTrue(record.deferred)
        self.assertEqual(record.data["input_through"], data["input_through"])
        self.assertEqual(record.data["settings"]["context_max_input_bytes"], 100000)
        self.assertNotIn("events", record.data)
        self.assertNotIn("system_prompt", record.data["settings"])
        self.assertNotIn("context_compression_prompt", record.data["settings"])
        self.assertEqual(dict(record.data.items()), record.data)
        with self.assertRaises(KeyError):
            record.data["settings"]["system_prompt"]
        self.assertEqual(reads, [])
        for _ in range(2):
            complete = journal.materialize(record)
            self.assertIs(type(complete.data), dict)
            self.assertEqual(complete.data, data)
            self.assertFalse(complete.deferred)
            self.assertIsNone(complete.fingerprint)
            self.assertIs(journal.materialize(complete), complete)
        self.assertEqual(reads, [1, 1], "explicit reads unexpectedly cached old payloads")
        self.assertIs(journal.get(record.key), record, "materialization replaced the lightweight index")

    def test_large_tool_fields_and_duplicate_reconciliation_do_not_stay_in_index(self):
        data = {"tool_key": "tool", "answer": "answer", "name": "exec",
                "arguments": {"command": "command" * 10000},
                "request": {"workdir": "/tmp", "ts_ms": 100}}
        messages = {}
        reads = []

        def load(seq):
            reads.append(seq)
            return messages[seq]

        journal = Journal(None, 10, "token", 22, load_event=load)
        for seq in (9, 12, 15):
            messages[seq] = self.message(kind="tool.prepare", key="tool", seq=seq,
                                         data=dict(reversed(tuple(data.items()))))
            journal.observe(messages[seq])
        record = journal.get("tool")
        self.assertEqual(record.seq, 9)
        self.assertTrue(record.same_data(data))
        self.assertTrue(record.same_data(parse(messages[12])))
        self.assertFalse(record.same_data(record.data))
        self.assertEqual(record.data["name"], "exec")
        self.assertNotIn("arguments", record.data)
        self.assertNotIn("request", record.data)
        self.assertEqual(reads, [])
        with self.assertRaisesRegex(WalError, "conflicting WAL key"):
            journal.observe(self.message(kind="tool.prepare", key="tool", seq=16,
                data={**data, "arguments": {"command": "changed"}}))
        self.assertEqual(reads, [], "duplicate validation fetched old payloads")
        self.assertEqual(journal.materialize(record).data["arguments"], data["arguments"])
        self.assertEqual(reads, [9])

    def test_materialize_checks_source_identity(self):
        data = {"name": "exec", "arguments": {"command": "original"}}
        original = self.message(kind="tool.prepare", key="tool", seq=9, data=data)
        loader = Mock(return_value=original)
        journal = Journal(None, 10, "token", 22, load_event=loader)
        record = journal.observe(original)
        for change in ({"principal": 11}, {"seq": 10}, {"kind": "tool.done"},
                       {"key": "other"}, {"channel": 31}):
            with self.subTest(change=change):
                loader.return_value = self.message(**{
                    "kind": "tool.prepare", "key": "tool", "seq": 9, "data": data, **change})
                with self.assertRaises(WalError):
                    journal.materialize(record)
        self.assertIs(journal.get(record.key), record)

    def test_small_effect_fields_remain_local_during_recovery(self):
        data = {"channel": 30, "module": "chat", "kind": "turn.start", "turn_id": "reply",
                "pre_seq": None, "source": {"type": "placeholder"}}
        journal = Journal(None, 10, "token", 22,
                          load_event=Mock(side_effect=AssertionError("small fact fetched its source")))
        record = journal.observe(self.message(kind="effect.prepare", data=data))
        self.assertIs(journal.get(record.key), record)
        self.assertEqual(dict(record.data.items()), data)
        complete = journal.materialize(record)
        self.assertEqual(complete.data, data)
        self.assertFalse(complete.deferred)
        self.assertIsNone(complete.fingerprint)
        self.assertIs(journal.materialize(complete), complete)
        complete.data["source"]["type"] = "changed copy"
        self.assertEqual(record.data["source"], {"type": "placeholder"})

    async def test_confirmed_put_keeps_only_index_and_checks_retries_without_source_reads(self):
        published = []
        client = Mock()
        client.get_uuid.return_value = 123
        client.publish_auto_seq.side_effect = lambda **kwargs: published.append(kwargs) or SimpleNamespace(seq=19)
        rpc = SimpleNamespace(client=client, call=lambda name, method: method(),
                              publish=lambda uuid, method: method().seq)
        sources = {}

        def confirm(channel, seq):
            message = SimpleNamespace(seq=seq, principal=10, payload=published[0]["payload"])
            sources[seq] = message
            journal.observe(message)

        loader = Mock(side_effect=lambda seq: sources[seq])
        journal = Journal(rpc, 10, "token", 22, confirm=confirm, load_event=loader)
        data = {"tool_key": "tool", "name": "exec", "arguments": {"command": "large" * 10000},
                "request": {"workdir": "/tmp", "ts_ms": 100}}
        first = await journal.put("tool.prepare", "tool:prepare", 30, data)
        self.assertNotIn("arguments", first.data)
        self.assertIs(await journal.put("tool.prepare", "tool:prepare", 30, data), first)
        with self.assertRaisesRegex(WalError, "different intent"):
            await journal.put("tool.prepare", "tool:prepare", 30, first.data)
        with self.assertRaisesRegex(WalError, "different intent"):
            await journal.put("tool.prepare", "tool:prepare", 30,
                              {**data, "arguments": {"command": "changed"}})
        loader.assert_not_called()
        self.assertEqual(client.publish_auto_seq.call_count, 1)
        self.assertEqual(journal.materialize(first).data, data)
        loader.assert_called_once_with(19)

    async def test_copying_a_fact_explicitly_materializes_via_background_entry(self):
        client = Mock()
        client.get_uuid.return_value = 123
        client.publish_auto_seq.return_value = SimpleNamespace(seq=19)
        rpc = SimpleNamespace(client=client, call=lambda name, method: method(),
                              publish=lambda uuid, method: method().seq)
        data = {"tool_key": "tool", "name": "exec", "arguments": {"command": "large" * 10000},
                "request": {"workdir": "/tmp", "ts_ms": 100}}
        source = self.message(kind="tool.prepare", key="original", data=data)
        background = []

        def load(seq):
            self.assertTrue(background, "source load bypassed the background entry")
            self.assertEqual(seq, source.seq)
            return source

        async def blocking(operation, *args, **kwargs):
            background.append(operation)
            try:
                return operation(*args, **kwargs)
            finally:
                background.pop()

        journal = Journal(rpc, 10, "token", 22, load_event=load, blocking=blocking)
        original = journal.observe(source)
        complete = await blocking(journal.materialize, original)
        copied = await journal.put("tool.prepare", "copied", 30, complete.data)
        self.assertEqual(copied.key, "copied")
        self.assertEqual(json.loads(client.publish_auto_seq.call_args.kwargs["payload"])["data"], data)
        self.assertNotIn("arguments", copied.data)
        self.assertTrue(copied.same_data(original))
        self.assertIs(await journal.put("tool.prepare", "original", 30, complete.data), original)
        self.assertEqual(client.publish_auto_seq.call_count, 1)


if __name__ == "__main__":
    unittest.main()
