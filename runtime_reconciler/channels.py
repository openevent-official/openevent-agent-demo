from __future__ import annotations
import time
from .common import _json_obj, _stable_json
from .model import ApplyError, ChannelIds, PROTOCOL_MODEL, PROTOCOL_CMD, PROTOCOL_WAL
from .state import load_state, save_state

PROTOCOLS = {"model": PROTOCOL_MODEL, "cmd": PROTOCOL_CMD, "wal": PROTOCOL_WAL}

def _description(spec, kind):
    description = {"metadata": {"deployment_id": spec.deployment_id}}
    if kind == "wal":
        description.update(version="v5", updated_at_ms=int(time.time() * 1000))
    return description

def _belongs_to_deployment(channel, spec):
    description = _json_obj(getattr(channel, "description", "")) or {}
    metadata = description.get("metadata")
    return isinstance(metadata, dict) and metadata.get("deployment_id") == spec.deployment_id

def _valid(channel, spec, kind):
    members = {spec.principals["agent"]}
    if kind != "wal":
        members.add(spec.principals["model_proxy" if kind == "model" else "cmd_worker"])
    desc = None if kind == "cmd" else _json_obj(getattr(channel, "description", ""))
    return (
        str(getattr(channel, "protocol", "")) == PROTOCOLS[kind]
        and int(getattr(channel, "visibility", -1)) == 2
        and set(channel.members) == members
        and (kind == "cmd" or isinstance(desc, dict))
        and (kind != "wal" or desc.get("version") == "v5")
        and (kind != "model" or (set(desc) == {"metadata"} and isinstance(desc["metadata"], dict)))
    )

def _resolve_channels(spec, runtime, tokens, previous):
    state = load_state(spec.paths.state_path, spec=spec) or previous or {}
    bound = state.get("channels")
    if bound is not None and (not isinstance(bound, dict) or set(bound) != set(PROTOCOLS)):
        raise ApplyError("incomplete permanent Channel binding")
    visible = None
    result = {}
    principal, token = spec.principals["agent"], tokens["agent"]
    for kind in PROTOCOLS:
        explicit = spec.channels.get(kind)
        remembered = bound.get(kind) if bound else None
        if remembered and explicit and remembered != explicit:
            raise ApplyError(f"{kind} Channel is permanently bound to {remembered}")
        channel_id = remembered or explicit
        if channel_id:
            channel = runtime.get_channel(principal, token, channel_id)
            if channel is None or not _valid(channel, spec, kind):
                raise ApplyError(f"bound/explicit {kind} Channel is missing or incompatible")
        else:
            if visible is None:
                visible = runtime.list_channels(principal, token)
            candidates = [
                c for c in visible
                if _valid(c, spec, kind)
                and _belongs_to_deployment(c, spec)
            ]
            if len(candidates) > 1:
                raise ApplyError(f"multiple {kind} Channels match this deployment")
            if candidates:
                channel = candidates[0]
            else:
                members = [principal]
                if kind != "wal":
                    members.append(spec.principals["model_proxy" if kind == "model" else "cmd_worker"])
                channel = runtime.create_channel(
                    principal, token, name=f"{spec.deployment_id}.{kind}", visibility=2,
                    protocol=PROTOCOLS[kind], description=_stable_json(_description(spec, kind)), members=members,
                )
                visible.append(channel)
            channel_id = int(channel.channel_id)
            if not _valid(channel, spec, kind):
                raise ApplyError(f"created {kind} Channel violates its contract")
        result[kind] = int(channel_id)
    if len(set(result.values())) != 3:
        raise ApplyError("auxiliary Channel IDs must be distinct")
    state["channels"] = result
    save_state(spec, state)
    return ChannelIds(**result)
