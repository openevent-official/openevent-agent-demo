from __future__ import annotations
from .common import _json_obj, _stable_json
from .model import ApplyError, ChannelIds, PROTOCOL_MODEL, PROTOCOL_CMD, PROTOCOL_WAL
from .state import load_state, save_state

PROTOCOLS = {"model": PROTOCOL_MODEL, "cmd": PROTOCOL_CMD, "wal": PROTOCOL_WAL}

def _description(spec, kind):
    description = {"metadata": {"deployment_id": spec.deployment_id}}
    if kind == "wal":
        description["version"] = "v5"
    return description

def _belongs_to_deployment(channel, spec):
    description = _json_obj(channel.description) or {}
    metadata = description.get("metadata")
    return isinstance(metadata, dict) and metadata.get("deployment_id") == spec.deployment_id

def _valid(channel, protocol, members):
    return (
        channel.protocol == protocol
        and channel.visibility == 2
        and set(channel.members) == members
    )

def _resolve_channels(spec, runtime, tokens, previous):
    state = load_state(spec.paths.state_path, spec=spec) or previous or {}
    bound = state.get("channels")
    if bound is not None and (not isinstance(bound, dict) or set(bound) != set(PROTOCOLS)):
        raise ApplyError("incomplete permanent Channel binding")
    visible = None
    result = {}
    principal, token = spec.principals["agent"], tokens["agent"]
    for kind, protocol in PROTOCOLS.items():
        members = {principal}
        if kind != "wal":
            members.add(spec.principals["model_proxy" if kind == "model" else "cmd_worker"])
        explicit = spec.channels.get(kind)
        remembered = bound.get(kind) if bound else None
        if remembered and explicit and remembered != explicit:
            raise ApplyError(f"{kind} Channel is permanently bound to {remembered}")
        channel_id = remembered or explicit
        if channel_id:
            channel = runtime.get_channel(principal, token, channel_id)
            if channel is None or not _valid(channel, protocol, members):
                raise ApplyError(f"bound/explicit {kind} Channel is missing or incompatible")
        else:
            if visible is None:
                visible = runtime.list_channels(principal, token)
            candidates = [
                c for c in visible
                if _valid(c, protocol, members)
                and _belongs_to_deployment(c, spec)
            ]
            if len(candidates) > 1:
                raise ApplyError(f"multiple {kind} Channels match this deployment")
            if candidates:
                channel = candidates[0]
            else:
                channel = runtime.create_channel(
                    principal, token, name=kind, visibility=2,
                    protocol=protocol, description=_stable_json(_description(spec, kind)), members=members,
                )
        result[kind] = channel.channel_id
    state["channels"] = result
    save_state(spec, state)
    return ChannelIds(**result)
