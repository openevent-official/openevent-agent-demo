from __future__ import annotations

import time
from typing import Any

from .model import ApplyError, DesiredSpec


class OpenEventRuntime:
    def __init__(self, grpc_addr: str, admin_addr: str):
        from openevent.sdk import AdminClient, OpenEventClient

        self.event = OpenEventClient(grpc_addr)
        self.admin = AdminClient(admin_addr)

    def token_usable(self, principal: int, token: str) -> bool:
        try:
            self.event.get_status(principal, token)
            return True
        except Exception:
            return False

    def list_tokens(self) -> list[Any]:
        return list(self.admin.list_tokens().bindings)

    def add_token(self, principal: int) -> str:
        return str(self.admin.add_token(principal).binding.token)

    def list_channels(self, principal: int, token: str) -> list[Any]:
        return list(self.event.list_channels(principal, token).channels)

    def get_channel(self, principal: int, token: str, channel_id: int) -> Any | None:
        try:
            return self.event.get_channel(principal, token, channel_id).channel
        except Exception:
            return None

    def create_channel(
        self,
        principal: int,
        token: str,
        *,
        name: str,
        visibility: int,
        protocol: str,
        description: str,
        members: list[int],
    ) -> Any:
        return self.event.create_channel(
            principal=principal,
            token=token,
            name=name,
            visibility=visibility,
            protocol=protocol,
            description=description,
            members=members,
        ).channel

    def add_member(self, principal: int, token: str, channel_id: int, target_principal: int) -> None:
        self.event.add_member(principal, token, channel_id, target_principal)


def wait_openevent_ready(spec: DesiredSpec, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            import grpc

            grpc.channel_ready_future(grpc.insecure_channel(spec.openevent_grpc_addr)).result(timeout=1)
            grpc.channel_ready_future(grpc.insecure_channel(spec.openevent_admin_addr)).result(timeout=1)
            runtime = OpenEventRuntime(spec.openevent_grpc_addr, spec.openevent_admin_addr)
            runtime.list_tokens()
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise ApplyError(f"OpenEvent admin endpoint is not ready: {last_error}")
