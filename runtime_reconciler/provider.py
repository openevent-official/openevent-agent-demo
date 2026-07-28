from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .model import ApplyError


class LarkOpenAPIClient:
    def __init__(self, *, provider: str, app_id: str, app_secret: str, api_base_url: str):
        self._provider = provider
        self._app_id = app_id
        self._app_secret = app_secret
        self._api_base_url = api_base_url.rstrip("/")
        self._tenant_access_token: str | None = None

    def _tenant_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        response = self._post_json(
            "/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self._app_id, "app_secret": self._app_secret},
        )
        token = response.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise ApplyError(f"{self._provider} tenant_access_token response missing tenant_access_token")
        self._tenant_access_token = token
        return token

    def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        query: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._api_base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                **(headers or {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApplyError(f"{self._provider} API {path} failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise ApplyError(f"{self._provider} API {path} failed: {exc.reason}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApplyError(f"{self._provider} API {path} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ApplyError(f"{self._provider} API {path} returned non-object JSON")
        code = data.get("code")
        if code not in (None, 0):
            raise ApplyError(f"{self._provider} API {path} failed: {code} {data.get('msg')}")
        return data


class LarkOpenAPIP2PResolver(LarkOpenAPIClient):
    def chat_id_for_user(self, open_id: str) -> str:
        response = self._post_json(
            "/open-apis/im/v1/chat_p2p/batch_query",
            {"chatter_ids": [open_id]},
            query={"chatter_id_type": "open_id"},
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ApplyError(f"{self._provider} chat_p2p batch_query response missing data")
        chats = data.get("p2p_chats")
        if not isinstance(chats, list):
            raise ApplyError(f"{self._provider} chat_p2p batch_query response missing data.p2p_chats")
        matches = []
        for item in chats:
            if not isinstance(item, dict):
                continue
            chat_id = item.get("chat_id")
            if isinstance(chat_id, str) and chat_id:
                matches.append(chat_id)
        if not matches:
            raise ApplyError(f"{self._provider} did not return a P2P chat_id for open_id {open_id}")
        if len(set(matches)) > 1:
            raise ApplyError(f"{self._provider} returned multiple P2P chat_ids for open_id {open_id}")
        return matches[0]


class LarkOpenAPIUserResolver(LarkOpenAPIClient):
    def open_id_by_phone(self, phone: str) -> str:
        return self._open_id_by_contact_key("mobiles", "mobile", phone, "phone")

    def open_id_by_email(self, email: str) -> str:
        return self._open_id_by_contact_key("emails", "email", email, "email")

    def _open_id_by_contact_key(
        self,
        request_key: str,
        response_key: str,
        value: str,
        label: str,
    ) -> str:
        response = self._post_json(
            "/open-apis/contact/v3/users/batch_get_id",
            {request_key: [value]},
            query={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
        )
        data = response.get("data")
        if not isinstance(data, dict):
            raise ApplyError(f"{self._provider} batch_get_id response missing data")
        user_list = data.get("user_list")
        if not isinstance(user_list, list):
            raise ApplyError(f"{self._provider} batch_get_id response missing data.user_list")

        matches = []
        for item in user_list:
            if not isinstance(item, dict):
                continue
            if item.get(response_key) == value:
                user_id = item.get("user_id")
                if isinstance(user_id, str) and user_id:
                    matches.append(user_id)
        if not matches:
            raise ApplyError(f"{self._provider} did not return an open_id for {label} {value}")
        if len(set(matches)) > 1:
            raise ApplyError(f"{self._provider} returned multiple open_ids for {label} {value}")
        return matches[0]
