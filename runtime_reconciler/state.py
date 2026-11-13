from __future__ import annotations
import json
import sys
from pathlib import Path
import yaml
from .common import _atomic_write, _dump_yaml, _sha256
from .model import ApplyError, STATE_VERSION

INITIALIZATION_FAILURE_MESSAGE = (
    "deployment initialization did not complete; automatic resume is unavailable. "
    "Existing processes, data and resources are preserved and require operator review; "
    "see the Runtime initialization-failure guidance before deciding what to keep or rebuild"
)

def load_state(path: Path, *, expected_version=STATE_VERSION, spec=None):
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != expected_version:
        raise ApplyError(f"unsupported state format in {path.name}; old IM state is not migrated")
    if spec and data.get("deployment_id") != spec.deployment_id:
        raise ApplyError("runtime state belongs to another deployment")
    if spec and data.get("principals") != spec.principals:
        raise ApplyError("deployment principals cannot change in an initialized runtime")
    if spec and data.get("work_root") != spec.agent["work_root"]:
        raise ApplyError("work_root cannot change in an initialized deployment")
    if spec and data.get("supervisor") != {"ctl": spec.supervisor_ctl, "programs": spec.supervisor_programs}:
        raise ApplyError("runtime state is missing its supervisor binding or supervisor.ctl/programs changed; use a new deployment")
    if data.get("initialized") and (not isinstance(data.get("channels"), dict) or set(data["channels"]) != {"model", "cmd", "wal"}):
        raise ApplyError("initialized deployment is missing its permanent Channel binding")
    return data

def save_state(spec, state):
    state = {**state, "version": STATE_VERSION, "deployment_id": spec.deployment_id, "principals": spec.principals,
             "work_root": spec.agent["work_root"],
             "supervisor": {"ctl": spec.supervisor_ctl, "programs": spec.supervisor_programs}}
    if "configs" in state:
        state["configs"] = {key: {"path": item["path"], "sha256": item["sha256"]}
                            for key, item in state["configs"].items()}
    _atomic_write(spec.paths.state_path, _dump_yaml(state), 0o600)

def require_completed_initialization(state):
    if state is not None and not state.get("initialized"):
        raise ApplyError(INITIALIZATION_FAILURE_MESSAGE)

def begin_apply(spec, pending=()):
    state = load_state(spec.paths.state_path, spec=spec)
    require_completed_initialization(state)
    if not spec.paths.state_path.exists() and spec.paths.secrets_path.exists():
        raise ApplyError("existing deployment is missing its runtime state; refusing to initialize new bindings")
    state = state or {}
    # Until every generated file is committed, even the old spec cannot
    # safely start a possibly mixed set of configuration files.
    state.pop("applied_spec_sha256", None)
    state["pending_restarts"] = sorted(set(state.get("pending_restarts", ())) | set(pending))
    save_state(spec, state)

def add_pending_restarts(spec, pending):
    state = load_state(spec.paths.state_path, spec=spec) or {}
    state["pending_restarts"] = sorted(set(state.get("pending_restarts", ())) | set(pending))
    save_state(spec, state)

def complete_start(spec):
    state = load_state(spec.paths.state_path, spec=spec) or {}
    state["initialized"] = True
    state["pending_restarts"] = []
    save_state(spec, state)

def _config_content(name, data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n" if name.endswith(".json") else _dump_yaml(data)

def config_record(spec, key, name, data, previous):
    digest = _sha256(_config_content(name, data).encode())
    return {"path": str(spec.paths.config_dir / name), "sha256": digest,
            "changed": (key in previous.get("pending_restarts", ())
                        or previous.get("configs", {}).get(key, {}).get("sha256") != digest)}

def _write_config(spec, name, data):
    path = spec.paths.config_dir / name
    content = _config_content(name, data)
    changed = not path.exists() or path.read_text(encoding="utf-8") != content
    if changed:
        _atomic_write(path, content, 0o600)
    return {"path": str(path), "sha256": _sha256(content.encode()), "changed": changed}

def write_openevent_config(spec):
    from .render import render_openevent_config
    Path(spec.openevent["storage_path"]).mkdir(parents=True, exist_ok=True)
    return _write_config(spec, "openevent-server.yaml", render_openevent_config(spec))

def prepare_runtime_files(spec, resolved):
    from .render import render_configs, validate_configs
    configs = render_configs(spec, resolved)
    validate_configs(configs)
    previous = load_state(spec.paths.state_path, spec=spec) or {}
    plan = {"actions": resolved.actions, "configs": {
        key: config_record(spec, key, item["path"], item["data"], previous)
        for key, item in configs.items()}}
    plan["configs"]["cmd_worker"]["changed"] |= previous.get("cmd_environment") != {
        "object_target": spec.agent["object_target"]}
    return configs, plan

def write_runtime_files(spec, resolved, dry_run, *, prepared=None, completed=()):
    from .render import render_configs
    plan = {"actions": resolved.actions, "configs": {}}
    if dry_run:
        configs = render_configs(spec, resolved)
        channels = {kind: channel_id for kind, channel_id in resolved.channels.as_dict().items()
                    if channel_id is not None}
        if channels:
            plan["channels"] = channels
        plan["notes"] = ["OpenEvent access tokens will be reused or created automatically during --apply."]
        for key, item in configs.items():
            plan["configs"][key] = {"path": str(spec.paths.config_dir / item["path"]), "would_write": True}
        return plan
    configs, plan = prepared or prepare_runtime_files(spec, resolved)
    for key, item in configs.items():
        _write_config(spec, item["path"], item["data"])
        if key in completed:
            plan["configs"][key]["changed"] = False
    environment = {"object_target": spec.agent["object_target"]}
    state = load_state(spec.paths.state_path, spec=spec) or {}
    pending = (set(state.get("pending_restarts", ()))
               | {key for key, item in plan["configs"].items() if item["changed"]}) - set(completed)
    state.pop("chat_launch", None)
    state.update({"channels": resolved.channels.as_dict(), "configs": plan["configs"],
                  "cmd_environment": environment,
                  "applied_spec_sha256": spec.fingerprint(),
                  "pending_restarts": sorted(pending)})
    save_state(spec, state)
    (spec.paths.config_dir / "chat-launch.json").unlink(missing_ok=True)
    return plan


def write_diagnostics(spec, plan):
    """Export optional snapshots after a successful apply; never used for recovery."""
    for path, data in ((spec.paths.desired_path, spec.normalized()), (spec.paths.plan_path, plan)):
        try:
            _atomic_write(path, _dump_yaml(data), 0o600)
        except OSError as exc:
            print(f"warning: could not write diagnostic snapshot {path}: {exc}", file=sys.stderr)
