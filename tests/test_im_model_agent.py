from __future__ import annotations

import json
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from grpc import RpcError, StatusCode
from im_model_agent.config import parse_config
from im_model_agent.prompt import (
    ToolCall,
    parse_assistant_message,
    system_prompt_content,
    trim_messages,
    visible_content,
)
from im_model_agent.state import AgentStateError, CommandState
from im_model_agent.wal import InputRef, encode_prepare, model_request_id, parse_model_request_id, parse_prepare
from im_model_agent.worker import ImModelAgent, _result_from_command
from openevent.cmd_sdk.codec import dumps_payload as cmd_dumps_payload
from openevent.cmd_sdk.codec import run_result_input_to_dict
from openevent.cmd_sdk.model import CmdOutput, CmdRunRequest, CmdRunResult, CmdRunResultInput
from openevent.im_sdk.codec import encode_send_result, encode_sync_record
from openevent.im_sdk.model import SendResultInput, SyncRecordInput
from openevent.model_proxy_sdk.codec import dumps_payload, result_input_to_dict


@dataclass
class Message:
    seq: int
    channel_id: int
    principal: int
    payload: bytes
    recipients: list[int] = field(default_factory=list)


@dataclass
class Channel:
    protocol: str
    description: str
    members: list[int]
    visibility: int = 2


class FakeOpenEvent:
    def __init__(self):
        self.next_seq = 100
        self.published: list[dict] = []
        self.history: list[Message] = []
        self.fetch_calls = 0
        self.channels = {
            11: Channel("im.v1", json.dumps({"version": "v1", "session_type": "p2p"}), [10001, 90002, 90001]),
            12: Channel("llm.v1", json.dumps({"version": "v1"}), [90002, 20001]),
            13: Channel(
                "agent.wal.v1",
                json.dumps({"version": "v1", "session_id": "s1", "im_channel_id": 11, "model_channel_id": 12}),
                [90002],
            ),
            14: Channel("cmd.v1", json.dumps({"version": "v1"}), [90002, 30001]),
        }

    def get_status(self, principal, token):
        return SimpleNamespace(max_seq=max([0, *(message.seq for message in self.history)]))

    def get_channel(self, principal, token, channel_id):
        return SimpleNamespace(channel=self.channels[channel_id])

    def fetch(self, principal, token, from_seq, limit, only_my_recipient=False, channels=()):
        self.fetch_calls += 1
        selected = [message for message in self.history if message.seq >= from_seq and (not channels or message.channel_id in channels)]
        messages = selected[:limit]
        last_seq = max([0, *(message.seq for message in self.history)])
        next_seq = messages[-1].seq + 1 if len(selected) > len(messages) else last_seq + 1
        return SimpleNamespace(messages=messages, next_seq=next_seq, last_seq=last_seq)

    def publish_auto_seq(self, principal, token, channel_id, payload, recipients):
        seq = self.next_seq
        self.next_seq += 1
        self.published.append({"seq": seq, "principal": principal, "channel_id": channel_id, "payload": payload, "recipients": tuple(recipients)})
        return SimpleNamespace(seq=seq)


def config(overrides: dict | None = None):
    agent = {
        "principal": 90002,
        "token": "agent-token",
        "system_prompt": "be useful",
        "model": "gpt-test",
    }
    agent.update(overrides or {})
    return parse_config(
        {
            "version": "v1",
            "agent": agent,
            "openevent": {"target": "127.0.0.1:9527"},
            "model_proxy": {"principal": 20001},
            "cmd_worker": {"principal": 30001},
            "im_sync_worker": {"principal": 90001},
            "sessions": [{"session_id": "s1", "im_channel_id": 11, "model_channel_id": 12, "wal_channel_id": 13, "cmd_channel_id": 14, "user_principal": 10001}],
        }
    )


def sync(seq: int, text: str) -> Message:
    payload = encode_sync_record(
        SyncRecordInput(
            provider_message_id=f"user-{seq}",
            msg_type="text",
            content_raw={"text": text},
            text=text,
            event_ms=1710000000000 + seq,
            ingested_ms=1710000000000 + seq,
        )
    )
    return Message(seq, 11, 10001, payload)


def model_result(event: FakeOpenEvent, request: dict, message: dict, status: int = 200) -> Message:
    seq = event.next_seq
    event.next_seq += 1
    payload = result_input_to_dict(
        SimpleNamespace(
            request_id=request["request_id"],
            prev_seq=request["_seq"],
            status_code=status,
            headers=[],
            body={"choices": [{"message": {"role": "assistant", **message}}]},
        ),
        ts_ms=1710000001000 + seq,
    )
    return Message(seq, 12, 20001, dumps_payload(payload), [90002])


def published_json(event: FakeOpenEvent, index: int) -> dict:
    data = json.loads(event.published[index]["payload"].decode("utf-8"))
    data["_seq"] = event.published[index]["seq"]
    return data


def published_message(item: dict) -> Message:
    return Message(
        seq=item["seq"],
        channel_id=item["channel_id"],
        principal=item["principal"],
        payload=item["payload"],
        recipients=list(item["recipients"]),
    )


class AgentDesignTests(unittest.TestCase):
    def test_config_uses_cmd_timeout_and_has_no_freeze(self):
        parsed = config()
        self.assertEqual(parsed.agent.cmd_result_timeout_ms, 330000)
        self.assertFalse(hasattr(parsed.agent, "freeze_message"))

    def test_wal_supports_users_and_tool_results(self):
        payload = encode_prepare(
            prepare_id="prepare:one",
            pre_llm_seq=9,
            user_message_seqs=[2],
            input_event_refs=[InputRef("exec_result", 8)],
            ts_ms=10,
        )
        parsed = parse_prepare(payload)
        self.assertEqual(parsed.user_message_seqs, (2,))
        self.assertEqual(parsed.input_event_refs, (InputRef("exec_result", 8),))
        self.assertEqual(model_request_id("s1", 11, 3), "agent:s1:wal:11:retry:3")
        self.assertEqual(
            parse_model_request_id(model_request_id("session/a b:wal:part", 11, 3)),
            ("session/a b:wal:part", 11, 3),
        )

    def test_assistant_tool_calls_require_openai_shape_and_unique_ids(self):
        valid = parse_assistant_message(
            {"choices": [{"message": {"role": "assistant", "content": "working", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{\"command\":\"pwd\"}", "provider_metadata": "ignored"}}]}}]}
        )
        self.assertIsNotNone(valid)
        self.assertEqual(
            valid.raw["tool_calls"],
            [{"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{\"command\":\"pwd\"}"}}],
        )
        invalid = parse_assistant_message(
            {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "exec", "arguments": "{\"command\":\"pwd\"}"}}, {"id": "x", "type": "function", "function": {"name": "exec", "arguments": "{\"command\":\"ls\"}"}}]}}]}
        )
        self.assertIsNone(invalid)

    def test_system_prompt_includes_business_and_protocol_rules(self):
        prompt = system_prompt_content("be useful")
        self.assertTrue(prompt.startswith("be useful\n\n"))
        self.assertIn("Tool results arrive as role=tool messages", prompt)
        self.assertIn("error_code=timeout", prompt)
        self.assertIn("Every assistant response must include non-empty content", prompt)

    def test_cmd_result_wait_timeout_has_structured_error_code(self):
        tool_call = ToolCall("call_1", "exec", {"command": "sleep 1"}, {})
        command = CommandState("model-1", 0, tool_call)
        command.timeout = SimpleNamespace()

        result = _result_from_command(command)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "timeout")
        self.assertNotIn("exec_id", result)

    def test_cmd_worker_execution_timeout_has_structured_error_code_and_exec_id(self):
        tool_call = ToolCall("call_1", "exec", {"command": "sleep 1"}, {})
        command = CommandState(
            "model-1",
            0,
            tool_call,
            request_seq=42,
            request=CmdRunRequest(command="sleep 1", ts_ms=1),
            result_seq=43,
            result=CmdRunResult(
                prev_seq=42,
                ts_ms=2,
                status="TIMEOUT",
                stdout=CmdOutput(file="42.stdout", bytes=0, content_encoding="utf-8", content=""),
                stderr=CmdOutput(file="42.stderr", bytes=0, content_encoding="utf-8", content=""),
                finished_at_ms=2,
            ),
        )

        result = _result_from_command(command)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "timeout")
        self.assertEqual(result["exec_id"], 42)
        self.assertEqual(result["error_message"], "command execution timed out")

    def test_content_empty_rule(self):
        self.assertIsNone(visible_content(None))
        self.assertIsNone(visible_content(" \t\r\n"))
        self.assertEqual(visible_content("  answer  "), "  answer  ")

    def test_legacy_failed_im_result_does_not_stop_agent_or_mark_echo(self):
        agent = ImModelAgent(config(), FakeOpenEvent())
        message = Message(
            seq=2,
            channel_id=11,
            principal=90001,
            recipients=[90002],
            payload=encode_send_result(
                SendResultInput(
                    request_id="legacy-failed",
                    prev_seq=1,
                    status="FAILED",
                    error_code="PROVIDER_SEND_FAILED",
                    error_message="legacy failure",
                    event_ms=1710000000000,
                )
            ),
        )

        agent.observe_message(message, realtime=False)

        session = agent.state.sessions_by_id["s1"]
        self.assertEqual(session.im_results_by_prev_seq, {})
        self.assertEqual(session.im_results_by_provider_id, {})

    def test_context_trimming_keeps_assistant_tool_group_atomic(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "{}"},
            {"role": "tool", "tool_call_id": "b", "content": "{}"},
        ]
        trimmed = trim_messages(messages, 3)
        self.assertEqual([item["role"] for item in trimmed], ["system", "assistant", "tool", "tool"])

    def test_content_and_tools_are_independent_and_tool_result_is_native(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        agent.validate_channels()
        agent.observe_message(sync(1, "list files"), realtime=True)
        agent.process_ready_sessions()
        request = published_json(event, 1)
        self.assertEqual(request["request_id"], "agent:s1:wal:100:retry:1")

        assistant = {
            "content": "I will inspect it.",
            "tool_calls": [{"id": "call_exec", "type": "function", "function": {"name": "exec", "arguments": "{\"command\":\"ls\"}"}}],
        }
        agent.observe_message(model_result(event, request, assistant), realtime=True)
        self.assertEqual([item["channel_id"] for item in event.published], [13, 12, 11, 14])
        send = published_json(event, 2)
        self.assertEqual(send["request_id"], f"model-content:{request['request_id']}")
        agent.observe_message(published_message(event.published[2]), realtime=True)

        cmd_request = published_json(event, 3)
        self.assertNotIn("request_id", cmd_request)
        result = run_result_input_to_dict(
            CmdRunResultInput(
                prev_seq=cmd_request["_seq"],
                status="SUCCESS",
                timeout_ms=300000,
                started_at_ms=1710000000000,
                finished_at_ms=1710000000100,
                exit_code=0,
                stdout=CmdOutput(file="14/103.stdout", bytes=10, content_encoding="utf-8", content="README.md\n"),
                stderr=CmdOutput(file="14/103.stderr", bytes=0, content_encoding="utf-8", content=""),
            )
        )
        result_message = Message(event.next_seq, 14, 30001, cmd_dumps_payload(result), [90002])
        event.next_seq += 1
        agent.observe_message(result_message, realtime=True)
        followup = published_json(event, 5)
        self.assertEqual([item["role"] for item in followup["body"]["messages"]], ["system", "user", "assistant", "tool"])
        self.assertEqual(followup["body"]["messages"][-1]["tool_call_id"], "call_exec")

    def test_empty_model_output_is_accepted_without_im(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        agent.observe_message(sync(1, "hello"), realtime=True)
        agent.process_ready_sessions()
        request = published_json(event, 1)
        with self.assertLogs("im_model_agent.worker", level="WARNING") as logs:
            agent.observe_message(model_result(event, request, {"content": ""}), realtime=True)
        self.assertEqual([item["channel_id"] for item in event.published], [13, 12])
        self.assertTrue(agent.state.sessions_by_id["s1"].wal_by_seq[100].latest_attempt.accepted)
        self.assertIn("accepted model result has empty content", logs.output[0])

    def test_failure_retries_same_wal_then_blocks_without_freeze_message(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config({"max_model_attempts": 2}), event)
        agent.observe_message(sync(1, "hello"), realtime=True)
        agent.process_ready_sessions()
        first = published_json(event, 1)
        agent.observe_message(model_result(event, first, {"content": None}, status=500), realtime=True)
        second = published_json(event, 2)
        self.assertEqual(second["request_id"], "agent:s1:wal:100:retry:2")
        agent.observe_message(model_result(event, second, {"content": None}, status=500), realtime=True)
        wal = agent.state.sessions_by_id["s1"].wal_by_seq[100]
        self.assertTrue(wal.blocked)
        self.assertEqual([item["channel_id"] for item in event.published], [13, 12, 12])

    def test_duplicate_prepare_uses_smallest_seq(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        agent.observe_message(sync(1, "hello"), realtime=False)
        payload = encode_prepare(
            prepare_id="prepare:duplicate",
            pre_llm_seq=0,
            user_message_seqs=[1],
            ts_ms=10,
        )

        agent.observe_message(Message(10, 13, 90002, payload), realtime=False)
        agent.observe_message(Message(11, 13, 90002, payload), realtime=False)

        session = agent.state.sessions_by_id["s1"]
        self.assertEqual(tuple(session.wal_by_seq), (10,))
        self.assertEqual(session.wal_aliases, {11: 10})

    def test_duplicate_model_request_and_result_use_canonical_seq(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        agent.observe_message(sync(1, "hello"), realtime=False)
        agent.process_ready_sessions()
        request_item = event.published[1]
        duplicate_request_seq = event.next_seq
        event.next_seq += 1
        agent.observe_message(
            Message(
                duplicate_request_seq,
                12,
                90002,
                request_item["payload"],
                list(request_item["recipients"]),
            ),
            realtime=False,
        )

        request = published_json(event, 1)
        result = model_result(event, request, {"content": "answer"})
        agent.observe_message(result, realtime=False)
        agent.observe_message(
            Message(result.seq + 1, result.channel_id, result.principal, result.payload, result.recipients),
            realtime=False,
        )

        session = agent.state.sessions_by_id["s1"]
        attempt = session.wal_by_seq[100].latest_attempt
        self.assertEqual(attempt.request_seq, request["_seq"])
        self.assertEqual(attempt.result_seq, result.seq)

    def test_conflicting_model_stable_ids_fail(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        agent.observe_message(sync(1, "hello"), realtime=False)
        agent.process_ready_sessions()
        request = published_json(event, 1)
        conflicting_request = {key: value for key, value in request.items() if key != "_seq"}
        conflicting_request["path"] = "/v1/responses"
        conflicting_seq = event.next_seq
        event.next_seq += 1

        with self.assertRaisesRegex(AgentStateError, "conflicting model request ID"):
            agent.observe_message(
                Message(conflicting_seq, 12, 90002, dumps_payload(conflicting_request)),
                realtime=False,
            )

        first_result = model_result(event, request, {"content": "first"})
        agent.observe_message(first_result, realtime=False)
        conflicting_result = model_result(event, request, {"content": "second"})
        with self.assertRaisesRegex(AgentStateError, "conflicting model result"):
            agent.observe_message(conflicting_result, realtime=False)

    def test_publish_parameter_error_does_not_reconcile_or_retry(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        calls = 0

        def publish():
            nonlocal calls
            calls += 1
            raise ValueError("invalid request")

        with self.assertRaisesRegex(ValueError, "invalid request"):
            agent._reliable_publish(
                channel_id=13,
                stable_id="stable",
                expected={"id": "stable"},
                publish=publish,
                decode=lambda message: None,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(event.fetch_calls, 0)

    def test_uncertain_publish_reconciles_committed_message(self):
        class Unavailable(RpcError):
            def code(self):
                return StatusCode.UNAVAILABLE

        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        payload = {"id": "stable"}
        calls = 0

        def publish():
            nonlocal calls
            calls += 1
            event.history.append(Message(1, 13, 90002, json.dumps(payload).encode("utf-8")))
            raise Unavailable()

        seq = agent._reliable_publish(
            channel_id=13,
            stable_id="stable",
            expected=payload,
            publish=publish,
            decode=lambda message: ("stable", json.loads(message.payload.decode("utf-8"))),
        )

        self.assertEqual(seq, 1)
        self.assertEqual(calls, 1)
        self.assertEqual(event.fetch_calls, 1)

    def test_prepare_rejects_missing_or_reused_inputs(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        agent.observe_message(sync(1, "hello"), realtime=False)
        first = encode_prepare(
            prepare_id="prepare:first",
            pre_llm_seq=0,
            user_message_seqs=[1],
            ts_ms=10,
        )
        agent.observe_message(Message(10, 13, 90002, first), realtime=False)

        reused = encode_prepare(
            prepare_id="prepare:second",
            pre_llm_seq=0,
            user_message_seqs=[1],
            ts_ms=11,
        )
        with self.assertRaisesRegex(AgentStateError, "reuses user reference"):
            agent.observe_message(Message(11, 13, 90002, reused), realtime=False)

        missing = encode_prepare(
            prepare_id="prepare:missing",
            pre_llm_seq=0,
            user_message_seqs=[2],
            ts_ms=12,
        )
        with self.assertRaisesRegex(AgentStateError, "does not belong to this session"):
            agent.observe_message(Message(12, 13, 90002, missing), realtime=False)

    def test_recovery_matches_cmd_v1_request_by_order_and_payload(self):
        event = FakeOpenEvent()
        agent = ImModelAgent(config(), event)
        user = sync(1, "list files")
        agent.observe_message(user, realtime=True)
        agent.process_ready_sessions()
        request = published_json(event, 1)
        result_message = model_result(
            event,
            request,
            {
                "content": "",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_exec",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{\"command\":\"ls\"}"},
                    }
                ],
            },
        )
        with self.assertLogs("im_model_agent.worker", level="WARNING"):
            agent.observe_message(result_message, realtime=True)
        cmd_request = published_json(event, 2)
        cmd_result = run_result_input_to_dict(
            CmdRunResultInput(
                prev_seq=cmd_request["_seq"],
                status="SUCCESS",
                timeout_ms=300000,
                finished_at_ms=1710000000200,
                exit_code=0,
                stdout=CmdOutput(file="14/103.stdout", bytes=0, content_encoding="utf-8", content=""),
                stderr=CmdOutput(file="14/103.stderr", bytes=0, content_encoding="utf-8", content=""),
            )
        )
        cmd_result_message = Message(event.next_seq, 14, 30001, cmd_dumps_payload(cmd_result), [90002])
        event.next_seq += 1
        agent.observe_message(cmd_result_message, realtime=True)

        recovered_event = FakeOpenEvent()
        recovered_event.history = sorted(
            [
                user,
                result_message,
                cmd_result_message,
                *(published_message(item) for item in event.published),
            ],
            key=lambda message: message.seq,
        )
        recovered_event.next_seq = max(message.seq for message in recovered_event.history) + 1
        recovered = ImModelAgent(config(), recovered_event)

        with self.assertLogs("im_model_agent.worker", level="WARNING"):
            recovered.recover()

        session = recovered.state.sessions_by_id["s1"]
        command = session.commands_by_seq[cmd_request["_seq"]]
        self.assertEqual(command.request_id, f"cmd:{request['request_id']}:0")
        self.assertEqual(command.tool_call.id, "call_exec")
        self.assertIsNotNone(command.result)


if __name__ == "__main__":
    unittest.main()
