from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Optional

from ..config import Settings
from ..db import Database
from ..errors import OpenAIError
from ..upstream.client import TraeClient, TraeError
from ..upstream.sse import extract_usage
from .accounts import AccountService
from .usage import UsageService

log = logging.getLogger("aiall.chat")


def sanitize_tool_tags(text: str) -> str:
    return (
        text.replace("</opencode_tool_call>", "</tool_invoke>")
        .replace("<opencode_tool_call>", "<tool_invoke>")
        .replace("</opencode_tool_result>", "</tool_result>")
        .replace("<opencode_tool_result", "<tool_result")
    )


class StopFilter:
    def __init__(self, stops: list[str]):
        self.stops = [s for s in stops if s]
        self.pending = ""
        self.hit = False
        self.hold = max((len(s) for s in self.stops), default=1) - 1

    def feed(self, text: str) -> str:
        if self.hit or not self.stops:
            return text if not self.hit else ""
        self.pending += text
        best = -1
        for stop in self.stops:
            pos = self.pending.find(stop)
            if pos >= 0 and (best < 0 or pos < best):
                best = pos
        if best >= 0:
            out = self.pending[:best]
            self.pending = ""
            self.hit = True
            return out
        if len(self.pending) > self.hold:
            cut = len(self.pending) - self.hold
            out = self.pending[:cut]
            self.pending = self.pending[cut:]
            return out
        return ""

    def flush(self) -> str:
        out = self.pending
        self.pending = ""
        return out


class ToolAccumulator:
    def __init__(self) -> None:
        self.items: dict[int, dict[str, Any]] = {}

    def feed(self, raw: Any) -> None:
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            return
        for i, call in enumerate(raw):
            if not isinstance(call, dict):
                continue
            index = int(call.get("index", i) or 0)
            item = self.items.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = call.get("id") or ""
            if call_id:
                item["id"] = call_id
            fn = call.get("function_call") if isinstance(call.get("function_call"), dict) else call.get("function")
            if not isinstance(fn, dict):
                continue
            name = fn.get("name") or ""
            if name:
                item["name"] = name
            partial = fn.get("partial_arguments") or ""
            args = fn.get("arguments") or ""
            if partial:
                item["arguments"] += str(partial)
            elif args:
                existing = item["arguments"]
                text = str(args)
                if not existing:
                    item["arguments"] = text
                elif text.startswith(existing):
                    item["arguments"] = text
                elif text != existing:
                    item["arguments"] += text

    def to_openai(self) -> list[dict[str, Any]]:
        result = []
        for index in sorted(self.items):
            item = self.items[index]
            if not item["name"]:
                continue
            result.append(
                {
                    "index": index,
                    "id": item["id"] or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": item["name"],
                        "arguments": item["arguments"] or "{}",
                    },
                }
            )
        return result

    def to_message(self) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in item.items() if k != "index"}
            for item in self.to_openai()
        ]


class ChatService:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        accounts: AccountService,
        usage: UsageService,
        client: TraeClient,
    ):
        self.settings = settings
        self.db = db
        self.accounts = accounts
        self.usage = usage
        self.client = client

    # ------------------------------------------------------------------ request mapping
    @staticmethod
    def _content_parts(content: Any) -> list[dict[str, Any]]:
        if isinstance(content, str):
            text = sanitize_tool_tags(content)
            return [{"type": "text", "text": text}] if text else []
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype in ("text", "input_text"):
                    text = sanitize_tool_tags(str(part.get("text") or ""))
                    if text:
                        parts.append({"type": "text", "text": text})
                elif ptype in ("image_url", "input_image"):
                    image = part.get("image_url") or part.get("image") or {}
                    if isinstance(image, str):
                        image = {"url": image}
                    if isinstance(image, dict) and image.get("url"):
                        parts.append({"type": "image_url", "image_url": {"url": image["url"]}})
            return parts
        return []

    def normalize_messages(self, messages: Any) -> list[dict[str, Any]]:
        if not isinstance(messages, list):
            raise OpenAIError("messages 必须是数组", status_code=400, error_type="invalid_request_error", code="invalid_messages")
        output = []
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "user")
            if role == "developer":
                role = "system"
            item: dict[str, Any] = {"role": role}
            if role == "assistant" and isinstance(raw.get("tool_calls"), list):
                calls = []
                for call in raw["tool_calls"]:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = str(fn.get("name") or "").strip()
                    if not name:
                        continue
                    calls.append(
                        {
                            "id": call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function_call": {
                                "name": name,
                                "arguments": str(fn.get("arguments") or "{}"),
                            },
                        }
                    )
                if calls:
                    item["tool_calls"] = calls
            if role == "tool":
                item["tool_call_id"] = raw.get("tool_call_id") or "unknown"
                if raw.get("name"):
                    item["name"] = raw.get("name")
            parts = self._content_parts(raw.get("content"))
            if parts:
                item["content"] = parts
            elif role == "assistant" and item.get("tool_calls"):
                item["content"] = []
            elif role != "assistant" or not item.get("tool_calls"):
                item["content"] = [{"type": "text", "text": ""}]
            output.append(item)
        if not output:
            output.append({"role": "user", "content": [{"type": "text", "text": ""}]})
        return output

    @staticmethod
    def normalize_tools(tools: Any, tool_choice: Any) -> tuple[list[dict[str, Any]], str]:
        if not isinstance(tools, list):
            return [], ""
        normalized = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type", "function") != "function":
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            params = fn.get("parameters")
            if not isinstance(params, str):
                params = json.dumps(params or {}, ensure_ascii=False, separators=(",", ":"))
            normalized.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": str(fn.get("description") or ""),
                        "parameters": params,
                    },
                }
            )
        choice = ""
        if isinstance(tool_choice, str):
            value = tool_choice.lower().strip()
            if value == "none":
                return [], ""
            if value in ("auto", "required"):
                choice = value
        elif isinstance(tool_choice, dict):
            fn = tool_choice.get("function") if isinstance(tool_choice.get("function"), dict) else tool_choice
            name = str(fn.get("name") or "").strip()
            if name:
                normalized = [t for t in normalized if t["function"]["name"] == name]
                choice = name
        return normalized, choice

    async def prepare_body(
        self, payload: dict[str, Any], account: dict[str, Any], requested_model: str
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        config_name, caps = await self.accounts.resolve_model(account, requested_model)
        messages = self.normalize_messages(payload.get("messages"))
        tools, tool_choice = self.normalize_tools(payload.get("tools") or payload.get("functions"), payload.get("tool_choice"))
        session_id = str(uuid.uuid4())
        function = str(caps.get("ide_function") or self.db.get_setting("chat_function", "solo_work_remote"))
        model_name = str(caps.get("model_name") or config_name)
        body: dict[str, Any] = {
            "session_id": session_id,
            "conversation_id": session_id,
            "messages": messages,
            "function": function,
            "stream": True,
            "config_name": config_name,
            "model": model_name,
            "model_name": model_name,
            "config_source": int(caps.get("config_source") or 1),
            "is_custom_model": False,
            "provider": str(caps.get("provider") or ""),
        }
        if isinstance(payload.get("thinking"), dict):
            body["thinking"] = payload["thinking"]
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        options = [str(x).lower() for x in (caps.get("reasoning_options") or [])]
        if effort and (not options or effort in options):
            body["reasoning_effort"] = effort
        if bool(payload.get("trae_max") or payload.get("max_mode")):
            if caps.get("max_model_name"):
                body["model"] = caps["max_model_name"]
                body["model_name"] = caps["max_model_name"]
            body["mode_type"] = 1
            body["context_window_size"] = int(payload.get("context_window_size") or 1000000)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        return body, config_name, caps

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _chunk(
        chunk_id: str,
        model: str,
        delta: dict[str, Any],
        finish: Optional[str] = None,
        usage: Optional[dict[str, int]] = None,
    ) -> str:
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

    @staticmethod
    def _error_frame(message: str, code: str = "upstream_error") -> str:
        payload = {"error": {"message": message, "type": "api_error", "code": code}}
        return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"

    @staticmethod
    def _usage_dict(usage: dict[str, int]) -> dict[str, int]:
        return {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
            "prompt_tokens_details": {"cached_tokens": int(usage.get("cached_tokens") or 0)},
            "completion_tokens_details": {"reasoning_tokens": int(usage.get("reasoning_tokens") or 0)},
        }

    def _map_error(self, exc: TraeError) -> OpenAIError:
        if exc.http_status == 401 or exc.code == 1001:
            return OpenAIError(str(exc), status_code=401, error_type="authentication_error", code="upstream_auth")
        if exc.http_status == 503:
            return OpenAIError(str(exc), status_code=503, error_type="api_error", code="no_available_account")
        if exc.code == 1005:
            return OpenAIError(str(exc), status_code=403, error_type="insufficient_quota", code="insufficient_quota")
        if exc.http_status == 429 or exc.code in (429, 4008, 4011):
            return OpenAIError(str(exc), status_code=429, error_type="rate_limit_error", code="rate_limit")
        if exc.code == 4001:
            return OpenAIError(str(exc), status_code=400, error_type="invalid_request_error", code="invalid_model")
        return OpenAIError(str(exc), status_code=502, error_type="api_error", code="upstream_error")

    async def _finalize(
        self,
        *,
        request_id: str,
        api_key: dict[str, Any],
        account: dict[str, Any],
        endpoint: str,
        model: str,
        caps: dict[str, Any],
        stream: bool,
        usage: dict[str, int],
        started: float,
        ip: str,
        status: str = "ok",
        error: str = "",
        credits_before: float = -1,
        credits_after: float = -1,
        provider_id: str = "trae-cn",
        virtual_model: str = "",
        ttft_ms: int = 0,
    ) -> None:
        before = credits_before
        after = credits_after
        if after < 0:
            try:
                after = await self.client.get_credits(account)
            except Exception:
                after = None
        if after is not None:
            self.db.execute(
                "UPDATE accounts SET credits=?, credits_updated_at=?, last_used_at=?, updated_at=? WHERE id=?",
                (float(after), int(time.time()), int(time.time()), int(time.time()), int(account["id"])),
            )
        delta = 0.0
        if before >= 0 and after is not None:
            delta = max(0.0, float(before) - float(after))
        estimated = self.accounts.estimate_credits(
            caps, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        )
        latency_ms = int((time.time() - started) * 1000)
        first_token_ms = int(ttft_ms or latency_ms)
        completion_ms = max(0, latency_ms - first_token_ms) if stream else latency_ms
        self.usage.record(
            request_id=request_id,
            api_key_id=api_key.get("id"),
            api_key_name=api_key.get("name", ""),
            account_id=account.get("id"),
            account_name=account.get("name", ""),
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
            credits_before=before,
            credits_after=after if after is not None else -1,
            credits_delta=delta,
            credits_estimated=estimated,
            latency_ms=latency_ms,
            ttft_ms=first_token_ms,
            completion_ms=completion_ms,
            status=status,
            error=error,
            ip=ip,
            provider_id=provider_id,
            virtual_model=virtual_model,
        )

    # ------------------------------------------------------------------ non-stream
    async def complete(
        self,
        payload: dict[str, Any],
        api_key: dict[str, Any],
        ip: str,
        endpoint: str = "/v1/chat/completions",
        provider_id: str = "trae-cn",
        virtual_model: str = "",
        region: str = "",
    ) -> dict[str, Any]:
        started = time.time()
        request_id = "chatcmpl-" + uuid.uuid4().hex
        requested_model = str(payload.get("model") or self.db.get_setting("default_model", self.settings.default_model))
        exclude: set[int] = set()
        last_error: Optional[TraeError] = None
        for attempt in range(2):
            account, sem = await self.accounts.pick_account(exclude=exclude, region=region)
            try:
                body, model, caps = await self.prepare_body(payload, account, requested_model)
                credits_before = float(account.get("credits") or -1)
                first_token_at = 0.0
                content: list[str] = []
                reasoning: list[str] = []
                usage: dict[str, int] = {}
                tools = ToolAccumulator()
                finish = "stop"
                stop_filter = StopFilter(payload.get("stop") if isinstance(payload.get("stop"), list) else [payload.get("stop")] if payload.get("stop") else [])
                async for event in self.client.chat_stream(account, body):
                    data = event.data
                    if event.event in ("output", "message", "text"):
                        tools.feed(data.get("tool_calls"))
                        text = data.get("content") or data.get("response") or ""
                        reasoning_piece = data.get("reasoning") or data.get("reasoning_content")
                        if (text or reasoning_piece) and not first_token_at:
                            first_token_at = time.time()
                        if text:
                            piece = stop_filter.feed(str(text))
                            if piece:
                                content.append(piece)
                        for key in ("reasoning", "reasoning_content"):
                            if data.get(key):
                                reasoning.append(str(data[key]))
                    elif event.event in ("token_usage", "notify_usage", "usage"):
                        extracted = extract_usage(data)
                        if extracted["total_tokens"] or extracted["prompt_tokens"] or extracted["completion_tokens"]:
                            usage = extracted
                    elif event.event == "done":
                        finish = str(data.get("finish_reason") or "stop")
                        if data.get("_done"):
                            break
                tail = stop_filter.flush()
                if tail:
                    content.append(tail)
                tool_calls = tools.to_message()
                if tool_calls:
                    finish = "tool_calls"
                message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
                if reasoning:
                    message["reasoning_content"] = "".join(reasoning)
                if tool_calls:
                    message["tool_calls"] = tool_calls
                result = {
                    "id": request_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": self._usage_dict(usage),
                }
                await self._finalize(
                    request_id=request_id,
                    api_key=api_key,
                    account=account,
                    endpoint=endpoint,
                    model=model,
                    caps=caps,
                    stream=False,
                    usage=usage,
                    started=started,
                    ip=ip,
                    credits_before=credits_before,
                    provider_id=provider_id,
                    virtual_model=virtual_model,
                    ttft_ms=int((first_token_at - started) * 1000) if first_token_at else 0,
                )
                return result
            except TraeError as exc:
                last_error = exc
                if attempt == 0 and (exc.http_status == 401 or exc.code == 1001):
                    try:
                        await self.accounts.ensure_token(account, force=True)
                        continue
                    except TraeError:
                        exclude.add(int(account["id"]))
                        continue
                if attempt == 0 and exc.retryable:
                    exclude.add(int(account["id"]))
                    continue
                await self._finalize(
                    request_id=request_id,
                    api_key=api_key,
                    account=account,
                    endpoint=endpoint,
                    model=requested_model,
                    caps={},
                    stream=False,
                    usage={},
                    started=started,
                    ip=ip,
                    status="error",
                    error=str(exc),
                    provider_id=provider_id,
                    virtual_model=virtual_model,
                )
                raise self._map_error(exc) from exc
            finally:
                self.accounts.release(sem)
        raise self._map_error(last_error or TraeError("上游请求失败"))

    # ------------------------------------------------------------------ stream
    async def stream(
        self,
        payload: dict[str, Any],
        api_key: dict[str, Any],
        ip: str,
        endpoint: str = "/v1/chat/completions",
        provider_id: str = "trae-cn",
        virtual_model: str = "",
        region: str = "",
    ) -> AsyncIterator[str]:
        started = time.time()
        request_id = "chatcmpl-" + uuid.uuid4().hex
        requested_model = str(payload.get("model") or self.db.get_setting("default_model", self.settings.default_model))
        include_usage = bool((payload.get("stream_options") or {}).get("include_usage"))
        exclude: set[int] = set()
        last_error: Optional[TraeError] = None
        for attempt in range(2):
            try:
                account, sem = await self.accounts.pick_account(exclude=exclude, region=region)
            except TraeError as exc:
                last_error = exc
                yield self._error_frame(str(exc), f"upstream_{exc.http_status or exc.code or 'error'}")
                return
            emitted = False
            try:
                body, model, caps = await self.prepare_body(payload, account, requested_model)
                credits_before = float(account.get("credits") or -1)
                first_token_at = 0.0
                usage: dict[str, int] = {}
                tools = ToolAccumulator()
                stop_filter = StopFilter(payload.get("stop") if isinstance(payload.get("stop"), list) else [payload.get("stop")] if payload.get("stop") else [])
                finish = "stop"
                async for event in self.client.chat_stream(account, body):
                    data = event.data
                    if event.event in ("output", "message", "text"):
                        tools.feed(data.get("tool_calls"))
                        text = data.get("content") or data.get("response") or ""
                        if text:
                            piece = stop_filter.feed(str(text))
                            if piece:
                                emitted = True
                                if not first_token_at:
                                    first_token_at = time.time()
                                yield self._chunk(request_id, model, {"content": piece})
                        reasoning = data.get("reasoning") or data.get("reasoning_content")
                        if reasoning:
                            emitted = True
                            if not first_token_at:
                                first_token_at = time.time()
                            yield self._chunk(request_id, model, {"reasoning_content": str(reasoning)})
                    elif event.event in ("token_usage", "notify_usage", "usage"):
                        extracted = extract_usage(data)
                        if extracted["total_tokens"] or extracted["prompt_tokens"] or extracted["completion_tokens"]:
                            usage = extracted
                    elif event.event == "done":
                        finish = str(data.get("finish_reason") or "stop")
                        if data.get("_done"):
                            break
                tail = stop_filter.flush()
                if tail:
                    emitted = True
                    yield self._chunk(request_id, model, {"content": tail})
                tool_calls = tools.to_openai()
                if tool_calls:
                    emitted = True
                    yield self._chunk(request_id, model, {"tool_calls": tool_calls})
                    finish = "tool_calls"
                final_usage = self._usage_dict(usage) if include_usage else None
                yield self._chunk(request_id, model, {}, finish, final_usage)
                yield "data: [DONE]\n\n"
                await self._finalize(
                    request_id=request_id,
                    api_key=api_key,
                    account=account,
                    endpoint=endpoint,
                    model=model,
                    caps=caps,
                    stream=True,
                    usage=usage,
                    started=started,
                    ip=ip,
                    credits_before=credits_before,
                    provider_id=provider_id,
                    virtual_model=virtual_model,
                    ttft_ms=int((first_token_at - started) * 1000) if first_token_at else 0,
                )
                return
            except TraeError as exc:
                last_error = exc
                if not emitted and attempt == 0 and (exc.http_status == 401 or exc.code == 1001):
                    try:
                        await self.accounts.ensure_token(account, force=True)
                        continue
                    except TraeError:
                        exclude.add(int(account["id"]))
                        continue
                if not emitted and attempt == 0 and exc.retryable:
                    exclude.add(int(account["id"]))
                    continue
                yield self._error_frame(str(exc), f"upstream_{exc.http_status or exc.code or 'error'}")
                await self._finalize(
                    request_id=request_id,
                    api_key=api_key,
                    account=account,
                    endpoint=endpoint,
                    model=requested_model,
                    caps={},
                    stream=True,
                    usage={},
                    started=started,
                    ip=ip,
                    status="error",
                    error=str(exc),
                    provider_id=provider_id,
                    virtual_model=virtual_model,
                )
                return
            finally:
                self.accounts.release(sem)
        if last_error:
            yield self._error_frame(str(last_error), "upstream_error")
