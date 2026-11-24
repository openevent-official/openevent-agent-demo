from __future__ import annotations
from pathlib import Path
import yaml
from .model import DesiredSpec, RuntimePaths, SPEC_VERSION, PROCESS_START_ORDER, SpecError

def _obj(value, name):
    if not isinstance(value, dict):
        raise SpecError(f"{name} must be a mapping")
    return dict(value)

def _str(value, name):
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{name} must be a non-empty string")
    return value

def _integer(value, name, minimum=1):
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise SpecError(f"{name} must be an integer >= {minimum}")
    return value

def _fields(value, allowed, name):
    extra = set(value) - set(allowed)
    if extra:
        raise SpecError(f"unknown {name} fields: " + ", ".join(sorted(extra)))

def parse_spec(path: Path, runtime_root_override=None):
    path = path.resolve()
    base_dir = path.parent
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise SpecError(f"invalid YAML in {path.name}{location}") from None
    raw = _obj(raw, "spec")
    if raw.get("version") != SPEC_VERSION:
        raise SpecError(f"version must be {SPEC_VERSION}")
    allowed = {"version", "runtime", "openevent", "principals", "tokens", "chat", "model", "cmd", "agent", "rpc", "channels", "view"}
    if set(raw) - allowed:
        raise SpecError("unknown spec fields: " + ", ".join(sorted(set(raw) - allowed)))
    runtime = _obj(raw.get("runtime"), "runtime")
    _fields(runtime, {"name", "root", "supervisor"}, "runtime")
    deployment_id = _str(runtime.get("name"), "runtime.name")
    root = Path(runtime_root_override or runtime.get("root") or f"runtime/{deployment_id}")
    root = (base_dir / root).resolve() if not root.is_absolute() else root.resolve()
    paths = RuntimePaths(root)
    def abs_path(value, default):
        p = Path(value or default)
        return str((base_dir / p).resolve() if not p.is_absolute() else p.resolve())
    supervisor = _obj(runtime.get("supervisor", {}), "runtime.supervisor")
    _fields(supervisor, {"ctl", "programs"}, "runtime.supervisor")
    defaults = {"openevent": "openevent", "model_proxy": "model-proxy", "cmd_worker": "cmd-worker", "chat_server": "chat-server", "agent": "chat-model-agent", "view": "openevent-view"}
    programs = {**defaults, **_obj(supervisor.get("programs", {}), "runtime.supervisor.programs")}
    if set(programs) != set(PROCESS_START_ORDER):
        raise SpecError("supervisor programs must contain only Chat deployment components")
    programs = {k: _str(v, f"programs.{k}") for k, v in programs.items()}
    principals = _obj(raw.get("principals"), "principals")
    if set(principals) != {"user", "agent", "model_proxy", "cmd_worker"}:
        raise SpecError("principals must contain user, agent, model_proxy and cmd_worker")
    principals = {k: _integer(v, f"principals.{k}") for k, v in principals.items()}
    if len(set(principals.values())) != 4 or max(principals.values()) > 2**64 - 1:
        raise SpecError("principals must be distinct positive uint64 values")
    tokens = _obj(raw.get("tokens", {}), "tokens")
    if set(tokens) - set(principals):
        raise SpecError("tokens must use configured principal names")
    tokens = {k: _str(v, f"tokens.{k}") for k, v in tokens.items()}
    event = _obj(raw.get("openevent"), "openevent")
    _fields(event, {"grpc_addr", "admin_addr", "max_payload_bytes", "storage"}, "openevent")
    _fields(_obj(event.get("storage", {}), "openevent.storage"), {"path"}, "openevent.storage")
    event = {"grpc_addr": _str(event.get("grpc_addr"), "openevent.grpc_addr"),
             "admin_addr": _str(event.get("admin_addr"), "openevent.admin_addr"),
             "max_payload_bytes": _integer(event.get("max_payload_bytes", 16 * 1024 * 1024), "openevent.max_payload_bytes"),
             "storage_path": abs_path(_obj(event.get("storage", {}), "openevent.storage").get("path"), root / "data/openevent")}
    rpc = {"max_retries": 3, "timeout_ms": 10000, **_obj(raw.get("rpc", {}), "rpc")}
    if set(rpc) != {"max_retries", "timeout_ms"}:
        raise SpecError("unknown rpc field")
    for k, v in rpc.items():
        _integer(v, f"rpc.{k}", 0 if k == "max_retries" else 1)
    chat = {"host": "0.0.0.0", "port": 8081, "max_user_message_bytes": 65536,
            **_obj(raw.get("chat"), "chat")}
    _fields(chat, {"host", "port", "web_token", "channels_dir", "origin", "max_user_message_bytes"}, "chat")
    chat["web_token"] = _str(chat.get("web_token"), "chat.web_token")
    chat["channels_dir"] = abs_path(chat.get("channels_dir"), root / "chat-sessions")
    _str(chat["host"], "chat.host")
    _integer(chat["port"], "chat.port")
    _integer(chat["max_user_message_bytes"], "chat.max_user_message_bytes")
    model = {"provider_name": "default", "response_header_ms": 60000, "idle_ms": 60000, "max_concurrency": 4, **_obj(raw.get("model"), "model")}
    _fields(model, {"provider_name", "response_header_ms", "idle_ms", "max_concurrency", "base_url", "api_key", "model"}, "model")
    for k in ("provider_name", "base_url", "api_key", "model"):
        _str(model.get(k), f"model.{k}")
    for k in ("response_header_ms", "idle_ms", "max_concurrency"):
        _integer(model[k], f"model.{k}")
    cmd = {"max_concurrent_tasks": 4, "default_timeout_ms": 300000, **_obj(raw.get("cmd", {}), "cmd")}
    _fields(cmd, {"max_concurrent_tasks", "default_timeout_ms", "output_dir"}, "cmd")
    cmd["output_dir"] = abs_path(cmd.get("output_dir"), root / "cmd-output")
    for k in ("max_concurrent_tasks", "default_timeout_ms"):
        _integer(cmd[k], f"cmd.{k}")
    agent = {"model_timeout_ms": 60000, "model_retry_interval_ms": 1000,
             "max_model_attempts": 3, "max_model_concurrency": 4,
             "cmd_result_timeout_ms": 330000, "max_tool_output_bytes": 32768, "output_read_chunk_bytes": 32768,
             "idle_sleep_ms": 50,
             **_obj(raw.get("agent", {}), "agent")}
    agent_fields = {"system_prompt_file", "context_compression_prompt_file", "context_max_input_bytes",
                    "model_timeout_ms", "model_retry_interval_ms", "max_model_attempts", "max_model_concurrency", "cmd_result_timeout_ms",
                    "max_tool_output_bytes", "output_read_chunk_bytes", "idle_sleep_ms",
                    "work_root", "object_target"}
    if set(agent) - agent_fields:
        raise SpecError("unknown agent fields: " + ", ".join(sorted(set(agent) - agent_fields)))
    for name in ("system_prompt", "context_compression_prompt"):
        field = f"{name}_file"
        source = Path(abs_path(_str(agent.pop(field, None), f"agent.{field}"), ""))
        try:
            # Decode bytes directly so even CRLF and trailing newlines survive.
            agent[name] = source.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise SpecError(f"cannot read UTF-8 agent.{field}: {source}") from exc
    if not agent["context_compression_prompt"].strip():
        raise SpecError("agent.context_compression_prompt_file must contain non-whitespace text")
    for k in ("context_max_input_bytes", "model_timeout_ms", "model_retry_interval_ms", "max_model_attempts", "max_model_concurrency", "cmd_result_timeout_ms", "max_tool_output_bytes", "output_read_chunk_bytes", "idle_sleep_ms"):
        _integer(agent.get(k), f"agent.{k}")
    agent["work_root"] = abs_path(agent.get("work_root"), root / "workdir")
    agent["object_target"] = _str(agent.get("object_target", event["grpc_addr"]), "agent.object_target")
    from chat_model_agent.budget import BudgetError, ContextBudget
    try:
        ContextBudget.from_settings(model=model["model"], system_prompt=agent["system_prompt"],
                                    context_compression_prompt=agent["context_compression_prompt"],
                                    context_max_input_bytes=agent["context_max_input_bytes"])
    except BudgetError as exc:
        raise SpecError(f"agent context budget: {exc}") from exc
    channels = _obj(raw.get("channels", {}), "channels")
    if set(channels) - {"model", "cmd", "wal"}:
        raise SpecError("channels only accepts model, cmd and wal")
    channels = {k: _integer(v, f"channels.{k}") for k, v in channels.items() if v != "auto"}
    if any(value > 2**64 - 1 for value in channels.values()) or len(set(channels.values())) != len(channels):
        raise SpecError("channels must be distinct positive uint64 values")
    view = {"port": 9999, **_obj(raw.get("view", {}), "view")}
    _fields(view, {"port"}, "view")
    _integer(view["port"], "view.port")
    if chat["port"] > 65535 or view["port"] > 65535:
        raise SpecError("listen ports must be in 1..65535")
    ctl = _str(supervisor.get("ctl", "supervisorctl"), "supervisor.ctl")
    if "/" in ctl:
        ctl = abs_path(ctl, ctl)
    return DesiredSpec(deployment_id, paths, ctl, programs,
                       principals, tokens, event, chat, model, cmd, agent, rpc, view, channels)
