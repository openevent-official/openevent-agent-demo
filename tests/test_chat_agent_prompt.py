import base64
import json
import unittest

from chat_model_agent.prompt import (
    UINT64_MAX, CompletionAccumulator, ModelOutputError,
    build_model_messages, command_result_event, encode_output, model_tools,
    output_read_event, parse_assistant_message, parse_tool_call, tool_error_result,
    assistant_event, tool_call_event, tool_result_event, user_cancelled_event, user_event,
    user_message, isoformat_ms, canonical_json, parse_summary,
)


def call(name="exec", arguments=None, call_id="call_1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments if arguments is not None else {"command": "pwd"}),
    }}


def response(*, content="answer", refusal=None, calls=None, reason="stop", **metadata):
    message = {"role": "assistant", "content": content, "refusal": refusal, **metadata}
    if calls is not None:
        message["tool_calls"] = calls
    return {"choices": [{"index": 0, "message": message, "finish_reason": reason}]}


def chunk(delta=None, reason=None):
    return {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}]}


def user(text="question", **extra):
    return user_message([user_event(7, 1791504000000, text, extra.pop("attachments", []), **extra)])


class CompletionTests(unittest.TestCase):
    def test_stream_preserves_whitespace_and_ignores_usage_and_reasoning(self):
        acc = CompletionAccumulator()
        self.assertEqual(acc.add_chunk({"choices": [], "usage": {"total_tokens": 20}}), "")
        self.assertEqual(acc.add_chunk(chunk({"content": " \t", "reasoning_content": "private"})), " \t")
        self.assertEqual(acc.add_chunk(chunk({"content": "\n"}, "stop")), "\n")
        result = acc.finish()
        self.assertEqual(result.content, " \t\n")
        self.assertIsNone(result.refusal)
        self.assertEqual(result.tool_calls, ())

    def test_interleaved_tool_fragments_assemble_by_index_only_at_completion(self):
        acc = CompletionAccumulator()
        acc.add_chunk(chunk({"tool_calls": [
            {"index": 1, "id": "call_", "type": "function", "function": {"name": "read_", "arguments": "{\"exec_id\":"}},
            {"index": 0, "id": "call_", "type": "function", "function": {"name": "ex", "arguments": "{\"command\":\"p"}},
        ]}))
        with self.assertRaises(ModelOutputError):
            acc.finish()
        acc.add_chunk(chunk({"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "ec", "arguments": "wd\"}"}},
            {"index": 1, "id": "b", "function": {"name": "stdout", "arguments": "4,\"offset\":5,\"nbytes\":2}"}},
        ]}, "tool_calls"))
        result = acc.finish()
        self.assertIsNone(result.content)
        self.assertEqual([item.id for item in result.tool_calls], ["call_a", "call_b"])
        self.assertEqual([item.name for item in result.tool_calls], ["exec", "read_stdout"])
        self.assertEqual(result.tool_calls[0].arguments, {"command": "pwd"})
        self.assertEqual(result.tool_calls[1].arguments, {"exec_id": 4, "offset": 5, "nbytes": 2})

    def test_one_invalid_call_rejects_entire_assistant(self):
        for bad in (call("unknown", {}, "bad"), call(arguments={"command": "pwd", "extra": 1}, call_id="bad"), call()):
            with self.subTest(bad=bad), self.assertRaises(ModelOutputError):
                parse_assistant_message(response(calls=[call(), bad], reason="tool_calls"))

    def test_single_character_tool_fragments_preserve_escaped_json_and_unicode(self):
        acc = CompletionAccumulator()
        expected = call(arguments={"command": "printf '中文😀\\n'\n" * 128}, call_id="工具😀")
        fields = {"id": expected["id"], **expected["function"]}
        for offset in range(max(map(len, fields.values()))):
            acc.add_chunk(chunk({"tool_calls": [{
                "index": 0,
                "id": fields["id"][offset:offset + 1],
                "function": {key: fields[key][offset:offset + 1] for key in ("name", "arguments")},
            }]}))
        acc.add_chunk(chunk(reason="tool_calls"))
        result = acc.finish()
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].id, expected["id"])
        self.assertEqual(result.tool_calls[0].name, expected["function"]["name"])
        self.assertEqual(result.tool_calls[0].arguments, json.loads(expected["function"]["arguments"]))
        self.assertEqual(acc.finish(), result)

    def test_invalid_tool_index_gap_rejects_final_output(self):
        acc = CompletionAccumulator()
        acc.add_chunk(chunk({"tool_calls": [{"index": 1, **call()}]}, "tool_calls"))
        with self.assertRaises(ModelOutputError):
            acc.finish()

    def test_refusal_is_separate_and_only_visible_after_acceptance(self):
        acc = CompletionAccumulator()
        self.assertEqual(acc.add_chunk(chunk({"content": "preface", "refusal": None})), "preface")
        self.assertEqual(acc.add_chunk(chunk({"refusal": "Cannot "})), "")
        self.assertEqual(acc.add_chunk(chunk({"refusal": "answer."}, "stop")), "")
        result = acc.finish()
        self.assertEqual(result.display_text, "Cannot answer.")
        self.assertEqual(result.content, "preface")
        self.assertEqual(result.refusal, "Cannot answer.")
        self.assertEqual(result.tool_calls, ())

    def test_nonstream_refusal_and_content_do_not_overwrite_each_other(self):
        result = parse_assistant_message(response(content="original", refusal="declined"))
        self.assertEqual(result.content, "original")
        self.assertEqual(result.refusal, "declined")
        self.assertEqual(result.display_text, "declined")
        refusal_only = parse_assistant_message(response(content=None, refusal="declined"))
        self.assertIsNone(refusal_only.content)
        self.assertEqual(refusal_only.display_text, "declined")

    def test_refusal_conflicts_with_tools_and_does_not_override_bad_completion(self):
        for reason in ("stop", "tool_calls"):
            with self.subTest(reason=reason), self.assertRaises(ModelOutputError):
                parse_assistant_message(response(refusal="declined", calls=[call()], reason=reason))
        for reason in ("length", "content_filter", "function_call", "unknown", None):
            with self.subTest(reason=reason), self.assertRaises(ModelOutputError):
                parse_assistant_message(response(refusal="declined", reason=reason))

    def test_does_not_guess_refusal_from_content(self):
        result = parse_assistant_message(response(content="抱歉，不能完成。"))
        self.assertIsNone(result.refusal)
        self.assertEqual(result.content, "抱歉，不能完成。")

    def test_tool_only_is_valid_but_empty_or_reasoning_only_is_not(self):
        self.assertEqual(len(parse_assistant_message(response(content=None, calls=[call()], reason="tool_calls")).tool_calls), 1)
        for body in (response(content=None), response(content=""),
                     response(content=None, reasoning="private"), response(content=None, refusal="")):
            with self.subTest(body=body), self.assertRaises(ModelOutputError):
                parse_assistant_message(body)

    def test_finish_reason_is_required_consistent_and_last(self):
        acc = CompletionAccumulator()
        acc.add_chunk(chunk({"content": "hello"}))
        with self.assertRaises(ModelOutputError):
            acc.finish()
        acc.add_chunk(chunk(reason="stop"))
        acc.add_chunk(chunk(reason="stop"))
        self.assertEqual(acc.finish().content, "hello")
        for bad in (chunk({"content": "late"}), chunk({"refusal": "late"}), chunk(reason="length")):
            with self.subTest(bad=bad), self.assertRaises(ModelOutputError):
                acc.add_chunk(bad)

    def test_invalid_finish_reason_diagnostic_does_not_copy_model_data(self):
        text = "'\"" * 5000
        for reason in (text, {text: [text]}, [text]):
            with self.subTest(reason_type=type(reason).__name__):
                with self.assertRaises(ModelOutputError) as caught:
                    parse_assistant_message(response(reason=reason))
                self.assertEqual(str(caught.exception), "invalid finish_reason")
        acc = CompletionAccumulator()
        acc.add_chunk(chunk({"content": "answer"}, reason=text))
        with self.assertRaises(ModelOutputError) as caught:
            acc.finish()
        self.assertEqual(str(caught.exception), "invalid finish_reason")

    def test_complete_end_body_is_not_concatenated_with_stream(self):
        acc = CompletionAccumulator()
        acc.add_chunk(chunk({"content": "ans"}))
        self.assertEqual(acc.finish(response(content="answer")).content, "answer")
        self.assertEqual(CompletionAccumulator().finish(response(content="answer")).content, "answer")

    def test_invalid_response_shapes_raise_model_output_error(self):
        invalid = [None, [], {}, {"choices": []}, {"choices": [{"index": 1}]},
                   response(content=[{"type": "text", "text": "x"}]), response(refusal=1)]
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ModelOutputError):
                parse_assistant_message(body)
        for delta in ({"content": []}, {"refusal": {}}, {"tool_calls": None}, {"role": "user"},
                      {"tool_calls": [{"index": True}]}, {"tool_calls": [{"index": 0, "id": None}]}):
            with self.subTest(delta=delta), self.assertRaises(ModelOutputError):
                CompletionAccumulator().add_chunk(chunk(delta))

    def test_invalid_unicode_text_is_rejected_before_stream_display_or_acceptance(self):
        for field in ("content", "refusal"):
            for value in ("\ud800", "\udfff"):
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaises(ModelOutputError):
                        CompletionAccumulator().add_chunk(chunk({field: value}))
                    with self.assertRaises(ModelOutputError):
                        parse_assistant_message(response(**{field: value}))

    def test_stream_tool_arguments_validate_unicode_after_json_decoding(self):
        acc = CompletionAccumulator()
        raw = call(arguments={"command": "\ud800"})
        self.assertEqual(acc.add_chunk(chunk({"tool_calls": [{"index": 0, **raw}]}, "tool_calls")), "")
        with self.assertRaises(ModelOutputError):
            acc.finish()


class ToolInputTests(unittest.TestCase):
    def test_deeply_nested_arguments_are_a_model_output_error(self):
        raw = call()
        depth = 20000
        raw["function"]["arguments"] = '{"command":' + '[' * depth + '0' + ']' * depth + '}'
        with self.assertRaises(ModelOutputError) as caught:
            parse_tool_call(raw)
        self.assertIsInstance(caught.exception.__cause__, RecursionError)
        self.assertEqual(str(caught.exception), "invalid exec arguments")

    def test_malformed_tool_json_keeps_parser_details_out_of_diagnostic(self):
        raw = call()
        raw["function"]["arguments"] = '{"command":"' + "x" * 10000
        with self.assertRaises(ModelOutputError) as caught:
            parse_tool_call(raw)
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
        self.assertEqual(str(caught.exception), "invalid exec arguments")

    def test_tool_strings_reject_surrogates_but_accept_valid_escaped_unicode(self):
        invalid = [call(arguments={"command": "\ud800"}),
                   call(arguments={"command": "pwd", "workdir": "/\udfff"}),
                   call(call_id="\ud800")]
        literal = call()
        literal["function"]["arguments"] = '{"command":"\ud800"}'
        invalid.append(literal)
        for raw in invalid:
            with self.subTest(raw=repr(raw)), self.assertRaises(ModelOutputError):
                parse_tool_call(raw)
        valid = call(arguments={"command": "echo 中文😀", "workdir": "/路径😀"}, call_id="工具😀")
        parsed = parse_tool_call(valid)
        self.assertEqual(parsed.arguments, {"command": "echo 中文😀", "workdir": "/路径😀"})
        self.assertEqual(parsed.id, "工具😀")
        self.assertEqual(parsed.name, "exec")

    def test_read_size_is_required_and_offset_is_optional(self):
        minimal = parse_tool_call(call("read_stdout", {"exec_id": 3, "nbytes": 1}))
        self.assertEqual(minimal.arguments, {"exec_id": 3, "nbytes": 1})
        with self.assertRaises(ModelOutputError):
            parse_tool_call(call("read_stdout", {"exec_id": 3}))
        for name in ("read_stdout", "read_stderr"):
            for offset, nbytes in ((0, 1), (UINT64_MAX, UINT64_MAX)):
                arguments = {"exec_id": UINT64_MAX, "offset": offset, "nbytes": nbytes}
                with self.subTest(name=name, offset=offset, nbytes=nbytes):
                    self.assertEqual(parse_tool_call(call(name, arguments)).arguments, arguments)

    def test_exec_attachment_selection_deduplicates_preserving_order(self):
        raw = call(arguments={"command": "read selected files", "object_ids": [9, 3, 9, UINT64_MAX, 3]})
        parsed = parse_tool_call(raw)
        self.assertEqual(parsed.arguments["object_ids"], [9, 3, UINT64_MAX])
        self.assertEqual(parsed.arguments["command"], "read selected files")
        boundary = parse_tool_call(call(arguments={"command": "read files", "object_ids": list(range(1, 1025)) + [1]}))
        self.assertEqual(len(boundary.arguments["object_ids"]), 1024)

    def test_exec_attachment_selection_rejects_invalid_ids_and_excessive_unique_ids(self):
        invalid = [None, "1", {}, [True], [0], [-1], [1.0], ["1"], [None], [UINT64_MAX + 1],
                   list(range(1, 1026))]
        for ids in invalid:
            with self.subTest(ids=ids), self.assertRaises(ModelOutputError):
                parse_tool_call(call(arguments={"command": "read files", "object_ids": ids}))
        with self.assertRaises(ModelOutputError):
            parse_tool_call(call("read_stdout", {"exec_id": 1, "nbytes": 1, "object_ids": [1]}))

    def test_rejects_boolean_out_of_range_null_and_unknown_arguments(self):
        bad = [("exec", {"command": "pwd", "timeout_ms": value}) for value in (0, True, None, UINT64_MAX + 1)]
        bad += [("exec", {"command": "pwd", "workdir": value}) for value in ("relative", "", None)]
        bad += [("read_stdout", {"exec_id": value, "nbytes": 1}) for value in (0, True, "1", UINT64_MAX + 1)]
        for name in ("read_stdout", "read_stderr"):
            bad += [(name, {"exec_id": 1, "nbytes": 1, "offset": value})
                    for value in (-1, True, None, UINT64_MAX + 1)]
            bad += [(name, {"exec_id": 1, "nbytes": value})
                    for value in (0, False, None, UINT64_MAX + 1)]
        bad += [("read_stdout", {"exec_id": 1, "nbytes": 1, "cursor": 2})]
        for name, args in bad:
            with self.subTest(name=name, args=args), self.assertRaises(ModelOutputError):
                parse_tool_call(call(name, args))
        malformed = call()
        malformed["function"]["name"] = []
        with self.assertRaises(ModelOutputError):
            parse_tool_call(malformed)

    def test_tools_schema_does_not_share_mutable_state(self):
        first = model_tools()
        first[0]["function"]["parameters"]["properties"]["timeout_ms"]["minimum"] = 2
        self.assertEqual(model_tools()[0]["function"]["parameters"]["properties"]["timeout_ms"]["minimum"], 1)
        self.assertNotIn('"maximum"', json.dumps(model_tools()))
        self.assertEqual([item["function"]["name"] for item in model_tools()], ["exec", "read_stdout", "read_stderr"])
        for tool in model_tools()[1:]:
            self.assertEqual(tool["function"]["parameters"]["required"], ["exec_id", "nbytes"])
            properties = tool["function"]["parameters"]["properties"]
            for name, minimum in (("offset", 0), ("nbytes", 1)):
                self.assertEqual(properties[name]["minimum"], minimum)


class ToolOutputTests(unittest.TestCase):
    def test_cmd_exit_codes_and_nonexit_states(self):
        success = command_result_event(exec_id=7, cmd_status="SUCCESS", exit_code=0, output_limit=10,
                                       stdout_bytes=0, stderr_bytes=0)
        self.assertEqual((success["status"], success["exit_code"]), ("ok", 0))
        self.assertEqual((success["stdout_bytes"], success["stderr_bytes"]), (0, 0))
        self.assertNotIn("command", success)
        failed = command_result_event(exec_id=7, cmd_status="FAILED", exit_code=12, output_limit=10,
                                      stdout_bytes=0, stderr_bytes=0, error_message="details")
        self.assertEqual(failed["status"], "error")
        self.assertNotIn("command", failed)
        self.assertIn("12", failed["error_message"])
        self.assertIn("details", failed["error_message"])
        for status, code in (("TIMEOUT", "timeout"), ("KILLED", "cancelled"), ("START_FAILED", None),
                             ("RECOVERY_FAILED", None)):
            with self.subTest(status=status):
                result = command_result_event(exec_id=7, cmd_status=status, output_limit=10,
                                              stdout_bytes=0, stderr_bytes=0)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result.get("error_code"), code)
                self.assertEqual(result["exec_id"], 7)
                self.assertNotIn("exit_code", result)
                self.assertNotIn("command", result)
        recovery = command_result_event(exec_id=7, cmd_status="RECOVERY_FAILED", output_limit=10,
                                        stdout_bytes=0, stderr_bytes=0)
        self.assertNotIn("did not execute", recovery["error_message"])

    def test_output_quota_is_per_stream_and_partial_utf8_is_base64(self):
        result = command_result_event(exec_id=7, cmd_status="SUCCESS", exit_code=0, output_limit=2,
                                      stdout_bytes=3, stderr_bytes=4, stdout="中".encode(), stderr=b"abcd")
        self.assertEqual((result["stdout_bytes"], result["stderr_bytes"]), (3, 4))
        self.assertEqual(result["stdout"], {"encoding": "base64", "content": base64.b64encode("中".encode()[:2]).decode(),
                                           "bytes": 2, "truncated": True})
        self.assertEqual(result["stderr"], {"encoding": "utf-8", "content": "ab", "bytes": 2, "truncated": True})
        self.assertEqual(encode_output(b""), {"encoding": "utf-8", "content": "", "bytes": 0, "truncated": False})
        self.assertEqual(encode_output(b"\xff")["encoding"], "base64")
        self.assertEqual(encode_output("中".encode()[:1] + "中".encode()[1:])["content"], "中")

    def test_range_echo_actual_bytes_eof_and_truncation_are_independent(self):
        result = output_read_event(stream="stdout", exec_id=7, offset=20, nbytes=100, data=b"abc", eof=False, truncated=True)
        self.assertEqual((result["offset"], result["nbytes"], result["stdout"]["bytes"]), (20, 100, 3))
        self.assertFalse(result["eof"])
        end = output_read_event(stream="stderr", exec_id=7, offset=20, nbytes=100, data=b"abc", eof=True, truncated=True)
        self.assertTrue(end["eof"])
        self.assertTrue(end["stderr"]["truncated"])
        full_range = output_read_event(stream="stdout", exec_id=7, offset=20, nbytes=3, data=b"abc", eof=False)
        self.assertFalse(full_range["stdout"]["truncated"])

    def test_read_failure_never_pretends_to_be_empty_eof(self):
        result = output_read_event(stream="stdout", exec_id=7, offset=20, nbytes=100, eof=True, error_message="missing")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("eof", result)
        self.assertNotIn("stdout", result)

    def test_timeout_and_presend_cancel_have_no_invented_exec_id(self):
        for code in ("timeout", "cancelled"):
            result = tool_error_result(parse_tool_call(call()), error_code=code, error_message="not completed")
            self.assertEqual(result["error_code"], code)
            self.assertNotIn("exec_id", result)
            self.assertNotIn("exit_code", result)
            self.assertNotIn("command", result)
        read = tool_error_result(parse_tool_call(call("read_stdout", {"exec_id": 8, "nbytes": 4096})), error_code="timeout",
                                 error_message="read timed out")
        self.assertEqual((read["exec_id"], read["offset"], read["nbytes"]), (8, 0, 4096))


class MessageTests(unittest.TestCase):
    def test_attachment_and_completed_reply_use_only_model_fields(self):
        attachment = {"name": "a.txt", "type": "text/plain", "nbytes": 1, "object_id": 99}
        message = user("", attachments=[attachment], reply_context=[
            {"role": "assistant", "status": "completed", "text": "finished", "attachments": [attachment]}
        ])
        event = json.loads(message["content"])["events"][0]
        self.assertEqual(event["text"], "")
        self.assertEqual(event["attachments"][0]["object_id"], 99)
        with self.assertRaises(ValueError):
            user(attachments=[{**attachment, "token": "secret"}])

    def test_reply_context_requires_completed_status(self):
        reference = {"role": "assistant", "text": "final content", "attachments": []}
        message = user(reply_context=[{**reference, "status": "completed"}])
        refs = json.loads(message["content"])["events"][0]["reply_context"]
        self.assertEqual(refs[0]["status"], "completed")
        for status in ("streaming", "cancelled"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "invalid reply_context"):
                user(reply_context=[{**reference, "status": status}])

    def test_cancel_records_explicit_answer_and_original_inputs(self):
        event = user_cancelled_event("answer", [7, 8], 1791504000123)
        self.assertEqual(event, {"type": "user_cancelled", "time": "2026-10-09T00:00:00.123Z",
                                 "target_answer_id": "answer", "target_input_ids": [7, 8]})
        for invalid in ([], [1, 1], [True], [0]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                user_cancelled_event("answer", invalid, 0)

    def test_event_history_needs_no_native_call_result_pairing(self):
        assistant = parse_assistant_message(response(content="working", calls=[call()], reason="tool_calls"))
        events = [user_event(7, 1000, "start", []), assistant_event("answer", 2000, assistant),
                  tool_call_event("answer", "task", 2000, assistant.tool_calls[0]),
                  user_event(8, 3000, "another question", [])]
        result = build_model_messages(system_prompt="full prompt \n", summary="older facts", events=events)
        self.assertEqual([message["role"] for message in result], ["system", "user"])
        self.assertEqual(result[0]["content"], "full prompt \n")
        context = json.loads(result[1]["content"])
        self.assertEqual(context, {"summary": "older facts", "events": events})
        self.assertEqual(context["events"][2]["arguments"], {"command": "pwd"})
        self.assertNotIn("call_1", result[1]["content"])
        completed = tool_result_event("task", 4000, {"type": "exec_result", "status": "error",
                                                    "error_message": "stopped"})
        self.assertEqual(completed["task_id"], "task")

    def test_tool_only_response_has_no_empty_assistant_event(self):
        assistant = parse_assistant_message(response(content=None, calls=[call()], reason="tool_calls"))
        self.assertIsNone(assistant_event("answer", 0, assistant))
        both = parse_assistant_message(response(content="body", refusal="declined"))
        event = assistant_event("answer", 0, both)
        self.assertEqual((event["text"], event["refusal"]), ("body", "declined"))

    def test_user_attachment_metadata_is_not_copied_to_later_messages(self):
        first = user_event(7, 0, "read", [{"name": "file", "type": "text/plain", "nbytes": 1, "object_id": 9}])
        second = user_event(8, 1000, "hello", [])
        context = json.loads(build_model_messages(system_prompt="", events=[first, second])[1]["content"])
        self.assertEqual(context["summary"], None)
        self.assertEqual(context["events"][1]["attachments"], [])
        self.assertEqual(context["events"][0]["input_id"], 7)

    def test_timestamp_preserves_utc_ms_and_full_protocol_range(self):
        self.assertEqual(isoformat_ms(0), "1970-01-01T00:00:00.000Z")
        self.assertEqual(isoformat_ms(951782400001), "2000-02-29T00:00:00.001Z")
        self.assertEqual(isoformat_ms(UINT64_MAX), "584556019-04-03T14:25:51.615Z")

    def test_summary_accepts_only_nonblank_plain_content_within_target(self):
        from chat_model_agent.budget import summary_size
        text = '中文\n"quoted"'
        assistant = parse_assistant_message(response(content=text))
        self.assertEqual(parse_summary(assistant, summary_size(text)), text)
        with self.assertRaises(ModelOutputError):
            parse_summary(assistant, summary_size(text) - 1)
        for assistant in (parse_assistant_message(response(content=" \n")),
                          parse_assistant_message(response(content="summary", refusal="no")),
                          parse_assistant_message(response(content="summary", calls=[call()], reason="tool_calls"))):
            with self.assertRaises(ModelOutputError):
                parse_summary(assistant, 10000)

    def test_json_encoding_is_canonical_and_rejects_non_json_numbers(self):
        self.assertEqual(canonical_json({"中": "é", "a": [2, 1]}), '{"a":[2,1],"中":"é"}')
        with self.assertRaises(ValueError):
            canonical_json({"bad": float("nan")})


if __name__ == "__main__":
    unittest.main()
