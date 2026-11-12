from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..config import Settings
from .base import ProviderError, StreamChunk, empty_usage, merge_usage, normalize_base_url


class AnthropicCompatProvider:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self.http = http

    def _headers(self, api_key: str, stream: bool = False) -> dict[str, str]:
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def _base(self, provider: dict[str, Any]) -> str:
        base = normalize_base_url(provider.get("base_url", ""), "https://api.anthropic.com")
        return base

    async def list_models(self, provider: dict[str, Any], account: dict[str, Any]) -> list[dict[str, Any]]:
        extra = provider.get("models_json") or "[]"
        try:
            configured = json.loads(extra) if isinstance(extra, str) else extra
        except Exception:
            configured = []
        if configured:
            return [
                {
                    "id": str(item.get("id") if isinstance(item, dict) else item),
                    "object": "model",
                    "owned_by": provider.get("name", "anthropic"),
                    "context_length": int(item.get("context_length") or 0) if isinstance(item, dict) else 200000,
                }
                for item in configured
                if item
            ]
        base = self._base(provider)
        try:
            resp = await self.http.get(
                base + "/models",
                headers=self._headers(account.get("api_key", "")),
                timeout=20,
            )
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        return [
            {
                "id": str(row["id"]),
                "object": "model",
                "owned_by": provider.get("name", "anthropic"),
                "context_length": int(row.get("context_length") or 200000),
            }
            for row in rows
            if isinstance(row, dict) and row.get("id")
        ]

    def _convert(self, payload: dict[str, Any]) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for raw in payload.get("messages") or []:
            if not isinstance(raw, dict):
                continue
            role = raw.get("role")
            content = raw.get("content")
            if role in ("system", "developer"):
                if isinstance(content, str):
                    system_parts.append(content)
                elif isinstance(content, list):
                    system_parts.extend(
                        str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") in ("text", "input_text")
                    )
                continue
            if role == "tool":
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                block = {
                    "type": "tool_result",
                    "tool_use_id": raw.get("tool_call_id") or "unknown",
                    "content": text,
                }
                messages.append({"role": "user", "content": [block]})
                continue
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str):
                if content:
                    blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") in ("text", "input_text"):
                        text = str(part.get("text") or "")
                        if text:
                            blocks.append({"type": "text", "text": text})
                    elif part.get("type") in ("image_url", "input_image"):
                        blocks.append({"type": "text", "text": "[image omitted: Anthropic custom provider requires base64 image]"})
            if role == "assistant" and isinstance(raw.get("tool_calls"), list):
                for call in raw["tool_calls"]:
                    fn = call.get("function") if isinstance(call, dict) else {}
                    if not isinstance(fn, dict) or not fn.get("name"):
                        continue
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:20]}",
                            "name": fn["name"],
                            "input": args,
                        }
                    )
            if blocks:
                messages.append({"role": "assistant" if role == "assistant" else "user", "content": blocks})
        if not messages:
            messages = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
        body: dict[str, Any] = {
            "model": payload.get("model"),
            "messages": messages,
            "max_tokens": int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 4096),
            "stream": bool(payload.get("stream")),
        }
        if system_parts:
            body["system"] = "\n".join(system_parts)
        if payload.get("temperature") is not None:
            body["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            body["top_p"] = payload["top_p"]
        if payload.get("stop"):
            stops = payload["stop"] if isinstance(payload["stop"], list) else [payload["stop"]]
            body["stop_sequences"] = [str(s) for s in stops if s]
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            converted = []
            for tool in tools:
                fn = tool.get("function") if isinstance(tool, dict) and isinstance(tool.get("function"), dict) else tool
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                params = fn.get("parameters") or {"type": "object", "properties": {}}
                if isinstance(params, str):
                    try:
                        params = json.loads(params)
                    except Exception:
                        params = {"type": "object", "properties": {}}
                converted.append(
                    {
                        "name": fn["name"],
                        "description": str(fn.get("description") or ""),
                        "input_schema": params,
                    }
                )
            if converted:
                body["tools"] = converted
                choice = payload.get("tool_choice")
                if isinstance(choice, str):
                    body["tool_choice"] = {"type": "any" if choice == "required" else "auto"}
                elif isinstance(choice, dict):
                    fn = choice.get("function") if isinstance(choice.get("function"), dict) else choice
                    if fn.get("name"):
                        body["tool_choice"] = {"type": "tool", "name": fn["name"]}
        return body

    @staticmethod
    def _finish_reason(reason: str | None) -> str:
        if reason == "max_tokens":
            return "length"
        if reason == "tool_use":
            return "tool_calls"
        return "stop"

    def _to_completion(self, data: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        content_parts = []
        reasoning_parts = []
        tool_calls = []
        for block in data.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                content_parts.append(str(block.get("text") or ""))
            elif block.get("type") == "thinking":
                reasoning_parts.append(str(block.get("thinking") or ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "",
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = data.get("usage") or {}
        prompt = int(usage.get("input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": data.get("model") or payload.get("model") or "",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": self._finish_reason(data.get("stop_reason")),
                }
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
                "prompt_tokens_details": {"cached_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0},
            },
        }

    async def chat_complete(
        self,
        provider: dict[str, Any],
        account: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        base = self._base(provider)
        body = self._convert({**payload, "stream": False})
        try:
            resp = await self.http.post(
                base + "/messages",
                headers=self._headers(account.get("api_key", "")),
                json=body,
                timeout=self.settings.upstream_timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic 上游网络错误: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(
                f"Anthropic 上游 HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
                retryable=resp.status_code in (401, 403, 408, 409, 425, 429) or resp.status_code >= 500,
            )
        try:
            data = resp.json()
        except Exception as exc:
            raise ProviderError("Anthropic 返回非 JSON") from exc
        if isinstance(data.get("error"), dict):
            raise ProviderError(str(data["error"].get("message") or data["error"]), retryable=False)
        return self._to_completion(data, payload)

    @staticmethod
    def _chunk(chunk_id: str, model: str, delta: dict[str, Any], finish: str | None = None, usage: dict[str, int] | None = None) -> str:
        payload: dict[str, Any] = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        if usage is not None:
            payload["usage"] = usage
        return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"

    async def chat_stream(
        self,
        provider: dict[str, Any],
        account: dict[str, Any],
        payload: dict[str, Any],
    ) -> AsyncIterator[StreamChunk]:
        base = self._base(provider)
        body = self._convert({**payload, "stream": True})
        chunk_id = "chatcmpl-" + uuid.uuid4().hex
        model = str(payload.get("model") or "")
        usage = empty_usage()
        tool_indexes: dict[int, int] = {}
        next_tool_index = 0
        finish = "stop"
        event_name = ""
        data_lines: list[str] = []
        try:
            async with self.http.stream(
                "POST",
                base + "/messages",
                headers=self._headers(account.get("api_key", ""), stream=True),
                json=body,
                timeout=httpx.Timeout(self.settings.upstream_timeout, connect=20),
            ) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise ProviderError(
                        f"Anthropic 上游 HTTP {resp.status_code}: {text}",
                        status_code=resp.status_code,
                        retryable=resp.status_code in (401, 403, 408, 409, 425, 429) or resp.status_code >= 500,
                    )
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                        continue
                    if line != "":
                        continue
                    if not data_lines:
                        event_name = ""
                        continue
                    raw = "\n".join(data_lines)
                    data_lines = []
                    try:
                        data = json.loads(raw)
                    except Exception:
                        event_name = ""
                        continue
                    ev = event_name or str(data.get("type") or "")
                    event_name = ""
                    if ev == "message_start":
                        message = data.get("message") or {}
                        merge_usage(usage, message.get("usage"))
                        model = str(message.get("model") or model)
                    elif ev == "content_block_start":
                        index = int(data.get("index") or 0)
                        block = data.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            tool_index = next_tool_index
                            next_tool_index += 1
                            tool_indexes[index] = tool_index
                            yield StreamChunk(
                                self._chunk(
                                    chunk_id,
                                    model,
                                    {
                                        "tool_calls": [
                                            {
                                                "index": tool_index,
                                                "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                                                "type": "function",
                                                "function": {"name": block.get("name") or "", "arguments": ""},
                                            }
                                        ]
                                    },
                                ),
                                usage,
                            )
                    elif ev == "content_block_delta":
                        index = int(data.get("index") or 0)
                        delta = data.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text = str(delta.get("text") or "")
                            if text:
                                yield StreamChunk(self._chunk(chunk_id, model, {"content": text}), usage)
                        elif dtype == "thinking_delta":
                            text = str(delta.get("thinking") or "")
                            if text:
                                yield StreamChunk(self._chunk(chunk_id, model, {"reasoning_content": text}), usage)
                        elif dtype == "input_json_delta":
                            partial = str(delta.get("partial_json") or "")
                            if partial and index in tool_indexes:
                                yield StreamChunk(
                                    self._chunk(
                                        chunk_id,
                                        model,
                                        {
                                            "tool_calls": [
                                                {
                                                    "index": tool_indexes[index],
                                                    "function": {"arguments": partial},
                                                }
                                            ]
                                        },
                                    ),
                                    usage,
                                )
                    elif ev == "message_delta":
                        delta = data.get("delta") or {}
                        finish = self._finish_reason(delta.get("stop_reason"))
                        merge_usage(usage, data.get("usage"))
                    elif ev == "message_stop":
                        final_usage = {
                            "prompt_tokens": usage["prompt_tokens"],
                            "completion_tokens": usage["completion_tokens"],
                            "total_tokens": usage["total_tokens"] or usage["prompt_tokens"] + usage["completion_tokens"],
                            "prompt_tokens_details": {"cached_tokens": usage["cached_tokens"]},
                            "completion_tokens_details": {"reasoning_tokens": usage["reasoning_tokens"]},
                        }
                        yield StreamChunk(self._chunk(chunk_id, model, {}, finish, final_usage), usage)
                        yield StreamChunk("data: [DONE]\n\n", usage)
                        return
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic 连接错误: {exc}") from exc
