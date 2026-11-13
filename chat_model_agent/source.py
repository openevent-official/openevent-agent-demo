"""Small source identities; historical payloads stay in OpenEvent."""
from dataclasses import dataclass


@dataclass(frozen=True)
class EventHeader:
    seq: int
    channel_id: int
    principal: int
    uuid: int
    ts_ms: int

    @classmethod
    def from_message(cls, message):
        return cls(*(int(getattr(message, name)) for name in
                     ("seq", "channel_id", "principal", "uuid", "ts_ms")))


class PayloadHeader(dict):
    """The identity/status subset used by matching and wait predicates."""

    @property
    def has_body(self):
        return "body" in self

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def to_dict(self):
        return dict(self)


@dataclass(frozen=True)
class ParsedHeader:
    seq: int
    principal: int
    ts_ms: int
    payload: PayloadHeader


def parsed_header(message, parsed, module):
    if module == "chat":
        payload = {key: value for key, value in parsed.payload.items()
                   if key in {"kind", "turn_id", "reply_to_seqs", "pre_seq", "target_turn"}}
    elif module == "model":
        model = parsed.payload
        payload = {"kind": model.kind, "stream_id": model.stream_id}
        for key in ("request_seq", "prev_seq"):
            value = getattr(model, key, None)
            if value is not None:
                payload[key] = value
        # ModelView needs presence, not the terminal body, to decide readiness.
        if (model.kind in {"infer.request", "infer.append"}
                or model.kind in {"infer.result", "infer.end"} and model.has_body):
            payload["body"] = None
    else:
        fields = ("kind", "prev_seq", "status", "bytes", "target_seq", "stream",
                  "offset", "nbytes", "timeout_ms", "exit_code", "ts_ms",
                  "content_encoding", "eof")
        payload = {key: getattr(parsed.payload, key) for key in fields
                   if hasattr(parsed.payload, key)}
    return ParsedHeader(int(message.seq), int(message.principal), int(message.ts_ms),
                        PayloadHeader(payload))
