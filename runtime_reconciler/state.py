from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

from .common import _atomic_write, _dump_yaml, _sha256
from .model import ChannelIds, DesiredSpec, ResolvedRuntime, _required_principal_refs
from .render import _validate_openevent_config, render_configs, render_openevent_config, validate_configs


APPLY_PHASES = (
    "parsed",
    "openevent_ready",
    "resources_resolved",
    "config_committed",
    "processes_running",
)
APPLY_STATUSES = {"in_progress", "complete", "failed"}


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def _state_channel_ids(state: dict[str, Any] | None, session_id: str) -> dict[str, int]:
    if not state:
        return {}
    raw = (((state.get("channels") or {}).get("sessions") or {}).get(session_id) or {})
    result: dict[str, int] = {}
    for key in ("im", "model", "wal", "cmd"):
        item = raw.get(key)
        if not isinstance(item, dict):
            return {}
        value = item.get("channel_id")
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return {}
        result[key] = value
    return result


def _state_channel_binding(state: dict[str, Any] | None, session_id: str) -> ChannelIds | None:
    ids = _state_channel_ids(state, session_id)
    return ChannelIds(**ids) if ids else None


def begin_apply(spec: DesiredSpec) -> None:
    spec.paths.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    spec.paths.config_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(spec.paths.state_path) or {
        "version": "v1",
        "runtime_name": spec.runtime_name,
    }
    _write_apply_state(spec, state, phase="parsed", status="in_progress", reset=True)


def record_apply_phase(
    spec: DesiredSpec,
    phase: str,
    status: str = "in_progress",
    *,
    failed_phase: str | None = None,
) -> None:
    state = load_state(spec.paths.state_path) or {
        "version": "v1",
        "runtime_name": spec.runtime_name,
    }
    _write_apply_state(
        spec,
        state,
        phase=phase,
        status=status,
        failed_phase=failed_phase,
    )


def write_openevent_config(spec: DesiredSpec) -> dict[str, Any]:
    spec.paths.runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    spec.paths.config_dir.mkdir(parents=True, exist_ok=True)
    spec.openevent_storage_path.mkdir(parents=True, exist_ok=True)
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
    spec.openevent_storage_path.mkdir(parents=True, exist_ok=True)
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
    previous_state = load_state(spec.paths.state_path) or {}
    apply_state = _next_apply_state(
        previous_state.get("last_apply"),
        phase="config_committed",
        status="in_progress",
    )
    state = {
        "version": "v1",
        "runtime_name": spec.runtime_name,
        "last_apply": apply_state,
        "configs": plan["configs"],
        "tokens": _token_state(spec, resolved),
        "channels": _channel_state(previous_state, resolved),
    }
    state["im"] = _im_state(spec, resolved)
    _atomic_write(spec.paths.secrets_path, _dump_yaml(_secrets_state(spec, resolved)), 0o600)
    _atomic_write(spec.paths.plan_path, _dump_yaml(plan), 0o600)
    _atomic_write(spec.paths.state_path, _dump_yaml(state), 0o600)
    return plan


def _write_apply_state(
    spec: DesiredSpec,
    state: dict[str, Any],
    *,
    phase: str,
    status: str,
    reset: bool = False,
    failed_phase: str | None = None,
) -> None:
    previous = None if reset else state.get("last_apply")
    apply_state = _next_apply_state(
        previous,
        phase=phase,
        status=status,
        failed_phase=failed_phase,
    )
    state["version"] = "v1"
    state["runtime_name"] = spec.runtime_name
    state["last_apply"] = apply_state
    state.pop("last_apply_status", None)
    state.pop("last_apply_ms", None)
    _atomic_write(spec.paths.state_path, _dump_yaml(state), 0o600)


def _next_apply_state(
    previous: Any,
    *,
    phase: str,
    status: str,
    failed_phase: str | None = None,
) -> dict[str, Any]:
    if phase not in APPLY_PHASES:
        raise ValueError(f"unknown apply phase: {phase}")
    if status not in APPLY_STATUSES:
        raise ValueError(f"unknown apply status: {status}")
    if failed_phase is not None and failed_phase not in APPLY_PHASES:
        raise ValueError(f"unknown failed apply phase: {failed_phase}")
    now_ms = int(time.time() * 1000)
    previous = previous if isinstance(previous, dict) else {}
    result = {
        "status": status,
        "phase": phase,
        "started_at_ms": previous.get("started_at_ms", now_ms),
        "updated_at_ms": now_ms,
    }
    if failed_phase is not None:
        result["failed_phase"] = failed_phase
    if status in {"complete", "failed"}:
        result["completed_at_ms"] = now_ms
    return result


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


def _channel_state(previous: dict[str, Any], resolved: ResolvedRuntime) -> dict[str, Any]:
    bindings: dict[str, ChannelIds] = {}
    previous_sessions = ((previous.get("channels") or {}).get("sessions") or {})
    if isinstance(previous_sessions, dict):
        for session_id in previous_sessions:
            if isinstance(session_id, str) and (binding := _state_channel_binding(previous, session_id)):
                bindings[session_id] = binding
    bindings.update(resolved.channels)
    return {
        "sessions": {
            session_id: {
                "im": {"channel_id": ids.im},
                "model": {"channel_id": ids.model},
                "wal": {"channel_id": ids.wal},
                "cmd": {"channel_id": ids.cmd},
            }
            for session_id, ids in bindings.items()
        }
    }


def _secrets_state(spec: DesiredSpec, resolved: ResolvedRuntime) -> dict[str, Any]:
    return {
        "version": "v1",
        "tokens": {
            key: {"principal": spec.principals[key], "token": resolved.tokens[key]}
            for key in _required_principal_refs(spec)
        },
    }
