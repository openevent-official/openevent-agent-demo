"""Source-ordered model events rebuilt from Chat, Model, Cmd and WAL facts."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading

from .budget import event_size
from .prompt import (append_reply_reference, assistant_event, tool_call_event, tool_result_event,
                     user_cancelled_event, user_event)
from .state import HistoryError
from .tool_runner import ReadProgress


def position(cursor):
    return (-1, -1) if cursor is None else (cursor["seq"], cursor["index"])


def cursor(seq, index=0):
    return {"seq": seq, "index": index}


@dataclass(frozen=True)
class HistoryEvent:
    cursor: dict
    ref: dict
    _body: object = field(compare=False, repr=False)
    trigger: bool = False
    targets: tuple = ()

    @property
    def event(self):
        # A snapshot owns positions, not a second copy of all pending bodies.
        return self._body() if callable(self._body) else self._body


@dataclass(frozen=True)
class Snapshot:
    """Uncovered history; all_events also includes candidates past processed."""

    read: int
    processed: int
    events: tuple
    all_events: tuple
    consumed: dict | None
    summary: str | None

    @property
    def watermarks(self):
        return {"read_through_seq": self.read, "processed_through_seq": self.processed}


@dataclass(frozen=True)
class _Reference:
    cursor: dict
    ref: dict
    trigger: bool = False
    targets: tuple = ()
    answer: str | None = None


@dataclass
class _Projection:
    """Disposable indexes; source facts and frozen requests remain in the WAL."""

    through: int = -1
    consumed: dict | None = None
    summary: str | None = None
    covered: tuple = (-1, -1)
    summary_accepted: str | None = None
    events: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    read_progress: dict = field(default_factory=dict)
    answers: dict = field(default_factory=dict)


class ConversationIndex:
    def __init__(self, conversation):
        self.session = conversation
        self.engine, self.journal, self.cid = conversation.engine, conversation.journal, conversation.cid
        self.users = {}
        self.rendered = {}
        self.tool_counts = {}
        self.covered = position(None)
        self.summary_accepted = None
        self.cache_lock = threading.RLock()
        self.projection_lock = threading.RLock()
        self.projection = _Projection()

    def user(self, seq, max_bytes=None):
        """Return the complete user event, or None once its admission limit is exceeded."""
        with self.cache_lock:
            cached = self.users.get(seq)
        if cached is not None:
            return cached if max_bytes is None or event_size(cached) <= max_bytes else None
        # The ordered reader has already confirmed this single and its reply
        # targets. The Chat SDK owns reconstruction of their final content.
        read = lambda terminal: self.engine.rpc.sdk(
            "Chat content", lambda: self.session.chat.get_turn_content(terminal))
        turn = read(seq)
        text = lambda value: "".join(part.text for part in value.content)
        attachments = lambda value: self.session._attachments(
            item.object_key for item in value.attachments)
        event = user_event(seq, self.engine.event_header(self.cid, seq).ts_ms,
                           text(turn), attachments(turn))
        targets = turn.reply_to_seqs or ()
        if targets:
            event["reply_context"] = []
        remaining = max_bytes
        if remaining is not None:
            # Reserve the array commas now. event_size(ref) includes one comma.
            remaining -= event_size(event) + max(0, len(targets) - 1)
            if remaining < 0:
                return None
        for target_seq in targets:
            target = read(target_seq)
            ref = {
                "role": "assistant" if target.turn_ref.principal == self.session.cfg.agent.principal else "user",
                "status": target.state,
                "text": text(target),
                "attachments": attachments(target),
            }
            if target.error is not None:
                ref["error"] = target.error
            if remaining is not None:
                remaining -= event_size(ref) - 1
                if remaining < 0:
                    return None
            append_reply_reference(event, ref)
        with self.cache_lock:
            if (position(cursor(seq)) > self.covered
                    and self.journal.get(f"chat:{self.cid}:input:{seq}:reject") is None):
                self.users[seq] = event
        return event

    def reject_user(self, seq):
        """Drop admission's rendered body; immutable Chat facts remain readable."""
        with self.cache_lock:
            self.users.pop(seq, None)

    def _retain_tool_count(self, accepted):
        attempt = self.journal.get(accepted.data["attempt"])
        # Cold history may be rendered before the scheduler discovers answers.
        # Keep those counts until discovery and then only for live executions or
        # uncovered bodies; do not replay old model streams just to count tools.
        return (attempt.data["purpose"] == "answer" and (
            position(cursor(accepted.data["terminal_seq"])) > self.covered
            or self.journal.get(attempt.data["owner"]).seq > self.session.answer_cursor
            or attempt.data["owner"] in self.session.children
            or attempt.data["owner"] in self.session.pending_answers))

    def cache_tool_count(self, accepted, count):
        with self.cache_lock:
            if self._retain_tool_count(accepted):
                self.tool_counts[accepted.key] = count

    def tool_count(self, accepted):
        with self.cache_lock:
            count = self.tool_counts.get(accepted.key)
        if count is None:
            count = len(self.session._accepted_assistant(accepted).tool_calls)
        return count

    def retain_assistant(self, accepted):
        """Called under cache_lock; frozen source reconstruction remains available."""
        attempt = self.journal.get(accepted.data["attempt"])
        if attempt.data["purpose"] == "compaction":
            return accepted.key == self.summary_accepted
        return position(cursor(accepted.data["terminal_seq"])) > self.covered

    def _prune_bodies(self, covered, summary_accepted):
        with self.cache_lock:
            # An older fixed snapshot may be rebuilt after a newer one. It must
            # not repopulate bodies already covered by the newer checkpoint.
            if covered < self.covered:
                return
            self.covered, self.summary_accepted = covered, summary_accepted
            self.users = {seq: event for seq, event in self.users.items()
                          if position(cursor(seq)) > covered}
            self.rendered = {key: value for key, value in self.rendered.items()
                             if value[0] > covered}
            self.session.assistants = {key: assistant for key, assistant in self.session.assistants.items()
                                       if self.retain_assistant(self.journal.get(key))}
            self.tool_counts = {key: count for key, count in self.tool_counts.items()
                                if self._retain_tool_count(self.journal.get(key))}

    def owner(self, accepted):
        attempt = self.journal.get(accepted.data["attempt"])
        return self.journal.get(attempt.data["owner"])

    def render(self, ref):
        kind = ref["type"]
        if kind == "user":
            return self.user(ref["seq"])
        key = kind, ref["record"]
        with self.cache_lock:
            cached = self.rendered.get(key)
        if cached is not None:
            return cached[1]
        record = self.journal.get(ref["record"])
        if record is None:
            raise HistoryError("model event source is missing")
        event, at = self._render_record(kind, record)
        with self.cache_lock:
            if at > self.covered:
                self.rendered[key] = at, event
        return event

    def _render_record(self, kind, record):
        if kind == "assistant":
            owner = self.owner(record)
            raw = self.engine.messages[record.data["terminal_seq"]]
            return (assistant_event(owner.data["turn_id"], int(raw.ts_ms), self.session._accepted_assistant(record)),
                    position(cursor(record.data["terminal_seq"])))
        if kind == "tool_call":
            from .prompt import ToolCall
            data = self.journal.materialize(record).data
            owner = self.journal.get(data["answer"])
            attempt_key = data["tool_key"].rsplit(":tool:", 1)[0]
            accepted = self.journal.get(attempt_key + ":accept")
            raw = self.engine.messages[accepted.data["terminal_seq"]]
            call = ToolCall(data["call_id"], data["name"], data["arguments"])
            return (tool_call_event(owner.data["turn_id"], data["task_id"], int(raw.ts_ms), call),
                    position(cursor(accepted.data["terminal_seq"], int(data["tool_key"].rsplit(":", 1)[1]) + 1)))
        if kind == "tool_result":
            prepared = self.journal.get(record.data["tool_key"] + ":prepare")
            at = self._tool_result_cursor(record)
            source = self.engine.messages[at["seq"]]
            return (tool_result_event(prepared.data["task_id"], int(source.ts_ms), self.session.tools.result(record)),
                    position(at))
        if kind == "cancel":
            owner = self.journal.get(record.key.removesuffix(":cancel"))
            raw = self.engine.messages[record.data["cancel_seq"]]
            return (user_cancelled_event(owner.data["turn_id"], owner.data["reply_to_seqs"], int(raw.ts_ms)),
                    position(cursor(raw.seq)))
        raise HistoryError("unsupported model event reference")

    def _tool_result_cursor(self, record):
        seq = self.session.tools.source_seq(record)
        index = (int(record.data["tool_key"].rsplit(":", 1)[1]) + 1
                 if record.data["outcome"] in {"cancelled", "read_cancelled"} else 0)
        return cursor(seq, index)

    def summary(self, key):
        if key is None:
            return None, None
        commit = self.journal.get(key)
        attempt = self.journal.get(commit.data["attempt"])
        prepared = self.journal.get(attempt.data["owner"])
        accepted = self.journal.get(attempt.key + ":accept")
        return self.session._accepted_assistant(accepted).content, prepared.data["range"]["through"]

    def _known(self, key, read):
        record = self.journal.get(key)
        return record if record is not None and record.seq <= read else None

    def _advance(self, state, read):
        # Inputs below state.through never re-enter the incremental projection.
        # Only rejections observed in this batch can exclude its new inputs.
        rejected = set()

        def add(at, ref, trigger=False, targets=(), answer=None):
            if position(at) > state.covered:
                state.events[(ref["type"], ref.get("record", ref.get("seq")))] = _Reference(
                    at, ref, trigger, targets, answer)

        # The reader applies each event and advances its watermark under one lock.
        # Facts above this fixed bound may arrive concurrently, so every lookup
        # (including cancellation/verdict lookups) must use the same bound.
        for record in self.journal.select(channel=self.cid, after=state.through, through=read):
            data = record.data
            if record.kind == "input.reject":
                rejected.add(data["input_seq"])
                state.events.pop(("user", data["input_seq"]), None)
                self.reject_user(data["input_seq"])
            elif record.kind == "answer.prepare":
                state.consumed = data["input_through"]
                state.answers[record.key] = record
            elif record.kind == "attempt.prepare":
                state.attempts[record.key] = record
            elif record.kind == "attempt.fail":
                state.attempts.pop(record.key.removesuffix(":fail"), None)
            elif record.kind == "attempt.accept":
                attempt = self._known(data["attempt"], read)
                if attempt.data["purpose"] == "answer":
                    add(cursor(data["terminal_seq"]), {"type": "assistant", "record": record.key})
            elif record.kind == "tool.prepare":
                state.tools[data["tool_key"]] = record
                accepted = self._known(data["tool_key"].rsplit(":tool:", 1)[0] + ":accept", read)
                index = int(data["tool_key"].rsplit(":", 1)[1]) + 1
                add(cursor(accepted.data["terminal_seq"], index), {"type": "tool_call", "record": record.key})
            elif record.kind == "tool.done":
                state.tools.pop(data["tool_key"], None)
                state.read_progress.pop(data["tool_key"], None)
                prepared = self._known(data["tool_key"] + ":prepare", read)
                owner = self._known(prepared.data["answer"], read)
                add(self._tool_result_cursor(record), {"type": "tool_result", "record": record.key},
                    data["outcome"] not in {"cancelled", "read_cancelled"},
                    tuple(owner.data["reply_to_seqs"]), owner.key)
            elif record.kind == "answer.cancel":
                state.answers.pop(record.key.removesuffix(":cancel"), None)
                add(cursor(data["cancel_seq"]), {"type": "cancel", "record": record.key})
            elif record.kind == "answer.freeze":
                # A local request failure can freeze before creating any Chat
                # turn, so there will never be a turn terminal to retire it.
                state.answers.pop(record.key.removesuffix(":freeze"), None)
            elif record.kind == "compaction.commit":
                attempt = self._known(data["attempt"], read)
                prepared = self._known(attempt.data["owner"], read)
                state.summary, state.summary_accepted = record.key, attempt.key + ":accept"
                state.covered = position(prepared.data["range"]["through"])
                state.events = {key: item for key, item in state.events.items()
                                if position(item.cursor) > state.covered}

        ready, _ = self.engine.pending_inputs(self.cid, after=state.through)
        for seq, terminal in ready:
            if (terminal <= read and seq not in rejected
                    and self.engine.parsed[seq].payload["kind"] == "turn.single"):
                add(cursor(seq), {"type": "user", "seq": seq}, True, (seq,))
        state.through = read

    def _barriers(self, state, read):
        barriers = []
        for key, attempt in list(state.attempts.items()):
            accepted = self._known(key + ":accept", read)
            if accepted is not None:
                if attempt.data["purpose"] == "answer" and any(
                        self._known(key + f":tool:{i}:prepare", read) is None
                        for i in range(self.tool_count(accepted))):
                    barriers.append(accepted.data["terminal_seq"])
                else:
                    del state.attempts[key]
                continue
            request = self._known(key + ":request:commit", read)
            if request is not None:
                view = self.session._observed_model(request.data["seq"], attempt.data["stream_id"], read)
                cancel = self._known(attempt.data["owner"] + ":cancel", read)
                timeout = self._known(key + ":timeout", read)
                stop = min([read + 1, *([cancel.data["cancel_seq"]] if cancel else []),
                            *([timeout.seq] if timeout else [])])
                if view.terminal is not None and view.terminal.seq < stop:
                    barriers.append(view.terminal.seq)
                elif timeout is not None and timeout.seq == stop:
                    barriers.append(timeout.seq)
                elif cancel is not None:
                    del state.attempts[key]
        # Summary pruning affects event references, never unresolved work. A
        # summarized tool call can still reserve a later, unconfirmed result.
        for key, prepared in state.tools.items():
            progress = None
            if prepared.data["name"] != "exec":
                progress = state.read_progress.get(key)
                if progress is None:
                    progress = state.read_progress[key] = ReadProgress()
            source = self.session.tools.pending_source(prepared, through=read, progress=progress)
            if source is not None and source <= read:
                barriers.append(source)
        for key, answer in list(state.answers.items()):
            turn = self.session._turn(answer)
            if turn is None or turn.terminal_seq is None or turn.terminal_seq > read:
                continue
            if turn.cancelled_by == self.session.cfg.agent.user_principal:
                barriers.append(turn.terminal_seq)
            else:
                del state.answers[key]
        return barriers

    def snapshot(self, *, admitted_through=None):
        with self.projection_lock:
            read = self.engine.scanned_through
            state = self.projection
            self._advance(state, read)
            self._prune_bodies(state.covered, state.summary_accepted)
            barriers = self._barriers(state, read)
            if admitted_through is not None:
                barriers.extend(item.cursor["seq"] for item in state.events.values()
                                if item.ref["type"] == "user" and item.cursor["seq"] > admitted_through)
            consumed, summary = state.consumed, state.summary
            refs = []
            for item in state.events.values():
                cancel = self._known(item.answer + ":cancel", read) if item.answer is not None else None
                trigger = item.trigger and not (cancel and cancel.data["cancel_seq"] < item.cursor["seq"])
                refs.append((item, trigger))
        # Freeze ordering before reading any payload. Bodies are materialized by
        # the budget/consumer, so a large queue is not copied into every snapshot.
        items = []
        for item, trigger in refs:
            # Empty tool-only assistant outputs have no independent event.
            if item.ref["type"] == "assistant" and self.render(item.ref) is None:
                continue
            items.append(HistoryEvent(item.cursor, item.ref,
                lambda ref=item.ref: self.render(ref), trigger, item.targets))
        processed = min([read, *(seq - 1 for seq in barriers)])
        all_events = tuple(sorted(items, key=lambda item: position(item.cursor)))
        items = tuple(item for item in all_events if item.cursor["seq"] <= processed)
        return Snapshot(read, processed, items, all_events, consumed, summary)

    def object_ids(self, prepared, selected):
        """Check only selected attachments against their frozen original sources."""
        through = position(prepared.data["input_through"])[0]
        read = prepared.data["snapshot"]["read_through_seq"]
        result = set()
        for ident in selected:
            for seq in self.engine.input_object_sources(self.cid, ident, through):
                rejected = self._known(f"chat:{self.cid}:input:{seq}:reject", read)
                if rejected is None:
                    result.add(ident)
                    break
        return frozenset(result)
