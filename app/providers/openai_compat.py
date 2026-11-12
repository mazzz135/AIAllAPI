from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..config import Settings
from .base import ProviderError, StreamChunk, empty_usage, merge_usage, normalize_base_url


class OpenAICompatProvider:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    def _headers(self, api_key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def list_models(self, provider: dict[str, Any], account: dict[str, Any]) -> list[dict[str, Any]]:
        base = normalize_base_url(provider.get("base_url", ""))
        if not base:
            return []
        try:
            resp = await self.http.get(
                base + "/models",
                headers=self._headers(account.get("api_key", "")),
                timeout=httpx.Timeout(20, connect=10),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"模型同步网络错误: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"模型同步 HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
                retryable=resp.status_code >= 500 or resp.status_code == 429,
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError("模型同步返回非 JSON") from exc
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                result.append(
                    {
                        "id": str(row["id"]),
                        "object": "model",
                        "owned_by": str(row.get("owned_by") or provider.get("name") or "custom"),
                        "context_length": int(row.get("context_length") or 0) or 131072,
                    }
                )
        return result

    async def chat_complete(
        self,
        provider: dict[str, Any],
        account: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        base = normalize_base_url(provider.get("base_url", ""))
        if not base:
            raise ProviderError("自定义服务商缺少 base_url", retryable=False)
        body = dict(payload)
        body["stream"] = False
        try:
            resp = await self.http.post(
                base + "/chat/completions",
                headers=self._headers(account.get("api_key", "")),
                json=body,
                timeout=self.settings.upstream_timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"上游网络错误: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"上游 HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
                retryable=resp.status_code in (401, 403, 408, 409, 425, 429) or resp.status_code >= 500,
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError("上游返回非 JSON") from exc
        if isinstance(data.get("error"), dict):
            raise ProviderError(str(data["error"].get("message") or data["error"]), retryable=False)
        return data

    async def chat_stream(
        self,
        provider: dict[str, Any],
        account: dict[str, Any],
        payload: dict[str, Any],
    ) -> AsyncIterator[StreamChunk]:
        base = normalize_base_url(provider.get("base_url", ""))
        if not base:
            raise ProviderError("自定义服务商缺少 base_url", retryable=False)
        body = dict(payload)
        body["stream"] = True
        usage = empty_usage()
        try:
            async with self.http.stream(
                "POST",
                base + "/chat/completions",
                headers={**self._headers(account.get("api_key", "")), "Accept": "text/event-stream"},
                json=body,
                timeout=httpx.Timeout(self.settings.upstream_timeout, connect=20),
            ) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise ProviderError(
                        f"上游 HTTP {resp.status_code}: {text}",
                        status_code=resp.status_code,
                        retryable=resp.status_code in (401, 403, 408, 409, 425, 429) or resp.status_code >= 500,
                    )
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            yield StreamChunk("data: [DONE]\n\n", usage)
                            return
                        try:
                            data = json.loads(raw)
                            merge_usage(usage, data.get("usage"))
                        except Exception:
                            pass
                    yield StreamChunk(line + "\n\n", usage)
        except httpx.HTTPError as exc:
            raise ProviderError(f"上游连接错误: {exc}") from exc
