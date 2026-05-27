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
    items = [
        {
            "time": isoformat_ms(message.event_ms),
            "text": message.text,
        }
        for message in sorted(messages, key=lambda item: item.seq)
    ]
    return json.dumps(items, ensure_ascii=False, indent=2, separators=(",", ": "))


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
