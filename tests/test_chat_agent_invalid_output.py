"""Malformed model output must fail an attempt without poisoning recovery."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from chat_model_agent.budget import FAILURE_REASONS, failure_notice

import test_chat_agent_session as sessions

TOOL_MESSAGE_LIMIT = 16 * 1024


def invalid_tool_response(arguments='{"command":"\\ud800"}'):
    return {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "tool-1", "type": "function", "function": {
                "name": "exec", "arguments": arguments,
            },
        }],
    }}]}


class InvalidOutputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = sessions.ConversationSDKTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.restart(max_model_attempts=2, context_max_input_bytes=5000)
        self.fixture.input()

    def test_invalid_unicode_tool_output_retries_without_accepting_or_running_tool(self):
        fixture = self.fixture
        def respond(request, payload):
            attempt = len(fixture.events.messages("infer.request", sessions.MODEL_CHANNEL))
            result = invalid_tool_response() if attempt == 1 else sessions.body("retry succeeded")
            fixture.end(request, payload, response_body=result)
        fixture.request_callback(respond)
        prepared = fixture.run_calls()
        self.assertEqual(len(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail")), 1)
        self.assertEqual(len(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept")), 1)
        self.assertFalse(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="tool.prepare"))
        self.assertFalse(fixture.events.messages("cmd.run.request", sessions.CMD_CHANNEL))
        self.assertTrue(fixture.session._answer_finished(prepared))
        self.assertEqual(fixture.answer_turns()[0].text, "retry succeeded")

    def test_invalid_unicode_tool_output_exhausts_and_stays_frozen_after_restart(self):
        self.assert_invalid_output_freezes_and_recovers(invalid_tool_response())

    def test_deeply_nested_tool_output_retries_freezes_and_recovers(self):
        depth = 20000
        arguments = '{"command":' + '[' * depth + '0' + ']' * depth + '}'
        self.assert_invalid_output_freezes_and_recovers(invalid_tool_response(arguments))

    def test_large_invalid_finish_reason_retries_freezes_and_recovers_with_payload_limit(self):
        fixture = self.fixture
        fixture.config = replace(fixture.config, openevent=replace(
            fixture.config.openevent, max_payload_bytes=16 * 1024))
        fixture.restart()
        response = sessions.body("answer")
        reason = "'\"" * 5000
        response["choices"][0]["finish_reason"] = reason
        limit = fixture.config.openevent.max_payload_bytes
        # The provider response fits, while interpolating repr(reason) into a
        # JSON WAL diagnostic expands it beyond this real deployment limit.
        self.assertLess(len(json.dumps(response).encode()), limit)
        self.assertGreater(len(json.dumps({"reason": f"invalid finish_reason: {reason!r}"}).encode()), limit)
        published_sizes = {}
        original_publish = fixture.events.publish_auto_seq

        def publish(**kwargs):
            payload = kwargs["payload"]
            kind = json.loads(payload)["kind"]
            self.assertLessEqual(len(payload), limit, f"{kind} exceeds the deployment payload limit")
            published_sizes.setdefault(kind, []).append(len(payload))
            return original_publish(**kwargs)

        with patch.object(fixture.events, "publish_auto_seq", side_effect=publish):
            self.assert_invalid_output_freezes_and_recovers(response)
        self.assertTrue(published_sizes["infer.end"])
        self.assertEqual(len(published_sizes["attempt.fail"]), 2)
        for failure in fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail"):
            self.assertEqual(failure.data["reason"], "Normal inference returned invalid model output")

    def test_attachment_outside_frozen_input_retries_without_running_any_tool(self):
        fixture = self.fixture
        fixture.engine.objects.batch_metadata_reader = lambda keys: [
            {"name": "later.txt", "type": "text/plain", "nbytes": 1} for _ in keys]
        def respond(request, payload):
            attempt = len(fixture.events.messages("infer.request", sessions.MODEL_CHANNEL))
            if attempt == 1:
                # This reference is already known to the Agent, but was absent
                # from the immutable input that produced these tool calls.
                fixture.input("later attachment", object_keys=[
                    sessions.ObjectKey(object_id=99, object_token="later-private-token")])
                response = invalid_tool_response(json.dumps({"command": "openevent-read-object 99", "object_ids": [99]}))
                response["choices"][0]["message"]["tool_calls"].insert(0, {
                    "id": "valid-tool", "type": "function", "function": {
                        "name": "exec", "arguments": '{"command":"pwd"}'}})
            else:
                response = sessions.body("retry succeeded")
            fixture.end(request, payload, response_body=response)
        fixture.request_callback(respond)
        fixture.run_answer()
        failed, = fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail")
        self.assertEqual(failed.data["reason"], "Normal inference returned invalid tool arguments")
        self.assertEqual(len(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept")), 1)
        self.assertFalse(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="tool.prepare"))
        self.assertFalse(fixture.events.messages("cmd.run.request", sessions.CMD_CHANNEL))
        requests = fixture.events.messages("infer.request", sessions.MODEL_CHANNEL)
        self.assertEqual(json.loads(requests[0].payload)["body"], json.loads(requests[1].payload)["body"])

    def test_excessive_attachment_selection_retries_freezes_and_recovers(self):
        self.assert_invalid_output_freezes_and_recovers(invalid_tool_response(json.dumps({
            "command": "read selected files", "object_ids": list(range(1, 1026))})))

    def test_oversized_tool_command_retries_freezes_and_recovers(self):
        command = "x" * (TOOL_MESSAGE_LIMIT * 2)
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", TOOL_MESSAGE_LIMIT):
            self.assert_invalid_output_freezes_and_recovers(
                invalid_tool_response(json.dumps({"command": command})))

    def test_small_stream_fragments_cannot_accept_a_tool_above_deployment_payload_limit(self):
        fixture = self.fixture
        fixture.config = replace(fixture.config, openevent=replace(
            fixture.config.openevent, max_payload_bytes=16 * 1024))
        fixture.restart()
        limit = fixture.config.openevent.max_payload_bytes
        arguments = json.dumps({"command": "printf " + "x" * (20 * 1024)})
        self.assertGreater(len(arguments.encode()), limit)
        fragments = [arguments[offset:offset + 4096] for offset in range(0, len(arguments), 4096)]
        published_chunks = []
        original_publish = fixture.events.publish_auto_seq

        def publish(**kwargs):
            payload = kwargs["payload"]
            kind = json.loads(payload)["kind"]
            self.assertLessEqual(len(payload), limit, f"{kind} exceeds the deployment payload limit")
            if kind == "infer.append":
                published_chunks.append(payload)
            return original_publish(**kwargs)

        def respond(request, payload):
            previous = fixture.head(request, payload)
            for index, fragment in enumerate(fragments):
                part = {"index": 0, "function": {"arguments": fragment}}
                if index == 0:
                    part.update(id="tool-1", type="function")
                    part["function"]["name"] = "exec"
                previous = fixture.append(request, payload, previous, {"choices": [{
                    "index": 0, "delta": {"tool_calls": [part]},
                    "finish_reason": "tool_calls" if index == len(fragments) - 1 else None,
                }]})
            fixture.end(request, payload)

        # Keep the real 64 MiB transport limit: only the deployment's smaller
        # cap can reject this fully assembled, otherwise valid tool call.
        with patch.object(fixture.events, "publish_auto_seq", side_effect=publish):
            self.assert_invalid_output_freezes_and_recovers(respond=respond)
        self.assertEqual(len(published_chunks), 2 * len(fragments))
        for failure in fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail"):
            self.assertEqual(failure.data["reason"], "Normal inference returned a complete event that exceeds the size limit")

    def test_json_escaping_can_make_an_otherwise_small_tool_command_too_large(self):
        command = "\x00" * (TOOL_MESSAGE_LIMIT // 6 + 1)
        arguments = json.dumps({"command": command})
        self.assertLess(len(command.encode()), TOOL_MESSAGE_LIMIT)
        self.assertGreater(len(arguments.encode()), TOOL_MESSAGE_LIMIT)
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", TOOL_MESSAGE_LIMIT):
            self.assert_invalid_output_retries_without_running_tool(invalid_tool_response(arguments))

    def test_oversized_last_tool_rejects_the_whole_group_before_any_tool_runs(self):
        response = invalid_tool_response(json.dumps({"command": "x" * (TOOL_MESSAGE_LIMIT * 2)}))
        response["choices"][0]["message"]["tool_calls"].insert(0, {
            "id": "valid-tool", "type": "function", "function": {
                "name": "exec", "arguments": '{"command":"pwd"}'}})
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", TOOL_MESSAGE_LIMIT):
            self.assert_invalid_output_retries_without_running_tool(response)

    def test_tool_wal_can_exceed_the_limit_even_when_the_cmd_request_fits(self):
        # The call ID is carried by tool.prepare, not by the small Cmd request.
        response = invalid_tool_response('{"command":"pwd"}')
        response["choices"][0]["message"]["tool_calls"][0]["id"] = "tool-" + "x" * TOOL_MESSAGE_LIMIT
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", TOOL_MESSAGE_LIMIT):
            self.assert_invalid_output_retries_without_running_tool(response)

    def test_cmd_publication_size_includes_selected_attachment_credentials(self):
        fixture = self.fixture
        fixture.input("use the file", object_keys=[
            sessions.ObjectKey(object_id=42, object_token="private-token-" * 2048)])
        fixture.engine.objects.batch_metadata_reader = lambda keys: [{
            "name": "input.txt", "type": "text/plain", "nbytes": 1} for _ in keys]
        response = invalid_tool_response(json.dumps({
            "command": "openevent-read-object 42", "object_ids": [42]}))
        with patch("chat_model_agent.display_limits.MAX_MESSAGE_BYTES", TOOL_MESSAGE_LIMIT):
            self.assert_invalid_output_retries_without_running_tool(response)
        failed, = fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail")
        self.assertNotIn("private-token-", failed.data["reason"])

    def assert_invalid_output_retries_without_running_tool(self, response):
        fixture = self.fixture
        def respond(request, payload):
            attempt = len(fixture.events.messages("infer.request", sessions.MODEL_CHANNEL))
            fixture.end(request, payload, response_body=(
                response if attempt == 1 else sessions.body("retry succeeded")))
        fixture.request_callback(respond)
        prepared = fixture.run_calls()
        self.assertEqual(len(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail")), 1)
        self.assertEqual(len(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept")), 1)
        self.assertFalse(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="tool.prepare"))
        self.assertFalse(fixture.events.messages("cmd.run.request", sessions.CMD_CHANNEL))
        self.assertTrue(fixture.session._answer_finished(prepared))
        self.assertEqual(fixture.answer_turns()[0].text, "retry succeeded")

    def assert_invalid_output_freezes_and_recovers(self, response=None, *, respond=None):
        fixture = self.fixture
        fixture.request_callback(respond or (lambda request, payload: fixture.end(
            request, payload, response_body=response)))
        prepared = fixture.run_calls()
        self.assertEqual(len(fixture.events.messages("infer.request", sessions.MODEL_CHANNEL)), 2)
        self.assertEqual(len(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.fail")), 2)
        self.assertFalse(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept"))
        self.assertFalse(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="tool.prepare"))
        self.assertFalse(fixture.events.messages("cmd.run.request", sessions.CMD_CHANNEL))
        self.assertIsNotNone(fixture.engine.journal.get(prepared.key + ":freeze"))
        notices = [turn for turn in fixture.visible_turns()
                   if turn.text in {failure_notice(reason) for reason in FAILURE_REASONS}]
        self.assertEqual(len(notices), 1)
        fixture.events.on_publish = None
        count = len(fixture.events.history)
        fixture.restart()
        self.assertIsNone(fixture.session._next_answer())
        self.assertEqual(len(fixture.events.history), count)
        fixture.input("try again")
        fixture.request_callback(lambda request, payload: fixture.end(
            request, payload, response_body=sessions.body("recovered")))
        fixture.run_calls()
        self.assertEqual(len(fixture.engine.journal.select(channel=sessions.CHAT_CHANNEL, kind="attempt.accept")), 1)
        self.assertEqual(fixture.answer_turns()[-1].text, "recovered")


if __name__ == "__main__":
    unittest.main()
