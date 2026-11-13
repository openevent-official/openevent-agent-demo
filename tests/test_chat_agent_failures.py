from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chat_model_agent.budget import FAILURE_REASONS, failure_reason
from chat_model_agent.config import parse_config
from chat_model_agent.errors import FailureCode, ModelOutputError
from chat_model_agent.prompt import CompletionAccumulator, parse_assistant_message, parse_summary, parse_tool_call
from chat_model_agent.tool_runner import ToolRunner
from test_chat_agent_config import configuration
from test_chat_agent_prompt import call, chunk, response


class FailureReasonTests(unittest.TestCase):
    def test_categories_preserve_answer_and_compaction_notices(self):
        expected = {
            FailureCode.TIMEOUT: ("Normal inference timed out waiting for the model response", "History compression timed out waiting for the model response"),
            FailureCode.CONTEXT_LIMIT: ("Normal inference exceeded the model context limit", "History compression exceeded the model context limit"),
            FailureCode.INVALID_TOOL_ARGUMENTS: ("Normal inference returned invalid tool arguments", "History compression returned an invalid summary"),
            FailureCode.INVALID_OUTPUT: ("Normal inference returned invalid model output", "History compression returned an invalid summary"),
            FailureCode.EVENT_TOO_LARGE: ("Normal inference returned a complete event that exceeds the size limit", "History compression produced a summary that exceeds the size limit"),
            FailureCode.DISPLAY_TOO_LARGE: ("The normal inference display message exceeds the publication limit", "History compression produced a summary that exceeds the size limit"),
            FailureCode.TOOL_REQUEST_TOO_LARGE: ("The normal inference tool request exceeds the publication limit", "History compression produced a summary that exceeds the size limit"),
            FailureCode.REQUEST_TOO_LARGE: ("The normal inference request exceeds the size limit", "The history compression request exceeds the size limit"),
            FailureCode.REQUEST_FAILED: ("The model request failed during normal inference", "The model request failed during history compression"),
        }
        self.assertEqual(set(expected), set(FailureCode))
        for code, reasons in expected.items():
            for purpose, reason in zip(("answer", "compaction"), reasons):
                with self.subTest(code=code, purpose=purpose):
                    self.assertEqual(failure_reason(code, purpose), reason)
                    self.assertEqual(failure_reason(ModelOutputError("diagnostic changed", code=code), purpose), reason)
        self.assertEqual(set(FAILURE_REASONS), {reason for reasons in expected.values() for reason in reasons})

    def test_diagnostic_wording_cannot_change_the_failure_category(self):
        for diagnostic in ("timed out", "provider context window exceeded", "invalid tool arguments",
                           "tool input exceeds limit", "model display exceeds limit", "request ended unsuccessfully"):
            with self.subTest(diagnostic=diagnostic):
                error = ModelOutputError(diagnostic)
                self.assertEqual(failure_reason(error), "Normal inference returned invalid model output")
                self.assertEqual(failure_reason(error, "compaction"), "History compression returned an invalid summary")

        class UnprintableError(ModelOutputError):
            def __str__(self):
                raise AssertionError("classification must not inspect diagnostics")

        self.assertEqual(failure_reason(UnprintableError("", code=FailureCode.TIMEOUT)), "Normal inference timed out waiting for the model response")

    def test_tool_validation_assigns_argument_errors_at_the_source(self):
        malformed = [call(arguments={"command": 1}), call(arguments={"command": "pwd", "object_ids": [0]}),
                     call(arguments={"command": "\ud800"}), call("unknown")]
        malformed.append({**call(), "function": {"name": "exec", "arguments": "\ud800"}})
        for raw in malformed:
            with self.subTest(raw=raw), self.assertRaises(ModelOutputError) as caught:
                parse_tool_call(raw)
            self.assertEqual(caught.exception.code, FailureCode.INVALID_TOOL_ARGUMENTS)
        for raw in (None, call(call_id="\ud800")):
            with self.subTest(raw=raw), self.assertRaises(ModelOutputError) as caught:
                parse_tool_call(raw)
            self.assertEqual(caught.exception.code, FailureCode.INVALID_OUTPUT)

    def test_stream_argument_errors_remain_distinct_from_tool_metadata_errors(self):
        for key in ("id", "name", "arguments"):
            for value in (5, "\ud800"):
                part = {"index": 0, **({key: value} if key == "id" else {"function": {key: value}})}
                with self.subTest(key=key, value=value), self.assertRaises(ModelOutputError) as caught:
                    CompletionAccumulator().add_chunk(chunk({"tool_calls": [part]}))
                expected = FailureCode.INVALID_TOOL_ARGUMENTS if key == "arguments" else FailureCode.INVALID_OUTPUT
                self.assertEqual(caught.exception.code, expected)

    def test_summary_size_and_invalid_summary_keep_separate_categories(self):
        for text, target, code in (("summary", 1, FailureCode.EVENT_TOO_LARGE),
                                   ("   ", 100, FailureCode.INVALID_OUTPUT)):
            assistant = parse_assistant_message(response(content=text))
            with self.subTest(text=text), self.assertRaises(ModelOutputError) as caught:
                parse_summary(assistant, target)
            self.assertEqual(caught.exception.code, code)

    def test_tool_preflight_distinguishes_event_and_transport_limits(self):
        runner = ToolRunner.__new__(ToolRunner)
        runner.cfg = parse_config(configuration())
        runner.cid = 7
        runner.conversation = SimpleNamespace(answer=SimpleNamespace(key="answer", data={
            "turn_id": "answer", "settings": {"context_history_max_bytes": 1 << 20}}))
        assistant = parse_assistant_message(response(calls=[call()], reason="tool_calls"))
        with self.assertRaises(ModelOutputError) as caught:
            runner.validate_errors(assistant.tool_calls[0], target=1)
        self.assertEqual(caught.exception.code, FailureCode.EVENT_TOO_LARGE)
        runner.conversation.answer.data["settings"]["context_history_max_bytes"] = 1
        with self.assertRaises(ModelOutputError) as caught:
            runner.validate(assistant, "attempt")
        self.assertEqual(caught.exception.code, FailureCode.EVENT_TOO_LARGE)
        runner.conversation.answer.data["settings"]["context_history_max_bytes"] = 1 << 20
        with patch("chat_model_agent.tool_runner.publication_fits", return_value=False):
            with self.assertRaises(ModelOutputError) as caught:
                runner.validate(assistant, "attempt")
        self.assertEqual(caught.exception.code, FailureCode.TOOL_REQUEST_TOO_LARGE)
        with patch("chat_model_agent.tool_runner.encode", side_effect=ValueError("arbitrary details")):
            with self.assertRaises(ModelOutputError) as caught:
                runner.validate(assistant, "attempt")
        self.assertEqual(caught.exception.code, FailureCode.TOOL_REQUEST_TOO_LARGE)


if __name__ == "__main__":
    unittest.main()
