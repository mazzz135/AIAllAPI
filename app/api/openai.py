from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from ..deps import client_ip, require_api_key, state
from ..errors import OpenAIError
from ..upstream.client import TraeError

router = APIRouter()

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Cache-Control": "no-cache",
}


def _static_models() -> list[dict[str, Any]]:
    names = [
        "DeepSeek-V4-Flash",
        "DeepSeek-V4-Pro",
        "Doubao-Seed-Evolving",
        "GLM-5.2",
        "Kimi-K2",
    ]
    return [
        {
            "id": name,
            "object": "model",
            "created": 1753600000,
            "owned_by": "trae",
            "display_name": name,
            "context_length": 131072,
        }
        for name in names
    ]


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as exc:
        raise OpenAIError(
            "请求体不是合法 JSON",
            status_code=400,
            error_type="invalid_request_error",
            code="invalid_json",
        ) from exc
    if not isinstance(body, dict):
        raise OpenAIError(
            "请求体必须是 JSON 对象",
            status_code=400,
            error_type="invalid_request_error",
            code="invalid_request",
        )
    return body


@router.get("/health")
@router.get("/healthz")
async def health(request: Request):
    ctx = state(request)
    return {
        "status": "ok",
        "name": ctx.settings.app_name,
        "version": ctx.settings.version,
        "time": int(time.time()),
    }


@router.options("/v1/{path:path}")
async def options(path: str):
    return JSONResponse({}, headers=CORS_HEADERS)


@router.get("/v1/models")
@router.get("/models")
async def models(request: Request, key: dict = Depends(require_api_key)):
    ctx = state(request)
    try:
        items = await ctx.router.all_models()
    except TraeError:
        items = _static_models()
    data = []
    for item in items:
        data.append(
            {
                "id": item["id"],
                "object": "model",
                "created": int(item.get("created") or 1753600000),
                "owned_by": "trae",
                "display_name": item.get("display_name") or item["id"],
                "context_length": int(item.get("context_length") or 131072),
            }
        )
    return JSONResponse({"object": "list", "data": data}, headers=CORS_HEADERS)


@router.post("/v1/chat/completions")
@router.post("/chat/completions")
async def chat_completions(request: Request, key: dict = Depends(require_api_key)):
    ctx = state(request)
    body = await _json_body(request)
    stream = bool(body.get("stream"))
    ip = client_ip(request, ctx.settings.trust_proxy)
    try:
        if stream:
            generator = ctx.router.stream(
                body,
                key,
                ip,
                headers={k.lower(): v for k, v in request.headers.items()},
                query=dict(request.query_params),
            )
            return StreamingResponse(
                generator,
                media_type="text/event-stream",
                headers={
                    **CORS_HEADERS,
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )
        return JSONResponse(
            await ctx.router.complete(
                body,
                key,
                ip,
                headers={k.lower(): v for k, v in request.headers.items()},
                query=dict(request.query_params),
            ),
            headers=CORS_HEADERS,
        )
    except OpenAIError as exc:
        return JSONResponse(exc.body(), status_code=exc.status_code, headers=CORS_HEADERS)
    except TraeError as exc:
        mapped = ctx.chat._map_error(exc)
        return JSONResponse(mapped.body(), status_code=mapped.status_code, headers=CORS_HEADERS)


def _responses_input(body: dict[str, Any]) -> list[dict[str, Any]]:
    instructions = body.get("instructions")
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": str(instructions)})
    raw = body.get("input")
    if isinstance(raw, str):
        messages.append({"role": "user", "content": raw})
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                if item.get("type") in ("message", None) and item.get("role"):
                    messages.append(
                        {
                            "role": item.get("role"),
                            "content": item.get("content", ""),
                        }
                    )
                elif item.get("type") in ("input_text", "text"):
                    messages.append({"role": "user", "content": item.get("text", "")})
    if not messages:
        messages.append({"role": "user", "content": ""})
    return messages


def _to_response(completion: dict[str, Any]) -> dict[str, Any]:
    choice = (completion.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content") or ""
    output = []
    if message.get("tool_calls"):
        for call in message["tool_calls"]:
            output.append(
                {
                    "type": "function_call",
                    "id": call.get("id"),
                    "call_id": call.get("id"),
                    "name": (call.get("function") or {}).get("name"),
                    "arguments": (call.get("function") or {}).get("arguments"),
                    "status": "completed",
                }
            )
    if text:
        output.append(
            {
                "type": "message",
                "id": "msg_" + uuid.uuid4().hex[:24],
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    usage = completion.get("usage") or {}
    return {
        "id": "resp_" + uuid.uuid4().hex[:24],
        "object": "response",
        "created_at": completion.get("created") or int(time.time()),
        "status": "completed",
        "model": completion.get("model", ""),
        "output": output,
        "output_text": text,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
    }


@router.post("/v1/responses")
async def responses(request: Request, key: dict = Depends(require_api_key)):
    ctx = state(request)
    body = await _json_body(request)
    chat_body = {
        "model": body.get("model"),
        "messages": _responses_input(body),
        "tools": body.get("tools"),
        "tool_choice": body.get("tool_choice"),
        "reasoning_effort": body.get("reasoning", {}).get("effort")
        if isinstance(body.get("reasoning"), dict)
        else None,
        "stream": False,
    }
    ip = client_ip(request, ctx.settings.trust_proxy)
    try:
        completion = await ctx.router.complete(
            chat_body,
            key,
            ip,
            endpoint="/v1/responses",
            headers={k.lower(): v for k, v in request.headers.items()},
            query=dict(request.query_params),
        )
        response = _to_response(completion)
        if body.get("stream"):
            return StreamingResponse(
                _responses_stream(response),
                media_type="text/event-stream",
                headers={**CORS_HEADERS, "X-Accel-Buffering": "no"},
            )
        return JSONResponse(response, headers=CORS_HEADERS)
    except OpenAIError as exc:
        return JSONResponse(exc.body(), status_code=exc.status_code, headers=CORS_HEADERS)
    except TraeError as exc:
        mapped = ctx.chat._map_error(exc)
        return JSONResponse(mapped.body(), status_code=mapped.status_code, headers=CORS_HEADERS)


async def _responses_stream(response: dict[str, Any]):
    created = {
        "type": "response.created",
        "response": {**response, "status": "in_progress", "output": []},
    }
    yield "event: response.created\ndata: " + json.dumps(created, ensure_ascii=False) + "\n\n"
    text = response.get("output_text") or ""
    if text:
        delta = {
            "type": "response.output_text.delta",
            "item_id": response["output"][0]["id"] if response.get("output") else "",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        }
        yield "event: response.output_text.delta\ndata: " + json.dumps(delta, ensure_ascii=False) + "\n\n"
    completed = {"type": "response.completed", "response": response}
    yield "event: response.completed\ndata: " + json.dumps(completed, ensure_ascii=False) + "\n\n"


@router.get("/metrics")
async def metrics(request: Request):
    """Prometheus 文本格式 0.0.4；默认关闭，开启后与 /status 同级鉴权。"""
    ctx = state(request)
    if not ctx.observability.metrics_enabled():
        return JSONResponse({"detail": "metrics disabled"}, status_code=404)
    await require_api_key(request)
    text = ctx.observability.render_prometheus()
    return PlainTextResponse(
        text,
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/v1/status")
async def status(request: Request, key: dict = Depends(require_api_key)):
    ctx = state(request)
    accounts = ctx.accounts.list_accounts(include_disabled=False)
    snapshot = ctx.accounts.snapshot()
    pool_snapshot = ctx.observability.pool_snapshot()
    return {
        "status": "ok",
        "accounts": [
            {
                "id": a["id"],
                "name": a["name"],
                "enabled": bool(a["enabled"]),
                "credits": a["credits"],
                "credits_updated_at": a["credits_updated_at"],
                "manual_disabled": bool(a.get("manual_disabled")),
                "manual_reason": a.get("manual_reason", ""),
                "last_error": a["last_error"],
            }
            for a in accounts
        ],
        "today": {
            "requests": snapshot["today_requests"],
            "tokens": snapshot["today_tokens"],
            "credits": snapshot["today_credits"],
        },
        "models": [m["id"] for m in await ctx.router.all_models()],
        "platforms": [
            {
                "id": p["id"],
                "name": p["name"],
                "kind": p["kind"],
                "region": p.get("region", ""),
                "enabled": bool(p["enabled"]),
                "accounts": p.get("account_count", 0),
                "models": p.get("model_count", 0),
            }
            for p in ctx.providers.list_providers()
        ],
        "model_costs": ctx.pool.cost_report(),
        "cost_explore": ctx.pool.explore_report(),
        "rate_limited_models": ctx.pool.model_cooldown_report(),
        "daily_budget": ctx.pool.budget_status(),
        "pool": {
            "max_in_flight": ctx.pool._max_in_flight(),
            "expiring_soon_days": ctx.pool.expiring_soon_days(),
            "explore_interval": ctx.pool.explore_interval(),
            "explore_max_per_day": ctx.pool.explore_max_per_day(),
        },
        "pool_health": {
            "realms": pool_snapshot["realms"],
            "sessions": pool_snapshot["sessions"],
            "explores": pool_snapshot["explores"],
            "explores_all": pool_snapshot.get("explores_all", {}),
            "waf_blocked": pool_snapshot["waf_blocked"],
        },
        "alerting": ctx.alerting.state_report(),
        "task_ledger": ctx.scheduler.task_ledger(),
    }


@router.get("/v1/usage")
async def usage(request: Request, key: dict = Depends(require_api_key)):
    ctx = state(request)
    summary = ctx.usage.summary(api_key_id=key.get("id") or None)
    rows, total = ctx.usage.page(page=1, limit=20, api_key_id=key.get("id") or 0)
    return {"summary": summary, "total": total, "recent": rows}
