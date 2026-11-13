"""Shared workflow locks survive their users, not the complete conversation."""

import asyncio
import gc
import unittest
import weakref
from unittest.mock import patch

from openevent.chat_sdk import TurnRef

import test_chat_agent_session as fixture


class LockLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def prepare(self):
        f = self.f
        f.input()
        prepared = f.session._next_answer()
        return prepared, f.session._child(prepared)

    def test_completed_streaming_answers_do_not_accumulate_shared_locks(self):
        f = self.f

        def stream(request, payload):
            previous = f.head(request, payload)
            for number in range(3):
                previous = f.append(request, payload, previous,
                    fixture.chunk(f"fragment {number} " * 500, done=number == 2))
            f.end(request, payload)

        f.request_callback(stream)
        for number in range(8):
            f.input(f"question {number}")
            f.session._model_step()
        self.assertEqual(len(f.events.messages("turn.end", fixture.CHAT_CHANNEL)), 8)
        self.assertGreater(len(f.events.messages("turn.append", fixture.CHAT_CHANNEL)), 8)
        self.assertEqual(f.session.children, {})
        gc.collect()
        self.assertEqual(dict(f.session.effect_locks), {})
        self.assertEqual(dict(f.session.close_locks), {})

    def test_waiting_effect_keeps_its_lock_when_a_new_caller_arrives_during_handoff(self):
        f = self.f
        prepared, child = self.prepare()
        key = prepared.key + ":start"

        async def exercise():
            entered, release, third_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_put, original_effect = child.put, child._effect
            order, references = [], []

            async def hold_commit(kind, record_key, data, **kwargs):
                if record_key == key + ":commit":
                    entered.set()
                    await release.wait()
                return await original_put(kind, record_key, data, **kwargs)

            async def observe_effect(*args, **kwargs):
                name = asyncio.current_task().get_name()
                order.append(name)
                result = await original_effect(*args, **kwargs)
                if name == "first":
                    # Wake the newcomer before effect() releases the lock and
                    # wakes its existing waiter. The newcomer must join that
                    # same lock's queue, even though locked() is now false.
                    third_gate.set()
                return result

            async def publish():
                return await child._chat_effect(key, "turn.start", source={"type": "placeholder"})

            async def newcomer():
                await third_gate.wait()
                self.assertFalse(references[0]().locked())
                self.assertIs(child.effect_locks.get(key), references[0]())
                return await publish()

            tasks = []
            try:
                with patch.object(child, "put", new=hold_commit), \
                        patch.object(child, "_effect", new=observe_effect):
                    tasks.append(asyncio.create_task(publish(), name="first"))
                    await asyncio.wait_for(entered.wait(), 2)
                    references.append(weakref.ref(child.effect_locks[key]))
                    tasks.append(asyncio.create_task(publish(), name="second"))
                    tasks.append(asyncio.create_task(newcomer(), name="third"))
                    await asyncio.sleep(0)
                    self.assertEqual(order, ["first"])
                    release.set()
                    results = await asyncio.wait_for(asyncio.gather(*tasks), 2)
                self.assertEqual(order, ["first", "second", "third"])
                self.assertEqual(results, [results[0]] * 3)
                self.assertEqual(len(f.events.messages("turn.start", fixture.CHAT_CHANNEL)), 1)
                self.assertEqual(f.engine.journal.get(key + ":commit").data["seq"], results[0])
                gc.collect()
                self.assertIsNone(references[0]())
                self.assertEqual(dict(child.effect_locks), {})
            finally:
                release.set()
                third_gate.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        f.async_runner.run(exercise())

    def test_cancelled_waiter_does_not_discard_an_active_close_or_cancel_lock(self):
        f = self.f
        for operation, locked_operation, table_name in (
                ("_finish_answer", "_finish_answer_locked", "close_locks"),
                ("_cancel_answer", "_cancel_answer_locked", "cancel_locks")):
            with self.subTest(operation=operation):
                f.fresh()
                prepared, child = self.prepare()
                if operation == "_cancel_answer":
                    f.async_runner.run(child._chat_effect(prepared.key + ":start", "turn.start",
                                                        source={"type": "placeholder"}))
                    f.user.cancel_turn(target_turn=TurnRef(fixture.AGENT, prepared.data["turn_id"]),
                                       start_seq=child._turn().creation_seq)

                async def exercise():
                    entered, release = asyncio.Event(), asyncio.Event()
                    original = getattr(child, locked_operation)
                    order = []

                    async def held_operation():
                        name = asyncio.current_task().get_name()
                        order.append(name)
                        if name == "holder":
                            entered.set()
                            await release.wait()
                        await original()

                    tasks = []
                    try:
                        with patch.object(child, locked_operation, new=held_operation):
                            tasks.append(asyncio.create_task(getattr(child, operation)(), name="holder"))
                            await asyncio.wait_for(entered.wait(), 2)
                            reference = weakref.ref(getattr(child, table_name)[prepared.key])
                            tasks.append(asyncio.create_task(getattr(child, operation)(), name="cancelled"))
                            tasks.append(asyncio.create_task(getattr(child, operation)(), name="survivor"))
                            await asyncio.sleep(0)
                            tasks[1].cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await tasks[1]
                            self.assertTrue(reference().locked())
                            self.assertIs(getattr(child, table_name)[prepared.key], reference())
                            self.assertEqual(order, ["holder"])
                            release.set()
                            await asyncio.wait_for(asyncio.gather(tasks[0], tasks[2]), 2)
                        self.assertEqual(order, ["holder", "survivor"])
                        gc.collect()
                        self.assertIsNone(reference())
                        self.assertEqual(dict(getattr(child, table_name)), {})
                    finally:
                        release.set()
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

                f.async_runner.run(exercise())


if __name__ == "__main__":
    unittest.main()
