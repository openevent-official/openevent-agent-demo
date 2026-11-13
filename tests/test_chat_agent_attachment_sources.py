"""Frozen attachment eligibility uses original references, without model rendering."""
import unittest
from unittest.mock import patch

import test_chat_agent_session as sessions


class AttachmentSourceTests(unittest.TestCase):
    def setUp(self):
        self.f = sessions.ConversationSDKTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.metadata_reads = []
        self.f.engine.objects.batch_metadata_reader = self.metadata

    @staticmethod
    def key(ident):
        return sessions.ObjectKey(object_id=ident, object_token=f"object-{ident}")

    def metadata(self, keys):
        self.metadata_reads.extend(key["object_id"] for key in keys)
        return [{"name": f"attachment-{key['object_id']}", "type": "text/plain", "nbytes": 1}
                for key in keys]

    def assert_source_ids(self, prepared, expected):
        index = self.f.session.index
        cached_users = dict(index.users)
        with patch.object(index, "user", side_effect=AssertionError("user event was rebuilt")), \
             patch.object(index, "render", side_effect=AssertionError("model event was rendered")), \
             patch.object(self.f.engine, "pending_inputs", side_effect=AssertionError("all inputs were scanned")), \
             patch.object(self.f.engine, "input_object_ids", side_effect=AssertionError("input attachments were rebuilt")), \
             patch.object(self.f.engine.journal, "select", side_effect=AssertionError("historical rejections were scanned")), \
             patch.object(self.f.engine.objects, "prepare_many", side_effect=AssertionError("attachment metadata was prepared")):
            self.assertEqual(index.object_ids(prepared, set(range(1, 16))), frozenset(expected))
        self.assertEqual(index.users, cached_users)

    def test_summarized_sources_survive_restart_without_rebuilding_old_users(self):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        f.engine.objects.batch_metadata_reader = self.metadata

        def respond(request, payload):
            compacting = "tools" not in payload["body"]
            f.end(request, payload, response_body=sessions.body("summary" if compacting else "x" * 1000))

        f.request_callback(respond)
        for ident in range(1, 13):
            f.input("question " + "q" * 800, object_keys=[self.key(ident)])
            f.session._model_step()
        self.assertTrue(f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="compaction.commit"))

        f.restart()
        f.engine.objects.batch_metadata_reader = self.metadata
        self.metadata_reads.clear()
        snapshot = f.session.index.snapshot()
        self.assertIsNotNone(snapshot.summary)
        self.assertEqual(f.session.index.users, {}, "snapshot should not eagerly load user bodies")
        prepared = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="answer.prepare")[-1]
        reads_before = list(self.metadata_reads)
        self.assert_source_ids(prepared, range(1, 13))
        self.assertEqual(self.metadata_reads, reads_before)
        with patch.object(f.engine, "input_object_sources", wraps=f.engine.input_object_sources) as sources:
            self.assertEqual(f.session.index.object_ids(prepared, {1, 99}), frozenset({1}))
        self.assertEqual({call.args[1] for call in sources.call_args_list}, {1, 99})
        self.assertEqual(sources.call_count, 2)

    def test_frozen_sources_exclude_future_inputs_and_other_conversations(self):
        f = self.f
        f.input("included", object_keys=[self.key(1)])
        channel = sessions.CHAT_CHANNEL + 1
        f.add_chat_channel(channel)
        another_chat = sessions.create_chat(f.events, principal=sessions.USER, token="user-token",
                                           channel_id=channel, max_retries=0)
        another_chat.single_turn(turn_id="other-conversation", content=(sessions.TextPart("elsewhere"),),
                                 object_keys=[self.key(3)])
        prepared = f.session._next_answer()
        f.input("arrived after preparation", object_keys=[self.key(4)])
        self.assert_source_ids(prepared, {1})

        f.restart()
        self.assertFalse(f.session.index.users)
        self.assert_source_ids(prepared, {1})
        self.assertFalse(f.session.index.users)

    def test_rejected_attachment_is_eligible_only_through_later_explicit_quote(self):
        f = self.f
        f.fresh(context_max_input_bytes=12000)
        f.engine.objects.batch_metadata_reader = self.metadata
        rejected = f.input("x" * (f.session.budget.target + 1), object_keys=[self.key(1)])
        f.session._reject_inputs()
        rejection, = f.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="input.reject")
        self.assertEqual(rejection.data["input_seq"], rejected)

        # The durable rejection remains excluded after the budget grows. The
        # larger budget allows a later explicit quote to be admitted normally.
        f.restart(context_max_input_bytes=1048576)
        f.engine.objects.batch_metadata_reader = self.metadata
        f.request_callback(lambda request, payload: f.end(request, payload, response_body=sessions.body("done")))
        f.input("ordinary input", object_keys=[self.key(2)])
        ordinary = f.run_answer()
        self.assert_source_ids(ordinary, {2})

        quoted = f.user.single_turn(turn_id="quote-rejected", content=(sessions.TextPart("consider this"),),
                                    reply_to_seqs=[rejected])
        prepared = f.session._next_answer()
        self.assertIn(quoted, prepared.data["reply_to_seqs"])
        self.assert_source_ids(prepared, {1, 2})

        f.restart()
        self.assertFalse(f.session.index.users)
        self.assert_source_ids(ordinary, {2})
        self.assert_source_ids(prepared, {1, 2})
        self.assertFalse(f.session.index.users)

    def test_direct_quote_uses_final_reset_attachments_without_recursive_ancestors(self):
        f = self.f
        # User streams become attachment sources only when a normal input
        # quotes them; their own completion does not trigger an answer.
        ancestor = f.user.start_turn(turn_id="ancestor", content=(sessions.TextPart("ancestor"),),
                                     object_keys=[self.key(1)]).complete()
        f.user.start_turn(turn_id="unquoted", content=(sessions.TextPart("unrelated"),),
                          object_keys=[self.key(2)]).complete()
        reference = f.user.start_turn(turn_id="direct-target", reply_to_seqs=[ancestor],
                                     content=(sessions.TextPart("draft"),), object_keys=[self.key(3)])
        reference.reset(content=(sessions.TextPart("final"),), object_keys=[self.key(4)])
        terminal = reference.complete()
        quoted = f.user.single_turn(turn_id="quote-final", reply_to_seqs=[terminal],
                                    content=(sessions.TextPart("use this"),), object_keys=[self.key(5)])
        prepared = f.session._next_answer()
        self.assertEqual(prepared.data["reply_to_seqs"], [quoted])
        self.assert_source_ids(prepared, {4, 5})

        f.restart()
        self.assertFalse(f.session.index.users)
        self.assert_source_ids(prepared, {4, 5})
        self.assertFalse(f.session.index.users)

    def test_unquoted_user_stream_and_empty_quote_are_not_input_sources(self):
        f = self.f
        stream = f.user.start_turn(turn_id="user-stream", content=(sessions.TextPart("stream"),),
                                   object_keys=[self.key(1)])
        terminal = stream.complete()
        # A reply without its own content is not a ready input, even when its
        # target has attachments. A later normal message cannot inherit them.
        f.user.single_turn(turn_id="empty-quote", reply_to_seqs=[terminal], content=())
        f.input("ordinary", object_keys=[self.key(2)])
        prepared = f.session._next_answer()
        self.assert_source_ids(prepared, {2})


if __name__ == "__main__":
    unittest.main()
