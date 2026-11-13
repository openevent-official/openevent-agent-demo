"""Whole-fragment sizing includes JSON escaping and transport envelopes."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from openevent.sdk.proto import openevent_pb2
from openevent.chat_sdk import serialize_payload

from chat_model_agent.config import OpenEventConfig
from chat_model_agent.display_limits import (
    display_fits, publication_fits, publication_payload_limit, publication_size_fits,
)


class DisplayLimitsTests(unittest.TestCase):
    config = SimpleNamespace(agent=SimpleNamespace(principal=11, token="credential"),
                             openevent=OpenEventConfig("events:9527"))

    def fits(self, text):
        return display_fits(self.config, 31, "turn.reset", "answer", 19, text)

    def test_ordinary_and_large_whole_fragments_fit_transport_limit(self):
        self.assertTrue(self.fits("hello"))
        self.assertTrue(self.fits("中" * 2000))

    def test_escaped_payload_counts_more_than_raw_text(self):
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", 350):
            self.assertTrue(self.fits("x" * 100))
            self.assertFalse(self.fits("\x00" * 100))

    def test_full_message_overhead_is_counted(self):
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", 100):
            self.assertFalse(self.fits("x" * 90))

    def test_default_payload_cap_rejects_a_message_below_the_transport_limit(self):
        self.assertFalse(publication_fits(self.config, 31, b"x" * (16 * 1024 * 1024 + 1)))

    def test_payload_cap_counts_encoded_payload_without_transport_or_credentials(self):
        config = SimpleNamespace(agent=self.config.agent,
                                 openevent=OpenEventConfig("events:9527", max_payload_bytes=4096))
        object_keys = [openevent_pb2.ObjectKey(object_id=42, object_token="x" * 4096)]
        self.assertTrue(publication_fits(config, 31, b"x" * 4096, object_keys=object_keys))
        self.assertFalse(publication_fits(config, 31, b"x" * 4097))

    def test_payload_allowance_respects_deployment_and_requested_maximum(self):
        config = SimpleNamespace(agent=self.config.agent,
                                 openevent=OpenEventConfig("events:9527", max_payload_bytes=4096))
        for maximum, expected in ((None, 4096), (8192, 4096), (100, 100), (0, 0)):
            with self.subTest(maximum=maximum):
                self.assertEqual(publication_payload_limit(config, 31, maximum=maximum), expected)

    def test_chat_payload_cap_counts_json_escaping_and_utf8_bytes(self):
        text = "中\x00" * 100
        payload = serialize_payload({"kind": "turn.reset", "turn_id": "answer", "pre_seq": 19,
                                     "content": [{"type": "text", "text": text}]})
        for cap, expected in ((len(payload), True), (len(payload) - 1, False)):
            with self.subTest(cap=cap):
                config = SimpleNamespace(agent=self.config.agent,
                                         openevent=OpenEventConfig("events:9527", max_payload_bytes=cap))
                self.assertEqual(display_fits(config, 31, "turn.reset", "answer", 19, text), expected)

    def test_publication_size_includes_selected_object_credentials(self):
        payload = b'{"kind":"cmd.run.request","command":"pwd"}'
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", 4096):
            self.assertTrue(publication_fits(self.config, 31, payload, object_keys=[
                openevent_pb2.ObjectKey(object_id=42, object_token="short-token")]))
            self.assertFalse(publication_fits(self.config, 31, payload, object_keys=[
                openevent_pb2.ObjectKey(object_id=42, object_token="x" * 4096)]))

    def test_publication_must_fit_the_one_message_fetch_envelope(self):
        payload = b"x" * 4000
        max_position = (1 << 64) - 1
        request = openevent_pb2.PublishAutoSeqRequest(
            principal=self.config.agent.principal, token=self.config.agent.token,
            channel_id=31, uuid=max_position, payload=payload)
        event = openevent_pb2.EventMessage(
            seq=max_position, principal=self.config.agent.principal, channel_id=31,
            uuid=max_position, ts_ms=max_position, payload=payload)
        response = openevent_pb2.FetchResponse(
            messages=[event], next_seq=max_position, last_seq=max_position)
        self.assertGreater(response.ByteSize(), request.ByteSize())
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", request.ByteSize()):
            self.assertFalse(publication_fits(self.config, 31, payload))
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", response.ByteSize()):
            self.assertTrue(publication_fits(self.config, 31, payload))

    def test_publication_size_includes_the_agent_credential(self):
        config = SimpleNamespace(agent=SimpleNamespace(principal=11, token="x" * 4096),
                                 openevent=self.config.openevent)
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", 4096):
            self.assertFalse(publication_fits(config, 31, b"small payload"))
            self.assertEqual(publication_payload_limit(config, 31), 0)

    def test_length_check_matches_real_protobuf_at_payload_and_nested_varint_boundaries(self):
        maximum = (1 << 64) - 1
        for token in ("credential", "中" * 100):
            config = SimpleNamespace(agent=SimpleNamespace(principal=11, token=token),
                                     openevent=self.config.openevent)
            for attachments in ([], [openevent_pb2.ObjectKey(object_id=42, object_token="attachment")]):
                # Ranges cover both payload and enclosing EventMessage widths.
                for size in [0, 1, *range(65, 132), *range(16300, 16389)]:
                    payload = b"x" * size
                    request = openevent_pb2.PublishAutoSeqRequest(principal=11, token=token,
                        channel_id=31, uuid=maximum, payload=payload, object_keys=attachments)
                    event = openevent_pb2.EventMessage(seq=maximum, principal=11, channel_id=31,
                        uuid=maximum, ts_ms=maximum, payload=payload, object_keys=attachments)
                    response = openevent_pb2.FetchResponse(messages=[event], next_seq=maximum, last_seq=maximum)
                    actual = max(request.ByteSize(), response.ByteSize())
                    for limit, expected in ((actual, True), (actual - 1, False)):
                        with self.subTest(size=size, token=token, attachments=bool(attachments), limit=limit):
                            with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", limit):
                                self.assertEqual(publication_size_fits(config, 31, size,
                                    object_keys=attachments), expected)
                                self.assertEqual(publication_fits(config, 31, payload,
                                    object_keys=attachments), expected)
                                if expected:
                                    self.assertEqual(publication_payload_limit(config, 31,
                                        object_keys=attachments), size)


if __name__ == "__main__":
    unittest.main()
