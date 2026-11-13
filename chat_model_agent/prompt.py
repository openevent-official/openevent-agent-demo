"""Pure Chat Agent model messages, completion parsing, and tool results."""
from __future__ import annotations

import base64
import copy
import json
from dataclasses import dataclass
from typing import Any

from .errors import FailureCode, ModelOutputError

UINT64_MAX = (1 << 64) - 1
MAX_TOOL_OBJECT_IDS = 1024
MODEL_IO_VERSION = 3


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AssistantMessage:
    content: str | None
    refusal: str | None
    tool_calls: tuple[ToolCall, ...]

    @property
    def display_text(self) -> str | None:
        return self.refusal or self.content


def canonical_json(value: Any) -> str:
    """Canonical encoding for model context content and request-budget accounting."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _require_utf8(value: str, name: str, *, code=FailureCode.INVALID_OUTPUT) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ModelOutputError(f"{name} must contain valid Unicode", code=code) from exc


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if (not isinstance(value, int) or isinstance(value, bool) or value < minimum
            or (maximum is not None and value > maximum)):
        raise ValueError(f"{name} must be an integer in the allowed range")
    return value


def isoformat_ms(value: int) -> str:
    """UTC milliseconds, including the full protocol uint64 timestamp range."""
    _integer(value, "ts_ms", maximum=UINT64_MAX)
    seconds, milliseconds = divmod(value, 1000)
    days, seconds = divmod(seconds, 86400)
    hour, seconds = divmod(seconds, 3600)
    minute, second = divmod(seconds, 60)
    # Gregorian civil date from days since 1970; datetime stops at year 9999.
    days += 719468
    era, day_of_era = divmod(days, 146097)
    year_of_era = (day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    month_index = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * month_index + 2) // 5 + 1
    month = month_index + (3 if month_index < 10 else -9)
    year += month <= 2
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}:{second:02d}.{milliseconds:03d}Z"


MAX_EVENT_TIME = isoformat_ms(UINT64_MAX)


def _attachments(value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError("attachments must be an array")
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "type", "nbytes", "object_id"}:
            raise ValueError("attachment must contain name, type, nbytes, and object_id only")
        if not isinstance(item["name"], str) or not isinstance(item["type"], str):
            raise ValueError("attachment name and type must be strings")
        _integer(item["nbytes"], "attachment.nbytes")
        _integer(item["object_id"], "attachment.object_id", minimum=1, maximum=UINT64_MAX)


def user_event(input_id, ts_ms, text, attachments, reply_context=None):
    _integer(input_id, "input_id", minimum=1, maximum=UINT64_MAX)
    if not isinstance(text, str):
        raise ValueError("user text must be a string")
    _attachments(attachments)
    event = {"type": "user", "time": isoformat_ms(ts_ms), "input_id": input_id,
             "text": text, "attachments": copy.deepcopy(attachments)}
    if reply_context is not None:
        if not isinstance(reply_context, list) or not reply_context:
            raise ValueError("reply_context must be a nonempty array")
        for ref in reply_context:
            if not isinstance(ref, dict) or set(ref) != {"role", "status", "text", "attachments"}:
                raise ValueError("invalid reply_context fields")
            if (ref["role"] not in {"user", "assistant"}
                    or ref["status"] != "completed"
                    or not isinstance(ref["text"], str)):
                raise ValueError("invalid reply_context")
            _attachments(ref["attachments"])
        event["reply_context"] = copy.deepcopy(reply_context)
    return event


def assistant_event(answer_id, ts_ms, assistant):
    if not assistant.content and not assistant.refusal:
        return None
    event = {"type": "assistant", "time": isoformat_ms(ts_ms), "answer_id": answer_id,
             "text": assistant.content or ""}
    if assistant.refusal:
        event["refusal"] = assistant.refusal
    return event


def tool_call_event(answer_id, task_id, ts_ms, call):
    return {"type": "tool_call", "time": isoformat_ms(ts_ms), "answer_id": answer_id,
            "task_id": task_id, "name": call.name, "arguments": copy.deepcopy(call.arguments)}


def tool_result_event(task_id, ts_ms, result):
    return {"type": "tool_result", "time": isoformat_ms(ts_ms), "task_id": task_id,
            "result": copy.deepcopy(result)}


def user_cancelled_event(target_answer_id, target_input_ids, ts_ms):
    targets = list(target_input_ids)
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("cancelled input IDs must be nonempty and unique")
    for ident in targets:
        _integer(ident, "target_input_id", minimum=1, maximum=UINT64_MAX)
    return {"type": "user_cancelled", "time": isoformat_ms(ts_ms),
            "target_answer_id": target_answer_id, "target_input_ids": targets}


def user_message(events, summary=None, *, allow_empty=False):
    if not events and not allow_empty:
        raise ValueError("model events must not be empty")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("summary must be text or null")
    return {"role": "user", "content": canonical_json({
        "summary": summary, "events": list(events)})}


def build_model_messages(*, system_prompt, summary=None, events, allow_empty=False):
    return [{"role": "system", "content": system_prompt},
            user_message(events, summary, allow_empty=allow_empty)]


def parse_summary(assistant, target):
    """Validate a completed response against its frozen encoded summary target."""
    from .budget import summary_size
    target = target.target if hasattr(target, "target") else target
    if assistant.tool_calls or assistant.refusal or not assistant.content or not assistant.content.strip():
        raise ModelOutputError("history compression returned invalid summary")
    if summary_size(assistant.content) > target:
        raise ModelOutputError("history compression summary exceeds the input budget",
                               code=FailureCode.EVENT_TOO_LARGE)
    return assistant.content


def model_tools() -> list[dict[str, Any]]:
    integer_id = {"type": "integer", "minimum": 1}
    tools = [{"type": "function", "function": {
        "name": "exec",
        "description": "通过 Cmd 执行非交互式 shell 命令，实际结果随后作为 tool_result 事件提供。默认使用当前会话工作目录。不要用来发送 Chat 消息，也不要请求 stdin、交互程序或后台常驻任务。",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {
            "command": {"type": "string", "minLength": 1, "description": "完整命令行，不含解释文字。"},
            "workdir": {"type": "string", "minLength": 1, "description": "可选的绝对工作目录。省略时使用会话工作目录。"},
            "timeout_ms": {**integer_id, "description": "可选命令超时，单位毫秒。"},
            "object_ids": {"type": "array", "items": dict(integer_id), "default": [],
                           "description": "只列出本次命令必须访问、且在当前输入中出现的附件 ID。最多 1024 个不同 ID；重复 ID 按首次出现顺序去重。省略或空数组表示不携带附件。"},
        }, "required": ["command"]},
    }}]
    for stream in ("stdout", "stderr"):
        tools.append({"type": "function", "function": {
            "name": f"read_{stream}",
            "description": f"读取已有 exec_result 对应的 {stream} 字节范围，实际结果随后作为 tool_result 事件提供。exec_id 必须取自该结果；继续读取请显式推进 offset，相同参数重复调用仍读取相同范围。",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {
                "exec_id": dict(integer_id),
                "offset": {"type": "integer", "minimum": 0, "default": 0, "description": "起始字节位置，省略时从头读取。"},
                "nbytes": {**integer_id, "description": "必填的原始字节数；成功时精确读取本值与输出额度中较小的字节数，所读范围必须存在。"},
            }, "required": ["exec_id", "nbytes"]},
        }})
    return tools


def parse_tool_call(raw: Any) -> ToolCall:
    if not isinstance(raw, dict) or raw.get("type") != "function":
        raise ModelOutputError("tool call must be a function")
    call_id, function = raw.get("id"), raw.get("function")
    if not isinstance(call_id, str) or not call_id or not isinstance(function, dict):
        raise ModelOutputError("invalid tool ID or function")
    name, arguments = function.get("name"), function.get("arguments")
    if not isinstance(name, str) or name not in {"exec", "read_stdout", "read_stderr"} or not isinstance(arguments, str):
        raise ModelOutputError("unknown tool or non-string arguments", code=FailureCode.INVALID_TOOL_ARGUMENTS)
    _require_utf8(call_id, "tool ID")
    _require_utf8(arguments, "tool arguments", code=FailureCode.INVALID_TOOL_ARGUMENTS)
    try:
        values = json.loads(arguments)
        if not isinstance(values, dict):
            raise ValueError("arguments must be an object")
        if name == "exec":
            if set(values) - {"command", "workdir", "timeout_ms", "object_ids"}:
                raise ValueError("unknown exec argument")
            if not isinstance(values.get("command"), str) or not values["command"]:
                raise ValueError("command must be a nonempty string")
            if "workdir" in values and (not isinstance(values["workdir"], str) or not values["workdir"].startswith("/")):
                raise ValueError("workdir must be an absolute path")
            _require_utf8(values["command"], "command")
            if "workdir" in values:
                _require_utf8(values["workdir"], "workdir")
            if "timeout_ms" in values:
                _integer(values["timeout_ms"], "timeout_ms", minimum=1, maximum=UINT64_MAX)
            if "object_ids" in values:
                if not isinstance(values["object_ids"], list):
                    raise ValueError("object_ids must be an array")
                for ident in values["object_ids"]:
                    _integer(ident, "object_ids item", minimum=1, maximum=UINT64_MAX)
                values["object_ids"] = list(dict.fromkeys(values["object_ids"]))
                if len(values["object_ids"]) > MAX_TOOL_OBJECT_IDS:
                    raise ValueError(f"object_ids must contain at most {MAX_TOOL_OBJECT_IDS} different IDs")
        else:
            if set(values) - {"exec_id", "offset", "nbytes"}:
                raise ValueError("unknown output read argument")
            _integer(values.get("exec_id"), "exec_id", minimum=1, maximum=UINT64_MAX)
            if "offset" in values:
                _integer(values["offset"], "offset", maximum=UINT64_MAX)
            _integer(values.get("nbytes"), "nbytes", minimum=1, maximum=UINT64_MAX)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ModelOutputError(f"invalid {name} arguments", code=FailureCode.INVALID_TOOL_ARGUMENTS) from exc
    return ToolCall(call_id, name, values)


def _assistant(content: Any, refusal: Any, raw_calls: Any, finish_reason: Any) -> AssistantMessage:
    if (content is not None and not isinstance(content, str)) or (refusal is not None and not isinstance(refusal, str)):
        raise ModelOutputError("content and refusal must be strings or null")
    for name, value in (("content", content), ("refusal", refusal)):
        if value is not None:
            _require_utf8(value, name)
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list):
        raise ModelOutputError("tool_calls must be an array")
    calls = tuple(parse_tool_call(raw) for raw in raw_calls)
    if len({call.id for call in calls}) != len(calls):
        raise ModelOutputError("duplicate tool call IDs")
    if refusal and calls:
        raise ModelOutputError("refusal conflicts with tool calls")
    if finish_reason != ("tool_calls" if calls else "stop"):
        raise ModelOutputError("invalid finish_reason")
    if not content and not refusal and not calls:
        raise ModelOutputError("empty assistant response")
    return AssistantMessage(content or None, refusal or None, calls)


def _choice(body: Any, *, allow_empty: bool) -> dict[str, Any] | None:
    if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
        raise ModelOutputError("response must contain a choices array")
    choices = body["choices"]
    if not choices and allow_empty:
        return None
    if len(choices) != 1 or not isinstance(choices[0], dict):
        raise ModelOutputError("expected exactly one choice")
    choice = choices[0]
    if type(choice.get("index")) is not int or choice["index"] != 0:
        raise ModelOutputError("only choice index 0 is supported")
    return choice


def parse_assistant_message(body: Any) -> AssistantMessage:
    choice = _choice(body, allow_empty=False)
    assert choice is not None
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ModelOutputError("expected assistant message")
    return _assistant(message.get("content"), message.get("refusal"), message.get("tool_calls"),
                      choice.get("finish_reason"))


class CompletionAccumulator:
    """One provider attempt; the caller confirms llm.v1 success before finish()."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._refusal: list[str] = []
        self._calls: dict[int, dict[str, list[str]]] = {}
        self._finish_reason: str | None = None

    def add_chunk(self, body: Any) -> str:
        choice = _choice(body, allow_empty=True)
        if choice is None:
            return ""
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ModelOutputError("chunk delta must be an object")
        if "role" in delta and delta["role"] != "assistant":
            raise ModelOutputError("unexpected delta role")
        content, refusal = delta.get("content"), delta.get("refusal")
        for name, value in (("content", content), ("refusal", refusal)):
            if value is not None and not isinstance(value, str):
                raise ModelOutputError("content/refusal delta must be a string or null")
            if value is not None:
                _require_utf8(value, name)
        calls = delta.get("tool_calls", [])
        if not isinstance(calls, list):
            raise ModelOutputError("tool_calls delta must be an array")
        if self._finish_reason is not None and (content or refusal or calls):
            raise ModelOutputError("content after finish_reason")
        self._content.append(content or "")
        self._refusal.append(refusal or "")
        for part in calls:
            if not isinstance(part, dict):
                raise ModelOutputError("invalid tool delta")
            index = part.get("index")
            if type(index) is not int or index < 0:
                raise ModelOutputError("tool index must be a nonnegative integer")
            if "type" in part and part["type"] != "function":
                raise ModelOutputError("tool delta type must be function")
            function = part.get("function", {})
            if not isinstance(function, dict):
                raise ModelOutputError("tool function delta must be an object")
            target = self._calls.setdefault(index, {"id": [], "name": [], "arguments": []})
            for source, key in ((part, "id"), (function, "name"), (function, "arguments")):
                if key in source:
                    code = FailureCode.INVALID_TOOL_ARGUMENTS if key == "arguments" else FailureCode.INVALID_OUTPUT
                    if not isinstance(source[key], str):
                        raise ModelOutputError(f"tool {key} delta must be a string", code=code)
                    _require_utf8(source[key], f"tool {key}", code=code)
                    target[key].append(source[key])
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str) or not reason:
                raise ModelOutputError("invalid finish_reason")
            if self._finish_reason is not None and reason != self._finish_reason:
                raise ModelOutputError("conflicting finish reasons")
            self._finish_reason = reason
        return content or ""

    def finish(self, body: Any = None) -> AssistantMessage:
        if body is not None:
            return parse_assistant_message(body)
        indexes = sorted(self._calls)
        if indexes != list(range(len(indexes))):
            raise ModelOutputError("tool indexes must be contiguous from zero")
        calls = [{"id": "".join(self._calls[index]["id"]), "type": "function", "function": {
            "name": "".join(self._calls[index]["name"]),
            "arguments": "".join(self._calls[index]["arguments"]),
        }} for index in indexes]
        return _assistant("".join(self._content), "".join(self._refusal), calls, self._finish_reason)


def encode_output(data: bytes, *, truncated: bool = False) -> dict[str, Any]:
    if not isinstance(data, bytes) or not isinstance(truncated, bool):
        raise ValueError("output requires bytes and a boolean truncated flag")
    try:
        content, encoding = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(data).decode("ascii"), "base64"
    return {"encoding": encoding, "content": content, "bytes": len(data), "truncated": truncated}


def command_result_event(*, exec_id: int, cmd_status: str, output_limit: int,
                         stdout_bytes: int, stderr_bytes: int,
                         exit_code: int | None = None, stdout: bytes | None = None,
                         stderr: bytes | None = None, error_message: str | None = None) -> dict[str, Any]:
    _integer(exec_id, "exec_id", minimum=1, maximum=UINT64_MAX)
    _integer(output_limit, "output_limit", minimum=1)
    _integer(stdout_bytes, "stdout_bytes")
    _integer(stderr_bytes, "stderr_bytes")
    errors = {
        "TIMEOUT": ("timeout", "Cmd reported execution timeout"),
        "KILLED": ("cancelled", "Cmd reported command termination"),
        "START_FAILED": (None, "command could not start"),
        "RECOVERY_FAILED": (None, "Cmd could not recover the execution"),
    }
    result: dict[str, Any] = {"type": "exec_result", "exec_id": exec_id,
                              "status": "ok" if cmd_status == "SUCCESS" else "error",
                              "stdout_bytes": stdout_bytes, "stderr_bytes": stderr_bytes}
    if cmd_status in {"SUCCESS", "FAILED"}:
        if type(exit_code) is not int or (exit_code == 0) != (cmd_status == "SUCCESS"):
            raise ValueError("SUCCESS/FAILED requires its real matching integer exit_code")
        result["exit_code"] = exit_code
        if cmd_status == "FAILED":
            result["error_message"] = f"command exited with code {exit_code}" + (f": {error_message}" if error_message else "")
    elif cmd_status in errors:
        if exit_code is not None:
            raise ValueError("this Cmd result must not have an exit_code")
        code, description = errors[cmd_status]
        if code:
            result["error_code"] = code
        result["error_message"] = description + (f": {error_message}" if error_message else "")
    else:
        raise ValueError(f"unsupported Cmd status: {cmd_status}")
    for stream, data in (("stdout", stdout), ("stderr", stderr)):
        if data is not None:
            result[stream] = encode_output(data[:output_limit], truncated=len(data) > output_limit)
    return result


def output_read_event(*, stream: str, exec_id: int, offset: int, nbytes: int,
                      data: bytes | None = None, eof: bool | None = None, truncated: bool = False,
                      error_code: str | None = None, error_message: str | None = None) -> dict[str, Any]:
    if stream not in {"stdout", "stderr"}:
        raise ValueError("invalid output stream")
    _integer(exec_id, "exec_id", minimum=1, maximum=UINT64_MAX)
    _integer(offset, "offset")
    _integer(nbytes, "nbytes", minimum=1)
    failed = error_message is not None
    result: dict[str, Any] = {"type": f"read_{stream}_result", "exec_id": exec_id,
                              "offset": offset, "nbytes": nbytes, "status": "error" if failed else "ok"}
    if failed:
        if not error_message:
            raise ValueError("error_message must be nonempty")
        result["error_message"] = error_message
        if error_code:
            result["error_code"] = error_code
    else:
        if not isinstance(data, bytes) or not isinstance(eof, bool) or len(data) > nbytes or error_code:
            raise ValueError("successful read requires bytes within nbytes, boolean eof, and no error_code")
        result[stream] = encode_output(data, truncated=truncated)
        result["eof"] = eof
    return result


def tool_error_result(call: ToolCall, *, error_code: str, error_message: str) -> dict[str, Any]:
    """Worker-decided timeout or pre-dispatch cancellation; never invent an exec_id."""
    if error_code not in {"timeout", "cancelled"} or not error_message:
        raise ValueError("tool error requires timeout/cancelled and an explanation")
    if call.name == "exec":
        return {"type": "exec_result", "status": "error",
                "error_code": error_code, "error_message": error_message}
    if call.name not in {"read_stdout", "read_stderr"}:
        raise ValueError("unknown tool")
    return output_read_event(stream=call.name.removeprefix("read_"), exec_id=call.arguments["exec_id"],
                             offset=call.arguments.get("offset", 0),
                             nbytes=call.arguments["nbytes"],
                             error_code=error_code, error_message=error_message)
