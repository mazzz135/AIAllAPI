from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request

from .errors import OpenAIError
from .runtime import AppState

log = logging.getLogger("aiall.deps")

SESSION_COOKIE = "aiall_session"
LEGACY_SESSION_COOKIE = "traeapi_session"


def state(request: Request) -> AppState:
    return request.app.state.ctx


async def admin_from_request(request: Request) -> Optional[dict[str, Any]]:
    token = request.cookies.get(SESSION_COOKIE, "") or request.cookies.get(LEGACY_SESSION_COOKIE, "")
    return state(request).admin_auth.get_session(token)


async def require_admin(request: Request) -> dict[str, Any]:
    admin = await admin_from_request(request)
    if not admin:
        raise AdminRedirect()
    return admin


class AdminRedirect(Exception):
    pass


async def api_key_from_request(request: Request) -> Optional[dict[str, Any]]:
    ctx = state(request)
    if ctx.db.get_setting("allow_any_key", "0") == "1":
        return {"id": 0, "name": "any", "plaintext": ""}
    auth = request.headers.get("authorization", "")
    key = ""
    if auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    if not key:
        key = request.headers.get("x-api-key", "").strip()
    return ctx.api_keys.verify(key)


async def require_api_key(request: Request) -> dict[str, Any]:
    key = await api_key_from_request(request)
    if not key:
        raise OpenAIError(
            "无效的 API Key，请在请求头使用 Authorization: Bearer <key> 或 X-API-Key",
            status_code=401,
            error_type="authentication_error",
            code="invalid_api_key",
        )
    return key


def client_ip(request: Request, trust_proxy: bool = True) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""
