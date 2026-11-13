from __future__ import annotations
import time
from typing import Any
import grpc
from .model import ApplyError

class OpenEventRuntime:
    def __init__(self, grpc_addr: str, admin_addr: str, timeout_ms=10000):
        from chat_model_agent.dependencies import require_sdk
        require_sdk()
        from openevent.sdk import AdminClient, OpenEventClient
        from openevent.sdk.proto import admin_pb2
        if "token" not in admin_pb2.AddTokenResponse.DESCRIPTOR.fields_by_name:
            raise ApplyError("installed openevent-sdk>=0.13.0 with AddTokenResponse.token is required")
        self.event = OpenEventClient(grpc_addr, timeout_ms=timeout_ms)
        self.admin = AdminClient(admin_addr, timeout_ms=timeout_ms)

    def token_usable(self, principal: int, token: str) -> bool:
        try:
            self.event.get_status(principal, token)
            return True
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
                return False
            raise ApplyError("token validation RPC failed; refusing to create a replacement token") from exc

    def add_token(self, principal: int) -> str:
        try:
            return str(self.admin.add_token(principal).token)
        except grpc.RpcError as exc:
            raise ApplyError("AddToken RPC failed") from exc

    def list_channels(self, principal: int, token: str) -> list[Any]:
        from openevent.sdk.proto import openevent_pb2
        try:
            return list(self.event.list_channels(principal, token, filter=openevent_pb2.CHANNEL_FILTER_JOINED).channels)
        except grpc.RpcError as exc:
            raise ApplyError("ListChannels RPC failed") from exc

    def get_channel(self, principal: int, token: str, channel_id: int):
        try:
            return self.event.get_channel(principal, token, channel_id).channel
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return None
            raise ApplyError("GetChannel RPC failed") from exc

    def create_channel(self, principal, token, **kwargs):
        try:
            return self.event.create_channel(principal=principal, token=token, **kwargs).channel
        except grpc.RpcError as exc:
            raise ApplyError("CreateChannel RPC failed") from exc

def wait_openevent_ready(spec, timeout_s=20.0):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            for address in (spec.openevent["grpc_addr"], spec.openevent["admin_addr"]):
                with grpc.insecure_channel(address) as channel:
                    grpc.channel_ready_future(channel).result(timeout=min(1, max(.01, deadline - time.monotonic())))
            return
        except grpc.FutureTimeoutError as exc:
            if time.monotonic() >= deadline:
                raise ApplyError("OpenEvent business/admin endpoints did not become ready") from exc
