from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .common import _sha256, _stable_json

SPEC_VERSION = "chat-runtime.v1"
STATE_VERSION = "chat-runtime.state.v1"
SECRETS_VERSION = "chat-runtime.secrets.v1"
PROTOCOL_MODEL, PROTOCOL_CMD, PROTOCOL_WAL = "llm.v1", "cmd.v3", "agent.wal.v5"
PROCESS_START_ORDER = ("openevent", "model_proxy", "chat_server", "view", "cmd_worker", "agent")

class ReconcileError(RuntimeError):
    exit_code = 1

class SpecError(ReconcileError):
    exit_code = 2

class ApplyError(ReconcileError):
    exit_code = 3

@dataclass(frozen=True)
class RuntimePaths:
    runtime_root: Path

    @property
    def config_dir(self):
        return self.runtime_root / "config"

    @property
    def state_path(self):
        return self.config_dir / "state.yaml"

    @property
    def desired_path(self):
        return self.config_dir / "desired.normalized.yaml"

    @property
    def plan_path(self):
        return self.config_dir / "plan.yaml"

    @property
    def secrets_path(self):
        return self.config_dir / "secrets.yaml"

@dataclass(frozen=True)
class DesiredSpec:
    deployment_id: str
    paths: RuntimePaths
    supervisor_ctl: str
    supervisor_programs: dict[str, str]
    principals: dict[str, int]
    tokens: dict[str, str]
    openevent: dict[str, Any]
    chat: dict[str, Any]
    model: dict[str, Any]
    cmd: dict[str, Any]
    agent: dict[str, Any]
    rpc: dict[str, Any]
    view: dict[str, Any]
    channels: dict[str, int]

    def normalized(self, *, redact=True):
        data = {
            "version": SPEC_VERSION, "deployment_id": self.deployment_id,
            "runtime_root": str(self.paths.runtime_root), "principals": self.principals,
            "supervisor": {"ctl": self.supervisor_ctl, "programs": self.supervisor_programs},
            "tokens": self.tokens,
            "openevent": self.openevent, "agent": self.agent, "cmd": self.cmd,
            "chat": self.chat, "model": self.model,
            "rpc": self.rpc, "view": self.view, "channels": self.channels,
        }
        if redact:
            data["chat"] = {**self.chat, "web_token": "<redacted>"}
            data["model"] = {**self.model, "api_key": "<redacted>"}
            data["tokens"] = {name: "<redacted>" for name in self.tokens}
        return data

    def fingerprint(self):
        return _sha256(_stable_json(self.normalized(redact=False)).encode("utf-8"))

@dataclass(frozen=True)
class ChannelIds:
    model: int | None
    cmd: int | None
    wal: int | None

    def as_dict(self):
        return {"model": self.model, "cmd": self.cmd, "wal": self.wal}

@dataclass
class ResolvedRuntime:
    tokens: dict[str, str | None]
    channels: ChannelIds
    actions: list[dict[str, Any]] = field(default_factory=list)
