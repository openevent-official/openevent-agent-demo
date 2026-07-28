from __future__ import annotations

from typing import Any

from .model import DesiredSpec, PROTOCOL_CMD, ResolvedRuntime, SpecError


def render_configs(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, dict[str, Any]]:
    return {
        "openevent": {
            "path": "openevent-server.yaml",
            "data": render_openevent_config(spec),
        },
        "im_syncer": {"path": "im-p2p-syncer.yaml", "data": _render_im_syncer(spec, resolved)},
        "model_proxy": {"path": "model-proxy.yaml", "data": _render_model_proxy(spec, resolved)},
        "cmd_worker": {"path": "cmd-worker.yaml", "data": _render_cmd_worker(spec, resolved)},
        "agent": {"path": "im-model-agent.yaml", "data": _render_agent(spec, resolved)},
    }


def _render_im_syncer(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, Any]:
    principal_tokens = []
    token_by_principal: dict[int, str] = {
        spec.im_bot_principal: resolved.tokens[spec.im_bot_principal_ref],
    }
    for session in spec.sessions:
        if session.enabled:
            token = resolved.tokens[session.user_principal_ref]
            existing = token_by_principal.get(session.user_principal)
            if existing is not None and existing != token:
                raise SpecError(f"user principal {session.user_principal} resolves to multiple tokens")
            token_by_principal[session.user_principal] = token
    for principal in sorted(token_by_principal):
        principal_tokens.append({"principal": principal, "token": token_by_principal[principal]})

    mappings = []
    for session in spec.sessions:
        if not session.enabled:
            continue
        provider_session_id = resolved.im_provider_session_ids[session.session_id]
        channel_id = resolved.channels[session.session_id].im
        mappings.append(
            {
                "provider": spec.im_provider,
                "identity_type": "user",
                "external_user_id": resolved.im_user_external_ids[session.user_principal],
                "principal": session.user_principal,
                "session_id": provider_session_id,
                "channel_id": channel_id,
            }
        )
        mappings.append(
            {
                "provider": spec.im_provider,
                "identity_type": "bot",
                "external_user_id": spec.bot_app_id,
                "principal": spec.im_bot_principal,
                "session_id": provider_session_id,
                "channel_id": channel_id,
            }
        )
    return {
        "version": "v1",
        "worker": {
            "principal": spec.im_worker_principal,
            "token": resolved.tokens[spec.im_worker_principal_ref],
        },
        "openevent": {"target": spec.openevent_grpc_addr},
        "principal_tokens": principal_tokens,
        "providers": [
            {
                "name": spec.im_provider,
                "sync": spec.im_sync,
                "credentials": {"app_id": spec.bot_app_id, "app_secret": spec.bot_app_secret},
                "options": {"api_base_url": spec.bot_api_base_url},
            }
        ],
        "mappings": mappings,
        "logging": {"level": "INFO"},
    }


def _render_model_proxy(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, Any]:
    return {
        "protocol": "llm.v1",
        "open_event": {"addr": spec.openevent_grpc_addr},
        "principal": spec.model_proxy_principal,
        "token": resolved.tokens[spec.model_proxy_principal_ref],
        "channels": [
            resolved.channels[session.session_id].model
            for session in spec.sessions
            if session.enabled
        ],
        "max_payload_bytes": spec.openevent_max_payload_bytes,
        "default_provider": spec.model_provider_name,
        "providers": {
            spec.model_provider_name: {
                "type": "openai_compatible",
                "base_url": spec.model_base_url,
                "api_key": spec.model_api_key,
                "timeout": {"total_ms": spec.model_timeout_ms},
                "allowlist": {
                    "methods": ["POST"],
                    "paths": ["/v1/chat/completions", "/v1/responses"],
                },
            }
        },
    }


def _render_cmd_worker(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_CMD,
        "open_event": {
            "addr": spec.openevent_grpc_addr,
            "max_payload_bytes": spec.openevent_max_payload_bytes,
        },
        "principal": spec.cmd_worker_principal,
        "token": resolved.tokens[spec.cmd_worker_principal_ref],
        "channel_ids": [
            resolved.channels[session.session_id].cmd
            for session in spec.sessions
            if session.enabled
        ],
        "output_dir": str(spec.cmd_output_dir),
        "max_concurrent_tasks": spec.cmd_max_concurrent_tasks,
        "default_timeout_ms": spec.cmd_default_timeout_ms,
    }


def _render_agent(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, Any]:
    return {
        "version": "v1",
        "agent": {
            "name": spec.agent_name,
            "principal": spec.agent_principal,
            "token": resolved.tokens[spec.agent_principal_ref],
            "system_prompt": spec.agent_system_prompt,
            "max_context_messages": spec.agent_max_context_messages,
            "model": spec.model_model,
            "model_timeout_ms": spec.agent_model_timeout_ms,
            "max_model_attempts": spec.agent_max_model_attempts,
            "cmd_result_timeout_ms": spec.agent_cmd_result_timeout_ms,
        },
        "openevent": {"target": spec.openevent_grpc_addr, "subscribe": {"only_my_recipient": False}},
        "model_proxy": {"principal": spec.model_proxy_principal},
        "cmd_worker": {"principal": spec.cmd_worker_principal},
        "im_sync_worker": {"principal": spec.im_worker_principal},
        "sessions": [
            {
                "session_id": session.session_id,
                "im_channel_id": resolved.channels[session.session_id].im,
                "model_channel_id": resolved.channels[session.session_id].model,
                "wal_channel_id": resolved.channels[session.session_id].wal,
                "cmd_channel_id": resolved.channels[session.session_id].cmd,
                "user_principal": session.user_principal,
                "enabled": session.enabled,
            }
            for session in spec.sessions
            if session.enabled
        ],
    }


def render_openevent_config(spec: DesiredSpec) -> dict[str, Any]:
    return {
        "grpc": {"listen_addr": spec.openevent_grpc_addr},
        "admin": {"listen_addr": spec.openevent_admin_addr},
        "storage": {"path": str(spec.openevent_storage_path)},
        "limits": {"max_payload_bytes": spec.openevent_max_payload_bytes},
    }


def _validate_openevent_config(openevent: dict[str, Any]) -> None:
    for field, value in (
        ("grpc.listen_addr", ((openevent.get("grpc") or {}).get("listen_addr"))),
        ("admin.listen_addr", ((openevent.get("admin") or {}).get("listen_addr"))),
        ("storage.path", ((openevent.get("storage") or {}).get("path"))),
    ):
        if not isinstance(value, str) or not value:
            raise SpecError(f"generated OpenEvent config missing {field}")
    max_payload = ((openevent.get("limits") or {}).get("max_payload_bytes"))
    if not isinstance(max_payload, int) or isinstance(max_payload, bool) or max_payload <= 0:
        raise SpecError("generated OpenEvent config limits.max_payload_bytes must be positive")


def validate_configs(configs: dict[str, dict[str, Any]]) -> None:
    _validate_openevent_config(configs["openevent"]["data"])
    from im_model_agent.config import parse_config as parse_agent_config
    from openevent.cmd_worker.config import parse_config as parse_cmd_config
    from openevent.im_p2p_syncer.config import parse_config as parse_im_config
    from openevent.model_proxy.config import parse_config as parse_model_config

    parse_im_config(configs["im_syncer"]["data"])
    parse_model_config(configs["model_proxy"]["data"])
    parse_cmd_config(configs["cmd_worker"]["data"])
    parse_agent_config(configs["agent"]["data"])
