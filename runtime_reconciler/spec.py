from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .model import (
    DEFAULT_AGENT_CMD_RESULT_TIMEOUT_MS,
    DEFAULT_AGENT_MODEL_TIMEOUT_MS,
    DEFAULT_MAX_PAYLOAD_BYTES,
    DEFAULT_MODEL_TIMEOUT_MS,
    IM_PROVIDERS,
    IM_PROVIDER_API_BASE_URLS,
    PROCESS_START_ORDER,
    DesiredSpec,
    ImUserSpec,
    RuntimePaths,
    SessionSpec,
    SpecError,
    VISIBILITY_VALUES,
)


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
    for key in PROCESS_START_ORDER:
        _str(programs.get(key), f"runtime.supervisor.programs.{key}")

    openevent = _obj(raw.get("openevent"), "openevent")
    openevent_storage = _obj(openevent.get("storage"), "openevent.storage")
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
    cmd = _obj(raw.get("cmd"), "cmd")
    cmd_worker_principal_ref, cmd_worker_principal = _principal_ref(
        cmd.get("worker_principal"),
        "cmd.worker_principal",
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
    _validate_unique([session.cmd_name for session in sessions if session.enabled], "sessions[].channels.cmd")

    spec = DesiredSpec(
        raw=raw,
        runtime_name=runtime_name,
        paths=RuntimePaths(repo_root=repo_root, runtime_root=runtime_root),
        supervisor_ctl=_str(supervisor.get("ctl", "supervisorctl"), "runtime.supervisor.ctl"),
        supervisor_programs={key: str(programs[key]) for key in PROCESS_START_ORDER},
        openevent_grpc_addr=_str(openevent.get("grpc_addr"), "openevent.grpc_addr"),
        openevent_admin_addr=_str(openevent.get("admin_addr"), "openevent.admin_addr"),
        openevent_max_payload_bytes=_positive_int(
            openevent.get("max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES),
            "openevent.max_payload_bytes",
        ),
        openevent_storage_path=_path_str(
            openevent_storage.get("path"),
            "openevent.storage.path",
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
            "history_retry_delay_ms": _non_negative_int(
                _obj(im.get("sync", {}), "im.sync").get("history_retry_delay_ms", 1000),
                "im.sync.history_retry_delay_ms",
            ),
            "history_overlap_ms": _non_negative_int(
                _obj(im.get("sync", {}), "im.sync").get("history_overlap_ms", 300000),
                "im.sync.history_overlap_ms",
            ),
            "history_lookback_ms": _positive_int(
                _obj(im.get("sync", {}), "im.sync").get("history_lookback_ms", 300000),
                "im.sync.history_lookback_ms",
            ),
            "page_size": _positive_int(_obj(im.get("sync", {}), "im.sync").get("page_size", 50), "im.sync.page_size"),
            "event_queue_size": _positive_int(
                _obj(im.get("sync", {}), "im.sync").get("event_queue_size", 1000),
                "im.sync.event_queue_size",
            ),
        },
        model_proxy_principal_ref=model_proxy_principal_ref,
        model_proxy_principal=model_proxy_principal,
        model_provider_name=_str(model.get("provider_name", "openai_main"), "model.provider_name"),
        model_base_url=_str(model.get("base_url"), "model.base_url").rstrip("/"),
        model_api_key=_str(model.get("api_key"), "model.api_key"),
        model_model=_str(model.get("model"), "model.model"),
        model_timeout_ms=_positive_int(model.get("timeout_ms", DEFAULT_MODEL_TIMEOUT_MS), "model.timeout_ms"),
        cmd_worker_principal_ref=cmd_worker_principal_ref,
        cmd_worker_principal=cmd_worker_principal,
        cmd_output_dir=_path_str(cmd.get("output_dir", str(runtime_root / "data/cmd-worker-output")), "cmd.output_dir", repo_root),
        cmd_max_concurrent_tasks=_positive_int(cmd.get("max_concurrent_tasks", 8), "cmd.max_concurrent_tasks"),
        cmd_default_timeout_ms=_positive_int(cmd.get("default_timeout_ms", 300000), "cmd.default_timeout_ms"),
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
        agent_cmd_result_timeout_ms=_positive_int(
            agent.get("cmd_result_timeout_ms", DEFAULT_AGENT_CMD_RESULT_TIMEOUT_MS),
            "agent.cmd_result_timeout_ms",
        ),
        channel_visibility=_str(_obj(raw.get("channels", {}), "channels").get("visibility", "private"), "channels.visibility"),
        sessions=sessions,
    )
    _validate_spec(spec)
    return spec


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
        cmd_name=_str(channels.get("cmd", f"{runtime_name}.cmd.{session_id}"), f"sessions[{index}].channels.cmd"),
        im_channel_id=_optional_positive_int(ids.get("im"), f"sessions[{index}].channel_ids.im"),
        model_channel_id=_optional_positive_int(ids.get("model"), f"sessions[{index}].channel_ids.model"),
        wal_channel_id=_optional_positive_int(ids.get("wal"), f"sessions[{index}].channel_ids.wal"),
        cmd_channel_id=_optional_positive_int(ids.get("cmd"), f"sessions[{index}].channel_ids.cmd"),
    )


def _principal_ref(raw: Any, field: str, principals: dict[str, int]) -> tuple[str, int]:
    ref = _str(raw, field)
    if ref not in principals:
        raise SpecError(f"{field} must reference a key in principals")
    return ref, principals[ref]


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
            for value in (session.im_channel_id, session.model_channel_id, session.wal_channel_id, session.cmd_channel_id)
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
    if spec.cmd_worker_principal == spec.agent_principal:
        raise SpecError("cmd.worker_principal must not equal agent.principal")
    if spec.cmd_worker_principal == spec.im_worker_principal:
        raise SpecError("cmd.worker_principal must not equal im.worker_principal")
    if spec.cmd_worker_principal == spec.model_proxy_principal:
        raise SpecError("cmd.worker_principal must not equal model.proxy_principal")
    for user in spec.im_users:
        if user.principal == spec.im_bot_principal:
            raise SpecError("im.users[].principal must not equal im.bot.principal")
        if user.principal == spec.im_worker_principal:
            raise SpecError("im.users[].principal must not equal im.worker_principal")
        if user.principal == spec.model_proxy_principal:
            raise SpecError("im.users[].principal must not equal model.proxy_principal")
        if user.principal == spec.cmd_worker_principal:
            raise SpecError("im.users[].principal must not equal cmd.worker_principal")


def _validate_unique(values: list[Any], name: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise SpecError(f"duplicate {name}: {value}")
        seen.add(value)


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
