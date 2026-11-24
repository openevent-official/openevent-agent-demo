"""Read-only projections of module events; SDKs own payload parsing."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from bisect import bisect_right


class HistoryError(ValueError):
    pass


@dataclass
class Turn:
    principal: int
    turn_id: str
    creation_seq: int
    ts_ms: int
    last_seq: int
    reply_to_seqs: tuple
    object_keys: list = field(default_factory=list)
    status: str = "streaming"
    terminal_seq: int | None = None
    cancelled_by: int | None = None
    content_seqs: list[int] = field(default_factory=list, repr=False)
    has_text: bool = field(default=False, repr=False)

    @property
    def has_content(self):
        return bool(self.has_text or self.object_keys)


@dataclass(frozen=True)
class TurnState:
    """Identity and writer state, without text or attachment copies."""

    principal: int
    turn_id: str
    creation_seq: int
    last_seq: int
    status: str
    terminal_seq: int | None
    cancelled_by: int | None
    content_seqs: tuple[int, ...]


class ChatProjection:
    def __init__(self):
        self.turns = {}
        self.by_creation = {}
        self.by_terminal = {}
        self.ready_turns = {}
        self.through_seq = 0

    def apply(self, event):
        if event.seq <= self.through_seq:
            return
        self.through_seq = event.seq
        data, kind = event.payload, event.payload["kind"]
        if kind == "submission.reserve":
            return
        if kind == "turn.cancel":
            ref = data["target_turn"]
            turn = self.turns.get((ref["principal"], ref["turn_id"]))
            if turn is None:
                raise HistoryError("cancel has no preceding turn")
            if turn.status == "streaming":
                if data["start_seq"] != turn.creation_seq:
                    raise HistoryError("cancel has a different creation position")
                turn.status, turn.terminal_seq, turn.cancelled_by = "cancelled", event.seq, event.principal
                turn.object_keys.extend(event.object_keys)
                turn.content_seqs.clear()
            return
        key = (event.principal, data["turn_id"])
        has_text = bool(data.get("content"))
        if kind in {"turn.single", "turn.start"}:
            if key in self.turns:
                raise HistoryError("duplicate Chat turn")
            for seq in data["reply_to_seqs"]:
                if seq not in self.by_terminal or seq >= event.seq:
                    raise HistoryError("reply target must be a preceding completed single or end")
            turn = Turn(event.principal, data["turn_id"], event.seq, event.ts_ms,
                        event.seq, tuple(data["reply_to_seqs"]), object_keys=list(event.object_keys),
                        content_seqs=[event.seq] if kind == "turn.start" else [], has_text=has_text)
            if kind == "turn.single":
                turn.status, turn.terminal_seq = "completed", event.seq
                self.by_terminal[event.seq] = turn
            self.turns[key] = turn
            self.by_creation[event.seq] = turn
            if kind == "turn.single" and turn.has_content:
                self.ready_turns.setdefault(turn.principal, []).append(turn)
            return
        turn = self.turns.get(key)
        if turn is None:
            raise HistoryError("Chat chain has no preceding creation")
        if turn.status != "streaming":
            return
        if data["pre_seq"] != turn.last_seq:
            raise HistoryError("forked Chat chain")
        if kind == "turn.end":
            if data["start_seq"] != turn.creation_seq:
                raise HistoryError("end has a different creation position")
            if "error" in data:
                turn.has_text, turn.object_keys = False, []
            elif data["content_seqs"] != turn.content_seqs:
                raise HistoryError("end content positions differ from the effective chain")
            turn.status, turn.terminal_seq = "completed", event.seq
            turn.content_seqs.clear()
            self.by_terminal[event.seq] = turn
            if turn.has_content:
                self.ready_turns.setdefault(turn.principal, []).append(turn)
        else:
            turn.last_seq = event.seq
            if kind == "turn.reset":
                turn.has_text, turn.object_keys = has_text, list(event.object_keys)
                turn.content_seqs = [event.seq]
            else:
                turn.has_text |= has_text
                turn.object_keys.extend(event.object_keys)
                turn.content_seqs.append(event.seq)

    def ready(self, principal, *, after=0):
        turns = self.ready_turns.get(principal, [])
        start = bisect_right(turns, after, key=lambda turn: turn.terminal_seq)
        return turns[start:]


@dataclass
class ModelView:
    request_seq: int
    stream_id: str
    chain_seq: int = 0
    head: bool = False
    terminal: object | None = None
    pending_chunks: deque = field(default_factory=deque)

    def apply(self, event, agent_principal, model_principal):
        payload = event.payload
        if payload.stream_id != self.stream_id or self.terminal is not None:
            return
        kind = payload.kind
        if kind == "infer.request":
            return
        if kind == "infer.cancel":
            if event.principal in {agent_principal, model_principal} and payload.request_seq == self.request_seq:
                self.terminal = event
            return
        if event.principal != model_principal:
            raise HistoryError("unexpected model response principal")
        if kind == "infer.result":
            if payload.prev_seq != self.request_seq:
                return
            if self.head:
                return
            self.head, self.chain_seq = True, event.seq
            if payload.has_body:
                self.terminal = event
        elif payload.request_seq == self.request_seq:
            if kind == "infer.append":
                if not self.head or payload.prev_seq != self.chain_seq:
                    raise HistoryError("broken model stream chain")
                self.chain_seq = event.seq
                self.pending_chunks.append(event)
            elif kind == "infer.end":
                self.terminal = event
