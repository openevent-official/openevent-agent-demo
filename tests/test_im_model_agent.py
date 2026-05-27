from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from im_model_agent.config import ConfigError, parse_config
from im_model_agent.prompt import UserPromptMessage, user_messages_content
from im_model_agent.wal import parse_prepare
from im_model_agent.worker import ImModelAgent
from openevent.im_sdk.codec import encode_send_request, encode_sync_record
from openevent.im_sdk.model import SendRequestInput, SyncRecordInput
from openevent.model_proxy_sdk.codec import dumps_payload, request_input_to_dict, result_input_to_dict


@dataclass
class Message:
    seq: int
    channel_id: int
    principal: int
    payload: bytes
    recipients: list[int] = field(default_factory=list)


@dataclass
class FetchResp:
    messages: list[Message]
    next_seq: int
    has_more: bool = False


@dataclass
class PublishResp:
    seq: int


@dataclass
class Channel:
    protocol: str
    description: str
    members: list[int]
    visibility: int = 2


class FakeOpenEvent:
    def __init__(self):
        self.next_seq = 100
        self.published = []
        self.history: list[Message] = []
        self.channels = {
            11: Channel(
                protocol="im.v1",
                description=json.dumps({"version": "v1", "session_type": "p2p"}),
                members=[10001, 90002, 90001],
            ),
            12: Channel(
                protocol="llm.v1",
                description=json.dumps({"version": "v1"}),
                members=[90002, 20001],
            ),
            13: Channel(
                protocol="agent.wal.v1",
                description=json.dumps(
                    {
                        "version": "v1",
                        "session_id": "s1",
                        "im_channel_id": 11,
                        "model_channel_id": 12,
                    }
                ),
                members=[90002],
            ),
        }

    def get_status(self, principal, token):
        return SimpleNamespace(max_seq=max([0, *(message.seq for message in self.history)]))

    def get_channel(self, principal, token, channel_id):
        return SimpleNamespace(channel=self.channels[channel_id])

    def fetch(self, principal, token, from_seq, limit, only_my_recipient=False):
        messages = [message for message in self.history if message.seq >= from_seq][:limit]
        next_seq = messages[-1].seq + 1 if messages else max([0, *(message.seq for message in self.history)]) + 1
        return FetchResp(messages=messages, next_seq=next_seq, has_more=False)

    def publish_auto_seq(self, principal, token, channel_id, payload, recipients):
        seq = self.next_seq
        self.next_seq += 1
        self.published.append(
            {
                "seq": seq,
                "principal": principal,
                "token": token,
                "channel_id": channel_id,
                "payload": payload,
                "recipients": tuple(recipients),
            }
        )
        return PublishResp(seq=seq)


def _config():
    return parse_config(
        {
            "version": "v1",
            "agent": {
                "principal": 90002,
                "token": "tok-agent",
                "system_prompt": "be useful",
                "model": "gpt-test",
            },
            "openevent": {"target": "127.0.0.1:9527"},
            "model_proxy": {"principal": 20001},
            "im_sync_worker": {"principal": 90001},
            "sessions": [
                {
                    "session_id": "s1",
                    "im_channel_id": 11,
                    "model_channel_id": 12,
                    "wal_channel_id": 13,
                    "user_principal": 10001,
                    "agent_bot_principal": 90002,
                }
            ],
        }
    )


def _sync(seq: int, text: str, event_ms: int) -> Message:
    return Message(
        seq=seq,
        channel_id=11,
        principal=10001,
        payload=encode_sync_record(
            SyncRecordInput(
                provider_message_id=f"msg-{seq}",
                msg_type="text",
                content_raw={"text": text},
                text=text,
                event_ms=event_ms,
                ingested_ms=event_ms + 1,
            )
        ),
    )


def _send_request(seq: int, request_id: str, text: str, event_ms: int) -> Message:
    return Message(
        seq=seq,
        channel_id=11,
        principal=90002,
        payload=encode_send_request(
            SendRequestInput(
                request_id=request_id,
                msg_type="text",
                content={"text": text},
                event_ms=event_ms,
            )
        ),
    )


class ImModelAgentTests(unittest.TestCase):
    def test_config_requires_agent_section(self):
        with self.assertRaises(ConfigError):
            parse_config({"version": "v1"})

    def test_config_rejects_subscribe_from_seq(self):
        data = {
            "version": "v1",
            "agent": {
                "principal": 90002,
                "token": "tok-agent",
                "system_prompt": "be useful",
                "model": "gpt-test",
            },
            "openevent": {"target": "127.0.0.1:9527", "subscribe": {"from_seq": 0}},
            "model_proxy": {"principal": 20001},
            "im_sync_worker": {"principal": 90001},
            "sessions": [
                {
                    "session_id": "s1",
                    "im_channel_id": 11,
                    "model_channel_id": 12,
                    "wal_channel_id": 13,
                    "user_principal": 10001,
                    "agent_bot_principal": 90002,
                }
            ],
        }

        with self.assertRaisesRegex(ConfigError, "from_seq"):
            parse_config(data)

    def test_user_messages_content_is_json_array_with_time_and_text_only(self):
        content = user_messages_content(
            [
                UserPromptMessage(seq=2, event_ms=1710000001500, text="one more sentence"),
                UserPromptMessage(seq=1, event_ms=1710000000000, text="hello"),
            ]
        )

        self.assertEqual(
            json.loads(content),
            [
                {"time": "2024-03-09T16:00:00.000Z", "text": "hello"},
                {"time": "2024-03-09T16:00:01.500Z", "text": "one more sentence"},
            ],
        )
        self.assertNotIn("seq", content)
        self.assertNotIn("principal", content)

    def test_agent_publishes_wal_then_model_request_for_user_message(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(_config(), event)

        agent.validate_channels()
        agent.observe_message(_sync(1, "hello", 1710000000000), realtime=True)
        agent.process_ready_sessions()

        self.assertEqual([item["channel_id"] for item in event.published], [13, 12])
        wal = parse_prepare(event.published[0]["payload"])
        self.assertEqual(wal.pre_llm_seq, 0)
        self.assertEqual(wal.user_message_seqs, (1,))
        request = json.loads(event.published[1]["payload"].decode("utf-8"))
        self.assertEqual(request["kind"], "infer.request")
        self.assertEqual(request["request_id"], "agent:s1:wal:100")
        self.assertEqual(request["body"]["model"], "gpt-test")
        self.assertEqual(request["body"]["messages"][0], {"role": "system", "content": "be useful"})
        user_content = json.loads(request["body"]["messages"][1]["content"])
        self.assertEqual(user_content, [{"time": "2024-03-09T16:00:00.000Z", "text": "hello"}])

    def test_realtime_batch_merges_pending_user_messages_into_one_prompt_message(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(_config(), event)

        agent.validate_channels()
        agent.observe_message(_sync(1, "hello", 1710000000000), realtime=True)
        agent.observe_message(_sync(2, "one more sentence", 1710000001500), realtime=True)
        agent.process_ready_sessions()

        self.assertEqual([item["channel_id"] for item in event.published], [13, 12])
        wal = parse_prepare(event.published[0]["payload"])
        self.assertEqual(wal.user_message_seqs, (1, 2))
        request = json.loads(event.published[1]["payload"].decode("utf-8"))
        self.assertEqual(len(request["body"]["messages"]), 2)
        self.assertEqual(request["body"]["messages"][1]["role"], "user")
        self.assertEqual(
            json.loads(request["body"]["messages"][1]["content"]),
            [
                {"time": "2024-03-09T16:00:00.000Z", "text": "hello"},
                {"time": "2024-03-09T16:00:01.500Z", "text": "one more sentence"},
            ],
        )

    def test_non_2xx_model_result_does_not_advance_prompt_messages(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(_config(), event)

        agent.validate_channels()
        agent.observe_message(_sync(1, "bad auth turn", 1710000000000), realtime=True)
        agent.process_ready_sessions()
        first_request = json.loads(event.published[1]["payload"].decode("utf-8"))
        result_payload = result_input_to_dict(
            SimpleNamespace(
                request_id=first_request["request_id"],
                prev_seq=event.published[1]["seq"],
                status_code=401,
                headers=[],
                body={"error": {"message": "unauthorized"}},
            ),
            ts_ms=1710000000500,
        )
        with self.assertLogs("im_model_agent.worker", level="WARNING"):
            agent.observe_message(
                Message(seq=102, channel_id=12, principal=20001, payload=dumps_payload(result_payload), recipients=[90002]),
                realtime=True,
            )
        event.next_seq = 103

        self.assertEqual(agent.state.sessions_by_id["s1"].prompt_messages, [])

        agent.observe_message(_sync(2, "next turn", 1710000001500), realtime=True)
        agent.process_ready_sessions()

        wal = parse_prepare(event.published[2]["payload"])
        self.assertEqual(wal.user_message_seqs, (2,))
        second_request = json.loads(event.published[3]["payload"].decode("utf-8"))
        self.assertEqual([item["role"] for item in second_request["body"]["messages"]], ["system", "user"])
        self.assertEqual(
            json.loads(second_request["body"]["messages"][1]["content"]),
            [{"time": "2024-03-09T16:00:01.500Z", "text": "next turn"}],
        )

    def test_next_turn_uses_last_successful_request_and_reply_as_base(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(_config(), event)

        agent.validate_channels()
        agent.observe_message(_sync(1, "first ok", 1710000000000), realtime=True)
        agent.process_ready_sessions()
        first_request = json.loads(event.published[1]["payload"].decode("utf-8"))
        ok_result = result_input_to_dict(
            SimpleNamespace(
                request_id=first_request["request_id"],
                prev_seq=event.published[1]["seq"],
                status_code=200,
                headers=[],
                body={"choices": [{"message": {"content": "first answer"}}]},
            ),
            ts_ms=1710000000500,
        )
        agent.observe_message(
            Message(seq=102, channel_id=12, principal=20001, payload=dumps_payload(ok_result), recipients=[90002]),
            realtime=True,
        )
        event.next_seq = 103

        agent.observe_message(_sync(2, "failed turn", 1710000001000), realtime=True)
        agent.process_ready_sessions()
        failed_request = json.loads(event.published[4]["payload"].decode("utf-8"))
        failed_result = result_input_to_dict(
            SimpleNamespace(
                request_id=failed_request["request_id"],
                prev_seq=event.published[4]["seq"],
                status_code=500,
                headers=[],
                body={"error": {"message": "provider failed"}},
            ),
            ts_ms=1710000001500,
        )
        with self.assertLogs("im_model_agent.worker", level="WARNING"):
            agent.observe_message(
                Message(seq=105, channel_id=12, principal=20001, payload=dumps_payload(failed_result), recipients=[90002]),
                realtime=True,
            )
        event.next_seq = 106

        agent.observe_message(_sync(3, "after failure", 1710000002000), realtime=True)
        agent.process_ready_sessions()

        next_request = json.loads(event.published[6]["payload"].decode("utf-8"))
        self.assertEqual([item["role"] for item in next_request["body"]["messages"]], ["system", "user", "assistant", "user"])
        user_messages = [
            json.loads(item["content"])
            for item in next_request["body"]["messages"]
            if item["role"] == "user"
        ]
        self.assertEqual(
            user_messages,
            [
                [{"time": "2024-03-09T16:00:00.000Z", "text": "first ok"}],
                [{"time": "2024-03-09T16:00:02.000Z", "text": "after failure"}],
            ],
        )
        self.assertEqual(next_request["body"]["messages"][2], {"role": "assistant", "content": "first answer"})

    def test_recovery_tracks_infer_request_without_result_as_in_flight(self):
        event = FakeOpenEvent()
        wal_payload = {
            "kind": "llm.request.prepare",
            "ts_ms": 1710000000100,
            "pre_llm_seq": 0,
            "user_message_seqs": [1],
        }
        request_payload = request_input_to_dict(
            SimpleNamespace(
                request_id="agent:s1:wal:2",
                method="POST",
                path="/v1/chat/completions",
                body={"model": "gpt-test", "messages": []},
            ),
            ts_ms=1710000000200,
        )
        event.history = [
            _sync(1, "hello", 1710000000000),
            Message(seq=2, channel_id=13, principal=90002, payload=json.dumps(wal_payload).encode("utf-8")),
            Message(seq=3, channel_id=12, principal=90002, payload=dumps_payload(request_payload)),
        ]

        agent = ImModelAgent(_config(), event)
        agent.recover()

        session = agent.state.sessions_by_id["s1"]
        self.assertEqual(session.in_flight_turn_id, "s1:1")
        self.assertFalse(session.pending)
        self.assertEqual(session.latest_attempt().request_seq, 3)

    def test_recovery_republishes_infer_request_for_orphan_wal(self):
        event = FakeOpenEvent()
        wal_payload = {
            "kind": "llm.request.prepare",
            "ts_ms": 1710000000100,
            "pre_llm_seq": 0,
            "user_message_seqs": [1],
        }
        event.history = [
            _sync(1, "hello", 1710000000000),
            Message(seq=2, channel_id=13, principal=90002, payload=json.dumps(wal_payload).encode("utf-8")),
        ]

        agent = ImModelAgent(_config(), event)
        agent.recover()

        self.assertEqual(len(event.published), 1)
        self.assertEqual(event.published[0]["channel_id"], 12)
        request = json.loads(event.published[0]["payload"].decode("utf-8"))
        self.assertEqual(request["request_id"], "agent:s1:wal:2")

    def test_recovery_filters_failed_requests_from_prompt_base(self):
        event = FakeOpenEvent()
        first_messages = [
            {"role": "system", "content": "be useful"},
            {
                "role": "user",
                "content": json.dumps(
                    [{"time": "2024-03-09T16:00:00.000Z", "text": "first ok"}],
                    ensure_ascii=False,
                ),
            },
        ]
        failed_messages = [
            *first_messages,
            {"role": "assistant", "content": "first answer"},
            {
                "role": "user",
                "content": json.dumps(
                    [{"time": "2024-03-09T16:00:01.000Z", "text": "failed turn"}],
                    ensure_ascii=False,
                ),
            },
        ]
        first_wal = {
            "kind": "llm.request.prepare",
            "ts_ms": 1710000000100,
            "pre_llm_seq": 0,
            "user_message_seqs": [1],
        }
        first_request = request_input_to_dict(
            SimpleNamespace(
                request_id="agent:s1:wal:2",
                method="POST",
                path="/v1/chat/completions",
                body={"model": "gpt-test", "messages": first_messages},
            ),
            ts_ms=1710000000200,
        )
        first_result = result_input_to_dict(
            SimpleNamespace(
                request_id="agent:s1:wal:2",
                prev_seq=3,
                status_code=200,
                headers=[],
                body={"choices": [{"message": {"content": "first answer"}}]},
            ),
            ts_ms=1710000000300,
        )
        failed_wal = {
            "kind": "llm.request.prepare",
            "ts_ms": 1710000000500,
            "pre_llm_seq": 3,
            "user_message_seqs": [6],
        }
        failed_request = request_input_to_dict(
            SimpleNamespace(
                request_id="agent:s1:wal:7",
                method="POST",
                path="/v1/chat/completions",
                body={"model": "gpt-test", "messages": failed_messages},
            ),
            ts_ms=1710000000600,
        )
        failed_result = result_input_to_dict(
            SimpleNamespace(
                request_id="agent:s1:wal:7",
                prev_seq=8,
                status_code=500,
                headers=[],
                body={"error": {"message": "provider failed"}},
            ),
            ts_ms=1710000000700,
        )
        event.history = [
            _sync(1, "first ok", 1710000000000),
            Message(seq=2, channel_id=13, principal=90002, payload=json.dumps(first_wal).encode("utf-8")),
            Message(seq=3, channel_id=12, principal=90002, payload=dumps_payload(first_request)),
            Message(seq=4, channel_id=12, principal=20001, payload=dumps_payload(first_result), recipients=[90002]),
            _send_request(5, "im:agent:s1:wal:2", "first answer", 1710000000400),
            _sync(6, "failed turn", 1710000001000),
            Message(seq=7, channel_id=13, principal=90002, payload=json.dumps(failed_wal).encode("utf-8")),
            Message(seq=8, channel_id=12, principal=90002, payload=dumps_payload(failed_request)),
            Message(seq=9, channel_id=12, principal=20001, payload=dumps_payload(failed_result), recipients=[90002]),
            _sync(10, "after failure", 1710000002000),
        ]
        event.next_seq = 100

        agent = ImModelAgent(_config(), event)
        agent.recover()
        agent.process_ready_sessions()

        self.assertEqual(len(event.published), 2)
        wal = parse_prepare(event.published[0]["payload"])
        self.assertEqual(wal.user_message_seqs, (10,))
        request = json.loads(event.published[1]["payload"].decode("utf-8"))
        self.assertEqual([item["role"] for item in request["body"]["messages"]], ["system", "user", "assistant", "user"])
        user_messages = [
            json.loads(item["content"])
            for item in request["body"]["messages"]
            if item["role"] == "user"
        ]
        self.assertEqual(
            user_messages,
            [
                [{"time": "2024-03-09T16:00:00.000Z", "text": "first ok"}],
                [{"time": "2024-03-09T16:00:02.000Z", "text": "after failure"}],
            ],
        )
        self.assertEqual(request["body"]["messages"][2], {"role": "assistant", "content": "first answer"})

    def test_recovery_sends_im_reply_for_result_without_send_request(self):
        event = FakeOpenEvent()
        wal_payload = {
            "kind": "llm.request.prepare",
            "ts_ms": 1710000000100,
            "pre_llm_seq": 0,
            "user_message_seqs": [1],
        }
        request_payload = request_input_to_dict(
            SimpleNamespace(
                request_id="agent:s1:wal:2",
                method="POST",
                path="/v1/chat/completions",
                body={"model": "gpt-test", "messages": []},
            ),
            ts_ms=1710000000200,
        )
        result_payload = result_input_to_dict(
            SimpleNamespace(
                request_id="agent:s1:wal:2",
                prev_seq=3,
                status_code=200,
                headers=[],
                body={"choices": [{"message": {"content": "hi back"}}]},
            ),
            ts_ms=1710000000300,
        )
        event.history = [
            _sync(1, "hello", 1710000000000),
            Message(seq=2, channel_id=13, principal=90002, payload=json.dumps(wal_payload).encode("utf-8")),
            Message(seq=3, channel_id=12, principal=90002, payload=dumps_payload(request_payload)),
            Message(seq=4, channel_id=12, principal=20001, payload=dumps_payload(result_payload), recipients=[90002]),
        ]

        agent = ImModelAgent(_config(), event)
        agent.recover()

        self.assertEqual(len(event.published), 1)
        self.assertEqual(event.published[0]["channel_id"], 11)
        send = json.loads(event.published[0]["payload"].decode("utf-8"))
        self.assertEqual(send["kind"], "send.request")
        self.assertEqual(send["data"]["content"]["text"], "hi back")


if __name__ == "__main__":
    unittest.main()
