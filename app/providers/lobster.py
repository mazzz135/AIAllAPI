from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..config import Settings
from ..credit_packs import make_pack, normalize_packs
from .base import ProviderError, StreamChunk, empty_usage, merge_usage

# ---------------------------------------------------------------------- 客户端指纹
# 兜底版本：仅在官方更新接口不可用时使用。
# 上游按 clientVersion 决定是否投放活动槽，版本过旧会返回 slotState=empty。
CLIENT_VERSION = "2026.11.6"
CLIENT_UA = f"LobsterAI/{CLIENT_VERSION}"
UPDATE_API = "https://api-overmind.youdao.com/openapi/get/luna/hardware/lobsterai/prod/update"
VERSION_TTL_SECONDS = 6 * 3600

# 客户端能力声明：与真实 Electron 客户端 buildUpstreamRequestHeaders 逐项一致。
# 上游据此判断是否下发虚拟模型 / 思维链 / 上游错误详情等能力，缺项会被降级或拒绝。
CLIENT_CAPABILITIES = ",".join(
    [
        "virtual-models-v1",
        "kimi-k3-agentic-v1",
        "thinking-level-control-v1",
        "upstream-error-details-v1",
    ]
)
# 活动槽容器协议：desktop_sidebar 用 NativeDailyCheckInV1(=2)，desktop_startup_modal 用 3。
ACTIVITY_PLACEMENT = "desktop_sidebar"
ACTIVITY_CONTAINER_API_VERSION = "2"
# 真实客户端上报 process.platform；网关固定按 Windows 桌面端伪装。
CLIENT_PLATFORM = "win32"


class LobsterProvider:
    """网易有道 LobsterAI（有道龙虾）适配器。

    登录：门户 /portal#/login?source=electron&redirect_uri=...&state=...
    换取：POST /api/auth/exchange {authCode, firstKeyfrom, latestKeyfrom, uuid, version}
    刷新：POST /api/auth/refresh
    聊天：POST /api/proxy/v1/chat/completions（强制 SSE）
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient, credential_saver=None):
        self.settings = settings
        self.http = http
        self._credential_saver = credential_saver
        self._version_cache: tuple[float, str] | None = None

    def base(self, provider: dict[str, Any]) -> str:
        return (provider.get("base_url") or "").rstrip("/")

    def portal(self, provider: dict[str, Any]) -> str:
        cfg = provider.get("config") or {}
        return str(cfg.get("portal") or "https://lobsterai.youdao.com").rstrip("/")

    @staticmethod
    def _keyfrom(cred: dict[str, Any]) -> dict[str, str]:
        body = {
            "firstKeyfrom": str(cred.get("first_keyfrom") or ""),
            "latestKeyfrom": str(int(time.time() * 1000)),
            "version": CLIENT_VERSION,
        }
        if cred.get("uuid"):
            body["uuid"] = str(cred["uuid"])
        if cred.get("user_id"):
            body["userId"] = str(cred["user_id"])
        return body

    @staticmethod
    def _headers(cred: dict[str, Any] | None = None, version: str = "") -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": f"LobsterAI/{version or CLIENT_VERSION}",
            "X-LobsterAI-Client-Version": version or CLIENT_VERSION,
            "X-LobsterAI-Client-Capabilities": CLIENT_CAPABILITIES,
        }
        if cred and cred.get("access_token"):
            headers["Authorization"] = "Bearer " + str(cred["access_token"])
        return headers

    async def client_version(self, provider: dict[str, Any]) -> str:
        """解析真实客户端版本：平台配置 > 官方更新接口 > 兜底常量。"""
        cfg = provider.get("config") or {}
        override = str(cfg.get("client_version") or "").strip()
        if override:
            return override
        cached = self._version_cache
        if cached and time.time() - cached[0] < VERSION_TTL_SECONDS:
            return cached[1]
        version = ""
        try:
            resp = await self.http.get(UPDATE_API, timeout=15)
            if resp.status_code == 200:
                payload = resp.json()
                value = ((payload.get("data") or {}).get("value") or {})
                version = str(value.get("version") or "").strip()
                if not version:
                    url = str((value.get("windowsX64") or {}).get("url") or "")
                    match = re.search(r"-(\d{4}\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)-official", url)
                    if match:
                        version = match.group(1)
        except Exception:
            version = ""
        if not version:
            version = CLIENT_VERSION
        self._version_cache = (time.time(), version)
        return version

    @staticmethod
    def _decode(data: Any) -> Any:
        if not isinstance(data, dict):
            raise ProviderError("上游返回非 JSON", retryable=False)
        if int(data.get("code") or 0) != 0:
            raise ProviderError(f"上游错误 code={data.get('code')} msg={data.get('msg')}", retryable=False)
        return data.get("data")

    @staticmethod
    def _raise_http(resp: httpx.Response, action: str) -> None:
        if resp.status_code in (401, 403):
            raise ProviderError(
                f"LobsterAI {action}登录已失效，请重新授权（HTTP {resp.status_code}）",
                status_code=resp.status_code,
                retryable=False,
            )
        if resp.status_code >= 300:
            raise ProviderError(
                f"LobsterAI {action} HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
                retryable=resp.status_code >= 500,
            )

    @staticmethod
    def _claimed_today(state: Any) -> bool:
        if not isinstance(state, dict):
            return False
        value = state.get("claimedToday")
        return value is True or str(value).lower() == "true"

    @staticmethod
    def _has_check_in(actions: Any) -> bool:
        if not isinstance(actions, list):
            return False
        for action in actions:
            if isinstance(action, dict):
                name = action.get("action") or action.get("type") or action.get("name") or ""
            else:
                name = action
            if str(name).lower() == "check_in":
                return True
        return False

    # ------------------------------------------------------------------ login
    def start_login(self, provider: dict[str, Any]) -> dict[str, str]:
        state = uuid.uuid4().hex
        install_uuid = str(uuid.uuid4())
        first = str(int(time.time() * 1000))
        redirect = "http://127.0.0.1:39999/auth/callback"
        auth_url = (
            f"{self.portal(provider)}/portal#/login?source=electron"
            f"&redirect_uri={httpx.URL(redirect)}&state={state}"
        )
        return {
            "state": state,
            "auth_url": auth_url,
            "uuid": install_uuid,
            "first_keyfrom": first,
            "redirect_uri": redirect,
        }

    async def exchange(self, provider: dict[str, Any], code: str, extra: dict[str, Any]) -> dict[str, Any]:
        body = {
            "authCode": code,
            "firstKeyfrom": str(extra.get("first_keyfrom") or int(time.time() * 1000)),
            "latestKeyfrom": str(int(time.time() * 1000)),
            "uuid": str(extra.get("uuid") or uuid.uuid4()),
            "version": CLIENT_VERSION,
        }
        try:
            resp = await self.http.post(
                self.base(provider) + "/api/auth/exchange",
                headers=self._headers(),
                json=body,
                timeout=40,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"授权码交换失败: {exc}") from exc
        if resp.status_code >= 300:
            raise ProviderError(
                f"授权码交换 HTTP {resp.status_code}: {resp.text[:300]}", status_code=resp.status_code
            )
        data = self._decode(resp.json()) or {}
        access = str(data.get("accessToken") or "")
        if not access:
            raise ProviderError("授权码交换未返回 accessToken", retryable=False)
        user = data.get("user") or {}
        uid = str(user.get("id") or user.get("userId") or "")
        return {
            "access_token": access,
            "refresh_token": str(data.get("refreshToken") or ""),
            "expires_at": int(time.time()) + int(data.get("expiresIn") or 0),
            "uid": uid,
            "user_id": str(user.get("userId") or user.get("yid") or ""),
            "nickname": str(user.get("nickname") or ""),
            "uuid": body["uuid"],
            "first_keyfrom": body["firstKeyfrom"],
            "quota": data.get("quota"),
        }

    async def refresh(self, provider: dict[str, Any], cred: dict[str, Any]) -> dict[str, Any]:
        refresh_token = str(cred.get("refresh_token") or "")
        if not refresh_token:
            raise ProviderError("缺少 refreshToken，请重新登录", status_code=401, retryable=False)
        body = self._keyfrom(cred)
        body["refreshToken"] = refresh_token
        try:
            resp = await self.http.post(
                self.base(provider) + "/api/auth/refresh",
                headers=self._headers(),
                json=body,
                timeout=40,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Token 刷新网络错误: {exc}") from exc
        if resp.status_code >= 300:
            raise ProviderError(
                f"Token 刷新 HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
                retryable=resp.status_code >= 500,
            )
        data = self._decode(resp.json()) or {}
        access = str(data.get("accessToken") or "")
        if not access:
            raise ProviderError("Token 刷新未返回 accessToken", retryable=False)
        updated = dict(cred)
        updated["access_token"] = access
        if data.get("refreshToken"):
            updated["refresh_token"] = str(data["refreshToken"])
        if data.get("expiresIn"):
            updated["expires_at"] = int(time.time()) + int(data["expiresIn"])
        updated["latest_keyfrom"] = str(int(time.time() * 1000))
        return updated

    async def ensure(self, provider: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        cred = account.get("credential") or {}
        if not cred.get("access_token"):
            raise ProviderError("账号缺少 accessToken，请重新授权", status_code=401, retryable=False)
        expires = int(cred.get("expires_at") or 0)
        if expires and expires - int(time.time()) > 600:
            return cred
        updated = await self.refresh(provider, cred)
        if self._credential_saver and account.get("id"):
            try:
                self._credential_saver(
                    int(account["id"]),
                    updated,
                    int(updated.get("expires_at") or 0),
                )
            except Exception:
                pass
        return updated

    # ------------------------------------------------------------------ models
    async def list_models(self, provider: dict[str, Any], account: dict[str, Any]) -> list[dict[str, Any]]:
        cred = await self.ensure(provider, account)
        cfg = provider.get("config") or {}
        paths = [str(cfg["models_path"])] if cfg.get("models_path") else ["/api/models/available"]
        resp = None
        last_error = ""
        for path in paths:
            try:
                candidate = await self.http.get(
                    self.base(provider) + path,
                    params=self._keyfrom(cred),
                    headers=self._headers(cred),
                    timeout=40,
                )
            except httpx.HTTPError as exc:
                last_error = f"网络错误: {exc}"
                continue
            if candidate.status_code < 300:
                resp = candidate
                break
            last_error = f"HTTP {candidate.status_code}: {candidate.text[:200]}"
        if resp is None:
            raise ProviderError(f"模型目录不可用: {last_error}", status_code=502, retryable=True)
        rows = self._decode(resp.json()) or []
        result = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("modelId"):
                continue
            result.append(
                {
                    "id": str(row["modelId"]),
                    "object": "model",
                    "owned_by": provider.get("id"),
                    "display_name": str(row.get("modelName") or row["modelId"]),
                    "context_length": int(row.get("contextWindow") or 131072),
                    "provider": str(row.get("provider") or ""),
                    "api_format": str(row.get("apiFormat") or ""),
                }
            )
        return result

    # ------------------------------------------------------------------ chat
    async def _post_chat(
        self, provider: dict[str, Any], account: dict[str, Any], payload: dict[str, Any]
    ) -> httpx.Response:
        cred = await self.ensure(provider, account)
        body = dict(payload)
        body["stream"] = True
        body.pop("stream_options", None)
        choice = body.get("tool_choice")
        if isinstance(choice, str) and choice in ("", "none"):
            body.pop("tool_choice", None)
        headers = self._headers(cred)
        headers["Accept"] = "text/event-stream, application/json"
        headers["X-LobsterAI-Client-Capabilities"] = CLIENT_CAPABILITIES
        headers["X-LobsterAI-Client-Version"] = await self.client_version(provider)
        # 官方客户端为每个出站 LLM 请求生成 W3C traceparent 并由服务端记录用量，
        # 缺失时上游会把这批请求判为非客户端来源。
        headers["traceparent"] = (
            "00-"
            + uuid.uuid4().hex
            + "-"
            + uuid.uuid4().hex[:16]
            + "-01"
        )
        headers["x-lobster-session-id"] = str(uuid.uuid4())
        headers["x-lobster-turn-id"] = uuid.uuid4().hex
        req = self.http.build_request(
            "POST",
            self.base(provider) + "/api/proxy/v1/chat/completions",
            headers=headers,
            json=body,
            timeout=httpx.Timeout(self.settings.upstream_timeout, connect=20),
        )
        resp = await self.http.send(req, stream=True)
        if resp.status_code >= 300:
            raw = (await resp.aread()).decode("utf-8", "replace")[:600]
            await resp.aclose()
            raise ProviderError(
                f"LobsterAI 上游 HTTP {resp.status_code}: {raw}",
                status_code=resp.status_code,
                retryable=resp.status_code in (401, 403, 408, 409, 425, 429) or resp.status_code >= 500,
            )
        return resp

    async def chat_complete(
        self, provider: dict[str, Any], account: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._post_chat(provider, account, payload)
        text: list[str] = []
        reasoning: list[str] = []
        tools: dict[int, dict[str, Any]] = {}
        usage = empty_usage()
        finish = "stop"
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
        finally:
            await resp.aclose()
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
        resp = await self._post_chat(provider, account, payload)
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
                    merge_usage(usage, json.loads(raw).get("usage"))
                except Exception:
                    pass
                yield StreamChunk(line + "\n\n", usage)
        finally:
            await resp.aclose()

    # ------------------------------------------------------------------ billing
    async def quota(self, provider: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        cred = await self.ensure(provider, account)
        cfg = provider.get("config") or {}
        paths = [str(cfg["quota_path"])] if cfg.get("quota_path") else ["/api/user/profile-summary"]
        resp = None
        last_error = ""
        for path in paths:
            try:
                candidate = await self.http.get(
                    self.base(provider) + path, headers=self._headers(cred), timeout=30
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
        data = self._decode(resp.json()) or {}
        remaining = float(data.get("totalCreditsRemaining") or data.get("remainingCredits") or 0)
        used = float(
            data.get("totalCreditsUsed")
            or data.get("usedCredits")
            or data.get("totalCreditsConsumed")
            or 0
        )
        total = float(data.get("totalCredits") or data.get("totalCreditsAmount") or 0)
        if total <= 0 and (remaining or used):
            total = remaining + used
        packs: list[dict[str, Any]] = []
        items = data.get("creditItems")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                amount = item.get("creditsRemaining")
                if amount is None:
                    amount = item.get("remaining") or item.get("credits")
                packs.append(
                    make_pack(
                        item.get("label") or item.get("labelEn") or item.get("type") or "积分包",
                        amount or 0,
                        amount or 0,
                        0,
                        item.get("expiresAt") or item.get("expire_at"),
                        "lobster",
                    )
                )
        packs = normalize_packs(packs)
        if not packs and (remaining or used or total):
            packs = [make_pack("总积分", remaining, total, used, 0, "lobster-summary")]
        return {"credits": remaining, "used": used, "total": total, "raw": data, "packs": packs}

    async def checkin(self, provider: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
        cred = await self.ensure(provider, account)
        base = self.base(provider)
        version = await self.client_version(provider)
        headers = self._headers(cred, version)
        headers["X-LobsterAI-Client-Version"] = version
        try:
            slot = await self.http.get(
                base + "/api/client-activities/slot",
                params={
                    "placement": ACTIVITY_PLACEMENT,
                    "clientVersion": version,
                    "containerApiVersion": ACTIVITY_CONTAINER_API_VERSION,
                    "platform": CLIENT_PLATFORM,
                },
                headers=headers,
                timeout=30,
            )
            self._raise_http(slot, "签到槽查询")
            slot_data = self._decode(slot.json()) or {}
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"LobsterAI 签到槽查询失败: {exc}") from exc
        slot_state = str(slot_data.get("slotState") or "").lower()
        if slot_state != "available":
            if slot_state in ("", "empty", "none"):
                return {
                    "status": "unavailable",
                    "message": "当前没有可参与的签到活动（上游未下发活动槽，不是已签到）",
                }
            return {
                "status": "unavailable",
                "message": f"当前无签到槽: {slot_data.get('slotState') or '未返回'}",
            }
        activity = slot_data.get("activity") or {}
        code = str(activity.get("activityCode") or "")
        try:
            rev = int(activity.get("configRevision") or 0)
        except Exception:
            rev = 0
        if not code:
            return {"status": "unavailable", "message": "签到活动未开放"}
        try:
            ctx = await self.http.get(
                f"{base}/api/client-activities/{code}/context",
                params={
                    "configRevision": rev,
                    "clientVersion": version,
                    "containerApiVersion": ACTIVITY_CONTAINER_API_VERSION,
                    "platform": CLIENT_PLATFORM,
                },
                headers=headers,
                timeout=30,
            )
            self._raise_http(ctx, "签到活动上下文")
            ctx_data = self._decode(ctx.json()) or {}
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"LobsterAI 签到活动上下文查询失败: {exc}") from exc
        if self._claimed_today(ctx_data.get("state")):
            return {"status": "already", "message": "今天已签到"}
        if not self._has_check_in(ctx_data.get("actions")):
            return {"status": "unavailable", "message": "签到动作不可用"}
        try:
            resp = await self.http.post(
                f"{base}/api/client-activities/{code}/actions/check_in",
                headers=headers,
                json={"configRevision": rev, "idempotencyKey": str(uuid.uuid4()), "payload": {}},
                timeout=40,
            )
            self._raise_http(resp, "签到领取")
            data = self._decode(resp.json()) or {}
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"LobsterAI 签到领取失败: {exc}") from exc
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        try:
            gained = float(
                result.get("creditsGranted")
                or result.get("rewardCredits")
                or result.get("credits")
                or 0
            )
        except Exception:
            gained = 0.0
        return {"status": "success", "credits": gained, "message": f"签到成功 +{gained:.0f} 积分"}
