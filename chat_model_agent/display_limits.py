"""Check encoded publications against deployment and transport limits."""

from openevent.chat_sdk import serialize_payload
from openevent.sdk.proto import openevent_pb2


# OpenEventClient's public transport contract, not the deployment's payload cap.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
UINT64_MAX = (1 << 64) - 1


def display_fits(config, channel_id, kind, turn_id, pre_seq, text):
    """Allow whole Chat fragments only when their encoded publication fits."""
    payload = serialize_payload({"kind": kind, "turn_id": turn_id, "pre_seq": pre_seq,
                                 "content": [{"type": "text", "text": text}]})
    return publication_fits(config, channel_id, payload)


def publication_fits(config, channel_id, payload, *, object_keys=()):
    """Check the actual payload, attachments and transport envelopes.

    UUID and committed positions are assigned after preparation. Reserve their
    full uint64 encoding widths without allocating an ID or publishing a probe.
    The payload cap comes from deployment configuration; it counts encoded
    payload bytes, independently of attachments and transport framing.
    """
    return publication_size_fits(config, channel_id, len(payload), object_keys=object_keys)


def publication_payload_limit(config, channel_id, *, maximum=None, object_keys=()) -> int:
    """Find the payload allowance without allocating a candidate body.

    Zero also represents an envelope that cannot fit even an empty payload.
    """
    low, high = 0, min(config.openevent.max_payload_bytes, MAX_MESSAGE_BYTES)
    if maximum is not None:
        high = min(high, maximum)
    object_keys = tuple(object_keys)
    while low < high:
        middle = (low + high + 1) // 2
        if publication_size_fits(config, channel_id, middle, object_keys=object_keys):
            low = middle
        else:
            high = middle - 1
    return low


def publication_size_fits(config, channel_id, payload_size, *, object_keys=()):
    """Check an encoded length without allocating a candidate payload.

    One placeholder byte retains the protobuf payload tag. Adjust its length
    varint and the enclosing Fetch message's length varint as the body grows.
    """
    if payload_size > config.openevent.max_payload_bytes:
        return False
    payload = b"x" if payload_size else b""
    request = openevent_pb2.PublishAutoSeqRequest(
        principal=config.agent.principal, token=config.agent.token,
        channel_id=channel_id, uuid=UINT64_MAX, payload=payload, object_keys=object_keys)
    # The committed event must also fit a one-message Fetch response.
    event = openevent_pb2.EventMessage(
        seq=UINT64_MAX, principal=config.agent.principal, channel_id=channel_id,
        uuid=UINT64_MAX, ts_ms=UINT64_MAX, payload=payload, object_keys=object_keys)
    response = openevent_pb2.FetchResponse(
        messages=[event], next_seq=UINT64_MAX, last_seq=UINT64_MAX)
    width = lambda value: max(1, (value.bit_length() + 6) // 7)
    growth = payload_size - 1 + width(payload_size) - 1 if payload_size else 0
    fetch_growth = growth + width(event.ByteSize() + growth) - width(event.ByteSize())
    return max(request.ByteSize() + growth, response.ByteSize() + fetch_growth) <= MAX_MESSAGE_BYTES
