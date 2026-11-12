from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator, Optional

from ..config import Settings
from ..db import Database
from ..errors import OpenAIError
from ..providers.base import ProviderError, empty_usage
from ..providers.anthropic_compat import AnthropicCompatProvider
from ..providers.openai_compat import OpenAICompatProvider
from ..request_policy import apply_request_policy
from .accounts import AccountService
from .chat import ChatService
from .pool import AccountPool
from .providers import ProviderService
from .usage import UsageService
from .virtuals import VirtualService


class RouterService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        accounts: AccountService,
        chat: ChatService,
        usage: UsageService,
        providers: ProviderService,
        virtuals: VirtualService,
        http,
        pool: Optional[AccountPool] = None,
    ):
        self.settings = settings
        self.db = db
        self.accounts = accounts
        self.chat = chat
        self.usage = usage
        self.providers = providers
        self.virtuals = virtuals
        self.http = http
        self.pool = pool or AccountPool(db)
        self.openai = OpenAICompatProvider(settings, http)
        self.anthropic = AnthropicCompatProvider(settings, http)

    # ------------------------------------------------------------------ models
    async def all_models(self) -> list[dict[str, Any]]:
        """模型列表只暴露已配置的虚拟模型组。"""
        result: list[dict[str, Any]] = []
        for group in self.virtuals.list_groups(include_disabled=False):
            result.append(
                {
                    "id": group["name"],
                    "object": "model",
                    "created": int(group.get("created_at") or 1753600000),
                    "owned_by": "virtual",
                    "display_name": group.get("display_name") or group["name"],
                    "context_length": 131072,
                    "provider": "virtual",
                    "protocol": "virtual",
                }
            )
        return result

    # ------------------------------------------------------------------ routing
    def resolve(self, payload: dict[str, Any], headers: dict[str, str], query: dict[str, str]) -> dict[str, Any]:
        model = str(payload.get("model") or self.db.get_setting("default_model", self.settings.default_model)).strip()
        if not model:
            model = self.db.get_setting("default_model", self.settings.default_model)
        providers = self.providers.list_providers(include_disabled=False)
        has_any = bool(self.accounts.list_accounts(include_disabled=False)) or any(
            p.get("account_count", 0) > 0 for p in providers
        )
        if not has_any:
            raise OpenAIError(
                "没有可用平台账号",
                status_code=503,
                error_type="api_error",
                code="no_available_account",
            )
        group = self.virtuals.get_by_name(model)
        if group:
            return {"type": "virtual", "virtual_model": model, "model": model, "group": group}
        header_provider = (
            headers.get("x-aiall-provider")
            or headers.get("x-traeapi-provider")
            or headers.get("x-platform")
            or query.get("provider")
            or ""
        ).strip()
        if header_provider:
            provider = self.providers.get_provider(header_provider)
            if not provider:
                raise OpenAIError(
                    f"平台不存在: {header_provider}",
                    status_code=400,
                    error_type="invalid_request_error",
                    code="invalid_provider",
                )
            return {
                "type": "provider",
                "provider_id": header_provider,
                "model": self._strip_provider(model, header_provider),
                "virtual_model": "",
            }
        for sep in (":", "/"):
            if sep in model:
                prefix, rest = model.split(sep, 1)
                alias = {"cn": "trae-cn", "global": "trae-global", "intl": "trae-global"}.get(prefix.lower())
                if alias and rest:
                    provider = self.providers.get_provider(alias)
                    if provider and provider.get("enabled"):
                        return {
                            "type": "provider",
                            "provider_id": alias,
                            "model": rest,
                            "virtual_model": "",
                        }
                provider = self.providers.get_provider(prefix)
                if provider and rest:
                    return {
                        "type": "provider",
                        "provider_id": prefix,
                        "model": rest,
                        "virtual_model": "",
                    }
        provider = self.providers.resolve_provider_for_model(model)
        if provider:
            return {
                "type": "provider",
                "provider_id": provider["id"],
                "model": model,
                "virtual_model": "",
            }
        if self.accounts.list_accounts(include_disabled=False):
            return {"type": "provider", "provider_id": "trae-cn", "model": model, "virtual_model": ""}
        raise OpenAIError(
            f"找不到模型或可用平台: {model}",
            status_code=404,
            error_type="invalid_request_error",
            code="model_not_found",
        )

    @staticmethod
    def _strip_provider(model: str, provider_id: str) -> str:
        for prefix in (provider_id + ":", provider_id + "/"):
            if model.startswith(prefix):
                return model[len(prefix) :]
        return model

    # ------------------------------------------------------------------ non-stream
    def _check_daily_budget(self) -> None:
        status = self.pool.check_budget()
        if not status["allowed"]:
            raise OpenAIError(
                f"已达当日积分上限：已用 {status['used']:.2f} / 上限 {status['limit']:.2f}",
                status_code=429,
                error_type="rate_limit_error",
                code="daily_budget_exceeded",
            )

    async def complete(
        self,
        payload: dict[str, Any],
        api_key: dict[str, Any],
        ip: str,
        endpoint: str = "/v1/chat/completions",
        headers: Optional[dict[str, str]] = None,
        query: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        self._check_daily_budget()
        payload = apply_request_policy(payload, self.db)
        payload = apply_request_policy(payload, self.db)
        route = self.resolve(payload, headers or {}, query or {})
        session_key = self.pool.session_key(payload, headers or {})
        if route["type"] == "virtual":
            result = await self._complete_virtual(route, payload, api_key, ip, endpoint, session_key)
            return self._rewrite_response_model(result, route["virtual_model"])
        return await self._complete_provider(
            route["provider_id"],
            route["model"],
            payload,
            api_key,
            ip,
            endpoint,
            route.get("virtual_model", ""),
            session_key,
        )

    async def _complete_virtual(self, route, payload, api_key, ip, endpoint, session_key: str = "") -> dict[str, Any]:
        group = route["group"]
        members = self.virtuals.ordered_members(group)
        if not members:
            raise OpenAIError("虚拟模型组没有可用成员", status_code=503, error_type="api_error", code="virtual_empty")
        last: Exception | None = None
        for member in members:
            try:
                return await self._complete_provider(
                    member["provider_id"],
                    member["model"],
                    payload,
                    api_key,
                    ip,
                    endpoint,
                    virtual_model=group["name"],
                    session_key=session_key,
                )
            except Exception as exc:
                last = exc
                continue
        if isinstance(last, OpenAIError):
            raise last
        raise OpenAIError(f"虚拟模型组全部成员失败: {last}", status_code=502, error_type="api_error", code="virtual_failed")

    def _accounts_for(
        self,
        provider_id: str,
        model: str = "",
        session_key: str = "",
        exclude: Optional[set[int]] = None,
    ) -> list[dict[str, Any]]:
        rows = self.providers.accounts(provider_id, include_disabled=False)
        accounts = self.pool.candidates(provider_id, model, exclude, session_key, rows)
        if accounts:
            return accounts
        return self.providers.ordered_accounts(provider_id)

    async def _complete_provider(
        self,
        provider_id: str,
        model: str,
        payload: dict[str, Any],
        api_key: dict[str, Any],
        ip: str,
        endpoint: str,
        virtual_model: str = "",
        session_key: str = "",
    ) -> dict[str, Any]:
        provider = self.providers.get_provider(provider_id)
        if not provider or not provider.get("enabled"):
            raise OpenAIError(
                f"平台未启用: {provider_id}", status_code=503, error_type="api_error", code="provider_disabled"
            )
        body = {**payload, "model": model}
        if provider["kind"] == "trae":
            return await self.chat.complete(
                body,
                api_key,
                ip,
                endpoint,
                provider_id=provider_id,
                virtual_model=virtual_model,
                region=provider.get("region", ""),
            )
        adapter = self.providers.adapter(provider)
        if adapter is None:
            accounts = self._accounts_for(provider_id, model, session_key)
            if not accounts:
                raise OpenAIError(
                    f"平台没有可用账号: {provider_id}", status_code=503, error_type="api_error",
                    code="provider_no_account",
                )
            return await self._complete_custom(
                provider, accounts, body, api_key, ip, endpoint, virtual_model, session_key
            )
        accounts = self._accounts_for(provider_id, model, session_key)
        if not accounts:
            raise OpenAIError(
                f"平台没有可用账号: {provider_id}", status_code=503, error_type="api_error", code="provider_no_account"
            )
        started = time.time()
        last: Exception | None = None
        for account in accounts:
            self.pool.acquire(int(account["id"]))
            try:
                result = await adapter.chat_complete(provider, account, body)
                usage = result.get("usage") or {}
                self.pool.mark_success(
                    int(account["id"]),
                    provider_id,
                    model,
                    float(usage.get("credits") or -1),
                    int(usage.get("total_tokens") or 0),
                )
                self.pool.bind_session(session_key, provider_id, int(account["id"]))
                self._log_custom(
                    api_key, provider, account, model, virtual_model, endpoint, False, usage, started, ip, "ok", ""
                )
                return result
            except ProviderError as exc:
                last = exc
                self.pool.mark_failure(
                    int(account["id"]),
                    provider_id,
                    model,
                    int(getattr(exc, "status_code", 0) or 0),
                    str(exc),
                )
                if not exc.retryable:
                    break
            finally:
                self.pool.release(int(account["id"]))
        self._log_custom(
            api_key, provider, accounts[0], model, virtual_model, endpoint, False, {}, started, ip, "error",
            str(last or "全部账号失败"),
        )
        raise self._map_provider_error(last or ProviderError("全部账号失败"))

    async def _complete_custom(
        self,
        provider: dict[str, Any],
        accounts: list[dict[str, Any]],
        body: dict[str, Any],
        api_key: dict[str, Any],
        ip: str,
        endpoint: str,
        virtual_model: str,
        session_key: str = "",
    ) -> dict[str, Any]:
        started = time.time()
        last: Exception | None = None
        for account in accounts:
            self.pool.acquire(int(account["id"]))
            try:
                if provider["protocol"] == "anthropic":
                    result = await self.anthropic.chat_complete(provider, account, body)
                else:
                    result = await self.openai.chat_complete(provider, account, body)
                usage = result.get("usage") or {}
                self.pool.mark_success(
                    int(account["id"]),
                    provider["id"],
                    body["model"],
                    float(usage.get("credits") or -1),
                    int(usage.get("total_tokens") or 0),
                )
                self.pool.bind_session(session_key, provider["id"], int(account["id"]))
                self._log_custom(
                    api_key, provider, account, body["model"], virtual_model, endpoint, False, usage, started, ip, "ok", ""
                )
                return result
            except ProviderError as exc:
                last = exc
                self.pool.mark_failure(
                    int(account["id"]),
                    provider["id"],
                    body["model"],
                    int(getattr(exc, "status_code", 0) or 0),
                    str(exc),
                )
                if not exc.retryable:
                    break
            finally:
                self.pool.release(int(account["id"]))
        self._log_custom(
            api_key, provider, accounts[0], body["model"], virtual_model, endpoint, False, {}, started, ip, "error",
            str(last or "全部账号失败"),
        )
        raise self._map_provider_error(last or ProviderError("全部账号失败"))

    # ------------------------------------------------------------------ stream
    async def stream(
        self,
        payload: dict[str, Any],
        api_key: dict[str, Any],
        ip: str,
        endpoint: str = "/v1/chat/completions",
        headers: Optional[dict[str, str]] = None,
        query: Optional[dict[str, str]] = None,
    ) -> AsyncIterator[str]:
        try:
            self._check_daily_budget()
        except OpenAIError as exc:
            yield self._error_frame(exc.message, exc.code)
            return
        route = self.resolve(payload, headers or {}, query or {})
        session_key = self.pool.session_key(payload, headers or {})
        if route["type"] == "virtual":
            group_name = route["virtual_model"]
            async for chunk in self._stream_virtual(route, payload, api_key, ip, endpoint, session_key):
                yield self._rewrite_stream_model(chunk, group_name)
            return
        async for chunk in self._stream_provider(
            route["provider_id"],
            route["model"],
            payload,
            api_key,
            ip,
            endpoint,
            route.get("virtual_model", ""),
            session_key,
        ):
            yield chunk

    async def _stream_virtual(self, route, payload, api_key, ip, endpoint, session_key: str = "") -> AsyncIterator[str]:
        group = route["group"]
        members = self.virtuals.ordered_members(group)
        if not members:
            yield self._error_frame("虚拟模型组没有可用成员", "virtual_empty")
            return
        for member in members:
            gen = self._stream_provider(
                member["provider_id"],
                member["model"],
                payload,
                api_key,
                ip,
                endpoint,
                virtual_model=group["name"],
                session_key=session_key,
            )
            try:
                first = await gen.__anext__()
            except StopAsyncIteration:
                continue
            except Exception:
                continue
            if first.startswith('data: {"error"'):
                try:
                    await gen.aclose()
                except Exception:
                    pass
                continue
            yield first
            async for chunk in gen:
                yield chunk
            return
        yield self._error_frame("虚拟模型组全部成员失败", "virtual_failed")

    async def _stream_provider(
        self,
        provider_id: str,
        model: str,
        payload: dict[str, Any],
        api_key: dict[str, Any],
        ip: str,
        endpoint: str,
        virtual_model: str = "",
        session_key: str = "",
    ) -> AsyncIterator[str]:
        provider = self.providers.get_provider(provider_id)
        if not provider or not provider.get("enabled"):
            yield self._error_frame(f"平台未启用: {provider_id}", "provider_disabled")
            return
        body = {**payload, "model": model}
        if provider["kind"] == "trae":
            async for chunk in self.chat.stream(
                body,
                api_key,
                ip,
                endpoint,
                provider_id=provider_id,
                virtual_model=virtual_model,
                region=provider.get("region", ""),
            ):
                yield chunk
            return
        adapter = self.providers.adapter(provider)
        accounts = self._accounts_for(provider_id, model, session_key)
        if not accounts:
            yield self._error_frame(f"平台没有可用账号: {provider_id}", "provider_no_account")
            return
        started = time.time()
        first_token_at = 0.0
        last: Exception | None = None
        for account in accounts:
            usage = empty_usage()
            self.pool.acquire(int(account["id"]))
            try:
                if adapter is not None:
                    gen = adapter.chat_stream(provider, account, body)
                elif provider["protocol"] == "anthropic":
                    gen = self.anthropic.chat_stream(provider, account, body)
                else:
                    gen = self.openai.chat_stream(provider, account, body)
                first = await gen.__anext__()
                if not first_token_at:
                    first_token_at = time.time()
                yield first.line
                usage = first.usage
                async for chunk in gen:
                    usage = chunk.usage or usage
                    yield chunk.line
                self.pool.mark_success(
                    int(account["id"]),
                    provider_id,
                    model,
                    float(usage.get("credits") or -1),
                    int(usage.get("total_tokens") or 0),
                )
                self.pool.bind_session(session_key, provider_id, int(account["id"]))
                self._log_custom(
                    api_key, provider, account, model, virtual_model, endpoint, True, usage, started, ip, "ok", "",
                    ttft_ms=int((first_token_at - started) * 1000) if first_token_at else 0,
                )
                return
            except StopAsyncIteration:
                # 上游 200 但一个 chunk 都没产出（空流 / 全是不可解析帧）。
                # 旧实现只 continue，导致 last=None、日志退化成「全部账号失败」，
                # 且失败账号不进 mark_failure，永不冷却、反复重试刷屏。
                err = ProviderError(
                    f"{provider_id} 上游返回空流（HTTP 200 但无可用 SSE 帧）",
                    status_code=502,
                    retryable=True,
                )
                last = err
                self.pool.mark_failure(int(account["id"]), provider_id, model, 502, str(err))
                continue
            except ProviderError as exc:
                last = exc
                self.pool.mark_failure(
                    int(account["id"]),
                    provider_id,
                    model,
                    int(getattr(exc, "status_code", 0) or 0),
                    str(exc),
                )
                if not exc.retryable:
                    break
            finally:
                self.pool.release(int(account["id"]))
        error_text = str(last or "全部账号失败")
        self._log_custom(
            api_key, provider, accounts[0], model, virtual_model, endpoint, True, {}, started, ip, "error",
            error_text,
        )
        yield self._error_frame(error_text, "provider_failed")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _rewrite_response_model(result: dict[str, Any], group_name: str) -> dict[str, Any]:
        """虚拟组统一对外模型名；只改响应，日志仍记录真实上游模型。"""
        if group_name and isinstance(result, dict) and isinstance(result.get("model"), str):
            if result["model"] != group_name:
                return {**result, "model": group_name}
        return result

    @staticmethod
    def _rewrite_stream_model(chunk: str, group_name: str) -> str:
        """逐 SSE chunk 改写 data 行的 model；[DONE]、错误帧、非 JSON 行原样透传。"""
        if not group_name or not chunk or "data:" not in chunk:
            return chunk
        out: list[str] = []
        changed = False
        for line in chunk.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            newline = line[len(body) :]
            lead = body[: len(body) - len(body.lstrip())]
            data_line = body.lstrip()
            if data_line.startswith("data:"):
                raw = data_line[5:].strip()
                if raw and raw != "[DONE]":
                    try:
                        data = json.loads(raw)
                    except (ValueError, TypeError):
                        out.append(line)
                        continue
                    if isinstance(data, dict) and isinstance(data.get("model"), str) and data["model"] != group_name:
                        data["model"] = group_name
                        out.append(
                            lead + "data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + newline
                        )
                        changed = True
                        continue
            out.append(line)
        return "".join(out) if changed else chunk

    @staticmethod
    def _error_frame(message: str, code: str) -> str:
        payload = {"error": {"message": message, "type": "api_error", "code": code}}
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"

    @staticmethod
    def _map_provider_error(exc: Exception) -> OpenAIError:
        if isinstance(exc, OpenAIError):
            return exc
        status = getattr(exc, "status_code", 0) or 0
        if status == 401:
            return OpenAIError(str(exc), status_code=401, error_type="authentication_error", code="upstream_auth")
        if status == 429:
            return OpenAIError(str(exc), status_code=429, error_type="rate_limit_error", code="rate_limit")
        return OpenAIError(str(exc), status_code=502, error_type="api_error", code="upstream_error")

    def _log_custom(
        self,
        api_key: dict[str, Any],
        provider: dict[str, Any],
        account: dict[str, Any],
        model: str,
        virtual_model: str,
        endpoint: str,
        stream: bool,
        usage: dict[str, Any],
        started: float,
        ip: str,
        status: str,
        error: str,
        ttft_ms: int = 0,
    ) -> None:
        latency_ms = int((time.time() - started) * 1000)
        first_token_ms = int(ttft_ms or latency_ms)
        completion_ms = max(0, latency_ms - first_token_ms) if stream else latency_ms
        self.usage.record(
            request_id="chatcmpl-" + uuid.uuid4().hex,
            api_key_id=api_key.get("id"),
            api_key_name=api_key.get("name", ""),
            account_id=0,
            account_name=f"{provider['id']}/{account.get('name', '')}",
            endpoint=endpoint,
            model=model,
            stream=stream,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            cached_tokens=int(usage.get("cached_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_tokens") or usage.get("cached_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
            cache_miss_tokens=int(usage.get("cache_miss_tokens") or 0),
            reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
            credits_before=-1,
            credits_after=-1,
            credits_delta=0,
            credits_estimated=0,
            latency_ms=latency_ms,
            ttft_ms=first_token_ms,
            completion_ms=completion_ms,
            status=status,
            error=error,
            ip=ip,
            provider_id=provider["id"],
            virtual_model=virtual_model,
        )
