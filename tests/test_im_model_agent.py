from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from im_model_agent.config import ConfigError, parse_config
from im_model_agent.dependencies import validate_runtime_dependencies
from im_model_agent.prompt import UserPromptMessage, user_messages_content
from im_model_agent.wal import WalError, parse_prepare
from im_model_agent.worker import ImModelAgent
from openevent.im_sdk.codec import encode_send_request, encode_sync_record
from openevent.im_sdk.model import SendRequestInput, SyncRecordInput
from openevent.model_proxy_sdk.codec import dumps_payload, request_input_to_dict, result_input_to_dict
from openevent.cmd_sdk.codec import (
    dumps_payload as cmd_dumps_payload,
    output_read_result_input_to_dict,
    run_result_input_to_dict,
)
from openevent.cmd_sdk.model import CmdOutput, CmdOutputReadResultInput, CmdRunResultInput


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
    last_seq: int


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
        self.fetch_calls = []
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
            14: Channel(
                protocol="cmd.v1",
                description=json.dumps({"version": "v1"}),
                members=[90002, 30001],
            ),
        }

    def get_status(self, principal, token):
        return SimpleNamespace(max_seq=max([0, *(message.seq for message in self.history)]))

    def get_channel(self, principal, token, channel_id):
        return SimpleNamespace(channel=self.channels[channel_id])

    def fetch(self, principal, token, from_seq, limit, only_my_recipient=False, channels=()):
        channels = tuple(channels)
        self.fetch_calls.append((from_seq, limit, only_my_recipient, channels))
        matches = [message for message in self.history if message.seq >= from_seq]
        if channels:
            requested_channels = {int(channel) for channel in channels}
            matches = [message for message in matches if int(message.channel_id) in requested_channels]
        messages = matches[:limit]
        last_seq = max([0, *(message.seq for message in self.history)])
        next_seq = messages[-1].seq + 1 if len(matches) > len(messages) else last_seq + 1
        return FetchResp(messages=messages, next_seq=next_seq, last_seq=last_seq)

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


def _config(agent_overrides: dict | None = None):
    agent = {
        "principal": 90002,
        "token": "tok-agent",
        "system_prompt": "be useful",
        "model": "gpt-test",
    }
    if agent_overrides:
        agent.update(agent_overrides)
    return parse_config(
        {
            "version": "v1",
            "agent": agent,
            "openevent": {"target": "127.0.0.1:9527"},
            "model_proxy": {"principal": 20001},
            "cmd_worker": {"principal": 30001},
            "im_sync_worker": {"principal": 90001},
            "sessions": [
                {
                    "session_id": "s1",
                    "im_channel_id": 11,
                    "model_channel_id": 12,
                    "wal_channel_id": 13,
                    "cmd_channel_id": 14,
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


def _event_content(message: dict) -> list[dict]:
    content = json.loads(message["content"])
    return content["events"]


class ImModelAgentTests(unittest.TestCase):
    def test_runtime_dependencies_support_cmd_request_ids(self):
        validate_runtime_dependencies()

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
            "cmd_worker": {"principal": 30001},
            "im_sync_worker": {"principal": 90001},
            "sessions": [
                {
                    "session_id": "s1",
                    "im_channel_id": 11,
                    "model_channel_id": 12,
                    "wal_channel_id": 13,
                    "cmd_channel_id": 14,
                    "user_principal": 10001,
                    "agent_bot_principal": 90002,
                }
            ],
        }

        with self.assertRaisesRegex(ConfigError, "from_seq"):
            parse_config(data)

    def test_user_messages_content_is_events_object_with_time_and_text_only(self):
        content = user_messages_content(
            [
                UserPromptMessage(seq=2, event_ms=1710000001500, text="one more sentence"),
                UserPromptMessage(seq=1, event_ms=1710000000000, text="hello"),
            ]
        )

        self.assertEqual(
            json.loads(content),
            {
                "events": [
                    {"type": "user", "time": "2024-03-09T16:00:00.000Z", "text": "hello"},
                    {"type": "user", "time": "2024-03-09T16:00:01.500Z", "text": "one more sentence"},
                ]
            },
        )
        self.assertNotIn("seq", content)
        self.assertNotIn("principal", content)

    def test_wal_rejects_unknown_payload_fields(self):
        payload = json.dumps(
            {
                "kind": "llm.request.prepare",
                "ts_ms": 1710000000000,
                "pre_llm_seq": 0,
                "user_message_seqs": [1],
                "future_field": "must not be ignored",
            }
        ).encode("utf-8")

        with self.assertRaisesRegex(WalError, "unknown fields"):
            parse_prepare(payload)

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
        self.assertEqual(user_content, {"events": [{"type": "user", "time": "2024-03-09T16:00:00.000Z", "text": "hello"}]})

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
            {
                "events": [
                    {"type": "user", "time": "2024-03-09T16:00:00.000Z", "text": "hello"},
                    {"type": "user", "time": "2024-03-09T16:00:01.500Z", "text": "one more sentence"},
                ]
            },
        )

    def test_non_2xx_model_result_retries_same_turn_without_advancing_prompt(self):
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

        wal = parse_prepare(event.published[2]["payload"])
        self.assertEqual(wal.user_message_seqs, (1,))
        self.assertEqual(wal.pre_llm_seq, event.published[1]["seq"])
        second_request = json.loads(event.published[3]["payload"].decode("utf-8"))
        self.assertEqual(second_request["request_id"], "agent:s1:wal:102")
        self.assertEqual([item["role"] for item in second_request["body"]["messages"]], ["system", "user"])
        self.assertEqual(
            json.loads(second_request["body"]["messages"][1]["content"]),
            {"events": [{"type": "user", "time": "2024-03-09T16:00:00.000Z", "text": "bad auth turn"}]},
        )

    def test_non_2xx_model_result_freezes_after_attempt_limit(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(
            _config({"max_model_attempts": 1, "freeze_message": "frozen"}),
            event,
        )

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

        self.assertEqual([item["channel_id"] for item in event.published], [13, 12, 11])
        send = json.loads(event.published[2]["payload"].decode("utf-8"))
        self.assertEqual(send["kind"], "send.request")
        self.assertEqual(send["request_id"], "freeze:s1:1")
        self.assertEqual(send["data"]["content"]["text"], "frozen")
        session = agent.state.sessions_by_id["s1"]
        self.assertTrue(session.frozen)
        self.assertIsNone(session.in_flight_turn_id)
        self.assertEqual(session.prompt_messages, [])

    def test_exec_tool_call_publishes_cmd_request_and_feeds_result_to_model(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(_config(), event)

        agent.validate_channels()
        agent.observe_message(_sync(1, "run local command", 1710000000000), realtime=True)
        agent.process_ready_sessions()
        first_request = json.loads(event.published[1]["payload"].decode("utf-8"))
        result_payload = result_input_to_dict(
            SimpleNamespace(
                request_id=first_request["request_id"],
                prev_seq=event.published[1]["seq"],
                status_code=200,
                headers=[],
                body={
                    "choices": [
                        {
                            "message": {
                                "content": "checking",
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {"name": "exec", "arguments": "{\"command\":\"pwd\"}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            ts_ms=1710000000500,
        )

        agent.observe_message(
            Message(seq=102, channel_id=12, principal=20001, payload=dumps_payload(result_payload), recipients=[90002]),
            realtime=True,
        )

        self.assertEqual([item["channel_id"] for item in event.published], [13, 12, 14])
        cmd_request = json.loads(event.published[2]["payload"].decode("utf-8"))
        self.assertEqual(cmd_request["kind"], "cmd.run.request")
        self.assertEqual(cmd_request["request_id"], "cmd:agent:s1:wal:100:0")
        self.assertEqual(cmd_request["command"], "pwd")
        self.assertEqual(event.published[2]["recipients"], ())

        cmd_result_payload = run_result_input_to_dict(
            CmdRunResultInput(
                prev_seq=event.published[2]["seq"],
                status="SUCCESS",
                timeout_ms=300000,
                finished_at_ms=1710000000600,
                exit_code=0,
                stdout=CmdOutput(file="14/102.stdout", bytes=16, content_encoding="utf-8", content="/workspace/demo\n"),
                stderr=CmdOutput(file="14/102.stderr", bytes=0, content_encoding="utf-8", content=""),
            )
        )
        agent.observe_message(
            Message(seq=103, channel_id=14, principal=30001, payload=cmd_dumps_payload(cmd_result_payload), recipients=[90002]),
            realtime=True,
        )

        self.assertEqual([item["channel_id"] for item in event.published], [13, 12, 14, 13, 12])
        followup = json.loads(event.published[4]["payload"].decode("utf-8"))
        self.assertEqual(followup["kind"], "infer.request")
        events = _event_content(followup["body"]["messages"][-1])
        self.assertEqual(
            events,
            [
                {
                    "type": "exec_result",
                    "command": "pwd",
                    "status": "ok",
                    "exec_id": event.published[2]["seq"],
                    "stdout": "/workspace/demo\n",
                    "stderr": "",
                }
            ],
        )

    def test_read_stdout_tool_call_publishes_output_read_and_feeds_result_to_model(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(_config(), event)

        agent.validate_channels()
        agent.observe_message(_sync(1, "inspect big output", 1710000000000), realtime=True)
        agent.process_ready_sessions()
        first_request = json.loads(event.published[1]["payload"].decode("utf-8"))
        result_payload = result_input_to_dict(
            SimpleNamespace(
                request_id=first_request["request_id"],
                prev_seq=event.published[1]["seq"],
                status_code=200,
                headers=[],
                body={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {"name": "read_stdout", "arguments": "{\"exec_id\":777}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            ts_ms=1710000000500,
        )
        agent.observe_message(
            Message(seq=102, channel_id=12, principal=20001, payload=dumps_payload(result_payload), recipients=[90002]),
            realtime=True,
        )

        self.assertEqual([item["channel_id"] for item in event.published], [13, 12, 14])
        read_request = json.loads(event.published[2]["payload"].decode("utf-8"))
        self.assertEqual(read_request["kind"], "cmd.output.read.request")
        self.assertEqual(read_request["request_id"], "cmd:agent:s1:wal:100:0")
        self.assertEqual(read_request["target_seq"], 777)
        self.assertEqual(read_request["stream"], "stdout")

        read_result_payload = output_read_result_input_to_dict(
            CmdOutputReadResultInput(
                prev_seq=event.published[2]["seq"],
                status="SUCCESS",
                target_seq=777,
                stream="stdout",
                offset=0,
                nbytes=65536,
                bytes=12,
                eof=True,
                content_encoding="utf-8",
                content="hello world\n",
            )
        )
        agent.observe_message(
            Message(seq=103, channel_id=14, principal=30001, payload=cmd_dumps_payload(read_result_payload), recipients=[90002]),
            realtime=True,
        )

        self.assertEqual([item["channel_id"] for item in event.published], [13, 12, 14, 13, 12])
        followup = json.loads(event.published[4]["payload"].decode("utf-8"))
        self.assertEqual(
            _event_content(followup["body"]["messages"][-1]),
            [{"type": "read_stdout_result", "exec_id": 777, "status": "ok", "stdout": "hello world\n"}],
        )

    def test_output_read_not_found_feeds_error_message_to_model(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(_config(), event)

        agent.validate_channels()
        agent.observe_message(_sync(1, "inspect missing output", 1710000000000), realtime=True)
        agent.process_ready_sessions()
        first_request = json.loads(event.published[1]["payload"].decode("utf-8"))
        result_payload = result_input_to_dict(
            SimpleNamespace(
                request_id=first_request["request_id"],
                prev_seq=event.published[1]["seq"],
                status_code=200,
                headers=[],
                body={
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "type": "function",
                                        "function": {"name": "read_stdout", "arguments": "{\"exec_id\":777}"},
                                    }
                                ],
                            }
                        }
                    ]
                },
            ),
            ts_ms=1710000000500,
        )
        agent.observe_message(
            Message(seq=102, channel_id=12, principal=20001, payload=dumps_payload(result_payload), recipients=[90002]),
            realtime=True,
        )

        read_result_payload = output_read_result_input_to_dict(
            CmdOutputReadResultInput(
                prev_seq=event.published[2]["seq"],
                status="NOT_FOUND",
                target_seq=777,
                stream="stdout",
                offset=0,
                nbytes=65536,
                bytes=0,
                eof=True,
                content_encoding="utf-8",
                content="",
            )
        )
        agent.observe_message(
            Message(seq=103, channel_id=14, principal=30001, payload=cmd_dumps_payload(read_result_payload), recipients=[90002]),
            realtime=True,
        )

        followup = json.loads(event.published[4]["payload"].decode("utf-8"))
        self.assertEqual(
            _event_content(followup["body"]["messages"][-1]),
            [{"type": "read_stdout_result", "exec_id": 777, "status": "error", "error_message": "command output not found"}],
        )

    def test_missing_assistant_text_is_attempt_failure(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(
            _config({"max_model_attempts": 1, "freeze_message": "empty frozen"}),
            event,
        )

        agent.validate_channels()
        agent.observe_message(_sync(1, "empty answer turn", 1710000000000), realtime=True)
        agent.process_ready_sessions()
        first_request = json.loads(event.published[1]["payload"].decode("utf-8"))
        result_payload = result_input_to_dict(
            SimpleNamespace(
                request_id=first_request["request_id"],
                prev_seq=event.published[1]["seq"],
                status_code=200,
                headers=[],
                body={"choices": [{"message": {"content": ""}}]},
            ),
            ts_ms=1710000000500,
        )

        with self.assertLogs("im_model_agent.worker", level="WARNING"):
            agent.observe_message(
                Message(seq=102, channel_id=12, principal=20001, payload=dumps_payload(result_payload), recipients=[90002]),
                realtime=True,
            )

        self.assertEqual([item["channel_id"] for item in event.published], [13, 12, 11])
        send = json.loads(event.published[2]["payload"].decode("utf-8"))
        self.assertEqual(send["request_id"], "freeze:s1:1")
        self.assertEqual(send["data"]["content"]["text"], "empty frozen")

    def test_stale_failed_result_does_not_retry_or_clear_in_flight_new_attempt(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(_config(), event)

        agent.validate_channels()
        agent.observe_message(_sync(1, "stale attempt turn", 1710000000000), realtime=True)
        agent.process_ready_sessions()
        first_request = json.loads(event.published[1]["payload"].decode("utf-8"))
        first_attempt = agent.state.sessions_by_id["s1"].latest_attempt()
        agent._handle_attempt_failure(agent.state.sessions_by_id["s1"], first_attempt, "test_timeout")
        retry_request = json.loads(event.published[3]["payload"].decode("utf-8"))
        stale_result = result_input_to_dict(
            SimpleNamespace(
                request_id=first_request["request_id"],
                prev_seq=event.published[1]["seq"],
                status_code=500,
                headers=[],
                body={"error": {"message": "late failure"}},
            ),
            ts_ms=1710000000500,
        )

        agent.observe_message(
            Message(seq=104, channel_id=12, principal=20001, payload=dumps_payload(stale_result), recipients=[90002]),
            realtime=True,
        )

        self.assertEqual(len(event.published), 4)
        session = agent.state.sessions_by_id["s1"]
        self.assertEqual(session.in_flight_turn_id, "s1:1")
        self.assertEqual(session.latest_attempt().request.request_id, retry_request["request_id"])
        self.assertFalse(session.frozen)

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
        retry_request = json.loads(event.published[6]["payload"].decode("utf-8"))
        retry_result = result_input_to_dict(
            SimpleNamespace(
                request_id=retry_request["request_id"],
                prev_seq=event.published[6]["seq"],
                status_code=200,
                headers=[],
                body={"choices": [{"message": {"content": "retry answer"}}]},
            ),
            ts_ms=1710000001800,
        )
        agent.observe_message(
            Message(seq=106, channel_id=12, principal=20001, payload=dumps_payload(retry_result), recipients=[90002]),
            realtime=True,
        )
        event.next_seq = 107

        agent.observe_message(_sync(3, "after failure", 1710000002000), realtime=True)
        agent.process_ready_sessions()

        next_request = json.loads(event.published[9]["payload"].decode("utf-8"))
        self.assertEqual([item["role"] for item in next_request["body"]["messages"]], ["system", "user", "assistant", "user", "assistant", "user"])
        user_messages = [
            _event_content(item)
            for item in next_request["body"]["messages"]
            if item["role"] == "user"
        ]
        self.assertEqual(
            user_messages,
            [
                [{"type": "user", "time": "2024-03-09T16:00:00.000Z", "text": "first ok"}],
                [{"type": "user", "time": "2024-03-09T16:00:01.000Z", "text": "failed turn"}],
                [{"type": "user", "time": "2024-03-09T16:00:02.000Z", "text": "after failure"}],
            ],
        )
        self.assertEqual(next_request["body"]["messages"][2], {"role": "assistant", "content": "first answer"})
        self.assertEqual(next_request["body"]["messages"][4], {"role": "assistant", "content": "retry answer"})

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

        self.assertEqual(event.fetch_calls[0], (1, 1000, False, (11, 12, 13, 14)))
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

    def test_recovery_retries_failed_attempt_without_advancing_prompt_base(self):
        event = FakeOpenEvent()
        first_messages = [
            {"role": "system", "content": "be useful"},
            {
                "role": "user",
                "content": json.dumps(
                    {"events": [{"type": "user", "time": "2024-03-09T16:00:00.000Z", "text": "first ok"}]},
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
                    {"events": [{"type": "user", "time": "2024-03-09T16:00:01.000Z", "text": "failed turn"}]},
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
        self.assertEqual(wal.user_message_seqs, (6,))
        request = json.loads(event.published[1]["payload"].decode("utf-8"))
        self.assertEqual([item["role"] for item in request["body"]["messages"]], ["system", "user", "assistant", "user"])
        user_messages = [
            _event_content(item)
            for item in request["body"]["messages"]
            if item["role"] == "user"
        ]
        self.assertEqual(
            user_messages,
            [
                [{"type": "user", "time": "2024-03-09T16:00:00.000Z", "text": "first ok"}],
                [{"type": "user", "time": "2024-03-09T16:00:01.000Z", "text": "failed turn"}],
            ],
        )
        self.assertEqual(request["body"]["messages"][2], {"role": "assistant", "content": "first answer"})

    def test_recovery_does_not_retry_older_failed_attempt_when_new_attempt_is_in_flight(self):
        event = FakeOpenEvent()
        first_wal = {
            "kind": "llm.request.prepare",
            "ts_ms": 1710000000100,
            "pre_llm_seq": 0,
            "user_message_seqs": [1],
        }
        first_messages = [
            {"role": "system", "content": "be useful"},
            {
                "role": "user",
                "content": json.dumps(
                    {"events": [{"type": "user", "time": "2024-03-09T16:00:00.000Z", "text": "failed turn"}]},
                    ensure_ascii=False,
                ),
            },
        ]
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
                status_code=500,
                headers=[],
                body={"error": {"message": "provider failed"}},
            ),
            ts_ms=1710000000300,
        )
        retry_wal = {
            "kind": "llm.request.prepare",
            "ts_ms": 1710000000400,
            "pre_llm_seq": 3,
            "user_message_seqs": [1],
        }
        retry_request = request_input_to_dict(
            SimpleNamespace(
                request_id="agent:s1:wal:5",
                method="POST",
                path="/v1/chat/completions",
                body={"model": "gpt-test", "messages": first_messages},
            ),
            ts_ms=1710000000500,
        )
        event.history = [
            _sync(1, "failed turn", 1710000000000),
            Message(seq=2, channel_id=13, principal=90002, payload=json.dumps(first_wal).encode("utf-8")),
            Message(seq=3, channel_id=12, principal=90002, payload=dumps_payload(first_request)),
            Message(seq=4, channel_id=12, principal=20001, payload=dumps_payload(first_result), recipients=[90002]),
            Message(seq=5, channel_id=13, principal=90002, payload=json.dumps(retry_wal).encode("utf-8")),
            Message(seq=6, channel_id=12, principal=90002, payload=dumps_payload(retry_request)),
        ]

        agent = ImModelAgent(_config(), event)
        agent.recover()

        self.assertFalse(event.published)
        session = agent.state.sessions_by_id["s1"]
        self.assertEqual(session.in_flight_turn_id, "s1:1")
        self.assertEqual(session.latest_attempt().request_seq, 6)

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
