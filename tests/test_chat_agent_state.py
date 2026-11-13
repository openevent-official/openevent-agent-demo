import json
from types import SimpleNamespace
import unittest

from openevent.chat_sdk import parse_message as parse_chat
from openevent.model_proxy_sdk import parse_message as parse_model

from chat_model_agent.state import ChatProjection, HistoryError, ModelView


def event(seq, principal, payload, *, keys=()):
    return SimpleNamespace(seq=seq, uuid=seq, channel_id=30, principal=principal,
                           ts_ms=seq * 1000, recipients=(), object_keys=keys,
                           payload=json.dumps(payload).encode())


def chat(seq, kind, *, principal=10, turn_id="turn", text=None, pre_seq=None, replies=(), keys=(), target=None, start_seq=1, content_seqs=None, error=None):
    payload = {"kind": kind}
    if target is not None:
        payload["target_turn"] = {"principal": target[0], "turn_id": target[1]}
    else:
        payload["turn_id"] = turn_id
    if kind in {"turn.single", "turn.start"}:
        payload["reply_to_seqs"] = list(replies)
    if kind in {"turn.single", "turn.start", "turn.append", "turn.reset"}:
        payload["content"] = [] if text is None else [{"type": "text", "text": text}]
    if pre_seq is not None:
        payload["pre_seq"] = pre_seq
    if kind in {"turn.cancel", "turn.end"}:
        payload["start_seq"] = start_seq
    if kind == "turn.end":
        if error is None:
            payload["content_seqs"] = list(content_seqs or (pre_seq,))
        else:
            payload["error"] = error
    return parse_chat(event(seq, principal, payload, keys=keys))


def model(seq, kind, *, principal=20, stream="stream", **fields):
    return parse_model(event(seq, principal, {"kind": kind, "stream_id": stream, "ts_ms": seq * 1000, **fields}))


class ChatStateTest(unittest.TestCase):
    def test_reset_replaces_effective_content_positions_and_attachments(self):
        projection = ChatProjection()
        projection.apply(chat(1, "turn.single", turn_id="user", text="question"))
        old = SimpleNamespace(object_id=4, object_token="first")
        new = SimpleNamespace(object_id=5, object_token="second")
        projection.apply(chat(2, "turn.start", principal=11, text="Processing...", replies=(1,), keys=(old,)))
        projection.apply(chat(3, "turn.append", principal=11, pre_seq=2, text="old"))
        projection.apply(chat(4, "turn.reset", principal=11, pre_seq=3, text="new", keys=(new,)))
        turn = projection.by_creation[2]
        self.assertEqual(turn.content_seqs, [4])
        self.assertEqual([key.object_id for key in turn.object_keys], [5])
        self.assertEqual((turn.creation_seq, turn.reply_to_seqs, turn.last_seq), (2, (1,), 4))
        self.assertTrue(turn.has_text)
        self.assertFalse(hasattr(turn, "text"))

    def test_user_cancel_wins_over_later_end_and_reset_and_keeps_its_attachments(self):
        projection = ChatProjection()
        key = SimpleNamespace(object_id=4, object_token="cancel-object")
        projection.apply(chat(1, "turn.start", principal=11, text="partial"))
        projection.apply(chat(2, "turn.cancel", target=(11, "turn"), keys=(key,)))
        projection.apply(chat(3, "turn.reset", principal=11, pre_seq=1, text="too late"))
        projection.apply(chat(4, "turn.end", principal=11, pre_seq=1))
        turn = projection.by_creation[1]
        self.assertEqual((turn.status, turn.terminal_seq, turn.cancelled_by), ("cancelled", 2, 10))
        self.assertTrue(turn.has_text)
        self.assertEqual([item.object_id for item in turn.object_keys], [4])
        self.assertEqual(turn.content_seqs, [])
        self.assertEqual(projection.by_terminal, {})

    def test_end_wins_over_later_user_cancel(self):
        projection = ChatProjection()
        projection.apply(chat(1, "turn.start", principal=11, text="complete"))
        projection.apply(chat(2, "turn.end", principal=11, pre_seq=1))
        projection.apply(chat(3, "turn.cancel", target=(11, "turn")))
        self.assertEqual(projection.by_creation[1].status, "completed")
        self.assertIs(projection.by_terminal[2], projection.by_creation[1])
        self.assertIsNone(projection.by_creation[1].cancelled_by)

    def test_ready_inputs_follow_completion_order_and_skip_cancelled_or_empty(self):
        projection = ChatProjection()
        projection.apply(chat(1, "turn.start", turn_id="slow", text="first start"))
        projection.apply(chat(2, "turn.single", turn_id="fast", text="ready first"))
        projection.apply(chat(3, "turn.single", turn_id="empty"))
        projection.apply(chat(4, "turn.end", turn_id="slow", pre_seq=1))
        projection.apply(chat(5, "turn.start", turn_id="cancel", text="cancel me"))
        projection.apply(chat(6, "turn.cancel", target=(10, "cancel"), start_seq=5))
        key = SimpleNamespace(object_id=1, object_token="object")
        projection.apply(chat(7, "turn.single", turn_id="attachment", keys=(key,)))
        self.assertEqual([turn.creation_seq for turn in projection.ready(10)], [2, 1, 7])

    def test_replaying_processed_events_does_not_duplicate_content_positions(self):
        projection = ChatProjection()
        start = chat(1, "turn.start", text="a")
        append = chat(2, "turn.append", pre_seq=1, text="b")
        for item in (start, append, start, append):
            projection.apply(item)
        self.assertEqual(projection.by_creation[1].content_seqs, [1, 2])

    def test_stream_projection_does_not_store_text_and_releases_completed_chain(self):
        projection = ChatProjection()
        projection.apply(chat(1, "turn.start", text="old"))
        projection.apply(chat(2, "turn.append", pre_seq=1, text="discarded"))
        projection.apply(chat(3, "turn.reset", pre_seq=2, text="new"))
        for seq in range(4, 104):
            projection.apply(chat(seq, "turn.append", pre_seq=seq - 1, text="片"))
        self.assertEqual(projection.by_creation[1].content_seqs, list(range(3, 104)))
        self.assertFalse(hasattr(projection.by_creation[1], "_text_parts"))
        projection.apply(chat(104, "turn.end", pre_seq=103, content_seqs=range(3, 104)))
        self.assertEqual([turn.creation_seq for turn in projection.ready(10)], [1])
        self.assertEqual(projection.by_creation[1].content_seqs, [])

    def test_reset_to_empty_removes_text_readiness_without_losing_attachment_inputs(self):
        projection = ChatProjection()
        projection.apply(chat(1, "turn.start", text="discarded"))
        projection.apply(chat(2, "turn.reset", pre_seq=1))
        projection.apply(chat(3, "turn.end", pre_seq=2))
        self.assertFalse(projection.ready(10))
        key = SimpleNamespace(object_id=1, object_token="object")
        projection.apply(chat(4, "turn.start", turn_id="attachment", text="discarded"))
        projection.apply(chat(5, "turn.reset", turn_id="attachment", pre_seq=4, keys=(key,)))
        projection.apply(chat(6, "turn.end", turn_id="attachment", start_seq=4, pre_seq=5))
        self.assertEqual([turn.creation_seq for turn in projection.ready(10)], [4])

    def test_forked_chain_is_rejected(self):
        projection = ChatProjection()
        projection.apply(chat(1, "turn.start", text="a"))
        projection.apply(chat(2, "turn.append", pre_seq=1, text="b"))
        with self.assertRaises(HistoryError):
            projection.apply(chat(3, "turn.append", pre_seq=1, text="fork"))

    def test_replies_use_completed_terminal_positions(self):
        for kind in ("turn.single", "turn.start"):
            for single in (False, True):
                with self.subTest(kind=kind, single=single):
                    projection = ChatProjection()
                    projection.apply(chat(1, "turn.single" if single else "turn.start", text="final"))
                    terminal = 1
                    if not single:
                        projection.apply(chat(2, "turn.end", pre_seq=1))
                        terminal = 2
                    projection.apply(chat(3, kind, turn_id="reply", text="question", replies=(terminal,)))
                    self.assertEqual(projection.by_creation[3].reply_to_seqs, (terminal,))
                    self.assertIs(projection.by_terminal[terminal], projection.by_creation[1])

    def test_replies_reject_streaming_creation_content_and_cancelled_positions(self):
        for target in (1, 2, 3):
            with self.subTest(target=target):
                projection = ChatProjection()
                projection.apply(chat(1, "turn.start", text="unfinished"))
                projection.apply(chat(2, "turn.append", pre_seq=1, text="draft"))
                projection.apply(chat(3, "turn.cancel", target=(10, "turn")))
                with self.assertRaisesRegex(HistoryError, "preceding completed"):
                    projection.apply(chat(4, "turn.single", turn_id="reply", text="question", replies=(target,)))
                self.assertNotIn(4, projection.by_creation)

    def test_error_end_clears_content_and_attachments_but_remains_replyable(self):
        projection = ChatProjection()
        key = SimpleNamespace(object_id=1, object_token="object")
        projection.apply(chat(1, "turn.start", text="draft", keys=(key,)))
        projection.apply(chat(2, "turn.end", pre_seq=1, error="turn_too_long"))
        turn = projection.by_terminal[2]
        self.assertEqual((turn.status, turn.has_text, turn.object_keys), ("completed", False, []))
        self.assertFalse(projection.ready(10))
        projection.apply(chat(3, "turn.single", turn_id="reply", text="retry?", replies=(2,)))
        self.assertEqual(projection.by_creation[3].reply_to_seqs, (2,))


class ModelStateTest(unittest.TestCase):
    def setUp(self):
        self.view = ModelView(100, "stream")

    def apply(self, item):
        self.view.apply(item, agent_principal=10, model_principal=20)

    def test_stream_chain_and_first_terminal_ignore_late_output(self):
        self.apply(model(101, "infer.result", prev_seq=100, status_code=200))
        first = model(102, "infer.append", request_seq=100, prev_seq=101, body={"choices": []})
        self.apply(first)
        end = model(103, "infer.end", request_seq=100, status_code=200, end_status="completed")
        self.apply(end)
        self.apply(model(104, "infer.append", request_seq=100, prev_seq=102, body={"late": True}))
        self.apply(model(105, "infer.cancel", principal=10, request_seq=100))
        self.assertEqual(self.view.chunks, [first])
        self.assertIs(self.view.terminal, end)

    def test_cancel_before_response_head_is_terminal(self):
        cancel = model(101, "infer.cancel", principal=10, request_seq=100)
        self.apply(cancel)
        self.apply(model(102, "infer.result", prev_seq=100, status_code=200))
        self.assertIs(self.view.terminal, cancel)
        self.assertFalse(self.view.head)

    def test_model_channel_member_may_cancel_the_request(self):
        cancel = model(101, "infer.cancel", principal=20, request_seq=100)
        self.apply(cancel)
        self.assertIs(self.view.terminal, cancel)

    def test_interrupted_end_before_head_is_valid(self):
        end = model(101, "infer.end", request_seq=100, status_code=60000, end_status="interrupted", body={"error": {"code": "timeout"}})
        self.apply(end)
        self.assertIs(self.view.terminal, end)

    def test_late_events_from_other_attempts_do_not_enter_current_stream(self):
        self.apply(model(101, "infer.result", stream="old-stream", prev_seq=90, status_code=200))
        self.apply(model(102, "infer.end", stream="old-stream", request_seq=90, status_code=200, end_status="completed"))
        self.assertFalse(self.view.head)
        self.assertIsNone(self.view.terminal)

    def test_first_matching_result_is_kept_and_subsequent_results_are_ignored(self):
        self.apply(model(101, "infer.result", prev_seq=100, status_code=200))
        self.apply(model(102, "infer.result", prev_seq=100, status_code=500, body={"error": "later result"}))
        self.assertEqual(self.view.chain_seq, 101)
        self.assertIsNone(self.view.terminal)
        self.apply(model(103, "infer.append", request_seq=100, prev_seq=101, body={"choices": []}))
        self.assertEqual(len(self.view.chunks), 1)

    def test_append_without_head_or_with_broken_chain_is_rejected(self):
        with self.assertRaises(HistoryError):
            self.apply(model(101, "infer.append", request_seq=100, prev_seq=100, body={}))
        self.apply(model(102, "infer.result", prev_seq=100, status_code=200))
        with self.assertRaises(HistoryError):
            self.apply(model(103, "infer.append", request_seq=100, prev_seq=101, body={}))

    def test_request_rejection_is_terminal_without_starting_a_stream(self):
        rejection = model(101, "infer.result", prev_seq=100, status_code=60009, body={"error": "provider"})
        self.apply(rejection)
        self.assertIs(self.view.terminal, rejection)


if __name__ == "__main__":
    unittest.main()
