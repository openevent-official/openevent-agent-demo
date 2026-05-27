from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any


KIND_LLM_REQUEST_PREPARE = "llm.request.prepare"
PROTOCOL = "agent.wal.v1"
REQUEST_ID_RE = re.compile(r"^agent:(?P<session_id>[A-Za-z0-9._:-]+):wal:(?P<wal_seq>[1-9][0-9]*)$")


class WalError(ValueError):
    pass


@dataclass(frozen=True)
class WalPrepare:
    ts_ms: int
    pre_llm_seq: int
    user_message_seqs: tuple[int, ...]


def now_ms() -> int:
    return int(time.time() * 1000)


def encode_prepare(pre_llm_seq: int, user_message_seqs: list[int] | tuple[int, ...], ts_ms: int | None = None) -> bytes:
    item = WalPrepare(
        ts_ms=ts_ms or now_ms(),
        pre_llm_seq=_non_negative_int(pre_llm_seq, "pre_llm_seq"),
        user_message_seqs=_seqs(user_message_seqs),
    )
    payload = {
        "kind": KIND_LLM_REQUEST_PREPARE,
        "ts_ms": item.ts_ms,
        "pre_llm_seq": item.pre_llm_seq,
        "user_message_seqs": list(item.user_message_seqs),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_prepare(payload: bytes) -> WalPrepare:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WalError("WAL payload must be UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise WalError("WAL payload must be a JSON object")
    unknown = set(data) - {"kind", "ts_ms", "pre_llm_seq", "user_message_seqs"}
    if unknown:
        raise WalError(f"WAL payload contains unknown fields: {sorted(unknown)}")
    if data.get("kind") != KIND_LLM_REQUEST_PREPARE:
        raise WalError("WAL kind must be llm.request.prepare")
    return WalPrepare(
        ts_ms=_positive_int(data.get("ts_ms"), "ts_ms"),
        pre_llm_seq=_non_negative_int(data.get("pre_llm_seq"), "pre_llm_seq"),
        user_message_seqs=_seqs(data.get("user_message_seqs")),
    )


def model_request_id(session_id: str, wal_seq: int) -> str:
    if not session_id:
        raise WalError("session_id must be non-empty")
    return f"agent:{session_id}:wal:{_positive_int(wal_seq, 'wal_seq')}"


def parse_model_request_id(request_id: str) -> tuple[str, int] | None:
    if not isinstance(request_id, str):
        return None
    match = REQUEST_ID_RE.fullmatch(request_id)
    if match is None:
        return None
    return match.group("session_id"), int(match.group("wal_seq"))


def turn_id(session_id: str, user_message_seqs: tuple[int, ...] | list[int]) -> str:
    seqs = _seqs(user_message_seqs)
    return f"{session_id}:{seqs[0]}"


def freeze_request_id(turn: str) -> str:
    if not turn:
        raise WalError("turn_id must be non-empty")
    return f"freeze:{turn}"


def _seqs(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise WalError("user_message_seqs must be a non-empty array")
    result = tuple(_positive_int(item, "user_message_seqs[]") for item in value)
    if tuple(sorted(result)) != result or len(set(result)) != len(result):
        raise WalError("user_message_seqs must be strictly increasing")
    return result


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WalError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WalError(f"{name} must be a non-negative integer")
    return value
