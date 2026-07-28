from __future__ import annotations


class RuntimeDependencyError(RuntimeError):
    pass


def validate_runtime_dependencies() -> None:
    try:
        from openevent.cmd_sdk import CmdOutputReadRequestInput, CmdRunRequestInput
    except Exception as exc:  # pragma: no cover - exercised by shell preflight
        raise RuntimeDependencyError(f"openevent.cmd_sdk is not importable: {exc}") from exc

    try:
        CmdRunRequestInput(command="true", ts_ms=1)
        CmdOutputReadRequestInput(target_seq=1, stream="stdout", offset=0, nbytes=1, ts_ms=1)
    except TypeError as exc:
        raise RuntimeDependencyError(
            "incompatible openevent-modules-cmd installation: cmd.v1 request inputs must use OpenEvent seq identity without request_id"
        ) from exc
