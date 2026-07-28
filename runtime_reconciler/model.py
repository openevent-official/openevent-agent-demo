from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
DEFAULT_MODEL_TIMEOUT_MS = 65000
DEFAULT_AGENT_MODEL_TIMEOUT_MS = 60000
DEFAULT_AGENT_CMD_RESULT_TIMEOUT_MS = 330000
PROTOCOL_IM = "im.v1"
PROTOCOL_MODEL = "llm.v1"
PROTOCOL_WAL = "agent.wal.v1"
PROTOCOL_CMD = "cmd.v1"
VISIBILITY_VALUES = {"public": 0, "protected": 1, "private": 2}
IM_PROVIDERS = {"feishu", "lark"}
IM_PROVIDER_API_BASE_URLS = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
PROCESS_START_ORDER = (
    "openevent",
    "model_proxy",
    "cmd_worker",
    "im_syncer",
    "agent",
)


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
    cmd_name: str
    im_channel_id: int | None = None
    model_channel_id: int | None = None
    wal_channel_id: int | None = None
    cmd_channel_id: int | None = None


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
    openevent_storage_path: Path
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
    cmd_worker_principal_ref: str
    cmd_worker_principal: int
    cmd_output_dir: Path
    cmd_max_concurrent_tasks: int
    cmd_default_timeout_ms: int
    agent_principal_ref: str
    agent_principal: int
    agent_name: str
    agent_system_prompt: str
    agent_max_context_messages: int
    agent_model_timeout_ms: int
    agent_max_model_attempts: int
    agent_cmd_result_timeout_ms: int
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
                "storage": {"path": str(self.openevent_storage_path)},
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
            "cmd": {
                "worker_principal": self.cmd_worker_principal_ref,
                "resolved_worker_principal": self.cmd_worker_principal,
                "output_dir": str(self.cmd_output_dir),
                "max_concurrent_tasks": self.cmd_max_concurrent_tasks,
                "default_timeout_ms": self.cmd_default_timeout_ms,
            },
            "agent": {
                "name": self.agent_name,
                "principal": self.agent_principal_ref,
                "resolved_principal": self.agent_principal,
                "system_prompt": self.agent_system_prompt,
                "max_context_messages": self.agent_max_context_messages,
                "model_timeout_ms": self.agent_model_timeout_ms,
                "max_model_attempts": self.agent_max_model_attempts,
                "cmd_result_timeout_ms": self.agent_cmd_result_timeout_ms,
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
                        "cmd": session.cmd_name,
                    },
                    "channel_ids": {
                        "im": session.im_channel_id,
                        "model": session.model_channel_id,
                        "wal": session.wal_channel_id,
                        "cmd": session.cmd_channel_id,
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
    cmd: int


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


def _required_principal_refs(spec: DesiredSpec) -> tuple[str, ...]:
    refs = [
        spec.im_worker_principal_ref,
        spec.im_bot_principal_ref,
        spec.model_proxy_principal_ref,
        spec.cmd_worker_principal_ref,
        spec.agent_principal_ref,
    ]
    refs.extend(user.principal_ref for user in spec.im_users)
    return tuple(dict.fromkeys(refs))


def _input_token_for_ref(spec: DesiredSpec, key: str) -> str | None:
    if token := spec.tokens.get(key):
        return token
    for user in spec.im_users:
        if user.principal_ref == key and user.user_token:
            return user.user_token
    return None
