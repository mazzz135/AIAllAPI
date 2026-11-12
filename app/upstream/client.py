from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlencode

import httpx

from ..config import Settings
from ..credit_packs import make_pack, normalize_packs, to_epoch
from .device import device_id_for, market_user_id_for, session_id_for
from .login import device_proof
from .sse import SSEDecoder, parse_json

log = logging.getLogger("aiall.upstream")


class TraeError(Exception):
    def __init__(
        self,
        message: str,
        *,
        http_status: int = 0,
        code: int = 0,
        retryable: bool = True,
    ):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code
        self.retryable = retryable

    def __str__(self) -> str:
        return self.message


@dataclass
class TraeEvent:
    event: str
    data: dict[str, Any]
    raw: str = ""


def _clean_host(value: str, default: str) -> str:
    host = (value or default).strip().rstrip("/")
    if not host.startswith("http"):
        host = default
    return host.rstrip("/")


def _oauth_error(detail: str) -> str:
    """把 OAuth 失败细节转成可读提示（原实现引用了未定义函数，会抛 NameError）。"""
    text = (detail or "").strip()
    lower = text.lower()
    if "未知后端路径" in text or "unknown backend" in lower:
        return "Trae 授权接口路径已变更（未知后端路径），请改用 Refresh Token / Cookie 登录"
    if "401" in text or "403" in text or "unauthorized" in lower or "invalid" in lower:
        return "授权码已失效或不被接受，请重新生成登录链接"
    if "timeout" in lower or "timed out" in lower:
        return "请求 Trae 授权接口超时，请检查网络或代理设置"
    return "换取 Trae 令牌失败：" + (text[:200] or "未知错误")


def _as_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _parse_expiry(raw: Any, duration: Any = None) -> int:
    if raw not in (None, "", 0, "0"):
        try:
            value = int(float(raw))
            if value > 1_000_000_000_000:
                value //= 1000
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    if duration not in (None, "", 0, "0"):
        try:
            return int(time.time()) + int(float(duration))
        except (TypeError, ValueError):
            pass
    return 0


def _deep_find(obj: Any, keys: tuple[str, ...], depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(obj, dict):
        for key in keys:
            if key in obj:
                return obj[key]
        for value in obj.values():
            found = _deep_find(value, keys, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _deep_find(value, keys, depth + 1)
            if found is not None:
                return found
    return None


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


class TraeClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    # ------------------------------------------------------------------ headers
    def ide_headers(self, account: dict[str, Any], *, sse: bool = True) -> dict[str, str]:
        token = account.get("access_token", "")
        uid = account.get("uid", "")
        device_id = account.get("device_id") or account.get("machine_id") or "0"
        machine_id = account.get("machine_id") or "0"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Cloud-IDE-JWT {token}",
            "X-Cloudide-Token": token,
            "X-Ide-Token": token,
            "X-Uid": uid,
            "x-uid": uid,
            "x-app-id": self.settings.app_id,
            "x-app-version": "default",
            "x-app-version-code": self.settings.ide_version_code,
            "x-device-id": device_id,
            "x-machine-id": machine_id,
            "x-request-id": str(uuid.uuid4()),
            "x-ide-version": self.settings.ide_version,
            "x-ide-version-code": self.settings.ide_version_code,
            "x-ide-version-type": "stable",
            "x-device-cpu": "AMD",
            "x-device-brand": "83DG",
            "x-device-type": "windows",
            "x-os-version": "Windows 11 Pro",
            "x-system-type": "Windows",
            "Accept": "text/event-stream" if sse else "application/json",
            "Connection": "keep-alive",
            "User-Agent": f"Trae/{self.settings.ide_version}",
        }
        return headers

    def ug_headers(self, account: dict[str, Any]) -> dict[str, str]:
        uid = account.get("uid", "")
        return {
            "Authorization": f"Cloud-IDE-JWT {account.get('access_token', '')}",
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": "VSCode 1.107.1 (TRAE SOLO CN)",
            "X-Market-Client-Id": "VSCode 1.107.1",
            "X-Market-User-Id": market_user_id_for(uid),
            "X-Device-Id": device_id_for(uid),
            "Vscode-Sessionid": session_id_for(uid),
            "X-Lgw-Req-Sdk-Type": "3",
            "Package-Type": "stable_cn",
            "X-Lscbd-Aid": "787976",
            "X-Lscbd-Platform": "windows",
            "App-Version": "0.1.45",
            "X-Tt-Trace-Id": "00-" + uuid.uuid4().hex[:16] + "-01",
            "X-Request-Id": str(uuid.uuid4()),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "none",
            "X-User-Region": "CN",
        }

    def _timeout(self, read: Optional[float] = None) -> httpx.Timeout:
        return httpx.Timeout(
            connect=20.0,
            read=read or self.settings.upstream_timeout,
            write=30.0,
            pool=20.0,
        )

    # ------------------------------------------------------------------ auth
    async def exchange_token(
        self,
        refresh_token: str,
        uid: str = "",
        *,
        oauth_host: str = "",
        client_id: str = "",
        device_info: dict[str, Any] | None = None,
        private_key_pem: str = "",
        ide_version: str = "",
    ) -> dict[str, Any]:
        if not refresh_token:
            raise TraeError("refreshToken 为空", retryable=False)
        host = _clean_host(oauth_host or self.settings.oauth_host, self.settings.oauth_host)
        effective_client = client_id or self.settings.client_id
        body = {
            "ClientID": effective_client,
            "RefreshToken": refresh_token,
            "ClientSecret": "-",
            "UserID": uid,
        }
        if device_info:
            body["DeviceInfo"] = device_info
            body["IDEVersion"] = ide_version or self.settings.ide_version
            if private_key_pem:
                body["ClientSecret"] = ""
                body["DeviceProof"] = device_proof(private_key_pem, effective_client, refresh_token)
        try:
            resp = await self.http.post(
                host + "/cloudide/api/v3/trae/oauth/ExchangeToken",
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": f"Trae/{self.settings.ide_version}",
                },
                timeout=self._timeout(30),
            )
        except httpx.HTTPError as exc:
            raise TraeError(f"ExchangeToken 网络错误: {exc}", retryable=True) from exc
        if resp.status_code != 200:
            raise TraeError(
                f"ExchangeToken HTTP {resp.status_code}: {resp.text[:300]}",
                http_status=resp.status_code,
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )
        data = _parse_json_object(resp.text)
        error = _deep_find(data, ("Error",))
        if isinstance(error, dict) and (error.get("Code") or error.get("Message")):
            raise TraeError("上游拒绝刷新，请重新登录", retryable=False)
        result = data.get("Result") or data.get("result") or {}
        if not isinstance(result, dict):
            raise TraeError("ExchangeToken 响应缺少 Result", retryable=False)
        token = result.get("Token") or result.get("token") or result.get("AccessToken") or ""
        if not token:
            raise TraeError("ExchangeToken 未返回 Token", retryable=False)
        return {
            "access_token": token,
            "refresh_token": result.get("RefreshToken") or result.get("refreshToken") or refresh_token,
            "expires_at": _parse_expiry(
                result.get("TokenExpireAt") or result.get("tokenExpireAt"),
                result.get("TokenExpireDuration"),
            ),
            "raw": result,
        }

    async def exchange_code(
        self,
        auth_code: str,
        code_verifier: str,
        device_info: dict[str, Any],
        *,
        ide_version: str = "",
        oauth_host: str = "",
        client_id: str = "",
    ) -> dict[str, Any]:
        if not auth_code or not code_verifier:
            raise TraeError("授权码或 PKCE verifier 为空", retryable=False)
        host = _clean_host(oauth_host or self.settings.oauth_host, self.settings.oauth_host)
        body = {
            "ClientID": client_id or self.settings.client_id,
            "AuthCode": auth_code,
            "CodeVerifier": code_verifier,
            "DeviceInfo": device_info,
            "IDEVersion": ide_version or self.settings.ide_version,
        }
        resp = None
        last_error = ""
        for candidate in self._oauth_hosts(host):
            try:
                attempt = await self.http.post(
                    candidate + "/trae/api/v3/oauth/ExchangeToken",
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": f"Trae/{self.settings.ide_version}",
                    },
                    timeout=self._timeout(30),
                )
            except httpx.HTTPError as exc:
                last_error = f"{candidate}: {exc}"
                continue
            if attempt.status_code == 200 and attempt.text.lstrip().startswith("{"):
                resp = attempt
                break
            last_error = f"{candidate}: HTTP {attempt.status_code} {attempt.text[:120]}"
        if resp is None:
            raise TraeError(_oauth_error(last_error), retryable=False)
        data = _parse_json_object(resp.text)
        result = data.get("Result") or data.get("result") or {}
        if not isinstance(result, dict):
            raise TraeError("授权码换取令牌缺少 Result", retryable=False)
        token = result.get("Token") or result.get("token") or ""
        if not token:
            raise TraeError("授权码换取令牌未返回 Token", retryable=False)
        return {
            "access_token": token,
            "refresh_token": result.get("RefreshToken") or result.get("refreshToken") or "",
            "expires_at": _parse_expiry(
                result.get("TokenExpireAt") or result.get("tokenExpireAt"),
                result.get("TokenExpireDuration"),
            ),
            "raw": result,
        }

    @staticmethod
    def _oauth_hosts(host: str) -> list[str]:
        candidates = [host.rstrip("/")]
        for fallback in ("https://www.trae.ai", "https://api.trae.ai", "https://core-normal.trae.ai"):
            if fallback not in candidates:
                candidates.append(fallback)
        return candidates

    async def get_user_info(self, access_token: str, *, oauth_host: str = "") -> dict[str, Any]:
        host = _clean_host(oauth_host or self.settings.oauth_host, self.settings.oauth_host)
        body = {"ReqSource": "IDE", "IDEVersion": self.settings.ide_version}
        resp = None
        last_error = ""
        for candidate in self._oauth_hosts(host):
            try:
                attempt = await self.http.post(
                    candidate + "/cloudide/api/v3/trae/GetUserInfo",
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": f"Trae/{self.settings.ide_version}",
                        "X-Cloudide-Token": access_token,
                        "Authorization": f"Cloud-IDE-JWT {access_token}",
                    },
                    timeout=self._timeout(30),
                )
            except httpx.HTTPError as exc:
                last_error = f"{candidate}: {exc}"
                continue
            if attempt.status_code == 200 and attempt.text.lstrip().startswith("{"):
                resp = attempt
                break
            last_error = f"{candidate}: HTTP {attempt.status_code} {attempt.text[:120]}"
        if resp is None:
            raise TraeError(_oauth_error(last_error), retryable=False)
        data = _parse_json_object(resp.text)
        result = data.get("Result") or data.get("result") or {}
        if not isinstance(result, dict) or not result.get("UserID"):
            raise TraeError("GetUserInfo 响应缺少 UserID", retryable=False)
        return result

    async def get_token_from_session(
        self,
        session_cookie: str,
        *,
        oauth_host: str = "",
        console_host: str = "",
    ) -> dict[str, Any]:
        if not session_cookie:
            raise TraeError("会话 Cookie 为空", retryable=False)
        host = _clean_host(oauth_host or self.settings.oauth_host, self.settings.oauth_host)
        console = _clean_host(console_host or self.settings.console_host, self.settings.console_host)
        try:
            resp = await self.http.post(
                host + "/cloudide/api/v3/common/GetUserToken",
                headers={
                    "Cookie": "X-Cloudide-Session=" + session_cookie,
                    "Referer": console + "/",
                    "Origin": console,
                    "User-Agent": "TraeCheckin/1.0",
                    "Accept": "application/json, text/plain, */*",
                },
                timeout=self._timeout(30),
            )
        except httpx.HTTPError as exc:
            raise TraeError(f"GetUserToken 网络错误: {exc}", retryable=True) from exc
        if resp.status_code == 401:
            raise TraeError("网页会话已失效（约 14 天），请重新登录", http_status=401, retryable=False)
        if resp.status_code != 200:
            raise TraeError(
                f"GetUserToken HTTP {resp.status_code}: {resp.text[:300]}",
                http_status=resp.status_code,
                retryable=resp.status_code >= 500,
            )
        data = _parse_json_object(resp.text)
        token = (data.get("Result") or {}).get("Token") or ""
        if not token:
            raise TraeError("GetUserToken 响应缺少 Result.Token", retryable=False)
        return {"access_token": token, "expires_at": int(time.time()) + 7 * 3600}

    # ------------------------------------------------------------------ models
    async def get_models(self, account: dict[str, Any], function: str) -> list[dict[str, Any]]:
        host = _clean_host(account.get("agent_host") or self.settings.agent_host, self.settings.agent_host)
        body = {
            "function": function,
            "config_names": None,
            "need_prompt": False,
            "current_config_info": None,
            "poly_prompt": True,
            "mode_type": None,
            "agent_type": None,
        }
        try:
            resp = await self.http.post(
                host + "/api/ide/v1/get_detail_param",
                json=body,
                headers=self.ide_headers(account, sse=False),
                timeout=self._timeout(30),
            )
        except httpx.HTTPError as exc:
            raise TraeError(f"拉取模型表网络错误: {exc}", retryable=True) from exc
        if resp.status_code != 200:
            raise TraeError(
                f"拉取模型表 HTTP {resp.status_code}: {resp.text[:300]}",
                http_status=resp.status_code,
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )
        data = _parse_json_object(resp.text)
        root = data.get("data") if isinstance(data.get("data"), dict) else data
        return self.parse_models(root, function)

    async def get_models_all(self, account: dict[str, Any]) -> list[dict[str, Any]]:
        functions = ["solo_work_remote", "chat_v3", "solo_agent"]
        merged: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for function in functions:
            try:
                models = await self.get_models(account, function)
            except TraeError as exc:
                errors.append(str(exc))
                continue
            for model in models:
                key = model["id"].lower()
                if key not in merged:
                    merged[key] = model
                else:
                    self._merge_model(merged[key], model)
            if merged:
                break
        if not merged:
            raise TraeError("模型表为空或账号未登录: " + "; ".join(errors[-2:]), retryable=True)
        return list(merged.values())

    @staticmethod
    def _merge_model(dst: dict[str, Any], src: dict[str, Any]) -> None:
        for key in ("display_name", "model_name", "max_model_name", "ide_function", "provider"):
            if not dst.get(key) and src.get(key):
                dst[key] = src[key]
        dst["max_mode"] = bool(dst.get("max_mode") or src.get("max_mode"))
        dst["context_length"] = max(int(dst.get("context_length") or 0), int(src.get("context_length") or 0))
        for key in ("rate_base", "rate_member", "rate_activity", "rate_activity_member"):
            if not dst.get(key) and src.get(key):
                dst[key] = src[key]
        if not dst.get("reasoning_options") and src.get("reasoning_options"):
            dst["reasoning_options"] = src["reasoning_options"]

    def parse_models(self, root: dict[str, Any], function: str) -> list[dict[str, Any]]:
        items = root.get("config_info_list")
        if not isinstance(items, list):
            return []
        merged: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            config_name = str(item.get("config_name") or "").strip()
            if not config_name or self._is_internal(config_name):
                continue
            source = item.get("config_source")
            if int(source if source is not None else 1) != 1:
                continue
            if bool(item.get("is_invisible_to_user")):
                continue
            display = _parse_json_object(item.get("display_config"))
            if not display or bool(display.get("is_custom_model")):
                continue
            details = item.get("model_detail_list")
            detail = details[0] if isinstance(details, list) and details and isinstance(details[0], dict) else {}
            model_name = str(detail.get("model_name") or item.get("model_name") or config_name)
            display_name = str(display.get("display_name") or item.get("display_name") or config_name)
            context_length = int(
                detail.get("max_input_tokens")
                or detail.get("context_window")
                or item.get("max_input_tokens")
                or 0
            )
            context_obj = item.get("context_window_size")
            if not context_length and isinstance(context_obj, dict):
                context_length = int(context_obj.get("default") or 0)
            features = _parse_json_object(item.get("features"))
            if not context_length and features:
                cw = features.get("context_windows")
                if isinstance(cw, dict) and isinstance(cw.get("data"), dict):
                    context_length = int(cw["data"].get("dev_context") or 0)
            if context_length <= 0:
                context_length = 131072

            max_model_name = ""
            is_max = bool(detail.get("max_mode") or item.get("max_mode"))
            if is_max or model_name.endswith("__max"):
                max_model_name = model_name
            reasoning_options: list[str] = []
            rec = item.get("reasoning_effort_config")
            if isinstance(rec, dict) and rec.get("support_thinking"):
                opts = rec.get("options")
                if isinstance(opts, list):
                    reasoning_options = [str(v) for v in opts if isinstance(v, str)]
            reo = item.get("reasoning_effort_options")
            if isinstance(reo, list):
                reasoning_options.extend(str(v) for v in reo if isinstance(v, str))

            rates = self._parse_rates(item.get("display_contact_config"))
            model = {
                "id": config_name,
                "object": "model",
                "created": 1753600000,
                "owned_by": "trae",
                "display_name": display_name,
                "model_name": model_name,
                "max_model_name": max_model_name,
                "context_length": context_length,
                "max_mode": is_max or bool(item.get("is_dollar_max")),
                "config_source": 1,
                "provider": str(item.get("provider") or ""),
                "ide_function": str(detail.get("raw_chat_function") or function),
                "reasoning_options": list(dict.fromkeys(reasoning_options)),
                "vision": True,
                **rates,
            }
            key = config_name.lower()
            if key in merged:
                self._merge_model(merged[key], model)
            else:
                merged[key] = model
        return list(merged.values())

    @staticmethod
    def _parse_rates(raw: Any) -> dict[str, float | str]:
        data = _parse_json_object(raw)
        if not data:
            return {"rate_base": 0.0, "rate_member": 0.0, "rate_activity": 0.0, "rate_activity_member": 0.0}
        result: dict[str, float | str] = {
            "rate_base": 0.0,
            "rate_member": 0.0,
            "rate_activity": 0.0,
            "rate_activity_member": 0.0,
            "activity_type": "",
        }
        cr = data.get("consumption_rate")
        if isinstance(cr, dict):
            cd = cr.get("data") if isinstance(cr.get("data"), dict) else cr
            result["rate_base"] = _as_number(cd.get("rate"))
        discount = data.get("discount")
        if isinstance(discount, dict) and isinstance(discount.get("data"), dict):
            dd = discount["data"]
            result["rate_member"] = _as_number(dd.get("consumption_rate"))
        activity = data.get("activity_discount")
        if isinstance(activity, dict) and isinstance(activity.get("data"), dict):
            ad = activity["data"]
            current = ad.get("current")
            if isinstance(current, dict):
                result["rate_activity"] = _as_number(current.get("consumption_rate"))
                result["rate_activity_before"] = _as_number(current.get("before_consumption_rate"))
                result["activity_type"] = str(current.get("discount_type") or "")
            member = ad.get("member")
            if isinstance(member, dict):
                result["rate_activity_member"] = _as_number(member.get("after_consumption_rate"))
        return result

    @staticmethod
    def _is_internal(name: str) -> bool:
        lower = name.lower()
        if lower.startswith(("custom_", "custom_model")):
            return True
        blocked = (
            "agent",
            "subagent",
            "summary",
            "sagitta",
            "aquila",
            "file_search",
            "explore_",
            "computer_use",
            "browser_use",
            "title_generation",
            "input_optimization",
            "context_selection",
            "fast_apply",
        )
        return any(token in lower for token in blocked)

    # ------------------------------------------------------------------ chat
    async def chat_stream(
        self,
        account: dict[str, Any],
        body: dict[str, Any],
    ) -> AsyncIterator[TraeEvent]:
        host = _clean_host(account.get("agent_host") or self.settings.agent_host, self.settings.agent_host)
        url = host + "/api/agent/v3/llm_utils_chat"
        decoder = SSEDecoder()
        headers = self.ide_headers(account, sse=True)
        conversation = str(body.get("conversation_id") or body.get("session_id") or uuid.uuid4().hex)
        turn = str(body.get("request_id") or conversation)
        message_id = uuid.uuid4().hex
        b3_trace = turn if len(turn) in (16, 32) and all(c in "0123456789abcdefABCDEF" for c in turn) else message_id
        headers.update(
            {
                "X-Conversation-ID": conversation,
                "X-Conversation-Request-ID": turn,
                "X-Conversation-Message-ID": message_id,
                "X-Request-ID": message_id,
                "X-Root-Request-ID": turn,
                "X-Trace-ID": turn,
                "X-B3-TraceId": b3_trace,
                "X-B3-SpanId": message_id[:16],
                "X-B3-Sampled": "1",
            }
        )
        try:
            async with self.http.stream(
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=self._timeout(),
            ) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")[:800]
                    code = 1001 if resp.status_code == 401 else 0
                    raise TraeError(
                        f"上游 HTTP {resp.status_code}: {text}",
                        http_status=resp.status_code,
                        code=code,
                        retryable=resp.status_code in (401, 403, 408, 409, 425, 429) or resp.status_code >= 500,
                    )
                async for line in resp.aiter_lines():
                    frame = decoder.feed_line(line)
                    if frame is None:
                        continue
                    if frame.data.strip() == "[DONE]":
                        yield TraeEvent("done", {"finish_reason": "stop", "_done": True}, frame.data)
                        return
                    data = parse_json(frame.data)
                    if data is None:
                        continue
                    event = frame.event.lower()
                    if event == "error":
                        raise TraeError(
                            f"Trae 错误 {data.get('code', 0)}: {data.get('message', '')}",
                            code=int(data.get("code") or 0),
                            retryable=int(data.get("code") or 0) in (429, 4008, 4011),
                        )
                    yield TraeEvent(event, data, frame.data)
                frame = decoder.finish()
                if frame and frame.data.strip() == "[DONE]":
                    yield TraeEvent("done", {"finish_reason": "stop", "_done": True}, frame.data)
        except httpx.HTTPError as exc:
            raise TraeError(f"上游连接错误: {exc}", retryable=True) from exc

    # ------------------------------------------------------------------ usage / credits
    async def get_credits(self, account: dict[str, Any]) -> Optional[float]:
        detail = await self.get_credit_detail(account)
        return detail.get("remaining") if detail else None

    async def get_credit_detail(self, account: dict[str, Any]) -> Optional[dict[str, Any]]:
        if str(account.get("region") or "cn").lower() in ("global", "intl", "international"):
            host = self._global_quota_host(account)
            try:
                resp = await self.http.post(
                    host + "/trae/api/v1/pay/user_current_entitlement_list",
                    json={},
                    headers=self.ide_headers(account, sse=False),
                    timeout=self._timeout(20),
                )
            except httpx.HTTPError:
                return None
            if resp.status_code != 200:
                return None
            return self._global_credit_detail_from_entitlements(_parse_json_object(resp.text))
        host = _clean_host(account.get("ug_host") or self.settings.ug_host, self.settings.ug_host)
        try:
            resp = await self.http.post(
                host + "/trae/api/v2/pay/ide_user_ent_usage",
                json={"require_usage": True, "req_source": 0},
                headers=self.ug_headers(account),
                timeout=self._timeout(20),
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        data = _parse_json_object(resp.text)
        return self._credit_detail_from_usage(data)

    @staticmethod
    def _global_quota_host(account: dict[str, Any]) -> str:
        raw = account.get("raw_json") or account.get("raw") or {}
        if isinstance(raw, str):
            raw = _parse_json_object(raw)
        region = ""
        if isinstance(raw, dict):
            region = str(raw.get("AIRegion") or raw.get("Region") or "").upper()
        oauth_host = str(account.get("oauth_host") or "").lower()
        if "US" in region or "us-east" in oauth_host:
            return "https://api-us-east.trae.ai"
        return "https://api-sg-central.trae.ai"

    @staticmethod
    def _global_credit_detail_from_entitlements(data: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not isinstance(data, dict):
            return None
        pack_list = data.get("user_entitlement_pack_list")
        if not isinstance(pack_list, list):
            pack_list = _deep_find(data, ("user_entitlement_pack_list",))
        if not isinstance(pack_list, list):
            return None
        dollar_billing = bool(data.get("is_dollar_usage_billing"))
        packs: list[dict[str, Any]] = []
        for item in pack_list:
            if not isinstance(item, dict):
                continue
            base = item.get("entitlement_base_info")
            if not isinstance(base, dict):
                base = {}
            quota = base.get("quota") if isinstance(base.get("quota"), dict) else {}
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            basic_limit = _as_number(quota.get("basic_usage_limit"), 0)
            bonus_limit = _as_number(quota.get("bonus_usage_limit"), 0)
            credits_limit = _as_number(quota.get("credits_limit"), 0)
            basic_used = _as_number(usage.get("basic_usage_amount"), 0)
            bonus_used = _as_number(usage.get("bonus_usage_amount"), 0)
            credits_used = _as_number(usage.get("credits_amount"), 0)
            if dollar_billing or basic_limit or bonus_limit:
                limit = basic_limit + bonus_limit
                used = basic_used + bonus_used
                unit = "USD"
            else:
                limit = credits_limit
                used = credits_used
                unit = "credits"
            remain = max(0.0, limit - used)
            if not (limit or used or remain):
                continue
            expiry = to_epoch(base.get("end_time") or item.get("expire_time") or item.get("yearly_expire_time"))
            name = item.get("display_desc") or item.get("group_name") or "Trae Global"
            packs.append(make_pack(name, remain, limit, used, expiry, "trae-global", unit))
        packs = normalize_packs(packs)
        if not packs:
            return None
        remaining = sum(float(pack["remain"]) for pack in packs)
        used = sum(float(pack["used"]) for pack in packs)
        total = sum(float(pack["total"]) for pack in packs)
        return {"remaining": remaining, "used": used, "total": total, "packs": packs}

    @staticmethod
    def _credit_detail_from_usage(data: dict[str, Any]) -> Optional[dict[str, float]]:
        summary = _deep_find(data, ("usage_summary",))
        summary_values: dict[str, float] | None = None
        if isinstance(summary, dict):
            remaining = _as_number(summary.get("remaining_amount", summary.get("available_amount")), -1)
            used = _as_number(summary.get("consumed_amount"), -1)
            total = _as_number(summary.get("total_amount"), -1)
            if remaining >= 0 or used >= 0 or total >= 0:
                if remaining < 0 and total >= 0 and used >= 0:
                    remaining = max(0.0, total - used)
                if total < 0 and remaining >= 0 and used >= 0:
                    total = remaining + used
                summary_values = {
                    "remaining": max(0.0, remaining),
                    "used": max(0.0, used),
                    "total": max(0.0, total),
                }

        pack_list = _deep_find(data, ("user_entitlement_pack_list",))
        packs: list[dict[str, Any]] = []
        if isinstance(pack_list, list):
            for item in pack_list:
                if not isinstance(item, dict):
                    continue
                base = item.get("entitlement_base_info")
                if not isinstance(base, dict):
                    base = {}
                expiry = to_epoch(
                    item.get("expire_time")
                    or item.get("yearly_expire_time")
                    or base.get("end_time")
                )
                quota = base.get("quota") if isinstance(base.get("quota"), dict) else {}
                limit = _as_number(quota.get("credits_limit"), -1)
                if limit < 0:
                    continue
                usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
                consumed = _as_number(usage.get("credits_amount"), 0)
                remain = max(0.0, limit - consumed)
                extra = base.get("product_extra") if isinstance(base.get("product_extra"), dict) else {}
                package_extra = (
                    extra.get("package_extra") if isinstance(extra.get("package_extra"), dict) else {}
                )
                subscription_extra = (
                    extra.get("subscription_extra")
                    if isinstance(extra.get("subscription_extra"), dict)
                    else {}
                )
                name = (
                    item.get("group_name")
                    or item.get("display_desc")
                    or package_extra.get("package_name")
                    or subscription_extra.get("package_name")
                    or "积分包"
                )
                packs.append(
                    make_pack(
                        name,
                        remain,
                        limit,
                        consumed,
                        expiry,
                        "trae",
                    )
                )
        packs = normalize_packs(packs)
        if packs:
            remaining_total = sum(float(pack["remain"]) for pack in packs)
            used_total = sum(float(pack["used"]) for pack in packs)
            limit_total = sum(float(pack["total"]) for pack in packs)
            return {
                "remaining": remaining_total,
                "used": used_total,
                "total": limit_total,
                "packs": packs,
            }
        if summary_values is not None:
            return {
                **summary_values,
                "packs": [
                    make_pack(
                        "总积分",
                        summary_values["remaining"],
                        summary_values["total"],
                        summary_values["used"],
                        0,
                        "trae-summary",
                    )
                ],
            }
        return None

    async def get_pay_identity(self, account: dict[str, Any]) -> int:
        host = _clean_host(account.get("ug_host") or self.settings.ug_host, self.settings.ug_host)
        try:
            resp = await self.http.post(
                host + "/trae/api/v2/pay/ide_user_pay_status",
                json={
                    "trae_client": "IDE",
                    "device_id": account.get("device_id") or account.get("machine_id") or "",
                },
                headers=self.ide_headers(account, sse=False),
                timeout=self._timeout(15),
            )
        except httpx.HTTPError:
            return -1
        if resp.status_code != 200:
            return -1
        data = _parse_json_object(resp.text)
        try:
            return int(data.get("user_pay_identity", -1))
        except (TypeError, ValueError):
            return -1

    # ------------------------------------------------------------------ check-in
    async def checkin_status(self, account: dict[str, Any]) -> dict[str, Any]:
        return await self._ug_post(account, "/trae/api/v2/ug/checkin_credits/status")

    async def checkin_claim(self, account: dict[str, Any]) -> dict[str, Any]:
        return await self._ug_post(account, "/trae/api/v2/ug/checkin_credits/claim")

    async def _ug_post(self, account: dict[str, Any], path: str) -> dict[str, Any]:
        host = _clean_host(account.get("ug_host") or self.settings.ug_host, self.settings.ug_host)
        try:
            resp = await self.http.post(
                host + path,
                json={"req_source": 1},
                headers=self.ug_headers(account),
                timeout=self._timeout(20),
            )
        except httpx.HTTPError as exc:
            raise TraeError(f"签到接口网络错误: {exc}", retryable=True) from exc
        if resp.status_code != 200:
            raise TraeError(
                f"签到接口 HTTP {resp.status_code}: {resp.text[:300]}",
                http_status=resp.status_code,
                retryable=resp.status_code >= 500,
            )
        return _parse_json_object(resp.text)

    # ------------------------------------------------------------------ login url
    def build_login_url(
        self,
        callback_url: str,
        trace_id: str,
        device_id: str,
        machine_id: str,
        code_challenge: str,
        console_host: str = "",
    ) -> str:
        params = {
            "login_version": "1",
            "auth_from": "trae",
            "login_channel": "native_ide",
            "plugin_version": "2.3.62834",
            "auth_type": "local",
            "client_id": self.settings.client_id,
            "redirect": "0",
            "login_trace_id": trace_id,
            "auth_callback_url": callback_url,
            "machine_id": machine_id,
            "device_id": device_id,
            "x_device_id": device_id,
            "x_machine_id": machine_id,
            "x_device_brand": "PC",
            "x_device_type": "PC",
            "x_os_version": "1.0",
            "x_env": "",
            "x_app_version": self.settings.ide_version,
            "x_app_type": "stable",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return (console_host or self.settings.console_host).rstrip("/") + "/authorization?" + urlencode(params)
