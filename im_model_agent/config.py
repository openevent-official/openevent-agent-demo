from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_AGENT_NAME = "im-model-agent"
DEFAULT_CONTEXT_MESSAGES = 20
DEFAULT_MODEL_TIMEOUT_MS = 60000
DEFAULT_MAX_MODEL_ATTEMPTS = 3
DEFAULT_CMD_RESULT_TIMEOUT_MS = 330000
DEFAULT_NON_TEXT_PLACEHOLDER = "[non-text message]"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    name: str
    principal: int
    token: str
    system_prompt: str
    max_context_messages: int
    model: str
    model_timeout_ms: int
    max_model_attempts: int
    cmd_result_timeout_ms: int
    non_text_placeholder: str


@dataclass(frozen=True)
class SubscribeConfig:
    only_my_recipient: bool
    idle_sleep_ms: int


@dataclass(frozen=True)
class OpenEventConfig:
    target: str
    subscribe: SubscribeConfig


@dataclass(frozen=True)
class PrincipalConfig:
    principal: int


@dataclass(frozen=True)
class CmdWorkerConfig:
    principal: int


@dataclass(frozen=True)
class SessionConfig:
    session_id: str
    im_channel_id: int
    model_channel_id: int
    wal_channel_id: int
    cmd_channel_id: int
    user_principal: int
    enabled: bool


@dataclass(frozen=True)
class AgentRuntimeConfig:
    version: str
    agent: AgentConfig
    openevent: OpenEventConfig
    model_proxy: PrincipalConfig
    cmd_worker: CmdWorkerConfig
    im_sync_worker: PrincipalConfig
    sessions: tuple[SessionConfig, ...]

    @property
    def enabled_sessions(self) -> tuple[SessionConfig, ...]:
        return tuple(session for session in self.sessions if session.enabled)


def load_config(path: str | Path) -> AgentRuntimeConfig:
    with Path(path).open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file)
    return parse_config(raw)


def parse_config(raw: Any) -> AgentRuntimeConfig:
    data = _obj(raw, "config")
    version = _str(data.get("version"), "version")
    if version != "v1":
        raise ConfigError("version must be v1")

    agent_raw = _obj(data.get("agent"), "agent")
    agent = AgentConfig(
        name=_str(agent_raw.get("name", DEFAULT_AGENT_NAME), "agent.name"),
        principal=_positive_int(agent_raw.get("principal"), "agent.principal"),
        token=_str(agent_raw.get("token"), "agent.token"),
        system_prompt=_str(agent_raw.get("system_prompt"), "agent.system_prompt"),
        max_context_messages=_positive_int(
            agent_raw.get("max_context_messages", DEFAULT_CONTEXT_MESSAGES),
            "agent.max_context_messages",
        ),
        model=_str(agent_raw.get("model"), "agent.model"),
        model_timeout_ms=_positive_int(
            agent_raw.get("model_timeout_ms", DEFAULT_MODEL_TIMEOUT_MS),
            "agent.model_timeout_ms",
        ),
        max_model_attempts=_positive_int(
            agent_raw.get("max_model_attempts", DEFAULT_MAX_MODEL_ATTEMPTS),
            "agent.max_model_attempts",
        ),
        cmd_result_timeout_ms=_positive_int(
            agent_raw.get("cmd_result_timeout_ms", DEFAULT_CMD_RESULT_TIMEOUT_MS),
            "agent.cmd_result_timeout_ms",
        ),
        non_text_placeholder=_str(
            agent_raw.get("non_text_placeholder", DEFAULT_NON_TEXT_PLACEHOLDER),
            "agent.non_text_placeholder",
        ),
    )

    openevent_raw = _obj(data.get("openevent"), "openevent")
    subscribe_raw = _obj(openevent_raw.get("subscribe", {}), "openevent.subscribe")
    if "from_seq" in subscribe_raw:
        raise ConfigError("openevent.subscribe.from_seq is not supported; recovery manages the resume seq")
    openevent = OpenEventConfig(
        target=_str(openevent_raw.get("target"), "openevent.target"),
        subscribe=SubscribeConfig(
            only_my_recipient=_bool(
                subscribe_raw.get("only_my_recipient", False),
                "openevent.subscribe.only_my_recipient",
            ),
            idle_sleep_ms=_non_negative_int(
                subscribe_raw.get("idle_sleep_ms", 200),
                "openevent.subscribe.idle_sleep_ms",
            ),
        ),
    )

    model_proxy = PrincipalConfig(
        principal=_positive_int(_obj(data.get("model_proxy"), "model_proxy").get("principal"), "model_proxy.principal")
    )
    cmd_worker = CmdWorkerConfig(
        principal=_positive_int(_obj(data.get("cmd_worker"), "cmd_worker").get("principal"), "cmd_worker.principal")
    )
    im_sync_worker = PrincipalConfig(
        principal=_positive_int(
            _obj(data.get("im_sync_worker"), "im_sync_worker").get("principal"),
            "im_sync_worker.principal",
        )
    )
    sessions = tuple(_parse_session(item, index) for index, item in enumerate(_list(data.get("sessions"), "sessions")))
    if not any(session.enabled for session in sessions):
        raise ConfigError("sessions must contain at least one enabled session")
    _validate_unique(sessions, "session_id", lambda item: item.session_id)
    _validate_unique(sessions, "im_channel_id", lambda item: item.im_channel_id)
    _validate_unique(sessions, "model_channel_id", lambda item: item.model_channel_id)
    _validate_unique(sessions, "wal_channel_id", lambda item: item.wal_channel_id)
    _validate_unique(sessions, "cmd_channel_id", lambda item: item.cmd_channel_id)
    for session in sessions:
        if session.user_principal == agent.principal:
            raise ConfigError("sessions[].user_principal must not equal agent.principal")

    return AgentRuntimeConfig(
        version=version,
        agent=agent,
        openevent=openevent,
        model_proxy=model_proxy,
        cmd_worker=cmd_worker,
        im_sync_worker=im_sync_worker,
        sessions=sessions,
    )


def _parse_session(raw: Any, index: int) -> SessionConfig:
    data = _obj(raw, f"sessions[{index}]")
    return SessionConfig(
        session_id=_str(data.get("session_id"), f"sessions[{index}].session_id"),
        im_channel_id=_positive_int(data.get("im_channel_id"), f"sessions[{index}].im_channel_id"),
        model_channel_id=_positive_int(data.get("model_channel_id"), f"sessions[{index}].model_channel_id"),
        wal_channel_id=_positive_int(data.get("wal_channel_id"), f"sessions[{index}].wal_channel_id"),
        cmd_channel_id=_positive_int(data.get("cmd_channel_id"), f"sessions[{index}].cmd_channel_id"),
        user_principal=_positive_int(data.get("user_principal"), f"sessions[{index}].user_principal"),
        enabled=_bool(data.get("enabled", True), f"sessions[{index}].enabled"),
    )


def _validate_unique(items: tuple[SessionConfig, ...], name: str, getter) -> None:
    seen: set[Any] = set()
    for item in items:
        value = getter(item)
        if value in seen:
            raise ConfigError(f"duplicate sessions[].{name}")
        seen.add(value)


def _obj(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty array")
    return value


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{name} must be a non-negative integer")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value
