from __future__ import annotations

from dataclasses import fields, is_dataclass
from inspect import signature
from typing import Any


class RuntimeDependencyError(RuntimeError):
    pass


def validate_runtime_dependencies() -> None:
    try:
        from openevent.cmd_sdk import CmdOutputReadRequestInput, CmdRunRequestInput
    except Exception as exc:  # pragma: no cover - exercised by shell preflight
        raise RuntimeDependencyError(f"openevent.cmd_sdk is not importable: {exc}") from exc

    missing: list[str] = []
    for cls in (CmdRunRequestInput, CmdOutputReadRequestInput):
        if not _has_request_id(cls):
            missing.append(cls.__name__)
    if missing:
        raise RuntimeDependencyError(
            "incompatible openevent-modules-cmd installation: "
            + ", ".join(missing)
            + " must accept request_id. Reinstall openevent-modules-cmd from the current design before starting agent-demo."
        )


def _has_request_id(cls: Any) -> bool:
    if is_dataclass(cls):
        return any(field.name == "request_id" for field in fields(cls))
    try:
        return "request_id" in signature(cls).parameters
    except (TypeError, ValueError):
        return False
