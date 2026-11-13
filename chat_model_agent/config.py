"""The single configuration format for the Chat Agent."""

from dataclasses import dataclass, field
import json
from pathlib import Path


MAX_UINT64 = (1 << 64) - 1


class ConfigError(ValueError):
    """Invalid configuration; messages never include configuration values."""


@dataclass(frozen=True)
class OpenEventConfig:
    target: str
    max_payload_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True)
class PrincipalConfig:
    principal: int


@dataclass(frozen=True)
class ChannelsConfig:
    model: int
    cmd: int
    wal: int


@dataclass(frozen=True)
class RpcConfig:
    max_retries: int = 3
    timeout_ms: int = 10000


@dataclass(frozen=True)
class AgentConfig:
    principal: int
    token: str = field(repr=False)
    user_principal: int
    system_prompt: str
    context_compression_prompt: str
    context_max_input_bytes: int
    model: str
    work_root: Path
    model_provider: str | None = None
    model_timeout_ms: int = 60000
    max_model_attempts: int = 3
    max_model_concurrency: int = 4
    cmd_result_timeout_ms: int = 330000
    max_tool_output_bytes: int = 32768
    output_read_chunk_bytes: int = 32768
    idle_sleep_ms: int = 50
    model_retry_interval_ms: int = 1000


@dataclass(frozen=True)
class AgentRuntimeConfig:
    version: str
    deployment_id: str
    openevent: OpenEventConfig
    agent: AgentConfig
    model_proxy: PrincipalConfig
    cmd_worker: PrincipalConfig
    channels: ChannelsConfig
    rpc: RpcConfig


def _mapping(value, name, allowed, required=()):
    if type(value) is not dict or value.keys() - allowed or set(required) - value.keys():
        raise ConfigError(f"invalid {name} fields")
    return value


def _string(value, name, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ConfigError(f"{name} must be a string" + ("" if empty else " with content"))
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ConfigError(f"{name} must be UTF-8") from None
    return value


def _integer(value, name, *, minimum=1, maximum=MAX_UINT64):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{name} is outside its integer range")
    return value


def _path(value, name):
    result = Path(_string(value, name))
    if not result.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    return result


def parse_config(data) -> AgentRuntimeConfig:
    root_fields = set(AgentRuntimeConfig.__dataclass_fields__)
    root = _mapping(data, "configuration", root_fields, root_fields - {"rpc"})
    if root["version"] != "chat-agent.v1":
        raise ConfigError("version must be chat-agent.v1")
    event = _mapping(root["openevent"], "openevent", {"target", "max_payload_bytes"}, {"target"})
    principal_configs = []
    for name in ("model_proxy", "cmd_worker"):
        value = _mapping(root[name], name, {"principal"}, {"principal"})
        principal_configs.append(PrincipalConfig(_integer(value["principal"], f"{name}.principal")))
    channels = _mapping(root["channels"], "channels", {"model", "cmd", "wal"}, {"model", "cmd", "wal"})
    channels = ChannelsConfig(**{key: _integer(value, f"channels.{key}") for key, value in channels.items()})
    if len({channels.model, channels.cmd, channels.wal}) != 3:
        raise ConfigError("channels must be distinct")
    rpc = _mapping(root.get("rpc", {}), "rpc", set(RpcConfig.__dataclass_fields__))
    rpc = RpcConfig(**{key: _integer(value, f"rpc.{key}", minimum=0 if key == "max_retries" else 1)
                       for key, value in rpc.items()})
    required = {"principal", "token", "user_principal", "system_prompt", "context_compression_prompt",
                "context_max_input_bytes", "model", "work_root"}
    agent = dict(_mapping(root["agent"], "agent", set(AgentConfig.__dataclass_fields__), required))
    string_fields = {"token", "system_prompt", "context_compression_prompt", "model"}
    paths = {"work_root"}
    for name, value in agent.items():
        label = f"agent.{name}"
        if name in string_fields:
            agent[name] = _string(value, label, empty=name == "system_prompt")
        elif name == "model_provider":
            agent[name] = None if value is None else _string(value, label)
        elif name in paths:
            agent[name] = _path(value, label)
        else:
            agent[name] = _integer(value, label)
    agent = AgentConfig(**agent)
    if agent.principal == agent.user_principal:
        raise ConfigError("agent and user principals must be distinct")
    return AgentRuntimeConfig(
        version=root["version"], deployment_id=_string(root["deployment_id"], "deployment_id"),
        openevent=OpenEventConfig(
            _string(event["target"], "openevent.target"),
            _integer(event.get("max_payload_bytes", OpenEventConfig.max_payload_bytes),
                     "openevent.max_payload_bytes", maximum=60 * 1024 * 1024)),
        agent=agent, model_proxy=principal_configs[0], cmd_worker=principal_configs[1],
        channels=channels, rpc=rpc,
    )


def load_config(path) -> AgentRuntimeConfig:
    def unique_pairs(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ConfigError("duplicate configuration field")
            result[name] = value
        return result

    try:
        with Path(path).open(encoding="utf-8") as stream:
            data = json.load(stream, object_pairs_hook=unique_pairs)
        return parse_config(data)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigError("could not read a valid JSON configuration") from None
