from __future__ import annotations
from .common import _atomic_write, _dump_yaml
from .model import ApplyError, ChannelIds, ResolvedRuntime, SECRETS_VERSION
from .openevent import OpenEventRuntime
from .state import load_state
from .channels import _resolve_channels

def dry_resolve(spec, previous=None):
    previous = previous or {}
    bound = previous.get("channels", {})
    for kind, supplied in spec.channels.items():
        if kind in bound and supplied != bound[kind]:
            raise ApplyError(f"{kind} Channel conflicts with the permanent binding")
    ids = {kind: bound.get(kind, spec.channels.get(kind))
           for kind in ("model", "cmd", "wal")}
    known = [value for value in ids.values() if value is not None]
    if len(set(known)) != len(known):
        raise ApplyError("auxiliary Channel IDs must be distinct")
    return ResolvedRuntime(
        {kind: spec.tokens.get(kind) for kind in spec.principals},
        ChannelIds(**ids), [{"action": "preview", "detail": "no RPCs or runtime files written"}],
    )

def apply_resolve(spec, previous=None):
    runtime = OpenEventRuntime(spec.openevent["grpc_addr"], spec.openevent["admin_addr"], spec.rpc["timeout_ms"])
    secrets = load_state(spec.paths.secrets_path, expected_version=SECRETS_VERSION) or {"version": SECRETS_VERSION, "tokens": {}}
    def save_token(name, principal, token):
        entry = {"principal": principal, "token": token}
        saved = secrets.setdefault("tokens", {})
        if saved.get(name) != entry:
            saved[name] = entry
            _atomic_write(spec.paths.secrets_path, _dump_yaml(secrets), 0o600)
    tokens = {}
    for name, principal in spec.principals.items():
        saved = secrets.get("tokens", {}).get(name, {})
        local_token = saved.get("token") if saved.get("principal") == principal else None
        for token in (spec.tokens.get(name), local_token):
            if token and runtime.token_usable(principal, token):
                break
        else:
            token = runtime.add_token(principal)
        save_token(name, principal, token)
        tokens[name] = token
    channels = _resolve_channels(spec, runtime, tokens, previous)
    return ResolvedRuntime(tokens, channels)
