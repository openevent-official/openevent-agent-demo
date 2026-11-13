"""Chat effects recover reply targets from their owning frozen answer."""
import json
import unittest
from unittest.mock import patch

import grpc

import test_chat_agent_session as sessions


class ChatEffectTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sessions.ConversationSDKTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_recovered_reply_and_failure_notice_keep_frozen_targets(self):
        fixture = self.fixture
        for creation_kind in ("turn.start", "turn.single"):
            for crash_at in ("prepare", "publication"):
                with self.subTest(creation_kind=creation_kind, crash_at=crash_at):
                    fixture.fresh(max_model_attempts=1)
                    inputs = [fixture.input("first"), fixture.input("second")]

                    def respond_and_crash(message, payload):
                        kind = payload["kind"]
                        if message.channel_id == sessions.MODEL_CHANNEL and kind == "infer.request":
                            fixture.end(message, payload, failure=creation_kind == "turn.single",
                                        response_body=None if creation_kind == "turn.single" else sessions.body())
                        if crash_at == "prepare":
                            crash = (message.channel_id == sessions.WAL_CHANNEL and kind == "effect.prepare"
                                     and payload["data"]["module"] == "chat"
                                     and payload["data"]["kind"] == creation_kind)
                        else:
                            crash = (message.channel_id == sessions.CHAT_CHANNEL and kind == creation_kind
                                     and message.principal == sessions.AGENT)
                        if crash:
                            raise sessions.Crash()

                    fixture.events.on_publish = respond_and_crash
                    with self.assertRaises(sessions.Crash):
                        fixture.run_answer()
                    frozen, = fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="answer.prepare")
                    fixture.events.on_publish = None
                    pending = [fixture.input("arrived after crash")]
                    fixture.restart()
                    fixture.request_callback(lambda request, payload: fixture.end(
                        request, payload, response_body=sessions.body("recovered")))
                    recovered = fixture.run_answer()
                    self.assertEqual(recovered.key, frozen.key)
                    creations = fixture.events.messages(creation_kind, sessions.CHAT_CHANNEL, sessions.AGENT)
                    self.assertEqual(len(creations), 1)
                    self.assertEqual(json.loads(creations[0].payload)["reply_to_seqs"], inputs)
                    if creation_kind == "turn.single" and crash_at == "prepare":
                        self.assertIsNone(fixture.session._next_answer())
                        pending.append(fixture.input("wake after notice"))
                    following = fixture.run_answer()
                    self.assertEqual(following.data["reply_to_seqs"], pending)
                    following_start = fixture.events.messages("turn.start", sessions.CHAT_CHANNEL, sessions.AGENT)[-1]
                    self.assertEqual(json.loads(following_start.payload)["reply_to_seqs"], pending)
                    for intent in fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="effect.prepare"):
                        if intent.data["module"] == "chat":
                            self.assertNotIn("reply_to_seqs", intent.data)

    def test_streaming_effects_do_not_repeat_reply_targets(self):
        fixture = self.fixture
        inputs = [fixture.input("first"), fixture.input("second")]

        def stream(request, payload):
            previous = fixture.head(request, payload)
            first = fixture.append(request, payload, previous, sessions.chunk("first fragment"))
            fixture.waiting.append(lambda: fixture.append(
                request, payload, first, sessions.chunk("second fragment", done=True)))
            fixture.waiting.append(lambda: fixture.end(request, payload))

        fixture.request_callback(stream)
        fixture.run_answer()
        effects = [intent for intent in fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="effect.prepare")
                   if intent.data["module"] == "chat"]
        self.assertTrue({"turn.start", "turn.reset", "turn.append", "turn.end"}.issubset(
            {intent.data["kind"] for intent in effects}))
        for intent in effects:
            self.assertNotIn("reply_to_seqs", intent.data)
        start, = fixture.events.messages("turn.start", sessions.CHAT_CHANNEL, sessions.AGENT)
        self.assertEqual(json.loads(start.payload)["reply_to_seqs"], inputs)

    def test_sdk_error_end_is_empty_and_replyable_after_restart(self):
        fixture = self.fixture
        fixture.input()
        fixture.request_callback(lambda request, payload: fixture.end(
            request, payload, response_body=sessions.body("draft that will not be retained")))
        publish = fixture.events.publish_auto_seq
        rejected = []

        class EndTooLarge(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.RESOURCE_EXHAUSTED

        def limited_publish(**kwargs):
            payload = json.loads(kwargs["payload"])
            if kwargs["channel_id"] == sessions.CHAT_CHANNEL and payload["kind"] == "turn.end" and "content_seqs" in payload:
                rejected.append(payload)
                raise EndTooLarge()
            return publish(**kwargs)

        with patch.object(fixture.events, "publish_auto_seq", side_effect=limited_publish):
            answer = fixture.run_answer()
        self.assertEqual(len(rejected), 1)
        terminal, = fixture.events.messages("turn.end", sessions.CHAT_CHANNEL, sessions.AGENT)
        self.assertEqual(json.loads(terminal.payload)["error"], "turn_too_long")
        self.assertTrue(fixture.session._answer_finished(answer))
        source = fixture.user.single_turn(turn_id="reply-to-error", content=(sessions.TextPart("what happened?"),),
                                          reply_to_seqs=[terminal.seq])
        for restart in (False, True):
            with self.subTest(restart=restart):
                if restart:
                    fixture.restart()
                visible = fixture.visible_turn(sessions.AGENT, answer.data["turn_id"])
                self.assertEqual((visible.text, visible.object_keys, visible.status), ("", [], "completed"))
                self.assertEqual(fixture.session.index.user(source)["reply_context"], [
                    {"role": "assistant", "status": "completed", "text": "", "attachments": []}])


if __name__ == "__main__":
    unittest.main()
