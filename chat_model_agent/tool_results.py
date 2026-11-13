"""Pure tool-result formatting shared by budget checks and tool execution."""

from .prompt import UINT64_MAX, output_read_event, tool_error_result


RESULT_TOO_LARGE = "Tool result is too large to provide. Request a smaller range."
TOOL_TIMEOUT = "waiting for the tool result timed out"
TOOL_CANCELLED = "tool was not dispatched because the user cancelled"
READ_CANCELLED = ("output reading stopped after completed chunks because the user cancelled; "
                  "the requested range was not fully read")


def oversized_result(result):
    """Keep known facts, never claim that refusing display changed execution."""
    fields = {"type", "exec_id", "exit_code", "stdout_bytes", "stderr_bytes", "offset", "nbytes", "eof"}
    return {**{name: value for name, value in result.items() if name in fields},
            "status": "error", "error_code": "result_too_large", "error_message": RESULT_TOO_LARGE}


def future_error_results(call):
    """All local errors with actual arguments and maximum future identifier widths."""
    result = [tool_error_result(call, error_code="timeout", error_message=TOOL_TIMEOUT),
              tool_error_result(call, error_code="cancelled", error_message=TOOL_CANCELLED)]
    if call.name == "exec":
        base = {"type": "exec_result", "exec_id": UINT64_MAX, "exit_code": -(1 << 63),
                "stdout_bytes": UINT64_MAX, "stderr_bytes": UINT64_MAX}
    else:
        base = {"type": call.name + "_result", "exec_id": call.arguments["exec_id"],
                "offset": call.arguments.get("offset", 0), "nbytes": call.arguments["nbytes"], "eof": False}
        result.append(output_read_event(stream=call.name.removeprefix("read_"),
            exec_id=base["exec_id"], offset=base["offset"], nbytes=base["nbytes"],
            error_code="cancelled", error_message=READ_CANCELLED))
    return [*result, oversized_result(base)]
