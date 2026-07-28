from __future__ import annotations

import time
from typing import Any

from .common import _json_obj, _stable_json, _unique_ints
from .model import (
    ApplyError,
    ChannelIds,
    DesiredSpec,
    PROTOCOL_CMD,
    PROTOCOL_IM,
    PROTOCOL_MODEL,
    PROTOCOL_WAL,
    SessionSpec,
    VISIBILITY_VALUES,
    _required_principal_refs,
)
from .openevent import OpenEventRuntime


def _resolve_channels(
    spec: DesiredSpec,
    runtime: OpenEventRuntime,
    tokens: dict[str, str],
    im_provider_session_ids: dict[str, str],
    bindings: dict[str, ChannelIds],
    actions: list[dict[str, Any]],
) -> dict[str, ChannelIds]:
    agent_bot_principal = spec.agent_principal
    agent_bot_token = tokens[spec.agent_principal_ref]
    visible_channels = runtime.list_channels(agent_bot_principal, agent_bot_token)
    principal_tokens = _principal_tokens(spec, tokens)
    result: dict[str, ChannelIds] = {}
    visibility = VISIBILITY_VALUES[spec.channel_visibility]
    for session in spec.sessions:
        if not session.enabled:
            continue
        binding = bindings.get(session.session_id)
        provider_session_id = im_provider_session_ids[session.session_id]
        im_id = _resolve_channel(
            spec=spec,
            runtime=runtime,
            visible_channels=visible_channels,
            operator_principal=agent_bot_principal,
            operator_token=agent_bot_token,
            principal_tokens=principal_tokens,
            bound_id=binding.im if binding else None,
            explicit_id=session.im_channel_id,
            name=session.im_channel_name,
            protocol=PROTOCOL_IM,
            visibility=visibility,
            description=_im_description(spec, session, provider_session_id),
            required_members=[
                session.user_principal,
                spec.agent_principal,
                spec.im_worker_principal,
            ],
            match=lambda desc: _matches_im_description(desc, spec, session, provider_session_id),
            actions=actions,
        )
        model_id = _resolve_channel(
            spec=spec,
            runtime=runtime,
            visible_channels=visible_channels,
            operator_principal=agent_bot_principal,
            operator_token=agent_bot_token,
            principal_tokens=principal_tokens,
            bound_id=binding.model if binding else None,
            explicit_id=session.model_channel_id,
            name=session.model_name,
            protocol=PROTOCOL_MODEL,
            visibility=visibility,
            description=_model_description(spec, session),
            required_members=[
                spec.agent_principal,
                spec.model_proxy_principal,
            ],
            match=lambda desc: _matches_model_description(desc, spec, session),
            actions=actions,
        )
        wal_id = _resolve_channel(
            spec=spec,
            runtime=runtime,
            visible_channels=visible_channels,
            operator_principal=agent_bot_principal,
            operator_token=agent_bot_token,
            principal_tokens=principal_tokens,
            bound_id=binding.wal if binding else None,
            explicit_id=session.wal_channel_id,
            name=session.wal_name,
            protocol=PROTOCOL_WAL,
            visibility=visibility,
            description=_wal_description(spec, session, im_id, model_id),
            required_members=[spec.agent_principal],
            match=lambda desc: _matches_wal_description(desc, spec, session, im_id, model_id),
            actions=actions,
        )
        cmd_id = _resolve_channel(
            spec=spec,
            runtime=runtime,
            visible_channels=visible_channels,
            operator_principal=agent_bot_principal,
            operator_token=agent_bot_token,
            principal_tokens=principal_tokens,
            bound_id=binding.cmd if binding else None,
            explicit_id=session.cmd_channel_id,
            name=session.cmd_name,
            protocol=PROTOCOL_CMD,
            visibility=visibility,
            description=_cmd_description(spec, session),
            required_members=[
                spec.agent_principal,
                spec.cmd_worker_principal,
            ],
            match=lambda desc: _matches_cmd_description(desc, spec, session),
            actions=actions,
        )
        result[session.session_id] = ChannelIds(im=im_id, model=model_id, wal=wal_id, cmd=cmd_id)
    return result


def _resolve_channel(
    *,
    spec: DesiredSpec,
    runtime: OpenEventRuntime,
    visible_channels: list[Any],
    operator_principal: int,
    operator_token: str,
    principal_tokens: dict[int, str],
    bound_id: int | None,
    explicit_id: int | None,
    name: str,
    protocol: str,
    visibility: int,
    description: dict[str, Any],
    required_members: list[int],
    match,
    actions: list[dict[str, Any]],
) -> int:
    if bound_id is not None:
        if explicit_id is not None and explicit_id != bound_id:
            raise ApplyError(
                f"initialized session channel {protocol} is bound to {bound_id}, not explicit id {explicit_id}"
            )
        channel = runtime.get_channel(operator_principal, operator_token, bound_id)
        if not channel or not _usable_channel(
            runtime=runtime,
            channel=channel,
            protocol=protocol,
            visibility=visibility,
            required_members=required_members,
            principal_tokens=principal_tokens,
            required_creator=operator_principal,
            match=match,
            actions=actions,
        ):
            raise ApplyError(
                f"bound {protocol} channel {bound_id} is missing, inaccessible, or incompatible with {name}"
            )
        actions.append(_channel_action("reuse_channel", "state", protocol, bound_id, name))
        return int(channel.channel_id)

    if explicit_id is not None:
        channel = runtime.get_channel(operator_principal, operator_token, explicit_id)
        if not channel or not _usable_channel(
            runtime=runtime,
            channel=channel,
            protocol=protocol,
            visibility=visibility,
            required_members=required_members,
            principal_tokens=principal_tokens,
            required_creator=operator_principal,
            match=match,
            actions=actions,
        ):
            raise ApplyError(
                f"explicit {protocol} channel {explicit_id} is missing, inaccessible, or incompatible with {name}"
            )
        actions.append(_channel_action("reuse_channel", "input", protocol, explicit_id, name))
        return int(channel.channel_id)

    for source, channel in _candidate_channels(visible_channels, name, protocol, match):
        if _usable_channel(
            runtime=runtime,
            channel=channel,
            protocol=protocol,
            visibility=visibility,
            required_members=required_members,
            principal_tokens=principal_tokens,
            required_creator=operator_principal,
            match=match,
            actions=actions,
        ):
            channel_id = int(channel.channel_id)
            actions.append(_channel_action("reuse_channel", source, protocol, channel_id, name))
            return channel_id

    channel = runtime.create_channel(
        principal=operator_principal,
        token=operator_token,
        name=name,
        visibility=visibility,
        protocol=protocol,
        description=_stable_json({**description, "updated_at_ms": int(time.time() * 1000)}),
        members=_unique_ints(required_members),
    )
    visible_channels.append(channel)
    channel_id = int(channel.channel_id)
    actions.append(_channel_action("create_channel", "openevent", protocol, channel_id, name))
    return channel_id


def _validate_channel_binding(session: SessionSpec, binding: ChannelIds) -> None:
    for kind, explicit_id, bound_id in (
        ("im", session.im_channel_id, binding.im),
        ("model", session.model_channel_id, binding.model),
        ("wal", session.wal_channel_id, binding.wal),
        ("cmd", session.cmd_channel_id, binding.cmd),
    ):
        if explicit_id is not None and explicit_id != bound_id:
            raise ApplyError(
                f"initialized session {session.session_id} {kind} channel is bound to {bound_id}, "
                f"not explicit id {explicit_id}"
            )


def _usable_channel(
    *,
    runtime: OpenEventRuntime,
    channel: Any,
    protocol: str,
    visibility: int,
    required_members: list[int],
    principal_tokens: dict[int, str],
    required_creator: int,
    match,
    actions: list[dict[str, Any]],
) -> bool:
    if str(getattr(channel, "protocol", "")) != protocol:
        return False
    if int(getattr(channel, "visibility", -1)) != visibility:
        return False
    desc = _json_obj(getattr(channel, "description", ""))
    if desc is None or not match(desc):
        return False
    creator = int(getattr(channel, "creator", 0) or 0)
    if creator != required_creator:
        return False
    members = {int(value) for value in getattr(channel, "members", [])}
    missing = [principal for principal in _unique_ints(required_members) if principal not in members]
    if not missing:
        return True
    creator_token = principal_tokens.get(creator)
    if not creator or not creator_token:
        return False
    for principal in missing:
        runtime.add_member(creator, creator_token, int(channel.channel_id), principal)
        actions.append(
            {
                "action": "add_member",
                "channel_id": int(channel.channel_id),
                "target_principal": principal,
                "operator_principal": creator,
            }
        )
    return True


def _candidate_channels(visible_channels: list[Any], name: str, protocol: str, match) -> list[tuple[str, Any]]:
    candidates: list[tuple[str, Any]] = []
    seen: set[int] = set()
    for channel in visible_channels:
        channel_id = int(getattr(channel, "channel_id", 0))
        if channel_id and getattr(channel, "name", "") == name:
            candidates.append(("name", channel))
            seen.add(channel_id)
    for channel in visible_channels:
        channel_id = int(getattr(channel, "channel_id", 0))
        if not channel_id or channel_id in seen or getattr(channel, "protocol", "") != protocol:
            continue
        desc = _json_obj(getattr(channel, "description", ""))
        if desc is not None and match(desc):
            candidates.append(("description", channel))
            seen.add(channel_id)
    return candidates


def _im_description(spec: DesiredSpec, session: SessionSpec, provider_session_id: str) -> dict[str, Any]:
    return {
        "version": "v1",
        "provider": spec.im_provider,
        "session_id": provider_session_id,
        "session_type": spec.im_session_type,
        "metadata": {
            "runtime_name": spec.runtime_name,
            "agent_session_id": session.session_id,
        },
    }


def _model_description(spec: DesiredSpec, session: SessionSpec) -> dict[str, Any]:
    return {
        "version": "v1",
        "metadata": {
            "runtime_name": spec.runtime_name,
            "agent_session_id": session.session_id,
            "model_proxy_principal": spec.model_proxy_principal,
        },
    }


def _wal_description(spec: DesiredSpec, session: SessionSpec, im_channel_id: int, model_channel_id: int) -> dict[str, Any]:
    return {
        "version": "v1",
        "session_id": session.session_id,
        "im_channel_id": im_channel_id,
        "model_channel_id": model_channel_id,
        "metadata": {"runtime_name": spec.runtime_name},
    }


def _cmd_description(spec: DesiredSpec, session: SessionSpec) -> dict[str, Any]:
    return {
        "version": "v1",
        "metadata": {
            "runtime_name": spec.runtime_name,
            "agent_session_id": session.session_id,
            "cmd_worker_principal": spec.cmd_worker_principal,
        },
    }


def _matches_im_description(
    desc: dict[str, Any],
    spec: DesiredSpec,
    session: SessionSpec,
    provider_session_id: str,
) -> bool:
    metadata = desc.get("metadata")
    return (
        desc.get("version") == "v1"
        and desc.get("provider") == spec.im_provider
        and desc.get("session_id") == provider_session_id
        and desc.get("session_type") == spec.im_session_type
        and isinstance(metadata, dict)
        and metadata.get("runtime_name") == spec.runtime_name
        and metadata.get("agent_session_id") == session.session_id
    )


def _matches_model_description(desc: dict[str, Any], spec: DesiredSpec, session: SessionSpec) -> bool:
    metadata = desc.get("metadata")
    return (
        desc.get("version") == "v1"
        and isinstance(metadata, dict)
        and metadata.get("runtime_name") == spec.runtime_name
        and metadata.get("agent_session_id") == session.session_id
        and metadata.get("model_proxy_principal") == spec.model_proxy_principal
    )


def _matches_wal_description(
    desc: dict[str, Any],
    spec: DesiredSpec,
    session: SessionSpec,
    im_channel_id: int,
    model_channel_id: int,
) -> bool:
    metadata = desc.get("metadata")
    return (
        desc.get("version") == "v1"
        and desc.get("session_id") == session.session_id
        and desc.get("im_channel_id") == im_channel_id
        and desc.get("model_channel_id") == model_channel_id
        and isinstance(metadata, dict)
        and metadata.get("runtime_name") == spec.runtime_name
    )


def _matches_cmd_description(desc: dict[str, Any], spec: DesiredSpec, session: SessionSpec) -> bool:
    metadata = desc.get("metadata")
    return (
        desc.get("version") == "v1"
        and isinstance(metadata, dict)
        and metadata.get("runtime_name") == spec.runtime_name
        and metadata.get("agent_session_id") == session.session_id
        and metadata.get("cmd_worker_principal") == spec.cmd_worker_principal
    )


def _principal_tokens(
    spec: DesiredSpec,
    tokens: dict[str, str],
) -> dict[int, str]:
    return {spec.principals[key]: tokens[key] for key in _required_principal_refs(spec)}


def _channel_action(action: str, source: str, protocol: str, channel_id: int, name: str) -> dict[str, Any]:
    return {
        "action": action,
        "source": source,
        "protocol": protocol,
        "channel_id": channel_id,
        "name": name,
    }
