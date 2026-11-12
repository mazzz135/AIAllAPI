from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote, urlencode

import httpx

from ..config import Settings
from ..credit_packs import make_pack, normalize_packs, to_epoch
from .base import ProviderError, StreamChunk, empty_usage, merge_usage

COSY_VERSION = "1.1.75"
COSY_USER_AGENT = "undici"
COSY_MACHINE_OS = "x86_64_linux"
COSY_CLIENT_IP = "169.254.198.161"
DEVICE_CLIENT_ID = "732aef47-9cf2-46a2-95fe-4cebb5d0d1fa"

STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
CUSTOM_ALPHABET = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"

COSY_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc
4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l
6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17
XcW+ML9FoCI6AOvOzwIDAQAB
-----END PUBLIC KEY-----"""


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _parse_expiry(value: Any) -> int:
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 1_000_000_000_000 else number
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.isdigit():
            return _parse_expiry(int(raw))
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except Exception:
            return 0
    return 0


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _compact(fields: list[tuple[str, str]]) -> bytes:
    return json.dumps({k: v for k, v in fields}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_body(plain: bytes) -> str:
    std = _b64e(plain)
    n = len(std)
    a = n // 3
    rearranged = std[n - a :] + std[a : n - a] + std[:a]
    table = str.maketrans(STD_ALPHABET, CUSTOM_ALPHABET)
    return rearranged.translate(table).replace("=", "$")


def decode_body(encoded: str) -> bytes:
    table = str.maketrans(CUSTOM_ALPHABET, STD_ALPHABET)
    std = encoded.replace("$", "=").translate(table)
    n = len(std)
    a = n // 3
    original = std[n - a :] + std[a : n - a] + std[:a]
    return base64.b64decode(original)


def _machine_id(uid: str) -> str:
    return hashlib.md5(("machine:" + uid).encode()).hexdigest()


def _machine_token(uid: str) -> str:
    digest = hashlib.sha512(("machinetoken:" + uid).encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")[:43]


def _machine_type(uid: str) -> str:
    return hashlib.md5(("machinetype:" + uid).encode()).hexdigest().replace("-", "")[:18]


def _pkcs7_pad(data: bytes, size: int = 16) -> bytes:
    pad = size - len(data) % size
    return data + bytes([pad]) * pad


def _rsa_encrypt(plain: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_public_key(COSY_PUBLIC_KEY.encode())
    return key.encrypt(plain, padding.PKCS1v15())


def _aes_cbc_encrypt(plain: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return encryptor.update(_pkcs7_pad(plain)) + encryptor.finalize()


def build_session(cred: dict[str, Any]) -> dict[str, Any]:
    uid = str(cred.get("uid") or cred.get("aid") or "")
    token = str(cred.get("security_oauth_token") or cred.get("access_token") or "")
    if not uid or not token:
        raise ProviderError("Qoder 账号缺少 uid / access_token", retryable=False)
    identity = _compact(
        [
            ("aid", str(cred.get("aid") or uid)),
            ("name", str(cred.get("name") or "")),
            ("organization_id", str(cred.get("organization_id") or "")),
            ("organization_name", str(cred.get("organization_name") or "")),
            ("refresh_token", str(cred.get("refresh_token") or "")),
            ("security_oauth_token", token),
            ("uid", uid),
            ("user_type", str(cred.get("user_type") or "")),
            ("yx_uid", str(cred.get("yx_uid") or "")),
        ]
    )
    temp_key = os.urandom(8).hex().encode()  # 16 bytes
    cosy_key = _b64e(_rsa_encrypt(temp_key))
    info = _b64e(_aes_cbc_encrypt(identity, temp_key))
    return {
        "uid": uid,
        "token": token,
        "machine_id": _machine_id(uid),
        "machine_token": _machine_token(uid),
        "machine_type": _machine_type(uid),
        "cosy_key": cosy_key,
        "info": info,
    }


def _sign_path(raw_url: str) -> str:
    parsed = httpx.URL(raw_url)
    path = parsed.path or "/"
    if path.startswith("/algo"):
        path = path[len("/algo") :]
    return path or "/"


def sign(session: dict[str, Any], body: str, raw_url: str, now: int | None = None) -> dict[str, str]:
    payload = _b64e(
        _compact(
            [
                ("cosyVersion", COSY_VERSION),
                ("ideVersion", ""),
                ("info", session["info"]),
                ("requestId", str(uuid.uuid4())),
                ("version", "v1"),
            ]
        )
    )
    date = str(now if now is not None else int(time.time()))
    raw = payload + "\n" + session["cosy_key"] + "\n" + date + "\n" + body + "\n" + _sign_path(raw_url)
    digest = hashlib.md5(raw.encode()).hexdigest()
    return {
        "Accept": "text/event-stream",
        "Authorization": f"Bearer COSY.{payload}.{digest}",
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "Cosy-ClientType": "5",
        "Cosy-Data-Policy": "agree",
        "Cosy-Date": date,
        "Cosy-Key": session["cosy_key"],
        "Cosy-MachineId": session["machine_id"],
        "Cosy-MachineToken": session["machine_token"],
        "Cosy-MachineType": session["machine_type"],
        "Cosy-User": session["uid"],
        "Cosy-Version": COSY_VERSION,
        "Login-Version": "v2",
        "Accept-Encoding": "identity",
        "Cosy-Business-Product": "cli",
        "Cosy-Business-Type": "agent",
        "Cosy-Scene": "assistant",
        "Cosy-Clientip": COSY_CLIENT_IP,
        "User-Agent": COSY_USER_AGENT,
        "Cosy-MachineOS": COSY_MACHINE_OS,
        "Cosy-MachineHostname": f"qoder-{session['uid'][:8]}",
    }


def _unwrap(frame: dict[str, Any]) -> dict[str, Any]:
    body: Any = frame
    for _ in range(3):
        if not isinstance(body, dict):
            break
        nested = body.get("body")
        if isinstance(nested, dict):
            body = nested
            continue
        if isinstance(nested, str):
            try:
                body = json.loads(nested)
                continue
            except Exception:
                break
        break
    return body if isinstance(body, dict) else {}


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None


def _quota_numbers(data: Any) -> Optional[dict[str, float]]:
    """从多种上游响应结构中提取 剩余/已用/总量。"""
    if not isinstance(data, dict):
        return None
    sources = [data]
    for key in (
        "usage",
        "quota",
        "credit",
        "credits",
        "subscription",
        "plan",
        "summary",
        "userQuota",
        "addOnQuota",
        "data",
    ):
        if isinstance(data.get(key), dict):
            sources.append(data[key])
    remaining = used = total = None
    for source in sources:
        for key in ("remainingCredits", "remaining_credits", "creditsRemaining", "remaining", "availableCredits", "credits"):
            value = _num(source.get(key))
            if value is not None:
                remaining = value if remaining is None else remaining
        for key in ("usedCredits", "used_credits", "consumedCredits", "consumed", "used"):
            value = _num(source.get(key))
            if value is not None:
                used = value if used is None else used
        for key in ("totalCredits", "total_credits", "creditsTotal", "total", "quota", "limit"):
            value = _num(source.get(key))
            if value is not None:
                total = value if total is None else total
    if remaining is None and used is None and total is None:
        return None
    if remaining is None and total is not None and used is not None:
        remaining = max(0.0, total - used)
    if total is None and remaining is not None and used is not None:
        total = remaining + used
    return {
        "credits": remaining if remaining is not None else -1,
        "used": used if used is not None else -1,
        "total": total if total is not None else -1,
    }


class QoderProvider:
    """Qoder 国内版 / 国际版适配器（COSY 签名协议）。

    账号可直接导入 Qoder CLI 的 user JSON，或填写 PAT + uid。
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    def base(self, provider: dict[str, Any]) -> str:
        return (provider.get("base_url") or "").rstrip("/")

    def portal(self, provider: dict[str, Any]) -> str:
        cfg = provider.get("config") or {}
        default = "https://qoder.com" if (provider.get("region") or "cn") == "global" else "https://qoder.com.cn"
        portal = str(cfg.get("portal") or default).rstrip("/")
        if "qoder.sh" in portal:
            portal = default
        return portal

    def openapi(self, provider: dict[str, Any]) -> str:
        cfg = provider.get("config") or {}
        default = "https://openapi.qoder.sh" if (provider.get("region") or "cn") == "global" else "https://openapi.qoder.com.cn"
        return str(cfg.get("openapi") or default).rstrip("/")

    # ------------------------------------------------------------------ login
    async def start_login(self, provider: dict[str, Any]) -> dict[str, Any]:
        verifier, challenge = _pkce_pair()
        nonce = str(uuid.uuid4())
        machine_id = str(uuid.uuid4())
        client_id = str((provider.get("config") or {}).get("client_id") or DEVICE_CLIENT_ID)
        params = urlencode(
            {
                "challenge": challenge,
                "challenge_method": "S256",
                "nonce": nonce,
                "machine_id": machine_id,
                "client_id": client_id,
            }
        )
        return {
            "state": nonce,
            "auth_url": f"{self.portal(provider)}/device/selectAccounts?{params}",
            "verifier": verifier,
            "nonce": nonce,
            "machine_id": machine_id,
            "client_id": client_id,
            "openapi": self.openapi(provider),
        }

    async def poll_login(
        self, provider: dict[str, Any], state: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        context = context or {}
        nonce = str(context.get("nonce") or state)
        verifier = str(context.get("verifier") or "")
        openapi = str(context.get("openapi") or self.openapi(provider)).rstrip("/")
        if not verifier:
            raise ProviderError("Qoder 登录会话缺少 verifier，请重新发起登录", retryable=False)
        try:
            resp = await self.http.get(
                openapi + "/api/v1/deviceToken/poll",
                params={"nonce": nonce, "verifier": verifier, "challenge_method": "S256"},
                headers={"Accept": "application/json", "User-Agent": f"Qoder/{COSY_VERSION}"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Qoder 登录轮询失败: {exc}") from exc
        if resp.status_code in (202, 404):
            return {"pending": True, "message": "等待浏览器授权"}
        if resp.status_code >= 300:
            raise ProviderError(f"Qoder 登录轮询 HTTP {resp.status_code}: {resp.text[:300]}", status_code=resp.status_code)
        try:
            data = resp.json()
        except Exception:
            return {"pending": True, "message": "等待浏览器授权"}
        token = str(data.get("token") or data.get("access_token") or "")
        if not token:
            return {"pending": True, "message": "等待浏览器授权"}
        refresh_token = str(data.get("refresh_token") or data.get("refreshToken") or "")
        expires_at = _parse_expiry(
            data.get("expires_at") or data.get("expiresAt") or data.get("expires_in") or data.get("expiresIn")
        )
        if not expires_at and (data.get("expires_in") or data.get("expiresIn")):
            expires_at = int(time.time()) + _parse_expiry(data.get("expires_in") or data.get("expiresIn"))

        uid = ""
        name = ""
        try:
            info_resp = await self.http.get(
                openapi + "/api/v1/userinfo",
                headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": f"Qoder/{COSY_VERSION}"},
                timeout=30,
            )
            if info_resp.status_code == 200:
                info = info_resp.json()
                if isinstance(info.get("data"), dict):
                    info = info["data"]
                user = info.get("user") if isinstance(info.get("user"), dict) else info
                uid = str(user.get("id") or user.get("user_id") or user.get("uid") or info.get("id") or "")
                name = str(user.get("name") or user.get("nickname") or user.get("display_name") or "")
        except Exception:
            pass
        if not uid:
            claims = _jwt_claims(token)
            uid = str(claims.get("uid") or claims.get("user_id") or claims.get("sub") or "")
            name = name or str(claims.get("name") or claims.get("nickname") or "")
        if not uid:
            uid = "qoder-" + hashlib.sha256(token.encode()).hexdigest()[:16]
        return {
            "pending": False,
            "credential": {
                "access_token": token,
                "security_oauth_token": token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "uid": uid,
                "name": name or uid,
                "region": provider.get("region") or "cn",
            },
        }

    def chat_url(self, provider: dict[str, Any]) -> str:
        return (
            self.base(provider)
            + "/algo/api/v2/service/pro/sse/agent_chat_generation"
            + "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1"
        )

    def models_candidates(self, provider: dict[str, Any]) -> list[str]:
        cfg = provider.get("config") or {}
        if cfg.get("models_path"):
            return [self.base(provider) + str(cfg["models_path"])]
        base = self.base(provider)
        return [
            base + "/algo/api/v2/model/list?Encode=1",
            base + "/algo/api/v2/model/list",
            base + "/api/v2/model/list?Encode=1",
        ]

    @staticmethod
    def session(account: dict[str, Any]) -> dict[str, Any]:
        cred = account.get("credential") or {}
        cached = cred.get("cosy_session")
        if isinstance(cached, dict) and cached.get("cosy_key") and cached.get("uid") == str(cred.get("uid") or ""):
            return cached
        return build_session(cred)

    async def _request(
        self,
        provider: dict[str, Any],
        account: dict[str, Any],
        method: str,
        url: str,
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        sess = self.session(account)
        body = ""
        if payload is not None:
            body = encode_body(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        headers = sign(sess, body, url)
        try:
            return await self.http.request(
                method,
                url,
                headers=headers,
                content=body if body else None,
                timeout=httpx.Timeout(self.settings.upstream_timeout, connect=20),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Qoder 网络错误: {exc}") from exc

    # ------------------------------------------------------------------ models
    async def list_models(self, provider: dict[str, Any], account: dict[str, Any]) -> list[dict[str, Any]]:
        resp = None
        last_error = ""
        for url in self.models_candidates(provider):
            candidate = await self._request(provider, account, "GET", url)
            if candidate.status_code < 300:
                resp = candidate
                break
            last_error = f"HTTP {candidate.status_code}: {candidate.text[:200]}"
        if resp is None:
            raise ProviderError(f"Qoder 模型目录不可用: {last_error}", status_code=502, retryable=True)
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError("Qoder 模型目录返回非 JSON") from exc
        rows: Any = None
        if isinstance(data, dict):
            for key in ("chat", "models", "list", "modelList", "data"):
                value = data.get(key)
                if isinstance(value, list):
                    rows = value
                    break
                if isinstance(value, dict):
                    inner = value.get("models") or value.get("list")
                    if isinstance(inner, list):
                        rows = inner
                        break
        elif isinstance(data, list):
            rows = data
        result = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            mid = str(row.get("key") or row.get("model") or row.get("id") or "")
            if not mid:
                continue
            context = row.get("max_input_tokens") or row.get("contextWindow") or row.get("context_window")
            if not context:
                cfg = row.get("context_config")
                if isinstance(cfg, dict) and cfg:
                    values = [v.get("token_count") for v in cfg.values() if isinstance(v, dict)]
                    context = max([int(v) for v in values if v] or [131072])
            result.append(
                {
                    "id": mid,
                    "upstream": mid,
                    "object": "model",
                    "owned_by": provider.get("id"),
                    "display_name": str(row.get("display_name") or row.get("displayName") or row.get("name") or mid),
                    "context_length": int(context or 131072),
                    "price_factor": row.get("price_factor") or row.get("priceFactor"),
                    "is_free": float(row.get("price_factor") or 0) <= 0,
                }
            )
        return result

    # ------------------------------------------------------------------ chat
    def _chat_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.pop("stream", None)
        body.pop("stream_options", None)
        return body

    async def chat_complete(
        self, provider: dict[str, Any], account: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._request(provider, account, "POST", self.chat_url(provider), self._chat_payload(payload))
        if resp.status_code >= 300:
            raise ProviderError(
                f"Qoder 上游 HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
                retryable=resp.status_code in (401, 403, 408, 409, 425, 429, 430) or resp.status_code >= 500,
            )
        text: list[str] = []
        reasoning: list[str] = []
        tools: dict[int, dict[str, Any]] = {}
        usage = empty_usage()
        finish = "stop"
        model = str(payload.get("model") or "")
        for line in resp.text.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                body = _unwrap(json.loads(raw))
            except Exception:
                continue
            if body.get("error"):
                err = body["error"]
                msg = err.get("message") if isinstance(err, dict) else str(err)
                raise ProviderError(f"Qoder 上游错误: {msg}", retryable=False)
            merge_usage(usage, body.get("usage"))
            if body.get("model"):
                model = str(body["model"])
            for choice in body.get("choices") or []:
                delta = choice.get("delta") or choice.get("message") or {}
                if delta.get("content"):
                    text.append(str(delta["content"]))
                if delta.get("reasoning_content"):
                    reasoning.append(str(delta["reasoning_content"]))
                for call in delta.get("tool_calls") or []:
                    idx = int(call.get("index") or 0)
                    slot = tools.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if call.get("id"):
                        slot["id"] = str(call["id"])
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += str(fn["name"])
                    if fn.get("arguments"):
                        slot["arguments"] += str(fn["arguments"])
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if tools:
            message["tool_calls"] = [
                {
                    "id": slot["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
                }
                for idx, slot in sorted(tools.items())
            ]
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage,
        }

    async def chat_stream(
        self, provider: dict[str, Any], account: dict[str, Any], payload: dict[str, Any]
    ) -> AsyncIterator[StreamChunk]:
        sess = self.session(account)
        url = self.chat_url(provider)
        body = encode_body(
            json.dumps(self._chat_payload(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        headers = {**sign(sess, body, url), "Accept": "text/event-stream"}
        req = self.http.build_request("POST", url, headers=headers, content=body)
        resp = await self.http.send(req, stream=True)
        if resp.status_code >= 300:
            raw = (await resp.aread()).decode("utf-8", "replace")[:600]
            await resp.aclose()
            raise ProviderError(
                f"Qoder 上游 HTTP {resp.status_code}: {raw}",
                status_code=resp.status_code,
                retryable=resp.status_code in (401, 403, 408, 409, 425, 429, 430) or resp.status_code >= 500,
            )
        usage = empty_usage()
        try:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    yield StreamChunk("data: [DONE]\n\n", usage)
                    return
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                inner = _unwrap(frame)
                if inner.get("error"):
                    err = inner["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    yield StreamChunk(
                        "data: "
                        + json.dumps({"error": {"message": str(msg), "type": "api_error"}}, ensure_ascii=False)
                        + "\n\n",
                        usage,
                    )
                    return
                merge_usage(usage, inner.get("usage"))
                if not inner.get("choices"):
                    continue
                yield StreamChunk(
                    "data: " + json.dumps(inner, ensure_ascii=False) + "\n\n",
                    usage,
                )
        finally:
            await resp.aclose()

    # ------------------------------------------------------------------ checkin
    @staticmethod
    def _fingerprint_headers(account: dict[str, Any]) -> dict[str, str]:
        cred = account.get("credential") or {}
        fp = cred.get("checkin_fingerprint") or cred.get("fingerprint") or {}
        if not isinstance(fp, dict):
            fp = {}
        token = str(fp.get("machine_token") or cred.get("machine_token") or "")
        if not token:
            return {}
        headers = {
            "Cosy-Version": str(fp.get("version") or COSY_VERSION),
            "Cosy-MachineId": str(fp.get("machine_id") or cred.get("machine_id") or ""),
            "Cosy-MachineToken": token,
            "Cosy-MachineType": str(fp.get("machine_type") or cred.get("machine_type") or ""),
            "Cosy-MachineCode": str(fp.get("machine_code") or ""),
            "Cosy-MachineHostname": str(fp.get("hostname") or "localhost"),
            "Cosy-MachineOS": str(fp.get("machine_os") or "x86_64_linux"),
        }
        return {key: value for key, value in headers.items() if value}

    async def _exchange_pat(
        self,
        provider: dict[str, Any],
        pat: str,
        account: dict[str, Any] | None = None,
    ) -> str:
        """PAT(pt-) 不能直接签到，先换 24 小时 job token(jt-)。"""
        url = self.openapi(provider) + "/api/v1/jobToken/exchange"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Qoder",
            "Cosy-ClientType": "10",
        }
        if account:
            headers.update(self._fingerprint_headers(account))
        try:
            resp = await self.http.post(url, headers=headers, json={"personal_token": pat}, timeout=30)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Qoder PAT 换取令牌失败: {exc}") from exc
        if resp.status_code >= 300:
            raise ProviderError(
                f"Qoder PAT 换取令牌 HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
                retryable=resp.status_code >= 500,
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError("Qoder PAT 换取令牌返回非 JSON") from exc
        if not isinstance(data, dict):
            raise ProviderError("Qoder PAT 换取令牌响应格式异常", retryable=False)
        if isinstance(data.get("data"), dict):
            data = data["data"]
        token = str(
            data.get("accessToken")
            or data.get("access_token")
            or data.get("token")
            or data.get("jobToken")
            or ""
        )
        if not token:
            raise ProviderError("Qoder PAT 换取令牌失败：响应中没有 accessToken", retryable=False)
        return token

    async def _checkin_token(self, provider: dict[str, Any], account: dict[str, Any]) -> str:
        cred = account.get("credential") or {}
        token = str(
            cred.get("device_token")
            or cred.get("access_token")
            or cred.get("security_oauth_token")
            or ""
        )
        if not token:
            raise ProviderError("Qoder 账号缺少 access_token，请重新登录", retryable=False)
        if token.startswith("pt-"):
            return await self._exchange_pat(provider, token, account)
        return token

    async def checkin(self, provider: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        """Qoder 每日签到：GET /sash/api/v1/me/campaigns + POST .../{id}/claim。"""
        token = await self._checkin_token(provider, account)
        base = self.openapi(provider)
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Accept-Language": "zh-CN",
            "User-Agent": "Qoder",
            "Cosy-ClientType": "10",
            **self._fingerprint_headers(account),
        }
        try:
            resp = await self.http.get(
                base + "/sash/api/v1/me/campaigns",
                params={"forceRefresh": "true"},
                headers=headers,
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Qoder 签到活动查询失败: {exc}") from exc
        if resp.status_code in (401, 403):
            raise ProviderError(
                f"Qoder 登录已失效，请重新登录（HTTP {resp.status_code}）",
                status_code=resp.status_code,
                retryable=False,
            )
        if resp.status_code >= 300:
            raise ProviderError(
                f"Qoder 签到活动查询 HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError("Qoder 签到活动返回非 JSON") from exc
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        campaigns = data.get("campaigns") if isinstance(data, dict) else None
        rows = campaigns if isinstance(campaigns, list) else []

        target: dict[str, Any] | None = None
        claimed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("actionType") or "").upper() != "CLAIM_BENEFIT":
                continue
            status = str(row.get("claimStatus") or "").upper()
            if status == "CLAIMED":
                claimed = True
            elif status == "CLAIMABLE" or row.get("claimable") is True:
                target = row
                break

        if target is None:
            if claimed:
                return {"status": "already", "message": "今天已签到"}
            region = (provider.get("region") or "cn").lower()
            hint = (
                "Qoder 国际版未下发签到活动：需要先在官方客户端签到一次或配置真实设备指纹"
                if region == "global"
                else "当前没有可领取的签到活动（可能活动未开放或今日已领取）"
            )
            return {"status": "unavailable", "message": hint}

        campaign_id = str(target.get("campaignId") or target.get("id") or "")
        if not campaign_id:
            raise ProviderError("Qoder 签到活动缺少 campaignId", retryable=False)
        claim_headers = {
            **headers,
            "Content-Type": "application/json",
            "Origin": base,
        }
        try:
            claim = await self.http.post(
                f"{base}/sash/api/v1/me/campaigns/{quote(campaign_id, safe='')}/claim",
                headers=claim_headers,
                json={},
                timeout=40,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Qoder 签到领取失败: {exc}") from exc
        text = claim.text[:400]
        try:
            body = claim.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if isinstance(body.get("data"), dict):
            body = body["data"]
        result = str(body.get("result") or "").upper()
        status = str(body.get("status") or body.get("claimStatus") or "").upper()
        if (
            claim.status_code == 409
            or body.get("replayed") is True
            or result == "ALREADY_CLAIMED"
            or status == "ALREADY_CLAIMED"
        ):
            return {"status": "already", "message": "今天已签到"}
        if claim.status_code >= 300:
            if claim.status_code in (401, 403):
                raise ProviderError(
                    f"Qoder 登录已失效，请重新登录（HTTP {claim.status_code}）",
                    status_code=claim.status_code,
                    retryable=False,
                )
            raise ProviderError(
                f"Qoder 签到领取 HTTP {claim.status_code}: {text}",
                status_code=claim.status_code,
            )
        if status and status != "CLAIMED":
            raise ProviderError(f"Qoder 签到未成功: {text}", retryable=False)
        benefit = body.get("benefit") if isinstance(body.get("benefit"), dict) else {}
        target_benefit = target.get("benefit") if isinstance(target.get("benefit"), dict) else {}
        amount = benefit.get("amount") or target_benefit.get("amount") or 0
        try:
            amount = float(amount)
        except Exception:
            amount = 0.0
        days = ((benefit.get("validity") or {}).get("days")) if isinstance(benefit.get("validity"), dict) else 0
        try:
            days = int(days or 0)
        except Exception:
            days = 0
        valid_until = int(time.time()) + days * 86400 if days else 0
        suffix = f"（{days} 天有效）" if days else ""
        return {
            "status": "success",
            "credits": amount,
            "valid_until": valid_until,
            "packs": [make_pack("每日签到奖励", amount, amount, 0, valid_until, "qoder-checkin")]
            if amount
            else [],
            "message": f"签到成功 +{amount:.0f} 积分{suffix}",
        }

    # ------------------------------------------------------------------ quota
    async def quota(self, provider: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        cfg = provider.get("config") or {}
        region = (provider.get("region") or "cn").lower()
        base = str(cfg.get("openapi") or "").rstrip("/")
        if not base:
            base = "https://openapi.qoder.sh" if region == "global" else "https://openapi.qoder.com.cn"
        cred = account.get("credential") or {}
        token = str(cred.get("access_token") or cred.get("security_oauth_token") or "")
        if not token:
            return {"credits": -1, "message": "账号缺少 access_token，请重新登录"}
        headers = {"Authorization": "Bearer " + token, "User-Agent": f"Qoder/{COSY_VERSION}", "Accept": "application/json"}
        candidates = [
            "/api/v2/quota/usage",
            "/api/v1/user/status",
            "/api/v1/me",
            "/api/v1/user/quota",
            "/api/v1/usage/summary",
            "/api/v1/credits",
        ]
        last_error = ""
        for path in candidates:
            try:
                resp = await self.http.get(base + path, headers=headers, timeout=30)
            except Exception as exc:
                last_error = str(exc)[:200]
                continue
            if resp.status_code >= 300:
                last_error = f"HTTP {resp.status_code}"
                continue
            try:
                data = resp.json()
            except Exception:
                continue
            if isinstance(data.get("data"), dict):
                data = data["data"]
            parsed = _quota_numbers(data)
            if parsed:
                expire_at = to_epoch(data.get("expiresAt") or data.get("expires_at"))
                packs: list[dict[str, Any]] = []
                quota_total = quota_used = quota_remaining = 0.0
                has_quota = False
                quota_sources = (
                    ("userQuota", "订阅额度"),
                    ("addOnQuota", "附加/签到积分"),
                    ("orgResourcePackage", "组织共享额度"),
                )
                for key, label in quota_sources:
                    quota = data.get(key)
                    if not isinstance(quota, dict):
                        continue
                    total = _num(quota.get("total")) or _num(quota.get("cap")) or 0
                    used = _num(quota.get("used")) or 0
                    remaining = _num(quota.get("remaining"))
                    if remaining is None:
                        remaining = max(0.0, total - used)
                    if total or used or remaining:
                        has_quota = True
                        quota_total += total
                        quota_used += used
                        quota_remaining += remaining
                        packs.append(
                            make_pack(label, remaining, total, used, expire_at, "qoder")
                        )
                if has_quota:
                    parsed = {
                        "credits": quota_remaining,
                        "used": quota_used,
                        "total": quota_total,
                    }
                packs = normalize_packs(packs)
                if not packs and (
                    float(parsed.get("credits") or 0) > 0
                    or float(parsed.get("used") or 0) > 0
                    or float(parsed.get("total") or 0) > 0
                ):
                    packs = [
                        make_pack(
                            "总积分",
                            parsed.get("credits") or 0,
                            parsed.get("total") or 0,
                            parsed.get("used") or 0,
                            0,
                            "qoder-summary",
                        )
                    ]
                result = {**parsed, "raw": data, "endpoint": path, "packs": packs}
                if not packs and (
                    float(parsed.get("credits") or 0) == 0
                    and float(parsed.get("used") or 0) == 0
                    and float(parsed.get("total") or 0) == 0
                ):
                    plan = ""
                    try:
                        plan_resp = await self.http.get(
                            base + "/api/v2/user/plan", headers=headers, timeout=15
                        )
                        if plan_resp.status_code == 200:
                            plan_data = plan_resp.json()
                            plan = str(plan_data.get("plan_tier_name") or plan_data.get("user_type") or "")
                    except Exception:
                        plan = ""
                    used_any = float(parsed.get("used") or 0) > 0
                    if used_any:
                        text_msg = f"{plan} 套餐额度已用尽" if plan else "当前账号额度已用尽"
                    else:
                        text_msg = f"{plan} 套餐无可用额度" if plan else "当前套餐无可用额度"
                    result["message"] = f"{text_msg}（上游返回 0）"
                return result
        return {"credits": -1, "message": f"额度接口不可用{(': ' + last_error) if last_error else ''}"}
