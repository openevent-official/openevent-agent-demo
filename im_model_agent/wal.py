from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any


KIND_PREPARE = "llm.request.prepare"
KIND_CMD_TIMEOUT = "cmd.request.timeout"
PROTOCOL = "agent.wal.v1"
MODEL_REQUEST_RE = re.compile(
    r"^agent:(?P<session_id>.+):wal:(?P<wal_seq>[1-9][0-9]*):retry:(?P<retry>[1-9][0-9]*)$"
)


class WalError(ValueError):
    pass


@dataclass(frozen=True)
class InputRef:
    type: str
    seq: int


@dataclass(frozen=True)
class WalPrepare:
    prepare_id: str
    ts_ms: int
    pre_llm_seq: int
    user_message_seqs: tuple[int, ...]
    input_event_refs: tuple[InputRef, ...]


@dataclass(frozen=True)
class CmdTimeout:
    timeout_id: str
    ts_ms: int
    cmd_request_id: str
    cmd_request_seq: int
    tool_call_id: str
    tool_name: str


def now_ms() -> int:
    return int(time.time() * 1000)


def encode_prepare(
    *,
    pre_llm_seq: int,
    user_message_seqs: list[int] | tuple[int, ...] = (),
    input_event_refs: list[InputRef] | tuple[InputRef, ...] = (),
    prepare_id: str | None = None,
    ts_ms: int | None = None,
) -> bytes:
    users = _seqs(user_message_seqs, "user_message_seqs")
    refs = _refs(input_event_refs)
    if not users and not refs:
        raise WalError("prepare must reference at least one input")
    item = WalPrepare(
        prepare_id=prepare_id or f"prepare:{uuid.uuid4().hex}",
        ts_ms=ts_ms or now_ms(),
        pre_llm_seq=_non_negative_int(pre_llm_seq, "pre_llm_seq"),
        user_message_seqs=users,
        input_event_refs=refs,
    )
    return _dump(
        {
            "kind": KIND_PREPARE,
            "prepare_id": item.prepare_id,
            "ts_ms": item.ts_ms,
            "pre_llm_seq": item.pre_llm_seq,
            "user_message_seqs": list(item.user_message_seqs),
            "input_event_refs": [{"type": ref.type, "seq": ref.seq} for ref in item.input_event_refs],
        }
    )


def encode_cmd_timeout(
    *,
    cmd_request_id: str,
    cmd_request_seq: int,
    tool_call_id: str,
    tool_name: str,
    ts_ms: int | None = None,
) -> bytes:
    request_id = _string(cmd_request_id, "cmd_request_id")
    payload = {
        "kind": KIND_CMD_TIMEOUT,
        "timeout_id": f"cmd-timeout:{request_id}",
        "ts_ms": ts_ms or now_ms(),
        "cmd_request_id": request_id,
        "cmd_request_seq": _positive_int(cmd_request_seq, "cmd_request_seq"),
        "tool_call_id": _string(tool_call_id, "tool_call_id"),
        "tool_name": _tool_name(tool_name),
    }
    return _dump(payload)


def parse_wal(payload: bytes) -> WalPrepare | CmdTimeout:
    data = _load(payload)
    kind = data.get("kind")
    if kind == KIND_PREPARE:
        _exact_fields(data, {"kind", "prepare_id", "ts_ms", "pre_llm_seq", "user_message_seqs", "input_event_refs"})
        users = _seqs(data.get("user_message_seqs"), "user_message_seqs")
        refs = _refs(data.get("input_event_refs"))
        if not users and not refs:
            raise WalError("prepare must reference at least one input")
        return WalPrepare(
            prepare_id=_string(data.get("prepare_id"), "prepare_id"),
            ts_ms=_positive_int(data.get("ts_ms"), "ts_ms"),
            pre_llm_seq=_non_negative_int(data.get("pre_llm_seq"), "pre_llm_seq"),
            user_message_seqs=users,
            input_event_refs=refs,
        )
    if kind == KIND_CMD_TIMEOUT:
        _exact_fields(data, {"kind", "timeout_id", "ts_ms", "cmd_request_id", "cmd_request_seq", "tool_call_id", "tool_name"})
        request_id = _string(data.get("cmd_request_id"), "cmd_request_id")
        if data.get("timeout_id") != f"cmd-timeout:{request_id}":
            raise WalError("timeout_id does not match cmd_request_id")
        return CmdTimeout(
            timeout_id=data["timeout_id"],
            ts_ms=_positive_int(data.get("ts_ms"), "ts_ms"),
            cmd_request_id=request_id,
            cmd_request_seq=_positive_int(data.get("cmd_request_seq"), "cmd_request_seq"),
            tool_call_id=_string(data.get("tool_call_id"), "tool_call_id"),
            tool_name=_tool_name(data.get("tool_name")),
        )
    raise WalError("unknown WAL kind")


def parse_prepare(payload: bytes) -> WalPrepare:
    parsed = parse_wal(payload)
    if not isinstance(parsed, WalPrepare):
        raise WalError("WAL payload is not llm.request.prepare")
    return parsed


def model_request_id(session_id: str, wal_seq: int, retry_index: int) -> str:
    return f"agent:{_string(session_id, 'session_id')}:wal:{_positive_int(wal_seq, 'wal_seq')}:retry:{_positive_int(retry_index, 'retry_index')}"


def parse_model_request_id(request_id: str) -> tuple[str, int, int] | None:
    if not isinstance(request_id, str):
        return None
    match = MODEL_REQUEST_RE.fullmatch(request_id)
    if match is None:
        return None
    return match.group("session_id"), int(match.group("wal_seq")), int(match.group("retry"))


def _dump(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _load(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WalError("WAL payload must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise WalError("WAL payload must be a JSON object")
    return value


def _exact_fields(data: dict[str, Any], expected: set[str]) -> None:
    if set(data) != expected:
        raise WalError(f"WAL fields must be exactly {sorted(expected)}")


def _refs(value: Any) -> tuple[InputRef, ...]:
    if not isinstance(value, (list, tuple)):
        raise WalError("input_event_refs must be an array")
    result: list[InputRef] = []
    for raw in value:
        if isinstance(raw, InputRef):
            ref = raw
        elif isinstance(raw, dict) and set(raw) == {"type", "seq"}:
            ref = InputRef(type=raw.get("type"), seq=raw.get("seq"))
        else:
            raise WalError("invalid input_event_refs item")
        if ref.type not in {"exec_result", "read_stdout_result", "read_stderr_result", "cmd_timeout"}:
            raise WalError("invalid input_event_refs type")
        result.append(InputRef(ref.type, _positive_int(ref.seq, "input_event_refs[].seq")))
    seqs = tuple(ref.seq for ref in result)
    if seqs != tuple(sorted(seqs)) or len(set(seqs)) != len(seqs):
        raise WalError("input_event_refs must be strictly increasing by seq")
    return tuple(result)


def _seqs(value: Any, name: str) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise WalError(f"{name} must be an array")
    result = tuple(_positive_int(item, f"{name}[]") for item in value)
    if result != tuple(sorted(result)) or len(set(result)) != len(result):
        raise WalError(f"{name} must be strictly increasing")
    return result


def _tool_name(value: Any) -> str:
    value = _string(value, "tool_name")
    if value not in {"exec", "read_stdout", "read_stderr"}:
        raise WalError("invalid tool_name")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise WalError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WalError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WalError(f"{name} must be a non-negative integer")
    return value
