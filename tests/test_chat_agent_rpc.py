import asyncio
from itertools import count
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

import grpc

from chat_model_agent.config import RpcConfig
from chat_model_agent.rpc import InfrastructureError, Rpc
from chat_model_agent.scheduling import RpcPool
from chat_model_agent.wal import Journal, WalError, encode


class RpcFailure(Exception):
    def __init__(self, code):
        self.status = code
    def code(self):
        return self.status


class RpcTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.rpc = Rpc(self.client, RpcConfig(max_retries=3))
        self.sleep = patch("chat_model_agent.rpc.time.sleep").start()
        self.addCleanup(patch.stopall)

    def test_read_budget_is_initial_attempt_plus_configured_retries(self):
        operation = Mock(side_effect=[RpcFailure(grpc.StatusCode.UNAVAILABLE)] * 3 + ["ready"])
        self.assertEqual(self.rpc.call("Fetch", operation), "ready")
        self.assertEqual(operation.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(0.1)] * 3)

    def test_read_exhaustion_is_a_process_level_infrastructure_error(self):
        operation = Mock(side_effect=RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED))
        with self.assertRaises(InfrastructureError):
            self.rpc.call("Fetch", operation)
        self.assertEqual(operation.call_count, 4)

    def test_non_retryable_error_is_not_repeated(self):
        operation = Mock(side_effect=RpcFailure(grpc.StatusCode.UNAUTHENTICATED))
        with self.assertRaises(InfrastructureError):
            self.rpc.call("Fetch", operation)
        self.assertEqual(operation.call_count, 1)
        self.sleep.assert_not_called()

    def test_unknown_publication_only_queries_original_uuid(self):
        operation = Mock(side_effect=RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED))
        self.client.get_seq_by_uuid.side_effect = [RpcFailure(grpc.StatusCode.NOT_FOUND), 321]
        self.assertEqual(self.rpc.publish(77, operation), 321)
        operation.assert_called_once_with()
        self.assertEqual(self.client.get_seq_by_uuid.call_args_list, [call(77), call(77)])
        self.client.get_uuid.assert_not_called()

    def test_publish_and_reconciliation_share_one_budget(self):
        operation = Mock(side_effect=RpcFailure(grpc.StatusCode.UNAVAILABLE))
        self.client.get_seq_by_uuid.side_effect = RpcFailure(grpc.StatusCode.NOT_FOUND)
        with self.assertRaisesRegex(InfrastructureError, "remains unknown"):
            self.rpc.publish(77, operation)
        self.assertEqual(operation.call_count + self.client.get_seq_by_uuid.call_count, 4)
        self.assertEqual(self.sleep.call_args_list, [call(0.1)] * 3)
        operation.assert_called_once_with()

    def test_definite_publish_not_found_is_not_retried(self):
        operation = Mock(side_effect=RpcFailure(grpc.StatusCode.NOT_FOUND))
        with self.assertRaises(InfrastructureError):
            self.rpc.publish(77, operation)
        operation.assert_called_once_with()
        self.client.get_seq_by_uuid.assert_not_called()

    def test_already_exists_is_reconciled_without_republishing(self):
        operation = Mock(side_effect=RpcFailure(grpc.StatusCode.ALREADY_EXISTS))
        self.client.get_seq_by_uuid.return_value = 123
        self.assertEqual(self.rpc.publish(77, operation), 123)
        operation.assert_called_once_with()

    def test_sdk_retry_owner_is_called_only_once(self):
        operation = Mock(side_effect=RuntimeError("SDK already exhausted its retries"))
        with self.assertRaises(InfrastructureError):
            self.rpc.sdk("Chat publish", operation)
        operation.assert_called_once_with()
        self.sleep.assert_not_called()

class JournalTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock()
        self.client.get_uuid.return_value = 99
        self.client.publish_auto_seq.return_value = SimpleNamespace(seq=100)
        self.rpc = Rpc(self.client, RpcConfig(max_retries=0))
        self.journal = Journal(self.rpc, 10, "private-token", 30)

    def message(self, seq, *, principal=10, kind="answer.prepare", key="answer-1", channel=31, data=None):
        return SimpleNamespace(seq=seq, principal=principal,
                               payload=encode(kind, key, channel, {"inputs": [1]} if data is None else data))

    async def test_same_logical_record_is_not_published_twice(self):
        first = await self.journal.put("answer.prepare", "answer-1", 31, {"inputs": [1]})
        again = await self.journal.put("answer.prepare", "answer-1", 31, {"inputs": [1]})
        self.assertIs(first, again)
        self.client.get_uuid.assert_called_once_with()
        self.client.publish_auto_seq.assert_called_once()
        self.assertEqual(self.client.publish_auto_seq.call_args.kwargs["uuid"], 99)

    async def test_conflicting_key_does_not_publish_a_second_intent(self):
        self.journal.observe(self.message(50))
        with self.assertRaises(WalError):
            await self.journal.put("answer.prepare", "answer-1", 31, {"inputs": [2]})
        self.client.publish_auto_seq.assert_not_called()

    async def test_failed_publication_is_not_claimed_as_a_committed_record(self):
        self.client.publish_auto_seq.side_effect = RpcFailure(grpc.StatusCode.UNAVAILABLE)
        with self.assertRaises(InfrastructureError):
            await self.journal.put("answer.prepare", "answer-1", 31, {"inputs": [1]})
        self.assertIsNone(self.journal.get("answer-1"))

    async def test_records_copy_mutable_inputs_and_keep_channel_filtering(self):
        data = {"inputs": [1]}
        await self.journal.put("answer.prepare", "answer-1", 31, data)
        data["inputs"].append(2)
        self.assertEqual(self.journal.get("answer-1").data, {"inputs": [1]})
        self.journal.observe(self.message(101, key="answer-2", channel=32))
        self.assertEqual([record.key for record in self.journal.select(channel=31)], ["answer-1"])

    async def test_reader_confirmation_does_not_occupy_rpc_workers(self):
        pool = RpcPool()
        self.addCleanup(pool.close)
        publication_count = pool.size * 3
        payloads, waiting = {}, []
        ready, release = asyncio.Event(), asyncio.Event()
        self.client.get_uuid.side_effect = count(100).__next__
        def publish(**kwargs):
            payloads[kwargs["uuid"]] = kwargs["payload"]
            return SimpleNamespace(seq=kwargs["uuid"])
        async def confirm(channel, seq):
            waiting.append(seq)
            if len(waiting) == publication_count:
                ready.set()
            await release.wait()
            journal.observe(SimpleNamespace(seq=seq, principal=10, payload=payloads[seq]))
        self.client.publish_auto_seq.side_effect = publish
        journal = Journal(self.rpc, 10, "token", 30, blocking=pool.call, confirm=confirm)
        tasks = [asyncio.create_task(journal.put("answer.prepare", f"answer-{i}", i, {"inputs": [i]}))
                 for i in range(publication_count)]
        try:
            await asyncio.wait_for(ready.wait(), 1)
            self.assertTrue(all(not task.done() for task in tasks))
            self.assertEqual(await asyncio.wait_for(pool.call(lambda: "available"), 1), "available")
            self.assertTrue(all(thread.daemon for thread in pool.threads))
        finally:
            release.set()
            await asyncio.gather(*tasks)


if __name__ == "__main__":
    unittest.main()
