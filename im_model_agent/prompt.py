from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class UserPromptMessage:
    seq: int
    event_ms: int
    text: str


@dataclass(frozen=True)
class ModelInputEvent:
    data: dict[str, Any]


def build_model_messages(
    *,
    system_prompt: str,
    previous_messages: list[dict[str, Any]],
    pending_user_messages: list[UserPromptMessage],
    max_context_messages: int,
) -> list[dict[str, Any]]:
    if previous_messages:
        messages = [dict(item) for item in previous_messages]
    else:
        messages = [{"role": "system", "content": system_prompt}]
    messages.append(
        {
            "role": "user",
            "content": user_messages_content(pending_user_messages),
        }
    )
    return trim_messages(messages, max_context_messages)


def user_messages_content(messages: list[UserPromptMessage]) -> str:
    return events_content(
        [
            {
                "type": "user",
                "time": isoformat_ms(message.event_ms),
                "text": message.text,
            }
            for message in sorted(messages, key=lambda item: item.seq)
        ]
    )


def events_content(events: list[dict[str, Any]]) -> str:
    return json.dumps({"events": events}, ensure_ascii=False, separators=(",", ":"))


def command_result_event(*, exec_id: int, command: str, status: str, stdout: Any, stderr: Any, error_message: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "exec_result",
        "command": command,
        "status": status,
        "exec_id": exec_id,
    }
    if stdout is not None:
        event["stdout"] = stdout
    if stderr is not None:
        event["stderr"] = stderr
    if error_message:
        event["error_message"] = error_message
    return event


def output_read_event(*, stream: str, exec_id: int, status: str, content: str | None, error_message: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": f"read_{stream}_result",
        "exec_id": exec_id,
        "status": status,
    }
    if status == "ok" and content is not None:
        event[stream] = content
    if error_message:
        event["error_message"] = error_message
    return event


def append_user_events(messages: list[dict[str, Any]], events: list[dict[str, Any]], max_context_messages: int) -> list[dict[str, Any]]:
    updated = [dict(item) for item in messages]
    updated.append({"role": "user", "content": events_content(events)})
    return trim_messages(updated, max_context_messages)


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "exec",
            "description": "Ask the Agent to run one non-interactive shell command in the local command environment. Use this only when you need to inspect local files, run tests, call local tools, or get command output. Do not use it to send user-visible messages; write user-visible messages in assistant content. After calling it, wait for a later exec_result input and do not pretend you have already seen the result.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The full command line to execute. It must be a non-empty string containing only the command itself, with no explanatory text. The command must be non-interactive; do not request stdin, background daemons, or programs that require manual input.",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "Optional working directory. When provided, it must be an absolute path. If omitted, the Agent uses the default working directory configured for the current session.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Optional timeout in milliseconds. When provided, it must be a positive integer and must not exceed the command timeout limit configured for the Agent.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_stdout",
            "description": "Read stdout for a previous exec_result. Call this only when that exec_result does not include a stdout field and stdout is actually needed to continue. Use the numeric exec_result.exec_id exactly as provided; do not invent an exec_id and do not pass it as a string.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "exec_id": {
                        "type": "integer",
                        "description": "The execution result seq whose stdout should be read. It must come from exec_result.exec_id in the input events.",
                    }
                },
                "required": ["exec_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_stderr",
            "description": "Read stderr for a previous exec_result. Call this only when that exec_result does not include a stderr field and stderr is actually needed to continue. Use the numeric exec_result.exec_id exactly as provided; do not invent an exec_id and do not pass it as a string.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "exec_id": {
                        "type": "integer",
                        "description": "The execution result seq whose stderr should be read. It must come from exec_result.exec_id in the input events.",
                    }
                },
                "required": ["exec_id"],
            },
        },
    },
]


def model_tools() -> list[dict[str, Any]]:
    return json.loads(json.dumps(TOOLS))


def parse_tool_call(raw: Any) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    function = raw.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        arguments = function.get("arguments")
    else:
        name = raw.get("name")
        arguments = raw.get("arguments")
    if not isinstance(name, str) or not name:
        return None
    if isinstance(arguments, str):
        try:
            parsed_arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    elif isinstance(arguments, dict):
        parsed_arguments = arguments
    elif arguments is None:
        parsed_arguments = {}
    else:
        return None
    if not isinstance(parsed_arguments, dict):
        return None
    if name == "exec":
        allowed = {"command", "workdir", "timeout_ms"}
        if set(parsed_arguments) - allowed:
            return None
        command = parsed_arguments.get("command")
        if not isinstance(command, str) or not command:
            return None
        workdir = parsed_arguments.get("workdir")
        if workdir is not None and (not isinstance(workdir, str) or not workdir.startswith("/")):
            return None
        timeout_ms = parsed_arguments.get("timeout_ms")
        if timeout_ms is not None and (not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0):
            return None
        return name, parsed_arguments
    if name in {"read_stdout", "read_stderr"}:
        if set(parsed_arguments) != {"exec_id"}:
            return None
        exec_id = parsed_arguments.get("exec_id")
        if not isinstance(exec_id, int) or isinstance(exec_id, bool) or exec_id <= 0:
            return None
        return name, parsed_arguments
    return None


def iso_events(messages: list[UserPromptMessage]) -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "time": isoformat_ms(message.event_ms),
            "text": message.text,
        }
        for message in sorted(messages, key=lambda item: item.seq)
    ]


def isoformat_ms(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("event_ms must be a positive integer")
    dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def extract_user_text(data: dict[str, Any], placeholder: str) -> str:
    if data.get("msg_type") == "text":
        for candidate in (
            data.get("text"),
            _nested(data, "content", "text"),
            _nested(data, "content_raw", "text"),
        ):
            if isinstance(candidate, str) and candidate:
                return candidate
    return placeholder


def extract_assistant_text(body: Any) -> str | None:
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                content = _nested(first, "message", "content")
                if isinstance(content, str):
                    return content
        output_text = body.get("output_text")
        if isinstance(output_text, str):
            return output_text
    if isinstance(body, str):
        return body
    return None


def extract_tool_calls(body: Any) -> list[Any]:
    if not isinstance(body, dict):
        return []
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            tool_calls = _nested(first, "message", "tool_calls")
            if isinstance(tool_calls, list):
                return tool_calls
    tool_calls = body.get("tool_calls")
    if isinstance(tool_calls, list):
        return tool_calls
    return []


def append_assistant_message(messages: list[dict[str, Any]], assistant_text: str, max_context_messages: int) -> list[dict[str, Any]]:
    updated = [dict(item) for item in messages]
    updated.append({"role": "assistant", "content": assistant_text})
    return trim_messages(updated, max_context_messages)


def trim_messages(messages: list[dict[str, Any]], max_context_messages: int) -> list[dict[str, Any]]:
    if max_context_messages <= 0 or len(messages) <= max_context_messages:
        return messages
    if messages and messages[0].get("role") == "system" and max_context_messages > 1:
        return [messages[0], *messages[-(max_context_messages - 1):]]
    return messages[-max_context_messages:]


def _nested(data: dict[str, Any], *keys: str) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current
