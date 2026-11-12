from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..config import Settings
from ..credit_packs import make_pack, normalize_packs
from .base import ProviderError, StreamChunk, empty_usage, merge_usage

CLI_VERSION = "2.168.2"
# 与本地安装包 resources/app/package.json 的 version 对齐（渠道校验会比对真实版本）。
DESKTOP_GLOBAL = "5.9.3"
DESKTOP_CLI_GLOBAL = "2.165.0"
DESKTOP_CN = "1.106.1"
DESKTOP_CLI_CN = "2.161.0"

SESSION_DEAD_CODE = 12153
SESSION_DEAD_TEXT = "Offline user session not found"


def _parse_time(value: Any) -> int:
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 1_000_000_000_000 else number
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.isdigit():
            return _parse_time(int(raw))
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                from datetime import datetime as _dt

                return int(_dt.strptime(raw[:19], fmt).timestamp())
            except Exception:
                continue
    return 0


def _int_safe(value: Any, default: int = 0) -> int:
    if isinstance(value, dict):
        for key in ("maxInputTokens", "max_input_tokens", "tokens", "contextWindow", "maxTokens"):
            if value.get(key) is not None:
                return _int_safe(value[key], default)
        return default
    try:
        return int(float(value))
    except Exception:
        return default


def _rid() -> str:
    return str(uuid.uuid4())


class WorkBuddyProvider:
    """WorkBuddy（国际版）与 CodeBuddy（国内版）共用协议。

    登录：POST /v2/plugin/auth/state?platform=CLI → 浏览器授权 → GET
    /v2/plugin/auth/token?state=... 换取双 token。
    聊天：POST /v2/chat/completions，上游强制 SSE。
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def region(provider: dict[str, Any]) -> str:
        return (provider.get("region") or "global").lower()

    def base(self, provider: dict[str, Any]) -> str:
        return (provider.get("base_url") or "").rstrip("/")

    def origin(self, provider: dict[str, Any]) -> str:
        cfg = provider.get("config") or {}
        if cfg.get("origin"):
            return str(cfg["origin"]).rstrip("/")
        return "https://www.workbuddy.ai" if self.region(provider) == "global" else "https://www.codebuddy.cn"

    def _common(self, provider: dict[str, Any]) -> dict[str, str]:
        origin = self.origin(provider)
        cfg = provider.get("config") or {}
        # 上游按客户端版本 / 平台投放能力与配额；缺这两项会被判为非官方客户端，返回 code=11128。
        default_version = DESKTOP_GLOBAL if self.region(provider) == "global" else DESKTOP_CN
        client_version = str(cfg.get("client_version") or default_version)
        platform_name = str(cfg.get("platform") or "win32")
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": f"CLI/{CLI_VERSION} CodeBuddy/{CLI_VERSION}",
            "X-Client-Version": client_version,  # 关键：必须用 X-Client-Version，不是 clientVersion
            "platform": platform_name,
        }

    def _identity(self, provider: dict[str, Any], cred: dict[str, Any]) -> dict[str, str]:
        headers = self._common(provider)
        token = cred.get("access_token") or ""
        headers["Authorization"] = f"Bearer {token}" if token else ""
        if not token:
            headers["X-No-Authorization"] = "1"
        uid = cred.get("uid") or ""
        if uid:
            headers["X-User-Id"] = uid
        else:
            headers["X-No-User-Id"] = "1"
        eid = cred.get("enterprise_id") or ""
        if eid:
            headers["X-Enterprise-Id"] = eid
        else:
            headers["X-No-Enterprise-Id"] = "1"
        domain = cred.get("domain") or ("workbuddy.ai" if self.region(provider) == "global" else "codebuddy.cn")
        headers["X-Domain"] = domain
        return {k: v for k, v in headers.items() if v}

    @staticmethod
    def _conversation_ids(payload: dict[str, Any]) -> tuple[str, str]:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        conversation = str(
            payload.get("conversation_id")
            or payload.get("conversationId")
            or metadata.get("conversation_id")
            or metadata.get("conversationId")
            or ""
        )
        request_id = str(
            payload.get("request_id")
            or payload.get("requestId")
            or metadata.get("request_id")
            or metadata.get("requestId")
            or conversation
            or ""
        )
        return conversation, request_id

    def _chat_headers(
        self,
        provider: dict[str, Any],
        cred: dict[str, Any],
        conversation_id: str = "",
        request_id: str = "",
    ) -> dict[str, str]:
        headers = self._identity(provider, cred)
        conv = conversation_id or _rid()
        turn = request_id or conv
        msg = _rid()
        b3_trace = turn if len(turn) in (16, 32) and all(c in "0123456789abcdefABCDEF" for c in turn) else msg
        headers.update(
            {
                "X-Request-ID": msg,
                "X-Conversation-ID": conv,
                "X-Conversation-Request-ID": turn,
                "X-Root-Request-ID": turn,
                "X-Conversation-Message-ID": msg,
                "X-Trace-ID": turn,
                "X-B3-TraceId": b3_trace,
                "X-B3-SpanId": msg[:16],
                "X-B3-Sampled": "1",
                "X-Agent-Type": "main",
                "X-Agent-Intent": "craft",
                "X-Agent-Purpose": "conversation",
                "X-Agent-Runtime": "other",
                "X-IDE-Type": "CLI",
                "X-IDE-Name": "CLI",
                "X-IDE-Version": CLI_VERSION,
                "X-Stainless-Lang": "js",
                "X-Stainless-Package-Version": "6.25.0",
                "X-Stainless-OS": "MacOS",
                "X-Stainless-Arch": "arm64",
                "X-Stainless-Runtime": "node",
                "X-Stainless-Runtime-Version": "v22.21.1",
                "X-Stainless-Retry-Count": "0",
                "X-Stainless-Timeout": "1200",
                "X-Private-Data": "true" if cred.get("enterprise_id") else "false",
            }
        )
        return headers

    @staticmethod
    def _decode(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ProviderError("上游返回非 JSON", retryable=False)
        code = int(data.get("code") or 0)
        if code:
            msg = str(data.get("msg") or "")
            if code == SESSION_DEAD_CODE or SESSION_DEAD_TEXT in msg:
                raise ProviderError("WorkBuddy 会话已失效，请重新登录", status_code=401, retryable=False)
            raise ProviderError(f"上游错误 code={code} msg={msg}", retryable=code not in (11, 11148))
        payload = data.get("data")
        return payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ login
    async def start_login(self, provider: dict[str, Any]) -> dict[str, str]:
        try:
            resp = await self.http.post(
                self.base(provider) + "/v2/plugin/auth/state?platform=CLI",
                headers=self._common(provider),
                json={},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"登录状态请求失败: {exc}") from exc
        if resp.status_code >= 300:
            raise ProviderError(f"登录状态 HTTP {resp.status_code}: {resp.text[:300]}", status_code=resp.status_code)
        data = self._decode(resp.json())
        state = str(data.get("state") or "")
        auth_url = str(data.get("authUrl") or data.get("auth_url") or "")
        if not state or not auth_url:
            raise ProviderError("上游未返回 state / authUrl", retryable=False)
        return {"state": state, "auth_url": auth_url}

    async def poll_login(
        self, provider: dict[str, Any], state: str, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        base = self.base(provider)
        try:
            resp = await self.http.get(
                base + "/v2/plugin/auth/token",
                params={"state": state},
                headers=self._common(provider),
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"登录轮询失败: {exc}") from exc
        if resp.status_code >= 500:
            raise ProviderError(f"登录轮询 HTTP {resp.status_code}", status_code=resp.status_code)
        if resp.status_code >= 300:
            return {"pending": True, "message": "等待浏览器授权"}
        try:
            envelope = resp.json()
        except Exception:
            return {"pending": True, "message": "等待浏览器授权"}
        if int(envelope.get("code") or 0) != 0:
            return {"pending": True, "message": str(envelope.get("msg") or "等待浏览器授权")}
        token = envelope.get("data") or {}
        access = str(token.get("accessToken") or "")
        if not access:
            return {"pending": True, "message": "等待浏览器授权"}
        identity: dict[str, Any] = {}
        try:
            acc = await self.http.get(
                base + "/v2/plugin/login/account",
                params={"state": state},
                headers={**self._common(provider), "Authorization": "Bearer " + access},
                timeout=30,
            )
            if acc.status_code == 200:
                env = acc.json()
                if int(env.get("code") or 0) == 0 and isinstance(env.get("data"), dict):
                    identity = env["data"]
        except Exception:
            identity = {}
        domain = str(token.get("domain") or "")
        if not domain:
            domain = "workbuddy.ai" if self.region(provider) == "global" else "codebuddy.cn"
        return {
            "pending": False,
            "credential": {
                "access_token": access,
                "refresh_token": str(token.get("refreshToken") or ""),
                "expires_at": int(time.time()) + int(token.get("expiresIn") or 0),
                "domain": domain,
                "uid": str(identity.get("uid") or ""),
                "enterprise_id": str(identity.get("enterpriseId") or ""),
                "nickname": str(identity.get("nickname") or ""),
            },
        }

    async def refresh(self, provider: dict[str, Any], cred: dict[str, Any]) -> dict[str, Any]:
        refresh_token = str(cred.get("refresh_token") or "")
        if not refresh_token:
            raise ProviderError("缺少 refreshToken，请重新登录", status_code=401, retryable=False)
        headers = self._common(provider)
        headers["X-Refresh-Token"] = refresh_token
        headers["X-Auth-Refresh-Source"] = "workbuddy"
        if cred.get("enterprise_id"):
            headers["X-Enterprise-Id"] = str(cred["enterprise_id"])
        try:
            resp = await self.http.post(
                self.base(provider) + "/v2/plugin/auth/token/refresh",
                headers=headers,
                json={},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Token 刷新网络错误: {exc}") from exc
        if resp.status_code >= 300:
            raise ProviderError(
                f"Token 刷新 HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
                retryable=resp.status_code >= 500,
            )
        data = self._decode(resp.json())
        access = str(data.get("accessToken") or "")
        if not access:
            raise ProviderError("Token 刷新未返回 accessToken", retryable=False)
        updated = dict(cred)
        updated["access_token"] = access
        if data.get("refreshToken"):
            updated["refresh_token"] = str(data["refreshToken"])
        if data.get("expiresIn"):
            updated["expires_at"] = int(time.time()) + int(data["expiresIn"])
        if data.get("domain"):
            updated["domain"] = str(data["domain"])
        return updated

    async def ensure(self, provider: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        cred = account.get("credential") or {}
        if not cred.get("access_token"):
            raise ProviderError("账号缺少 accessToken，请重新登录", status_code=401, retryable=False)
        expires = int(cred.get("expires_at") or 0)
        if expires and expires - int(time.time()) > 120:
            return cred
        return await self.refresh(provider, cred)

    # ------------------------------------------------------------------ models
    async def list_models(self, provider: dict[str, Any], account: dict[str, Any]) -> list[dict[str, Any]]:
        cred = await self.ensure(provider, account)
        headers = self._identity(provider, cred)
        if self.region(provider) == "global":
            headers["User-Agent"] = (
                f"WorkBuddy/{DESKTOP_GLOBAL} workbuddy-ai/{DESKTOP_GLOBAL} CLI/{DESKTOP_CLI_GLOBAL}"
            )
        else:
            headers["User-Agent"] = f"WorkBuddy/{DESKTOP_CN} WorkBuddy/{DESKTOP_CN} CLI/{DESKTOP_CLI_CN}"
        cfg = provider.get("config") or {}
        candidates = [str(cfg["models_path"])] if cfg.get("models_path") else ["/v3/config", "/v3/config?platform=CLI"]
        resp = None
        last_error = ""
        for path in candidates:
            try:
                candidate = await self.http.get(self.base(provider) + path, headers=headers, timeout=40)
            except httpx.HTTPError as exc:
                last_error = f"网络错误: {exc}"
                continue
            if candidate.status_code < 300:
                resp = candidate
                break
            last_error = f"HTTP {candidate.status_code}: {candidate.text[:200]}"
        if resp is None:
            raise ProviderError(f"模型目录不可用: {last_error}", status_code=502, retryable=True)
        data = self._decode(resp.json())
        rows = data.get("models") or []
        cli_ids: set[str] = set()
        for agent in data.get("agents") or []:
            if str(agent.get("name") or "").lower() in ("cli", "codebuddy", "workbuddy"):
                cli_ids.update(str(x) for x in (agent.get("models") or []))
        result = []
        for row in rows:
            if not isinstance(row, dict) or row.get("disabled") or not row.get("id"):
                continue
            mid = str(row["id"])
            if cli_ids and mid not in cli_ids:
                continue
            result.append(
                {
                    "id": mid,
                    "object": "model",
                    "owned_by": provider.get("id"),
                    "display_name": str(row.get("name") or mid),
                    "context_length": _int_safe(row.get("contextWindow") or row.get("context_length"), 131072) or 131072,
                }
            )
        return result

    # ------------------------------------------------------------------ chat
    @staticmethod
    def prepare_body(payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        body.pop("stream_options", None)
        body["stream"] = True
        body.setdefault("cache_retention", "long")
        messages = body.get("messages")
        if not isinstance(messages, list):
            messages = []
        cleaned = []
        for item in messages:
            if isinstance(item, dict):
                content = item.get("content")
                if content in ("", None) and not item.get("tool_calls") and item.get("role") != "tool":
                    continue
            cleaned.append(item)
        if not cleaned or str(cleaned[0].get("role") or "").lower() != "system":
            cleaned.insert(0, {"role": "system", "content": "You are a helpful assistant."})
        body["messages"] = cleaned
        choice = body.get("tool_choice")
        if isinstance(choice, str) and choice in ("", "none"):
            body.pop("tool_choice", None)
            body.pop("tools", None)
        if body.get("max_tokens") in (None, 0):
            body.pop("max_tokens", None)
        return body

    async def _post_chat(
        self, provider: dict[str, Any], account: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[httpx.Response, dict[str, Any]]:
        cred = await self.ensure(provider, account)
        body = self.prepare_body(payload)
        req = self.http.build_request(
            "POST",
            self.base(provider) + "/v2/chat/completions",
            headers={
                **self._chat_headers(
                    provider,
                    cred,
                    *self._conversation_ids(payload),
                ),
                "Accept": "text/event-stream",
            },
            json=body,
            timeout=httpx.Timeout(self.settings.upstream_timeout, connect=20),
        )
        resp = await self.http.send(req, stream=True)
        if resp.status_code >= 300:
            raw = (await resp.aread()).decode("utf-8", "replace")[:600]
            await resp.aclose()
            retryable = resp.status_code in (401, 403, 408, 409, 425, 429) or resp.status_code >= 500
            raise ProviderError(
                f"WorkBuddy 上游 HTTP {resp.status_code}: {raw}",
                status_code=resp.status_code,
                retryable=retryable,
            )
        return resp, cred

    async def chat_complete(
        self, provider: dict[str, Any], account: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        resp, _ = await self._post_chat(provider, account, payload)
        text: list[str] = []
        reasoning: list[str] = []
        tools: dict[int, dict[str, Any]] = {}
        finish = "stop"
        usage = empty_usage()
        model = str(payload.get("model") or "")
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if isinstance(frame.get("error"), dict):
                    raise ProviderError(str(frame["error"].get("message") or frame["error"]), retryable=False)
                merge_usage(usage, frame.get("usage"))
                if frame.get("model"):
                    model = str(frame["model"])
                for choice in frame.get("choices") or []:
                    delta = choice.get("delta") or choice.get("message") or {}
                    if delta.get("content"):
                        text.append(str(delta["content"]))
                    if delta.get("reasoning_content"):
                        reasoning.append(str(delta["reasoning_content"]))
                    for call in delta.get("tool_calls") or []:
                        index = int(call.get("index") or 0)
                        slot = tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if call.get("id"):
                            slot["id"] = str(call["id"])
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += str(fn["name"])
                        if fn.get("arguments"):
                            slot["arguments"] += str(fn["arguments"])
                    if choice.get("finish_reason"):
                        finish = str(choice["finish_reason"])
        finally:
            await resp.aclose()
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if tools:
            message["tool_calls"] = [
                {
                    "id": slot["id"] or f"call_{index}",
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"] or "{}"},
                }
                for index, slot in sorted(tools.items())
            ]
            if not message["content"]:
                message["content"] = None
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
        resp, _ = await self._post_chat(provider, account, payload)
        usage = empty_usage()
        try:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    yield StreamChunk(line + "\n\n", usage)
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    yield StreamChunk("data: [DONE]\n\n", usage)
                    return
                try:
                    frame = json.loads(raw)
                    merge_usage(usage, frame.get("usage"))
                except Exception:
                    pass
                yield StreamChunk(line + "\n\n", usage)
        finally:
            await resp.aclose()

    # ------------------------------------------------------------------ billing
    async def quota(self, provider: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        cred = await self.ensure(provider, account)
        cfg = provider.get("config") or {}
        if cfg.get("billing_base"):
            base = str(cfg["billing_base"]).rstrip("/")
        elif self.region(provider) == "global":
            base = "https://www.workbuddy.ai"
        else:
            base = "https://www.codebuddy.cn"
        if cfg.get("quota_path"):
            paths = [str(cfg["quota_path"])]
        elif self.region(provider) == "global":
            paths = ["/billing/meter/get-user-resource", "/v2/billing/meter/get-user-resource"]
        else:
            paths = ["/v2/billing/meter/get-user-resource", "/billing/meter/get-user-resource"]
        resp = None
        last_error = ""
        now = time.time()
        body = {
            "PageNumber": 1,
            "PageSize": 100,
            "ProductCode": "p_tcaca",
            "Status": [0, 3],
            "PackageEndTimeRangeBegin": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "PackageEndTimeRangeEnd": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(now + 365 * 101 * 86400)
            ),
        }
        for path in paths:
            try:
                candidate = await self.http.post(
                    base + path, headers=self._identity(provider, cred), json=body, timeout=30
                )
            except httpx.HTTPError as exc:
                last_error = f"网络错误: {exc}"
                continue
            if candidate.status_code < 300:
                resp = candidate
                break
            last_error = f"HTTP {candidate.status_code}: {candidate.text[:200]}"
        if resp is None:
            raise ProviderError(f"额度查询不可用: {last_error}", status_code=502)
        data = self._decode(resp.json())
        accounts = (((data.get("Response") or {}).get("Data") or {}).get("Accounts")) or []
        remain = used = total = 0.0
        packs: list[dict[str, Any]] = []
        if isinstance(accounts, list) and accounts:
            for item in accounts:
                if not isinstance(item, dict):
                    continue
                cycle_remain = item.get("CycleCapacityRemain")
                cycle_used = item.get("CycleCapacityUsed")
                cycle_size = item.get("CycleCapacitySize")
                pack_remain = float(cycle_remain if cycle_remain is not None else item.get("CapacityRemain") or 0)
                pack_used = float(cycle_used if cycle_used is not None else item.get("CapacityUsed") or 0)
                pack_total = float(cycle_size if cycle_size is not None else item.get("CapacitySize") or 0)
                remain += pack_remain
                used += pack_used
                total += pack_total
                expire_at = _parse_time(
                    item.get("CycleEndTime") or item.get("ExpiredTime") or item.get("PackageEndTime") or item.get("EndTime")
                )
                name = (
                    item.get("PackageName")
                    or item.get("SubProductName")
                    or item.get("ProductName")
                    or "积分包"
                )
                if pack_remain > 0 or pack_total > 0:
                    packs.append(
                        make_pack(name, pack_remain, pack_total, pack_used, expire_at, "workbuddy")
                    )
            packs = normalize_packs(packs)
            if total <= 0 and (remain or used):
                total = remain + used
            if not packs and (remain or used or total):
                packs = [make_pack("总积分", remain, total, used, 0, "workbuddy-summary")]
            return {
                "credits": remain,
                "used": used,
                "total": total,
                "raw": data,
                "packs": packs,
                "pack_count": len(accounts),
            }
        packages = data.get("resourcePackages") or data.get("packages") or []
        for pkg in packages:
            try:
                pack_remain = float(pkg.get("remain") or pkg.get("remaining") or 0)
                pack_used = float(pkg.get("used") or pkg.get("usage") or 0)
                pack_total = float(pkg.get("total") or pkg.get("size") or pkg.get("amount") or 0)
                remain += pack_remain
                used += pack_used
                total += pack_total
                packs.append(
                    make_pack(
                        pkg.get("name") or pkg.get("label") or "积分包",
                        pack_remain,
                        pack_total,
                        pack_used,
                        pkg.get("expire_at") or pkg.get("expires_at") or pkg.get("expireTime"),
                        "workbuddy",
                    )
                )
            except Exception:
                continue
        packs = normalize_packs(packs)
        if total <= 0 and (remain or used):
            total = remain + used
        if not packs and (remain or used or total):
            packs = [make_pack("总积分", remain, total, used, 0, "workbuddy-summary")]
        return {"credits": remain, "used": used, "total": total, "raw": data, "packs": packs}

    async def checkin(self, provider: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        cred = await self.ensure(provider, account)
        cfg = provider.get("config") or {}
        if cfg.get("billing_base"):
            base = str(cfg["billing_base"]).rstrip("/")
        elif self.region(provider) == "global":
            base = "https://www.workbuddy.ai"
        else:
            base = "https://www.codebuddy.cn"
        checkin_path = "/billing/meter/daily-checkin" if self.region(provider) == "global" else "/v2/billing/meter/daily-checkin"
        try:
            resp = await self.http.post(
                base + checkin_path,
                headers=self._identity(provider, cred),
                json={},
                timeout=40,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"签到网络错误: {exc}") from exc
        text = resp.text[:600]
        try:
            data = resp.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        msg = str(data.get("msg") or data.get("message") or "")
        code = int(data.get("code") or 0)
        lower = msg.lower()
        if resp.status_code >= 300 or code != 0:
            if "已签到" in msg or "already" in lower:
                return {"status": "already", "message": msg or "今天已签到"}
            if any(token in msg for token in ("活动未开启", "活动已过期", "未开启", "已结束")):
                return {"status": "unavailable", "message": msg}
            raise ProviderError(
                f"签到失败 HTTP {resp.status_code}: {msg or text}",
                status_code=resp.status_code,
                retryable=resp.status_code >= 500,
            )
        return {"status": "success", "message": msg or "签到成功", "data": data.get("data")}
