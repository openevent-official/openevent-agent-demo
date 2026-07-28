from __future__ import annotations

from typing import Any

from .channels import _resolve_channels, _validate_channel_binding
from .common import _sha256
from .model import (
    ApplyError,
    ChannelIds,
    DesiredSpec,
    ResolvedRuntime,
    SessionSpec,
    SpecError,
    _input_token_for_ref,
    _required_principal_refs,
)
from .openevent import OpenEventRuntime
from .provider import LarkOpenAPIP2PResolver, LarkOpenAPIUserResolver
from .spec import _validate_unique
from .state import _state_channel_binding, _state_channel_ids, load_state


def dry_resolve(spec: DesiredSpec, previous: dict[str, Any] | None = None) -> ResolvedRuntime:
    bindings = _session_channel_bindings(spec, previous)
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
            channels[session.session_id] = bindings.get(session.session_id) or ChannelIds(
                im=session.im_channel_id or 10000 + index,
                model=session.model_channel_id or 20000 + index,
                wal=session.wal_channel_id or 30000 + index,
                cmd=session.cmd_channel_id or 40000 + index,
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
    bindings = _session_channel_bindings(spec, previous)
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
        bindings,
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


def _session_channel_bindings(
    spec: DesiredSpec,
    previous: dict[str, Any] | None,
) -> dict[str, ChannelIds]:
    result: dict[str, ChannelIds] = {}
    for session in spec.sessions:
        if not session.enabled:
            continue
        binding = _state_channel_binding(previous, session.session_id)
        if binding is None:
            continue
        _validate_channel_binding(session, binding)
        result[session.session_id] = binding
    return result


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


def _secret_token(secrets: dict[str, Any] | None, key: str) -> str | None:
    if not secrets:
        return None
    value = ((secrets.get("tokens") or {}).get(key) or {}).get("token")
    return value if isinstance(value, str) and value else None


def _token_action(action: str, label: str, principal: int, source: str, token: str) -> dict[str, Any]:
    return {
        "action": action,
        "label": label,
        "principal": principal,
        "source": source,
        "token_sha256": _sha256(token.encode("utf-8")),
    }


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
    _validate_unique([channels[session.session_id].cmd for session in enabled_sessions], "resolved cmd_channel_id")
    _validate_unique([im_provider_session_ids[session.session_id] for session in enabled_sessions], "resolved IM provider session_id")
