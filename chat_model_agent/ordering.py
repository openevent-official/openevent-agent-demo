"""Read business facts from one confirmed prefix of the shared event stream."""
from __future__ import annotations

from .state import HistoryError


class OrderedFacts:
    """Bound indexed facts without copying history or performing source RPCs."""

    def __init__(self, engine, cid, read=None):
        self.engine, self.cid = engine, cid
        with engine.condition:
            self.read = engine.scanned_through if read is None else min(read, engine.scanned_through)

    def record(self, key):
        with self.engine.condition:
            record = self.engine.journal.get(key)
            return record if record is not None and record.seq <= self.read else None

    def cancellation_seq(self, answer):
        if answer is None or "turn_id" not in answer.data:
            return None
        with self.engine.condition:
            turn = self.engine.current_turn(self.cid, self.engine.config.agent.principal,
                                            answer.data["turn_id"])
            if (turn is not None and turn.status == "cancelled"
                    and turn.cancelled_by == self.engine.config.agent.user_principal
                    and turn.terminal_seq <= self.read):
                return turn.terminal_seq
        return None

    def attempt_boundary(self, attempt, answer):
        """Only sources preceding the first cancellation/timeout can decide."""
        with self.engine.condition:
            cancellation = self.cancellation_seq(answer)
            timeout = self.record(attempt.key + ":timeout")
            stops = []
            if cancellation is not None:
                stops.append((cancellation, "cancel"))
            if timeout is not None:
                stops.append((timeout.seq, "timeout"))
            if not stops:
                return self.read, None, None
            seq, kind = min(stops)
            return min(self.read, seq - 1), kind, seq

    def final_failure(self, answer):
        """Find an exhausted failure by its source, before any winning cancel."""
        if answer is None or answer.kind != "answer.prepare":
            return None
        with self.engine.condition:
            cancellation = self.cancellation_seq(answer)
            previous = None
            for number in range(answer.data["max_model_attempts"]):
                key = answer.key + f":attempt:{number}"
                attempt = self.record(key)
                if attempt is None:
                    return None
                if (attempt.data["owner"] != answer.key or attempt.data["number"] != number
                        or (previous is not None and attempt.seq <= previous.seq)):
                    raise HistoryError("model attempts do not follow their preceding failure")
                if self.record(key + ":accept") is not None:
                    return None
                failed = self.record(key + ":fail")
                if failed is None:
                    return None
                source = failed.data["source_seq"]
                if source > self.read or (cancellation is not None and cancellation <= source):
                    return None
                if not failed.data["retryable"] or number + 1 == answer.data["max_model_attempts"]:
                    return failed
                previous = failed
        return None
