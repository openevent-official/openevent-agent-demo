from __future__ import annotations
from chat_model_agent.rpc import RETRY_INTERVAL_MS

def validate_openevent_config(config):
    """Validate the generated fields against OpenEvent's public config contract."""
    from .model import SpecError
    for section, field in (("grpc", "listen_addr"), ("admin", "listen_addr"), ("storage", "path")):
        value = config[section][field]
        if not isinstance(value, str) or not value.strip():
            raise SpecError(f"OpenEvent {section}.{field} must be non-empty")
    if config["grpc"]["listen_addr"] == config["admin"]["listen_addr"]:
        raise SpecError("OpenEvent business and admin listen addresses must differ")
    limit = config["limits"]["max_payload_bytes"]
    if type(limit) is not int or not 1 <= limit <= 62914560:
        raise SpecError("OpenEvent max_payload_bytes must be in 1..62914560")

def render_openevent_config(spec):
    return {"grpc": {"listen_addr": spec.openevent["grpc_addr"]}, "admin": {"listen_addr": spec.openevent["admin_addr"]},
            "storage": {"path": spec.openevent["storage_path"]}, "limits": {"max_payload_bytes": spec.openevent["max_payload_bytes"]}}

def render_configs(spec, resolved):
    p, t, channels, addr = spec.principals, resolved.tokens, resolved.channels, spec.openevent["grpc_addr"]
    agent = {key: value for key, value in spec.agent.items() if key != "object_target"}
    return {
        "openevent": {"path": "openevent-server.yaml", "data": render_openevent_config(spec)},
        "chat_server": {"path": "chat-server.json", "data": {
            "host": spec.chat["host"], "port": spec.chat["port"], "origin": spec.chat.get("origin"),
            "openevent_target": addr, "channels_dir": spec.chat["channels_dir"], "web_token": spec.chat["web_token"],
            "user_principal": str(p["user"]), "user_openevent_token": t["user"], "agent_principal": str(p["agent"]),
            "rpc_timeout_ms": spec.rpc["timeout_ms"], "max_retries": spec.rpc["max_retries"],
            "max_user_message_bytes": spec.chat["max_user_message_bytes"],
            "max_payload_bytes": spec.openevent["max_payload_bytes"],
        }},
        "model_proxy": {"path": "model-proxy.yaml", "data": {
            "protocol": "llm.v1", "open_event": {"addr": addr, "rpc_timeout_ms": spec.rpc["timeout_ms"]},
            "worker": {"max_concurrency": spec.model["max_concurrency"], "max_retries": spec.rpc["max_retries"], "retry_interval_ms": RETRY_INTERVAL_MS},
            "principal": p["model_proxy"], "token": t["model_proxy"], "channels": [channels.model],
            "max_payload_bytes": spec.openevent["max_payload_bytes"],
            "default_provider": spec.model["provider_name"],
            "providers": {spec.model["provider_name"]: {
                "type": "openai_compatible", "base_url": spec.model["base_url"], "api_key": spec.model["api_key"],
                "timeout": {"response_header_ms": spec.model["response_header_ms"], "idle_ms": spec.model["idle_ms"]},
            }},
        }},
        "cmd_worker": {"path": "cmd-worker.yaml", "data": {
            "open_event": {"addr": addr, "max_payload_bytes": spec.openevent["max_payload_bytes"]},
            "principal": p["cmd_worker"], "token": t["cmd_worker"], "channel_ids": [channels.cmd],
            "output_dir": spec.cmd["output_dir"], "max_concurrent_tasks": spec.cmd["max_concurrent_tasks"],
            "default_timeout_ms": spec.cmd["default_timeout_ms"],
        }},
        "agent": {"path": "chat-model-agent.json", "data": {
            "version": "chat-agent.v1", "deployment_id": spec.deployment_id,
            "openevent": {"target": addr, "max_payload_bytes": spec.openevent["max_payload_bytes"]},
            "agent": {**agent, "principal": p["agent"], "token": t["agent"], "user_principal": p["user"],
                      "model": spec.model["model"], "model_provider": spec.model["provider_name"]},
            "model_proxy": {"principal": p["model_proxy"]}, "cmd_worker": {"principal": p["cmd_worker"]},
            "channels": channels.as_dict(), "rpc": spec.rpc,
        }},
        "view": {"path": "openevent-view.yaml", "data": {
            "version": "v1", "server": {"host": "0.0.0.0", "port": spec.view["port"]},
            "openevent": {"target": addr},
        }},
    }

def validate_configs(configs):
    from chat_model_agent.config import parse_config as parse_agent
    from openevent.model_proxy.config import parse_config as parse_model
    from openevent.cmd_worker.config import parse_config as parse_cmd
    import json
    import tempfile
    from pathlib import Path
    from openevent.chat_app.config import ServerConfig
    from openevent.view.config import parse_config as parse_view
    validate_openevent_config(configs["openevent"]["data"])
    parse_agent(configs["agent"]["data"])
    parse_model(configs["model_proxy"]["data"])
    parse_cmd(configs["cmd_worker"]["data"])
    temporary_root = Path.cwd() / "build" / "validation"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        path = Path(directory) / "chat.json"
        path.write_text(json.dumps(configs["chat_server"]["data"]), encoding="utf-8")
        path.chmod(0o600)
        ServerConfig.load(path)
    parse_view(configs["view"]["data"])
