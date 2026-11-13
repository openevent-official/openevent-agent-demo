"""Live admission and scheduling revisit new work, not completed history."""

import asyncio
import unittest
from unittest.mock import patch

import test_chat_agent_session as fixture


class IncrementalAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def finish_notices(self):
        async def finish():
            await asyncio.gather(*tuple(self.f.session.rejection_tasks.values()))
            await asyncio.sleep(0)
        self.f.async_runner.run(finish())

    def test_live_next_answer_queries_skip_old_inputs_answers_and_compressions(self):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        f.request_callback(lambda request, payload: f.end(request, payload,
            response_body=fixture.body("summary" if "tools" not in payload["body"] else "x" * 600)))
        for number in range(12):
            f.input(f"question {number}:" + "x" * 600)
            f.session._model_step()
        self.assertTrue(f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="compaction.prepare"))

        for restarted in (False, True):
            with self.subTest(restarted=restarted):
                if restarted:
                    f.restart()
                # Startup may reconstruct state once. Subsequent scheduler
                # passes must not copy the old preparation/input lists again.
                self.assertIsNone(f.session._next_answer())
                answer = f.engine.journal.latest(channel=fixture.CHAT_CHANNEL, kind="answer.prepare")
                consumed = answer.data["input_through"]["seq"]
                select, pending_inputs = f.engine.journal.select, f.engine.pending_inputs

                def only_new_preparations(**kwargs):
                    if kwargs.get("kind") in {"answer.prepare", "compaction.prepare"}:
                        self.assertGreater(kwargs.get("after", 0), 0,
                                           "scheduler queried the whole preparation history")
                        if kwargs["kind"] == "answer.prepare":
                            self.assertGreaterEqual(kwargs["after"], answer.seq)
                    return select(**kwargs)

                def only_new_inputs(channel, **kwargs):
                    self.assertGreaterEqual(kwargs.get("after", 0), consumed,
                                            "scheduler queried already consumed inputs")
                    return pending_inputs(channel, **kwargs)

                with patch.object(f.engine.journal, "select", side_effect=only_new_preparations), \
                        patch.object(f.engine, "pending_inputs", side_effect=only_new_inputs):
                    for number in range(2):
                        source = f.input(f"new input {restarted}/{number}")
                        f.session._model_step()
                        latest = f.engine.journal.latest(channel=fixture.CHAT_CHANNEL, kind="answer.prepare")
                        self.assertEqual(latest.data["reply_to_seqs"], [source])
                        self.assertTrue(f.session._answer_finished(latest))

    def test_repeated_rejections_do_not_recheck_old_bodies_or_retain_finished_tasks(self):
        f = self.f
        f.fresh(context_max_input_bytes=5000)
        rejected = []
        original_user = f.session.index.user
        for number in range(3):
            old = set(rejected)
            source = f.input("x" * f.session.budget.target)
            rejected.append(source)

            def new_body_only(seq):
                self.assertNotIn(seq, old, "admission rendered an already rejected input")
                return original_user(seq)

            with patch.object(f.session.index, "user", side_effect=new_body_only):
                f.session._reject_inputs()
                self.finish_notices()
                for _ in range(3):
                    f.session._reject_inputs()
            self.assertEqual(f.session.rejection_tasks, {})
            self.assertEqual(len(f.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)), number + 1)
            self.assertIsNone(f.session.index.snapshot().consumed)

        f.restart()
        f.session._reconcile()
        with patch.object(f.session.index, "user", side_effect=AssertionError("rechecked persisted rejection")):
            f.session._reject_inputs()
            f.session._reject_inputs()
        self.assertEqual(f.session.rejection_tasks, {})
        self.assertEqual(len(f.events.messages("turn.single", fixture.CHAT_CHANNEL, fixture.AGENT)), len(rejected))

    def test_new_input_during_metadata_wait_is_checked_on_the_next_pass(self):
        f = self.f
        f.fresh(context_max_input_bytes=5000)
        f.input("first input with an attachment",
                object_keys=[fixture.ObjectKey(object_id=42, object_token="attachment")])
        arriving = []

        def metadata(keys):
            arriving.append(f.input("x" * f.session.budget.target))
            return [{"name": "file", "type": "text/plain", "nbytes": 1} for _ in keys]

        f.engine.objects.batch_metadata_reader = metadata
        f.session._reject_inputs()
        self.assertEqual(len(arriving), 1)
        self.assertEqual(f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="input.reject"), [])
        f.session._reject_inputs()
        self.finish_notices()
        rejected, = f.engine.journal.select(channel=fixture.CHAT_CHANNEL, kind="input.reject")
        self.assertEqual(rejected.data["input_seq"], arriving[0])
        self.assertEqual(f.session.rejection_tasks, {})


if __name__ == "__main__":
    unittest.main()
