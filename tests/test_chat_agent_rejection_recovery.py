"""Recovery completes persisted rejection notices before admitting new inputs."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from chat_model_agent.budget import ContextBudget, INPUT_REJECTION
from chat_model_agent.rpc import InfrastructureError
from chat_model_agent.session import Conversation
import test_chat_agent_session as support
from test_chat_agent_session import AGENT, CHAT_CHANNEL, MODEL_CHANNEL


class RejectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = support.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        fixed = ContextBudget.from_config(self.f.config.agent).fixed
        self.f.fresh(context_max_input_bytes=fixed + 2000)
        self.f.session._reconcile()

    def reject(self):
        f = self.f
        source = f.input("x" * 5000)
        return f.session.put("input.reject", f"chat:{CHAT_CHANNEL}:input:{source}:reject", {
            "input_seq": source, "reason": "message_too_long",
            "snapshot": f.session.index.snapshot().watermarks, "turn_id": f"rejected-{source}"})

    def prepare_notice(self, rejected):
        return self.f.session.put("effect.prepare", rejected.key + ":notice:prepare", {
            "channel": CHAT_CHANNEL, "module": "chat", "kind": "turn.single",
            "turn_id": rejected.data["turn_id"], "pre_seq": None,
            "source": {"type": "input_rejection", "record": rejected.key}})

    def notices(self):
        return self.f.events.messages("turn.single", CHAT_CHANNEL, AGENT)

    def test_recovery_sends_unsent_notice_even_after_later_input_was_consumed(self):
        f = self.f
        for prepared_intent in (False, True):
            with self.subTest(prepared_intent=prepared_intent):
                f.fresh()
                f.session._reconcile()
                rejected = self.reject()
                if prepared_intent:
                    self.prepare_notice(rejected)
                later = f.input("normal input admitted after the rejection")
                # The original process can prepare a later ordinary answer while
                # its independent rejection publication has not started yet.
                with patch.object(f.session._target, "_chat_effect", new=AsyncMock()):
                    answer = f.session._next_answer()
                self.assertEqual(answer.data["input_through"], {"seq": later, "index": 0})
                self.assertEqual(answer.data["reply_to_seqs"], [later])
                self.assertEqual(self.notices(), [])

                f.restart()
                f.session._reconcile()
                commit = f.engine.journal.get(rejected.key + ":notice:commit")
                self.assertIsNotNone(commit)
                notice, = self.notices()
                payload = json.loads(notice.payload)
                self.assertEqual(commit.data["seq"], notice.seq)
                self.assertEqual(payload["turn_id"], rejected.data["turn_id"])
                self.assertEqual(payload["reply_to_seqs"], [rejected.data["input_seq"]])
                self.assertEqual(payload["content"], [{"type": "text", "text": INPUT_REJECTION}])
                self.assertEqual(f.session.index.snapshot().consumed, answer.data["input_through"])
                self.assertEqual(f.events.messages("infer.request", MODEL_CHANNEL), [])

                before = len(f.events.history)
                f.restart()
                f.session._reconcile()
                self.assertEqual(len(f.events.history), before)

    def test_recovery_confirms_already_sent_notice_without_publishing_it_again(self):
        f = self.f
        rejected = self.reject()

        def crash_after_publication(message, payload):
            if message.channel_id == CHAT_CHANNEL and message.principal == AGENT:
                raise support.Crash()

        f.events.on_publish = crash_after_publication
        with self.assertRaises(support.Crash):
            f.session._chat_effect(rejected.key + ":notice", "turn.single", terminal_ok=True,
                turn_id=rejected.data["turn_id"], source={"type": "input_rejection", "record": rejected.key})
        notice, = self.notices()
        self.assertIsNone(f.engine.journal.get(rejected.key + ":notice:commit"))
        f.events.on_publish = None
        f.restart()
        f.session._reconcile()
        commit = f.engine.journal.get(rejected.key + ":notice:commit")
        self.assertEqual(commit.data["seq"], notice.seq)
        self.assertEqual([event.seq for event in self.notices()], [notice.seq])
        self.assertEqual(f.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare"), [])

    def test_recovery_waits_for_each_notice_commit_before_next_notice_or_new_input(self):
        f = self.f
        first, second = self.reject(), self.reject()
        f.restart()
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=support.body("done")))

        async def exercise():
            reached, release = asyncio.Event(), asyncio.Event()
            session = f.session._target
            original_put = session.put

            async def hold_first_commit(kind, key, data, **kwargs):
                if key == first.key + ":notice:commit":
                    reached.set()
                    await release.wait()
                return await original_put(kind, key, data, **kwargs)

            async def wait_actual(timeout=None, *, ready=None):
                await Conversation.wait(session, timeout, ready=ready)

            session.wait = wait_actual
            runner = None
            try:
                with patch.object(session, "put", side_effect=hold_first_commit):
                    runner = asyncio.create_task(session.run())
                    await asyncio.wait_for(reached.wait(), 2)
                    # Even an input arriving during recovery waits for both
                    # previously recorded notices to be confirmed in order.
                    later = await f.engine.blocking(f.input, "new input during recovery")
                    self.assertEqual(len(self.notices()), 1)
                    notice = self.notices()[0]
                    self.assertEqual(json.loads(notice.payload)["turn_id"], first.data["turn_id"])
                    self.assertIsNone(f.engine.journal.get(first.key + ":notice:commit"))
                    self.assertIsNone(f.engine.journal.get(second.key + ":notice:prepare"))
                    self.assertEqual(f.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare"), [])
                    self.assertFalse(session.reconciled)
                    self.assertIn(CHAT_CHANNEL, f.engine.recovery_pending)
                    release.set()
                    await asyncio.wait_for(f.engine.wait_async(lambda: bool(
                        f.engine.journal.select(channel=CHAT_CHANNEL, kind="attempt.accept"))), 2)
                    first_commit = f.engine.journal.get(first.key + ":notice:commit")
                    second_prepare = f.engine.journal.get(second.key + ":notice:prepare")
                    second_commit = f.engine.journal.get(second.key + ":notice:commit")
                    answer, = f.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare")
                    self.assertLess(first_commit.seq, second_prepare.seq)
                    self.assertLess(second_commit.seq, answer.seq)
                    self.assertEqual(answer.data["reply_to_seqs"], [later])
                    self.assertEqual([json.loads(event.payload)["turn_id"] for event in self.notices()],
                                     [first.data["turn_id"], second.data["turn_id"]])
                    self.assertTrue(session.reconciled)
                    self.assertNotIn(CHAT_CHANNEL, f.engine.recovery_pending)
                    self.assertIsNone(f.engine.error)
            finally:
                release.set()
                f.engine.request_stop()
                if runner is not None:
                    await asyncio.wait_for(runner, 2)

        f.async_runner.run(exercise())

    def test_partial_recovery_failure_blocks_new_input_and_restart_only_sends_remaining_notice(self):
        f = self.f
        first, second = self.reject(), self.reject()
        later = f.input("wait until recovery succeeds")
        f.restart()
        failure = InfrastructureError("rejection notice publication failed")
        session = f.session._target
        original_effect = session._chat_effect

        async def fail_second_notice(key, kind, **kwargs):
            if key == second.key + ":notice":
                raise failure
            return await original_effect(key, kind, **kwargs)

        with patch.object(session, "_chat_effect", new=fail_second_notice):
            f.async_runner.run(session.run())
        self.assertIs(f.engine.error, failure)
        self.assertFalse(session.reconciled)
        self.assertIn(CHAT_CHANNEL, f.engine.recovery_pending)
        first_commit = f.engine.journal.get(first.key + ":notice:commit")
        self.assertIsNotNone(first_commit)
        self.assertIsNone(f.engine.journal.get(second.key + ":notice:commit"))
        self.assertEqual(len(self.notices()), 1)
        self.assertEqual(f.engine.journal.select(channel=CHAT_CHANNEL, kind="answer.prepare"), [])
        self.assertEqual(f.events.messages("infer.request", MODEL_CHANNEL), [])

        f.restart()
        f.session._reconcile()
        second_commit = f.engine.journal.get(second.key + ":notice:commit")
        self.assertEqual(f.engine.journal.get(first.key + ":notice:commit"), first_commit)
        self.assertIsNotNone(second_commit)
        self.assertEqual([json.loads(event.payload)["turn_id"] for event in self.notices()],
                         [first.data["turn_id"], second.data["turn_id"]])
        answer = f.session._next_answer()
        self.assertEqual(answer.data["reply_to_seqs"], [later])
        self.assertLess(second_commit.seq, answer.seq)


if __name__ == "__main__":
    unittest.main()
