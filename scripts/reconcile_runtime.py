#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


sys.dont_write_bytecode = True

DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
DEFAULT_MODEL_TIMEOUT_MS = 65000
DEFAULT_AGENT_MODEL_TIMEOUT_MS = 60000
DEFAULT_FREEZE_MESSAGE = "The model service is temporarily unavailable. The session is paused. Send another message to continue."
PROTOCOL_IM = "im.v1"
PROTOCOL_MODEL = "llm.v1"
PROTOCOL_WAL = "agent.wal.v1"
VISIBILITY_VALUES = {"public": 0, "protected": 1, "private": 2}
IM_PROVIDERS = {"feishu", "lark"}
IM_PROVIDER_API_BASE_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}


class ReconcileError(RuntimeError):
    exit_code = 1


class SpecError(ReconcileError):
    exit_code = 2


class ApplyError(ReconcileError):
    exit_code = 3


@dataclass(frozen=True)
class RuntimePaths:
    repo_root: Path
    runtime_root: Path

    @property
    def config_dir(self) -> Path:
        return self.runtime_root / "config"

    @property
    def state_path(self) -> Path:
        return self.config_dir / "state.yaml"

    @property
    def desired_path(self) -> Path:
        return self.config_dir / "desired.normalized.yaml"

    @property
    def plan_path(self) -> Path:
        return self.config_dir / "plan.yaml"

    @property
    def secrets_path(self) -> Path:
        return self.config_dir / "secrets.yaml"


@dataclass(frozen=True)
class ImUserSpec:
    principal_ref: str
    principal: int
    user_token: str | None
    im_user_external_id: str | None
    im_user_phone: str | None
    im_user_email: str | None


@dataclass(frozen=True)
class SessionSpec:
    session_id: str
    enabled: bool
    user_principal_ref: str
    user_principal: int
    im_channel_name: str
    model_name: str
    wal_name: str
    im_channel_id: int | None = None
    model_channel_id: int | None = None
    wal_channel_id: int | None = None


@dataclass(frozen=True)
class DesiredSpec:
    raw: dict[str, Any]
    runtime_name: str
    paths: RuntimePaths
    supervisor_ctl: str
    supervisor_programs: dict[str, str]
    openevent_grpc_addr: str
    openevent_admin_addr: str
    openevent_max_payload_bytes: int
    openevent_metadata_path: Path
    openevent_message_store_path: Path
    principals: dict[str, int]
    tokens: dict[str, str]
    im_worker_principal_ref: str
    im_worker_principal: int
    im_bot_principal_ref: str
    im_bot_principal: int
    im_provider: str
    im_session_type: str
    im_users: tuple[ImUserSpec, ...]
    bot_app_id: str
    bot_app_secret: str
    bot_api_base_url: str
    im_sync: dict[str, int]
    model_proxy_principal_ref: str
    model_proxy_principal: int
    model_provider_name: str
    model_base_url: str
    model_api_key: str
    model_model: str
    model_timeout_ms: int
    agent_principal_ref: str
    agent_principal: int
    agent_name: str
    agent_system_prompt: str
    agent_max_context_messages: int
    agent_model_timeout_ms: int
    agent_max_model_attempts: int
    agent_freeze_message: str
    channel_visibility: str
    sessions: tuple[SessionSpec, ...]

    def normalized(self) -> dict[str, Any]:
        return {
            "version": "v1",
            "runtime": {
                "name": self.runtime_name,
                "root": str(self.paths.runtime_root),
                "supervisor": {"ctl": self.supervisor_ctl, "programs": self.supervisor_programs},
            },
            "openevent": {
                "grpc_addr": self.openevent_grpc_addr,
                "admin_addr": self.openevent_admin_addr,
                "max_payload_bytes": self.openevent_max_payload_bytes,
                "storage": {"metadata_path": str(self.openevent_metadata_path)},
                "store": {"rocksdb": {"path": str(self.openevent_message_store_path)}},
            },
            "principals": self.principals,
            "tokens": {key: "<redacted>" for key in self.tokens},
            "im": {
                "provider": self.im_provider,
                "session_type": self.im_session_type,
                "worker_principal": self.im_worker_principal_ref,
                "resolved_worker_principal": self.im_worker_principal,
                "users": [
                    {
                        "principal": user.principal_ref,
                        "resolved_principal": user.principal,
                        "token": "<redacted>" if user.user_token else None,
                        **{
                            key: value
                            for key, value in {
                                "external_id": user.im_user_external_id,
                                "user_phone": user.im_user_phone,
                                "user_email": user.im_user_email,
                            }.items()
                            if value is not None
                        },
                    }
                    for user in self.im_users
                ],
                "bot": {
                    "principal": self.im_bot_principal_ref,
                    "resolved_principal": self.im_bot_principal,
                    "app_id": self.bot_app_id,
                    "app_secret": "<redacted>",
                    "api_base_url": self.bot_api_base_url,
                },
                "sync": self.im_sync,
            },
            "model": {
                "proxy_principal": self.model_proxy_principal_ref,
                "resolved_proxy_principal": self.model_proxy_principal,
                "provider_name": self.model_provider_name,
                "base_url": self.model_base_url,
                "api_key": "<redacted>",
                "model": self.model_model,
                "timeout_ms": self.model_timeout_ms,
            },
            "agent": {
                "name": self.agent_name,
                "principal": self.agent_principal_ref,
                "resolved_principal": self.agent_principal,
                "system_prompt": self.agent_system_prompt,
                "max_context_messages": self.agent_max_context_messages,
                "model_timeout_ms": self.agent_model_timeout_ms,
                "max_model_attempts": self.agent_max_model_attempts,
                "freeze_message": self.agent_freeze_message,
            },
            "channels": {"visibility": self.channel_visibility},
            "sessions": [
                {
                    "session_id": session.session_id,
                    "enabled": session.enabled,
                    "user": {
                        "principal": session.user_principal_ref,
                        "resolved_principal": session.user_principal,
                    },
                    "channels": {
                        "im": session.im_channel_name,
                        "model": session.model_name,
                        "wal": session.wal_name,
                    },
                    "channel_ids": {
                        "im": session.im_channel_id,
                        "model": session.model_channel_id,
                        "wal": session.wal_channel_id,
                    },
                }
                for session in self.sessions
            ],
        }


@dataclass(frozen=True)
class ChannelIds:
    im: int
    model: int
    wal: int


@dataclass
class ResolvedRuntime:
    tokens: dict[str, str]
    token_sources: dict[str, str]
    im_user_external_ids: dict[int, str]
    im_user_external_id_sources: dict[int, str]
    im_provider_session_ids: dict[str, str]
    im_provider_session_id_sources: dict[str, str]
    channels: dict[str, ChannelIds]
    actions: list[dict[str, Any]] = field(default_factory=list)


def parse_spec(path: Path, repo_root: Path, runtime_root_override: str | None = None) -> DesiredSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SpecError("spec root must be a YAML mapping")
    if raw.get("version") != "v1":
        raise SpecError("version must be v1")
    runtime = _obj(raw.get("runtime"), "runtime")
    runtime_name = _str(runtime.get("name"), "runtime.name")
    runtime_root = Path(runtime_root_override or runtime.get("root") or f"runtime/{runtime_name}")
    if not runtime_root.is_absolute():
        runtime_root = repo_root / runtime_root
    supervisor = _obj(runtime.get("supervisor"), "runtime.supervisor")
    programs = _obj(supervisor.get("programs"), "runtime.supervisor.programs")
    for key in ("openevent", "im_syncer", "model_proxy", "agent"):
        _str(programs.get(key), f"runtime.supervisor.programs.{key}")

    openevent = _obj(raw.get("openevent"), "openevent")
    openevent_storage = _obj(openevent.get("storage"), "openevent.storage")
    openevent_store = _obj(openevent.get("store"), "openevent.store")
    openevent_rocksdb = _obj(openevent_store.get("rocksdb"), "openevent.store.rocksdb")
    principals = _obj(raw.get("principals"), "principals")
    principal_values = {
        str(key): _positive_int(value, f"principals.{key}")
        for key, value in principals.items()
    }
    _validate_unique(list(principal_values.values()), "principals values")
    tokens = {}
    for key, value in _obj(raw.get("tokens", {}), "tokens").items():
        token_ref = str(key)
        if token_ref not in principal_values:
            raise SpecError(f"tokens.{token_ref} must reference a key in principals")
        if isinstance(value, str) and value:
            tokens[token_ref] = value

    im = _obj(raw.get("im"), "im")
    im_provider = _str(im.get("provider", "lark"), "im.provider")
    im_worker_principal_ref, im_worker_principal = _principal_ref(
        im.get("worker_principal"),
        "im.worker_principal",
        principal_values,
    )
    bot = _obj(im.get("bot"), "im.bot")
    im_bot_principal_ref, im_bot_principal = _principal_ref(
        bot.get("principal"),
        "im.bot.principal",
        principal_values,
    )
    bot_app_id = _str(bot.get("app_id"), "im.bot.app_id")
    model = _obj(raw.get("model"), "model")
    model_proxy_principal_ref, model_proxy_principal = _principal_ref(
        model.get("proxy_principal"),
        "model.proxy_principal",
        principal_values,
    )
    agent = _obj(raw.get("agent"), "agent")
    agent_principal_ref, agent_principal = _principal_ref(
        agent.get("principal"),
        "agent.principal",
        principal_values,
    )
    sessions_raw = _list(raw.get("sessions"), "sessions")
    im_users = tuple(
        _parse_im_user(item, index, principal_values)
        for index, item in enumerate(_list(im.get("users"), "im.users"))
    )
    _validate_unique([user.principal for user in im_users], "im.users[].principal")
    for index, user in enumerate(im_users):
        token = tokens.get(user.principal_ref)
        if token and user.user_token and token != user.user_token:
            raise SpecError(f"im.users[{index}].token must match tokens.{user.principal_ref}")
    im_users_by_ref = {user.principal_ref: user for user in im_users}
    sessions = tuple(
        _parse_session(
            runtime_name,
            item,
            index,
            im_users_by_ref,
        )
        for index, item in enumerate(sessions_raw)
    )
    if not any(session.enabled for session in sessions):
        raise SpecError("sessions must contain at least one enabled session")
    _validate_unique([session.session_id for session in sessions], "sessions[].session_id")
    _validate_unique([session.im_channel_name for session in sessions if session.enabled], "sessions[].channels.im")
    _validate_unique([session.model_name for session in sessions if session.enabled], "sessions[].channels.model")
    _validate_unique([session.wal_name for session in sessions if session.enabled], "sessions[].channels.wal")

    spec = DesiredSpec(
        raw=raw,
        runtime_name=runtime_name,
        paths=RuntimePaths(repo_root=repo_root, runtime_root=runtime_root),
        supervisor_ctl=_str(supervisor.get("ctl", "supervisorctl"), "runtime.supervisor.ctl"),
        supervisor_programs={key: str(programs[key]) for key in ("openevent", "im_syncer", "model_proxy", "agent")},
        openevent_grpc_addr=_str(openevent.get("grpc_addr"), "openevent.grpc_addr"),
        openevent_admin_addr=_str(openevent.get("admin_addr"), "openevent.admin_addr"),
        openevent_max_payload_bytes=_positive_int(
            openevent.get("max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES),
            "openevent.max_payload_bytes",
        ),
        openevent_metadata_path=_path_str(
            openevent_storage.get("metadata_path"),
            "openevent.storage.metadata_path",
            repo_root,
        ),
        openevent_message_store_path=_path_str(
            openevent_rocksdb.get("path"),
            "openevent.store.rocksdb.path",
            repo_root,
        ),
        principals=principal_values,
        tokens=tokens,
        im_worker_principal_ref=im_worker_principal_ref,
        im_worker_principal=im_worker_principal,
        im_bot_principal_ref=im_bot_principal_ref,
        im_bot_principal=im_bot_principal,
        im_provider=im_provider,
        im_session_type=_str(im.get("session_type", "p2p"), "im.session_type"),
        im_users=im_users,
        bot_app_id=bot_app_id,
        bot_app_secret=_str(bot.get("app_secret"), "im.bot.app_secret"),
        bot_api_base_url=_str(
            bot.get("api_base_url", _default_im_api_base_url(im_provider)),
            "im.bot.api_base_url",
        ),
        im_sync={
            "interval_ms": _positive_int(_obj(im.get("sync", {}), "im.sync").get("interval_ms", 5000), "im.sync.interval_ms"),
            "page_size": _positive_int(_obj(im.get("sync", {}), "im.sync").get("page_size", 50), "im.sync.page_size"),
            "startup_lookback_ms": _non_negative_int(
                _obj(im.get("sync", {}), "im.sync").get("startup_lookback_ms", 300000),
                "im.sync.startup_lookback_ms",
            ),
        },
        model_proxy_principal_ref=model_proxy_principal_ref,
        model_proxy_principal=model_proxy_principal,
        model_provider_name=_str(model.get("provider_name", "openai_main"), "model.provider_name"),
        model_base_url=_str(model.get("base_url"), "model.base_url").rstrip("/"),
        model_api_key=_str(model.get("api_key"), "model.api_key"),
        model_model=_str(model.get("model"), "model.model"),
        model_timeout_ms=_positive_int(model.get("timeout_ms", DEFAULT_MODEL_TIMEOUT_MS), "model.timeout_ms"),
        agent_principal_ref=agent_principal_ref,
        agent_principal=agent_principal,
        agent_name=_str(agent.get("name", "im-model-agent"), "agent.name"),
        agent_system_prompt=_str(agent.get("system_prompt"), "agent.system_prompt"),
        agent_max_context_messages=_positive_int(agent.get("max_context_messages", 20), "agent.max_context_messages"),
        agent_model_timeout_ms=_positive_int(
            agent.get("model_timeout_ms", DEFAULT_AGENT_MODEL_TIMEOUT_MS),
            "agent.model_timeout_ms",
        ),
        agent_max_model_attempts=_positive_int(agent.get("max_model_attempts", 3), "agent.max_model_attempts"),
        agent_freeze_message=_str(agent.get("freeze_message", DEFAULT_FREEZE_MESSAGE), "agent.freeze_message"),
        channel_visibility=_str(_obj(raw.get("channels", {}), "channels").get("visibility", "private"), "channels.visibility"),
        sessions=sessions,
    )
    _validate_spec(spec)
    return spec


def render_configs(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, dict[str, Any]]:
    return {
        "openevent": {
            "path": "openevent-server.yaml",
            "data": render_openevent_config(spec),
        },
        "im_syncer": {"path": "im-p2p-syncer.yaml", "data": _render_im_syncer(spec, resolved)},
        "model_proxy": {"path": "model-proxy.yaml", "data": _render_model_proxy(spec, resolved)},
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
                "status": "active",
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
                "status": "active",
            }
        )
    return {
        "version": "v1",
        "worker": {
            "name": f"im-sync-p2p-{spec.im_provider}",
            "principal": spec.im_worker_principal,
            "token": resolved.tokens[spec.im_worker_principal_ref],
        },
        "openevent": {"target": spec.openevent_grpc_addr, "publish": {"use_auto_seq": True}},
        "principal_tokens": principal_tokens,
        "providers": [
            {
                "name": spec.im_provider,
                "enabled": True,
                "adapter": spec.im_provider,
                "sync": {"mode": "poll", **spec.im_sync},
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
        "idempotency_dsn": f"sqlite:///{spec.paths.runtime_root / 'data/model-proxy/model_proxy.db'}",
        "max_payload_bytes": spec.openevent_max_payload_bytes,
        "filter_response_headers": True,
        "default_provider": spec.model_provider_name,
        "providers": {
            spec.model_provider_name: {
                "type": "openai_compatible",
                "base_url": spec.model_base_url,
                "api_key": spec.model_api_key,
                "timeout": {"total_ms": spec.model_timeout_ms},
            }
        },
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
            "freeze_message": spec.agent_freeze_message,
        },
        "openevent": {"target": spec.openevent_grpc_addr, "subscribe": {"only_my_recipient": False}},
        "model_proxy": {"principal": spec.model_proxy_principal},
        "im_sync_worker": {"principal": spec.im_worker_principal},
        "sessions": [
            {
                "session_id": session.session_id,
                "im_channel_id": resolved.channels[session.session_id].im,
                "model_channel_id": resolved.channels[session.session_id].model,
                "wal_channel_id": resolved.channels[session.session_id].wal,
                "user_principal": session.user_principal,
                "agent_bot_principal": spec.agent_principal,
                "enabled": session.enabled,
            }
            for session in spec.sessions
            if session.enabled
        ],
    }


class OpenEventRuntime:
    def __init__(self, grpc_addr: str, admin_addr: str):
        _ensure_project_path()
        from openevent.sdk import AdminClient, OpenEventClient

        self.event = OpenEventClient(grpc_addr)
        self.admin = AdminClient(admin_addr)

    def token_usable(self, principal: int, token: str) -> bool:
        try:
            self.event.get_status(principal, token)
            return True
        except Exception:
            return False

    def list_tokens(self) -> list[Any]:
        return list(self.admin.list_tokens().bindings)

    def add_token(self, principal: int) -> str:
        return str(self.admin.add_token(principal).binding.token)

    def list_channels(self, principal: int, token: str) -> list[Any]:
        return list(self.event.list_channels(principal, token).channels)

    def get_channel(self, principal: int, token: str, channel_id: int) -> Any | None:
        try:
            return self.event.get_channel(principal, token, channel_id).channel
        except Exception:
            return None

    def create_channel(
        self,
        principal: int,
        token: str,
        *,
        name: str,
        visibility: int,
        protocol: str,
        description: str,
        members: list[int],
    ) -> Any:
        return self.event.create_channel(
            principal=principal,
            token=token,
            name=name,
            visibility=visibility,
            protocol=protocol,
            description=description,
            members=members,
        ).channel

    def add_member(self, principal: int, token: str, channel_id: int, target_principal: int) -> None:
        self.event.add_member(principal, token, channel_id, target_principal)


def wait_openevent_ready(spec: DesiredSpec, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            import grpc

            grpc.channel_ready_future(grpc.insecure_channel(spec.openevent_grpc_addr)).result(timeout=1)
            grpc.channel_ready_future(grpc.insecure_channel(spec.openevent_admin_addr)).result(timeout=1)
            runtime = OpenEventRuntime(spec.openevent_grpc_addr, spec.openevent_admin_addr)
            runtime.list_tokens()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise ApplyError(f"OpenEvent admin endpoint is not ready: {last_error}")


def dry_resolve(spec: DesiredSpec, previous: dict[str, Any] | None = None) -> ResolvedRuntime:
    tokens: dict[str, str] = {}
    token_sources: dict[str, str] = {}
    for key in _required_principal_refs(spec):
        principal = spec.principals[key]
        input_token = _input_token_for_ref(spec, key)
        if input_token:
            tokens[key] = input_token
            token_sources[key] = "input"
        else:
            tokens[key] = f"dry-token-{principal}"
            token_sources[key] = "dry-run"
    channels = {}
    actions = [
        {
            "action": "dry_run_only",
            "detail": "token, phone-derived external ids, and channel ids are previews; --apply resolves real resources",
        }
    ]
    im_user_external_ids, im_user_external_id_sources = _resolve_im_user_external_ids(
        spec,
        dry_run=True,
        actions=actions,
    )
    for index, session in enumerate(spec.sessions, start=1):
        if session.enabled:
            ids = _state_channel_ids(previous, session.session_id)
            channels[session.session_id] = ChannelIds(
                im=session.im_channel_id or ids.get("im") or 10000 + index,
                model=session.model_channel_id or ids.get("model") or 20000 + index,
                wal=session.wal_channel_id or ids.get("wal") or 30000 + index,
            )
    im_provider_session_ids, im_provider_session_id_sources = _resolve_im_provider_session_ids(
        spec,
        im_user_external_ids,
        previous,
        dry_run=True,
        actions=actions,
    )
    _validate_resolved(spec, tokens, im_user_external_ids, im_provider_session_ids, channels)
    return ResolvedRuntime(
        tokens=tokens,
        token_sources=token_sources,
        im_user_external_ids=im_user_external_ids,
        im_user_external_id_sources=im_user_external_id_sources,
        im_provider_session_ids=im_provider_session_ids,
        im_provider_session_id_sources=im_provider_session_id_sources,
        channels=channels,
        actions=actions,
    )


def apply_resolve(spec: DesiredSpec, previous: dict[str, Any] | None = None) -> ResolvedRuntime:
    runtime = OpenEventRuntime(spec.openevent_grpc_addr, spec.openevent_admin_addr)
    secrets = load_state(spec.paths.secrets_path)
    actions: list[dict[str, Any]] = []
    tokens, token_sources = _resolve_principal_tokens(spec, runtime, previous, secrets, actions)
    im_user_external_ids, im_user_external_id_sources = _resolve_im_user_external_ids(
        spec,
        dry_run=False,
        actions=actions,
    )
    im_provider_session_ids, im_provider_session_id_sources = _resolve_im_provider_session_ids(
        spec,
        im_user_external_ids,
        previous,
        runtime=runtime,
        dry_run=False,
        actions=actions,
    )
    channels = _resolve_channels(
        spec,
        runtime,
        tokens,
        im_provider_session_ids,
        previous,
        actions,
    )
    _validate_resolved(spec, tokens, im_user_external_ids, im_provider_session_ids, channels)
    return ResolvedRuntime(
        tokens=tokens,
        token_sources=token_sources,
        im_user_external_ids=im_user_external_ids,
        im_user_external_id_sources=im_user_external_id_sources,
        im_provider_session_ids=im_provider_session_ids,
        im_provider_session_id_sources=im_provider_session_id_sources,
        channels=channels,
        actions=actions,
    )


def render_openevent_config(spec: DesiredSpec) -> dict[str, Any]:
    return {
        "grpc": {"listen_addr": spec.openevent_grpc_addr},
        "admin": {"listen_addr": spec.openevent_admin_addr},
        "storage": {"metadata_path": str(spec.openevent_metadata_path)},
        "store": {"rocksdb": {"path": str(spec.openevent_message_store_path)}},
        "limits": {"max_payload_bytes": spec.openevent_max_payload_bytes},
        "log": {"level": "info"},
    }


def write_openevent_config(spec: DesiredSpec) -> dict[str, Any]:
    spec.paths.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    spec.paths.config_dir.mkdir(parents=True, exist_ok=True)
    spec.openevent_metadata_path.mkdir(parents=True, exist_ok=True)
    spec.openevent_message_store_path.mkdir(parents=True, exist_ok=True)
    data = render_openevent_config(spec)
    _validate_openevent_config(data)
    path = spec.paths.config_dir / "openevent-server.yaml"
    content = _dump_yaml(data)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    changed = previous != content
    if changed:
        _atomic_write(path, content, 0o600)
    return {"path": str(path), "sha256": _sha256(content.encode("utf-8")), "changed": changed}


def write_runtime_files(
    spec: DesiredSpec,
    resolved: ResolvedRuntime,
    dry_run: bool,
    openevent_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    configs = render_configs(spec, resolved)
    validate_configs(configs)
    plan = {"actions": list(resolved.actions), "configs": {}}
    if dry_run:
        for name, item in configs.items():
            content = _dump_yaml(item["data"])
            plan["configs"][name] = {
                "path": str(spec.paths.config_dir / item["path"]),
                "sha256": _sha256(content.encode("utf-8")),
                "would_write": True,
            }
        return plan
    spec.paths.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    spec.paths.config_dir.mkdir(parents=True, exist_ok=True)
    spec.openevent_metadata_path.mkdir(parents=True, exist_ok=True)
    spec.openevent_message_store_path.mkdir(parents=True, exist_ok=True)
    (spec.paths.runtime_root / "data/model-proxy").mkdir(parents=True, exist_ok=True)
    _atomic_write(spec.paths.desired_path, _dump_yaml(spec.normalized()), 0o600)
    for name, item in configs.items():
        if openevent_config is not None and name == "openevent":
            plan["configs"][name] = openevent_config
            continue
        path = spec.paths.config_dir / item["path"]
        content = _dump_yaml(item["data"])
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        changed = previous != content
        if changed:
            _atomic_write(path, content, 0o600)
        plan["configs"][name] = {"path": str(path), "sha256": _sha256(content.encode("utf-8")), "changed": changed}
    state = {
        "version": "v1",
        "runtime_name": spec.runtime_name,
        "last_apply_ms": int(time.time() * 1000),
        "configs": plan["configs"],
        "tokens": _token_state(spec, resolved),
        "channels": {
            "sessions": {
                session_id: {
                    "im": {"channel_id": ids.im},
                    "model": {"channel_id": ids.model},
                    "wal": {"channel_id": ids.wal},
                }
                for session_id, ids in resolved.channels.items()
            }
        },
        "last_apply_status": "success",
    }
    state["im"] = _im_state(spec, resolved)
    _atomic_write(spec.paths.state_path, _dump_yaml(state), 0o600)
    _atomic_write(spec.paths.secrets_path, _dump_yaml(_secrets_state(spec, resolved)), 0o600)
    _atomic_write(spec.paths.plan_path, _dump_yaml(plan), 0o600)
    return plan


def _validate_openevent_config(openevent: dict[str, Any]) -> None:
    for field, value in (
        ("grpc.listen_addr", ((openevent.get("grpc") or {}).get("listen_addr"))),
        ("admin.listen_addr", ((openevent.get("admin") or {}).get("listen_addr"))),
        ("storage.metadata_path", ((openevent.get("storage") or {}).get("metadata_path"))),
        ("store.rocksdb.path", (((openevent.get("store") or {}).get("rocksdb") or {}).get("path"))),
    ):
        if not isinstance(value, str) or not value:
            raise SpecError(f"generated OpenEvent config missing {field}")
    max_payload = ((openevent.get("limits") or {}).get("max_payload_bytes"))
    if not isinstance(max_payload, int) or isinstance(max_payload, bool) or max_payload <= 0:
        raise SpecError("generated OpenEvent config limits.max_payload_bytes must be positive")


def validate_configs(configs: dict[str, dict[str, Any]]) -> None:
    _validate_openevent_config(configs["openevent"]["data"])
    _ensure_project_path()
    from im_model_agent.config import parse_config as parse_agent_config
    from openevent.im_p2p_syncer.config import parse_config as parse_im_config
    from openevent.model_proxy.config import parse_config as parse_model_config

    parse_im_config(configs["im_syncer"]["data"])
    parse_model_config(configs["model_proxy"]["data"])
    parse_agent_config(configs["agent"]["data"])


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--runtime-root")
    parser.add_argument("--print-config", choices=["openevent", "im_syncer", "model_proxy", "agent"])
    args = parser.parse_args(argv)
    mode_count = int(args.dry_run) + int(args.apply) + int(args.print_config is not None)
    if mode_count != 1:
        raise SpecError("choose exactly one of --dry-run, --apply, or --print-config")
    repo_root = Path(__file__).resolve().parents[1]
    spec = parse_spec(Path(args.spec), repo_root=repo_root, runtime_root_override=args.runtime_root)
    previous = load_state(spec.paths.state_path)
    if args.apply:
        openevent_config = write_openevent_config(spec)
        restart_changed(spec, {"configs": {"openevent": openevent_config}})
        ensure_program_running(spec, "openevent")
        wait_openevent_ready(spec)
        resolved = apply_resolve(spec, previous)
    else:
        openevent_config = None
        resolved = dry_resolve(spec, previous)
    if args.print_config:
        print(_dump_yaml(render_configs(spec, resolved)[args.print_config]["data"]), end="")
        return 0
    plan = write_runtime_files(spec, resolved, dry_run=args.dry_run, openevent_config=openevent_config)
    print(_dump_yaml(plan), end="")
    if args.apply:
        force_restart = {"model_proxy", "im_syncer", "agent"} if openevent_config and openevent_config.get("changed") else set()
        restart_changed(spec, plan, skip={"openevent"}, force=force_restart)
        for logical in ("model_proxy", "im_syncer", "agent"):
            ensure_program_running(spec, logical)
    return 0


def ensure_program_running(spec: DesiredSpec, logical: str) -> None:
    program_key = {"openevent": "openevent", "im_syncer": "im_syncer", "model_proxy": "model_proxy", "agent": "agent"}[logical]
    program = spec.supervisor_programs[program_key]
    status = subprocess.run([spec.supervisor_ctl, "status", program], check=False, capture_output=True, text=True)
    output = f"{status.stdout}\n{status.stderr}"
    if status.returncode == 0 and "RUNNING" in output:
        return
    start = subprocess.run([spec.supervisor_ctl, "start", program], check=False, capture_output=True, text=True)
    if start.returncode != 0:
        raise ApplyError(f"supervisor start failed for {program}: {start.stderr or start.stdout}")


def restart_changed(
    spec: DesiredSpec,
    plan: dict[str, Any],
    skip: set[str] | None = None,
    force: set[str] | None = None,
) -> None:
    skip = skip or set()
    force = force or set()
    configs = plan.get("configs", {})
    for logical in ("openevent", "model_proxy", "im_syncer", "agent"):
        if logical in skip:
            continue
        config = configs.get(logical, {})
        if logical not in force and not config.get("changed"):
            continue
        program_key = {"openevent": "openevent", "im_syncer": "im_syncer", "model_proxy": "model_proxy", "agent": "agent"}[logical]
        program = spec.supervisor_programs[program_key]
        result = subprocess.run([spec.supervisor_ctl, "restart", program], check=False)
        if result.returncode != 0:
            raise ApplyError(f"supervisor restart failed for {program}")


def _parse_im_user(raw: Any, index: int, principals: dict[str, int]) -> ImUserSpec:
    data = _obj(raw, f"im.users[{index}]")
    principal_ref, principal = _principal_ref(data.get("principal"), f"im.users[{index}].principal", principals)
    return _build_im_user(
        principal_ref=principal_ref,
        principal=principal,
        user_token=data.get("token") if isinstance(data.get("token"), str) and data.get("token") else None,
        external_id=_optional_str(data.get("external_id"), f"im.users[{index}].external_id"),
        user_phone=_optional_str(data.get("user_phone"), f"im.users[{index}].user_phone"),
        user_email=_optional_str(data.get("user_email"), f"im.users[{index}].user_email"),
        field=f"im.users[{index}]",
    )


def _build_im_user(
    *,
    principal_ref: str,
    principal: int,
    user_token: str | None,
    external_id: str | None,
    user_phone: str | None,
    user_email: str | None,
    field: str,
) -> ImUserSpec:
    identity_fields = [
        name
        for name, value in (
            ("external_id", external_id),
            ("user_phone", user_phone),
            ("user_email", user_email),
        )
        if value
    ]
    if len(identity_fields) > 1:
        raise SpecError(f"{field} must provide only one of external_id, user_phone, or user_email")
    if not identity_fields:
        raise SpecError(f"{field} must provide external_id, user_phone, or user_email")
    return ImUserSpec(
        principal_ref=principal_ref,
        principal=principal,
        user_token=user_token,
        im_user_external_id=external_id,
        im_user_phone=user_phone,
        im_user_email=user_email,
    )


def _parse_session(
    runtime_name: str,
    raw: Any,
    index: int,
    im_users_by_ref: dict[str, ImUserSpec],
) -> SessionSpec:
    data = _obj(raw, f"sessions[{index}]")
    user = _obj(data.get("user"), f"sessions[{index}].user")
    if "im" in data:
        raise SpecError(f"sessions[{index}].im is not allowed; configure sessions[{index}].channels.im")
    if "token" in user:
        raise SpecError(f"sessions[{index}].user.token is not allowed; configure im.users[].token")
    if "external_id" in user:
        raise SpecError(
            f"sessions[{index}].user.external_id is not allowed; "
            "configure im.users[].external_id, im.users[].user_phone, or im.users[].user_email"
        )
    channels = _obj(data.get("channels"), f"sessions[{index}].channels")
    if "names" in channels:
        raise SpecError(f"sessions[{index}].channels.names is not allowed; configure sessions[{index}].channels.model/wal")
    if "ids" in channels:
        raise SpecError(f"sessions[{index}].channels.ids is not allowed; configure sessions[{index}].channel_ids")
    ids = _obj(data.get("channel_ids", {}), f"sessions[{index}].channel_ids")
    session_id = _str(data.get("session_id"), f"sessions[{index}].session_id")
    user_principal_ref = _str(user.get("principal"), f"sessions[{index}].user.principal")
    if user_principal_ref not in im_users_by_ref:
        raise SpecError(f"sessions[{index}].user.principal must reference an im.users[].principal")
    user_principal = im_users_by_ref[user_principal_ref].principal
    im_channel_name = _str(channels.get("im"), f"sessions[{index}].channels.im")
    return SessionSpec(
        session_id=session_id,
        enabled=_bool(data.get("enabled", True), f"sessions[{index}].enabled"),
        user_principal_ref=user_principal_ref,
        user_principal=user_principal,
        im_channel_name=im_channel_name,
        model_name=_str(channels.get("model", f"{runtime_name}.llm.{session_id}"), f"sessions[{index}].channels.model"),
        wal_name=_str(channels.get("wal", f"{runtime_name}.wal.{session_id}"), f"sessions[{index}].channels.wal"),
        im_channel_id=_optional_positive_int(ids.get("im"), f"sessions[{index}].channel_ids.im"),
        model_channel_id=_optional_positive_int(ids.get("model"), f"sessions[{index}].channel_ids.model"),
        wal_channel_id=_optional_positive_int(ids.get("wal"), f"sessions[{index}].channel_ids.wal"),
    )


def _state_channel_ids(state: dict[str, Any] | None, session_id: str) -> dict[str, int]:
    if not state:
        return {}
    raw = (((state.get("channels") or {}).get("sessions") or {}).get(session_id) or {})
    result = {}
    for key in ("im", "model", "wal"):
        value = (raw.get(key) or {}).get("channel_id")
        if isinstance(value, int) and value > 0:
            result[key] = value
    return result


def _principal_ref(raw: Any, field: str, principals: dict[str, int]) -> tuple[str, int]:
    ref = _str(raw, field)
    if ref not in principals:
        raise SpecError(f"{field} must reference a key in principals")
    return ref, principals[ref]


def _required_principal_refs(spec: DesiredSpec) -> tuple[str, ...]:
    refs = [
        spec.im_worker_principal_ref,
        spec.im_bot_principal_ref,
        spec.model_proxy_principal_ref,
        spec.agent_principal_ref,
    ]
    refs.extend(user.principal_ref for user in spec.im_users)
    return tuple(dict.fromkeys(refs))


def _resolve_principal_tokens(
    spec: DesiredSpec,
    runtime: OpenEventRuntime,
    previous: dict[str, Any] | None,
    secrets: dict[str, Any] | None,
    actions: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    bindings = runtime.list_tokens()
    tokens: dict[str, str] = {}
    sources: dict[str, str] = {}
    for key in _required_principal_refs(spec):
        principal = spec.principals[key]
        token, source = _resolve_token(
            runtime=runtime,
            principal=principal,
            input_token=_input_token_for_ref(spec, key),
            secret_token=_secret_token(secrets, key),
            existing_bindings=bindings,
            label=key,
            actions=actions,
        )
        tokens[key] = token
        sources[key] = source
    return tokens, sources


def _resolve_im_user_external_ids(
    spec: DesiredSpec,
    *,
    dry_run: bool,
    actions: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    user_resolver: LarkOpenAPIUserResolver | None = None
    external_ids: dict[str, str] = {}
    sources: dict[str, str] = {}
    for user in spec.im_users:
        if user.im_user_external_id:
            external_id = user.im_user_external_id
            source = "input"
        elif user.im_user_phone:
            if dry_run:
                external_id = f"dry-open-id-{_sha256(user.im_user_phone.encode('utf-8'))[:12]}"
                source = "dry-run-phone"
            else:
                if user_resolver is None:
                    user_resolver = LarkOpenAPIUserResolver(
                        provider=spec.im_provider,
                        app_id=spec.bot_app_id,
                        app_secret=spec.bot_app_secret,
                        api_base_url=spec.bot_api_base_url,
                    )
                external_id = user_resolver.open_id_by_phone(user.im_user_phone)
                source = f"{spec.im_provider}-phone"
        elif user.im_user_email:
            if dry_run:
                external_id = f"dry-open-id-{_sha256(user.im_user_email.encode('utf-8'))[:12]}"
                source = "dry-run-email"
            else:
                if user_resolver is None:
                    user_resolver = LarkOpenAPIUserResolver(
                        provider=spec.im_provider,
                        app_id=spec.bot_app_id,
                        app_secret=spec.bot_app_secret,
                        api_base_url=spec.bot_api_base_url,
                    )
                external_id = user_resolver.open_id_by_email(user.im_user_email)
                source = f"{spec.im_provider}-email"
        else:
            raise SpecError(f"im user {user.principal} is missing IM user identity")
        external_ids[user.principal] = external_id
        sources[user.principal] = source
        actions.append(
            {
                "action": "resolve_external_id",
                "principal": user.principal,
                "source": source,
                "external_id_sha256": _sha256(external_id.encode("utf-8")),
            }
        )
    return external_ids, sources


def _resolve_im_provider_session_ids(
    spec: DesiredSpec,
    im_user_external_ids: dict[int, str],
    previous: dict[str, Any] | None,
    *,
    runtime: OpenEventRuntime | None = None,
    dry_run: bool,
    actions: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    result: dict[str, str] = {}
    sources: dict[str, str] = {}
    resolver: LarkOpenAPIP2PResolver | None = None
    for session in spec.sessions:
        if not session.enabled:
            continue
        external_id = im_user_external_ids[session.user_principal]
        existing = _state_im_provider_session_id(previous, session.session_id, spec.im_provider)
        if existing:
            provider_session_id = existing
            source = "state"
        else:
            existing = _config_im_provider_session_id(spec, session)
            if existing:
                provider_session_id = existing
                source = "generated-config"
            elif dry_run:
                provider_session_id = f"dry-{spec.im_provider}-chat-{_sha256(external_id.encode('utf-8'))[:12]}"
                source = "dry-run"
            else:
                if resolver is None:
                    resolver = LarkOpenAPIP2PResolver(
                        provider=spec.im_provider,
                        app_id=spec.bot_app_id,
                        app_secret=spec.bot_app_secret,
                        api_base_url=spec.bot_api_base_url,
                    )
                provider_session_id = resolver.chat_id_for_user(external_id)
                source = spec.im_provider
        result[session.session_id] = provider_session_id
        sources[session.session_id] = source
        actions.append(
            {
                "action": "resolve_provider_session",
                "session_id": session.session_id,
                "user_principal": session.user_principal,
                "source": source,
                "provider_session_id_sha256": _sha256(provider_session_id.encode("utf-8")),
            }
        )
    return result, sources


def _state_im_provider_session_id(state: dict[str, Any] | None, session_id: str, provider: str) -> str | None:
    if not state:
        return None
    im_state = state.get("im") or {}
    if im_state.get("provider") != provider:
        return None
    session_state = (im_state.get("sessions") or {}).get(session_id) or {}
    if session_state.get("provider") != provider:
        return None
    value = session_state.get("provider_session_id")
    return value if isinstance(value, str) and value else None


def _config_im_provider_session_id(spec: DesiredSpec, session: SessionSpec) -> str | None:
    config_path = spec.paths.config_dir / "im-p2p-syncer.yaml"
    if not config_path.exists():
        return None
    data = load_state(config_path)
    if not data:
        return None
    channel_id = _state_channel_ids(load_state(spec.paths.state_path), session.session_id).get("im")
    for mapping in data.get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        if channel_id is not None and mapping.get("channel_id") != channel_id:
            continue
        if mapping.get("provider") != spec.im_provider:
            continue
        if mapping.get("principal") != session.user_principal:
            continue
        value = mapping.get("session_id")
        if isinstance(value, str) and value:
            return value
    return None


class LarkOpenAPIClient:
    def __init__(self, *, provider: str, app_id: str, app_secret: str, api_base_url: str):
        self._provider = provider
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_base_url = api_base_url.rstrip("/")
        self._tenant_access_token: str | None = None

    def _tenant_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        response = self._post_json(
            "/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self._app_id, "app_secret": self._app_secret},
        )
        token = response.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise ApplyError(f"{self._provider} tenant_access_token response missing tenant_access_token")
        self._tenant_access_token = token
        return token

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._api_base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                **(headers or {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApplyError(f"{self._provider} API {path} failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ApplyError(f"{self._provider} API {path} failed: {exc.reason}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApplyError(f"{self._provider} API {path} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ApplyError(f"{self._provider} API {path} returned non-object JSON")
        code = data.get("code")
        if code not in (None, 0):
            raise ApplyError(f"{self._provider} API {path} failed: {code} {data.get('msg')}")
        return data


class LarkOpenAPIP2PResolver(LarkOpenAPIClient):
    def chat_id_for_user(self, open_id: str) -> str:
        response = self._post_json(
            "/open-apis/im/v1/chat_p2p/batch_query",
            {"chatter_ids": [open_id]},
            query={"chatter_id_type": "open_id"},
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ApplyError(f"{self._provider} chat_p2p batch_query response missing data")
        chats = data.get("p2p_chats")
        if not isinstance(chats, list):
            raise ApplyError(f"{self._provider} chat_p2p batch_query response missing data.p2p_chats")
        matches = []
        for item in chats:
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            if isinstance(chat_id, str) and chat_id:
                matches.append(chat_id)
        if not matches:
            raise ApplyError(f"{self._provider} did not return a P2P chat_id for open_id {open_id}")
        if len(set(matches)) > 1:
            raise ApplyError(f"{self._provider} returned multiple P2P chat_ids for open_id {open_id}")
        return matches[0]


class LarkOpenAPIUserResolver(LarkOpenAPIClient):
    def open_id_by_phone(self, phone: str) -> str:
        return self._open_id_by_contact_key("mobiles", "mobile", phone, "phone")

    def open_id_by_email(self, email: str) -> str:
        return self._open_id_by_contact_key("emails", "email", email, "email")

    def _open_id_by_contact_key(
        self,
        request_key: str,
        response_key: str,
        value: str,
        label: str,
    ) -> str:
        response = self._post_json(
            "/open-apis/contact/v3/users/batch_get_id",
            {request_key: [value]},
            query={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ApplyError(f"{self._provider} batch_get_id response missing data")
        user_list = data.get("user_list")
        if not isinstance(user_list, list):
            raise ApplyError(f"{self._provider} batch_get_id response missing data.user_list")

        matches = []
        for item in user_list:
            if not isinstance(item, dict):
                continue
            if item.get(response_key) == value:
                user_id = item.get("user_id")
                if isinstance(user_id, str) and user_id:
                    matches.append(user_id)
        if not matches:
            raise ApplyError(f"{self._provider} did not return an open_id for {label} {value}")
        if len(set(matches)) > 1:
            raise ApplyError(f"{self._provider} returned multiple open_ids for {label} {value}")
        return matches[0]

def _resolve_token(
    *,
    runtime: OpenEventRuntime,
    principal: int,
    input_token: str | None,
    secret_token: str | None,
    existing_bindings: list[Any],
    label: str,
    actions: list[dict[str, Any]],
) -> tuple[str, str]:
    for token, source in ((input_token, "input"), (secret_token, "secret")):
        if token and runtime.token_usable(principal, token):
            actions.append(_token_action("use_token", label, principal, source, token))
            return token, source
        if token:
            actions.append(_token_action("reject_token", label, principal, source, token))

    for binding in existing_bindings:
        if int(getattr(binding, "principal", 0)) != principal:
            continue
        token = str(getattr(binding, "token", ""))
        if token and runtime.token_usable(principal, token):
            actions.append(_token_action("adopt_token", label, principal, "openevent", token))
            return token, "adopted"

    token = runtime.add_token(principal)
    if not runtime.token_usable(principal, token):
        raise ApplyError(f"created token is not usable for principal {principal}")
    actions.append(_token_action("create_token", label, principal, "openevent", token))
    return token, "created"


def _resolve_channels(
    spec: DesiredSpec,
    runtime: OpenEventRuntime,
    tokens: dict[str, str],
    im_provider_session_ids: dict[str, str],
    previous: dict[str, Any] | None,
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
        state_ids = _state_channel_ids(previous, session.session_id)
        provider_session_id = im_provider_session_ids[session.session_id]
        im_id = _resolve_channel(
            spec=spec,
            runtime=runtime,
            visible_channels=visible_channels,
            operator_principal=agent_bot_principal,
            operator_token=agent_bot_token,
            principal_tokens=principal_tokens,
            previous_id=state_ids.get("im"),
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
            previous_id=state_ids.get("model"),
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
            previous_id=state_ids.get("wal"),
            explicit_id=session.wal_channel_id,
            name=session.wal_name,
            protocol=PROTOCOL_WAL,
            visibility=visibility,
            description=_wal_description(spec, session, im_id, model_id),
            required_members=[spec.agent_principal],
            match=lambda desc: _matches_wal_description(desc, spec, session, im_id, model_id),
            actions=actions,
        )
        result[session.session_id] = ChannelIds(im=im_id, model=model_id, wal=wal_id)
    return result


def _resolve_channel(
    *,
    spec: DesiredSpec,
    runtime: OpenEventRuntime,
    visible_channels: list[Any],
    operator_principal: int,
    operator_token: str,
    principal_tokens: dict[int, str],
    previous_id: int | None,
    explicit_id: int | None,
    name: str,
    protocol: str,
    visibility: int,
    description: dict[str, Any],
    required_members: list[int],
    match,
    actions: list[dict[str, Any]],
) -> int:
    for source, channel_id in (("state", previous_id), ("input", explicit_id)):
        if channel_id is None:
            continue
        channel = runtime.get_channel(operator_principal, operator_token, channel_id)
        if channel and _usable_channel(
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
            actions.append(_channel_action("reuse_channel", source, protocol, channel_id, name))
            return int(channel.channel_id)
        actions.append(_channel_action("ignore_channel", source, protocol, channel_id, name))

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


def _principal_tokens(
    spec: DesiredSpec,
    tokens: dict[str, str],
) -> dict[int, str]:
    return {spec.principals[key]: tokens[key] for key in _required_principal_refs(spec)}


def _input_token_for_ref(spec: DesiredSpec, key: str) -> str | None:
    if token := spec.tokens.get(key):
        return token
    for user in spec.im_users:
        if user.principal_ref == key and user.user_token:
            return user.user_token
    return None


def _secret_token(secrets: dict[str, Any] | None, key: str) -> str | None:
    if not secrets:
        return None
    value = ((secrets.get("tokens") or {}).get(key) or {}).get("token")
    return value if isinstance(value, str) and value else None


def _token_state(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, Any]:
    return {
        key: {
            "principal": spec.principals[key],
            "source": resolved.token_sources[key],
            "token_sha256": _sha256(resolved.tokens[key].encode("utf-8")),
        }
        for key in _required_principal_refs(spec)
    }


def _im_state(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, Any]:
    return {
        "provider": spec.im_provider,
        "users": {
            user.principal: {
                "external_id_source": resolved.im_user_external_id_sources[user.principal],
                "external_id_sha256": _sha256(
                    resolved.im_user_external_ids[user.principal].encode("utf-8")
                ),
            }
            for user in spec.im_users
        },
        "sessions": {
            session.session_id: {
                "provider": spec.im_provider,
                "provider_session_id": resolved.im_provider_session_ids[session.session_id],
                "provider_session_id_source": resolved.im_provider_session_id_sources[session.session_id],
                "provider_session_id_sha256": _sha256(
                    resolved.im_provider_session_ids[session.session_id].encode("utf-8")
                ),
            }
            for session in spec.sessions
            if session.enabled
        },
    }


def _secrets_state(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, Any]:
    return {
        "version": "v1",
        "tokens": {
            key: {"principal": spec.principals[key], "token": resolved.tokens[key]}
            for key in _required_principal_refs(spec)
        },
    }


def _default_im_api_base_url(provider: str) -> str:
    if provider in IM_PROVIDER_API_BASE_URLS:
        return IM_PROVIDER_API_BASE_URLS[provider]
    raise SpecError(f"im.provider must be one of {', '.join(sorted(IM_PROVIDERS))}")


def _validate_spec(spec: DesiredSpec) -> None:
    if spec.im_provider not in IM_PROVIDERS:
        raise SpecError(f"im.provider must be one of {', '.join(sorted(IM_PROVIDERS))}")
    expected_api_base_url = _default_im_api_base_url(spec.im_provider)
    if spec.bot_api_base_url != expected_api_base_url:
        raise SpecError(f"im.bot.api_base_url is managed by im.provider; expected {expected_api_base_url}")
    if spec.im_session_type != "p2p":
        raise SpecError("im.session_type currently must be p2p")
    if spec.channel_visibility not in VISIBILITY_VALUES:
        raise SpecError("channels.visibility must be public, protected, or private")
    if spec.channel_visibility == "public":
        raise SpecError("channels.visibility=public is not allowed because llm.v1 channels must not be public")
    explicit_ids = []
    for session in spec.sessions:
        if not session.enabled:
            continue
        explicit_ids.extend(
            value
            for value in (session.im_channel_id, session.model_channel_id, session.wal_channel_id)
            if value is not None
        )
    _validate_unique(explicit_ids, "channel ids")
    if spec.agent_principal != spec.im_bot_principal:
        raise SpecError("agent.principal must reference the same principal as im.bot.principal")
    if spec.im_worker_principal == spec.im_bot_principal:
        raise SpecError("im.worker_principal must not equal im.bot.principal")
    if spec.model_proxy_principal == spec.agent_principal:
        raise SpecError("model.proxy_principal must not equal agent.principal")
    if spec.model_proxy_principal == spec.im_worker_principal:
        raise SpecError("model.proxy_principal must not equal im.worker_principal")
    for user in spec.im_users:
        if user.principal == spec.im_bot_principal:
            raise SpecError("im.users[].principal must not equal im.bot.principal")
        if user.principal == spec.im_worker_principal:
            raise SpecError("im.users[].principal must not equal im.worker_principal")
        if user.principal == spec.model_proxy_principal:
            raise SpecError("im.users[].principal must not equal model.proxy_principal")


def _validate_resolved(
    spec: DesiredSpec,
    tokens: dict[str, str],
    im_user_external_ids: dict[int, str],
    im_provider_session_ids: dict[str, str],
    channels: dict[str, ChannelIds],
) -> None:
    for user in spec.im_users:
        if user.principal_ref not in tokens:
            raise ApplyError(f"missing resolved token for principal {user.principal_ref}")
        if user.principal not in im_user_external_ids:
            raise ApplyError(f"missing resolved IM user external_id for principal {user.principal}")
    enabled_sessions = [session for session in spec.sessions if session.enabled]
    for session in enabled_sessions:
        if session.session_id not in channels:
            raise ApplyError(f"missing resolved channels for session {session.session_id}")
    _validate_unique([channels[session.session_id].im for session in enabled_sessions], "resolved im_channel_id")
    _validate_unique([channels[session.session_id].model for session in enabled_sessions], "resolved model_channel_id")
    _validate_unique([channels[session.session_id].wal for session in enabled_sessions], "resolved wal_channel_id")
    _validate_unique([im_provider_session_ids[session.session_id] for session in enabled_sessions], "resolved IM provider session_id")


def _validate_unique(values: list[Any], name: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise SpecError(f"duplicate {name}: {value}")
        seen.add(value)


def _token_action(action: str, label: str, principal: int, source: str, token: str) -> dict[str, Any]:
    return {
        "action": action,
        "label": label,
        "principal": principal,
        "source": source,
        "token_sha256": _sha256(token.encode("utf-8")),
    }


def _channel_action(action: str, source: str, protocol: str, channel_id: int, name: str) -> dict[str, Any]:
    return {
        "action": action,
        "source": source,
        "protocol": protocol,
        "channel_id": channel_id,
        "name": name,
    }


def _json_obj(value: str) -> dict[str, Any] | None:
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _stable_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique_ints(values: list[int]) -> list[int]:
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        value = int(value)
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _ensure_project_path() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    path = str(repo_root)
    if path not in sys.path:
        sys.path.insert(0, path)


def _dump_yaml(data: Any) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=True)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        file.write(content)
        tmp_name = file.name
    os.chmod(tmp_name, mode)
    os.replace(tmp_name, path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _obj(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SpecError(f"{name} must be a mapping")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise SpecError(f"{name} must be a non-empty array")
    return value


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise SpecError(f"{name} must be a non-empty string")
    return value


def _path_str(value: Any, name: str, base: Path) -> Path:
    raw = _str(value, name)
    path = Path(raw)
    return path if path.is_absolute() else base / path


def _optional_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _str(value, name)


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise SpecError(f"{name} must be a boolean")
    return value


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SpecError(f"{name} must be a positive integer")
    return value


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, name)


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SpecError(f"{name} must be a non-negative integer")
    return value


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReconcileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)
