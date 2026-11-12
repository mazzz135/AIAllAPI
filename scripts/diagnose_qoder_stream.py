"""受控诊断：抓取 Qoder chat 端点原始响应，定位「200 但无可用 data 帧」。

只读诊断：不写库、不写 usage_logs、不修改任何账号/配置，
并且只打印凭证字段名，不打印 token / uid 等账号信息。

用法（生产服务器项目根目录执行）::

    /www/server/pyporject_evn/Traeapi/bin/python scripts/diagnose_qoder_stream.py \
        qoder-cn dfmodel --repeat=3 --details
    /www/server/pyporject_evn/Traeapi/bin/python scripts/diagnose_qoder_stream.py \
        qoder-cn dfmodel --concurrency=6 --details

输出：HTTP 状态、响应头、响应体前若干字节，以及按 ``QoderProvider.chat_stream()``
相同规则解析出的帧统计；``yielded=0`` 即复现「全部账号失败」的上游响应。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.providers.qoder import QoderProvider, _unwrap, encode_body, sign  # noqa: E402
from app.security import SecretBox, load_or_create_secret  # noqa: E402
from app.services.providers import ProviderService  # noqa: E402

MAX_BYTES = 16 * 1024
MAX_SECONDS = 60.0


def _frame_stats(raw: bytes) -> dict[str, object]:
    """复刻 chat_stream() 的解析口径：空行/非 data/JSON 失败/无 choices 全部跳过。"""
    lines = raw.decode("utf-8", "replace").splitlines()
    stats: dict[str, object] = {
        "lines": len(lines),
        "data_lines": 0,
        "done": 0,
        "json_ok": 0,
        "json_fail": 0,
        "yielded": 0,
        "skipped": 0,
        "error_frames": 0,
        "non_data_lines": 0,
        "frames": [],
    }
    for line in lines:
        if not line or not line.startswith("data:"):
            stats["non_data_lines"] = int(stats["non_data_lines"]) + 1
            continue
        stats["data_lines"] = int(stats["data_lines"]) + 1
        body = line[5:].strip()
        if body == "[DONE]":
            stats["done"] = int(stats["done"]) + 1
            stats["yielded"] = int(stats["yielded"]) + 1
            continue
        try:
            frame = json.loads(body)
            stats["json_ok"] = int(stats["json_ok"]) + 1
        except Exception:
            stats["json_fail"] = int(stats["json_fail"]) + 1
            continue
        inner = _unwrap(frame)
        detail = {
            "wrapper_keys": sorted(frame.keys()) if isinstance(frame, dict) else [],
            "statusCodeValue": frame.get("statusCodeValue") if isinstance(frame, dict) else None,
            "inner_keys": sorted(inner.keys()),
            "model": inner.get("model"),
            "choices": len(inner.get("choices") or []),
            "error": str(inner.get("error"))[:300] if inner.get("error") else "",
        }
        frames = stats["frames"]
        assert isinstance(frames, list)
        frames.append(detail)
        if inner.get("error"):
            stats["error_frames"] = int(stats["error_frames"]) + 1
            stats["yielded"] = int(stats["yielded"]) + 1
        elif inner.get("choices"):
            stats["yielded"] = int(stats["yielded"]) + 1
        else:
            stats["skipped"] = int(stats["skipped"]) + 1
    return stats


async def _collect(resp: httpx.Response, deadline: float) -> bytes:
    buf = bytearray()
    async for piece in resp.aiter_bytes():
        buf.extend(piece)
        if len(buf) >= MAX_BYTES or time.monotonic() > deadline:
            break
    return bytes(buf)


async def request_raw(
    http: httpx.AsyncClient,
    q: QoderProvider,
    provider: dict,
    account: dict,
    model: str,
    payload_chars: int = 0,
    payload_override: dict | None = None,
) -> tuple[int, dict[str, str], bytes, int, str]:
    content = "只回复 OK"
    if payload_chars > 0:
        filler = "上下文压力测试 ABCDEFG 0123456789 abcdefghijklmnopqrstuvwxyz "
        content = (filler * (payload_chars // len(filler) + 1))[:payload_chars] + "\n请只回复 OK"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": True,
        "temperature": 0.1,
    }
    if payload_override:
        payload = dict(payload_override)
        payload["model"] = model
    url = q.chat_url(provider)
    body = encode_body(
        json.dumps(q._chat_payload(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    headers = {**sign(q.session(account), body, url), "Accept": "text/event-stream"}
    req = http.build_request("POST", url, headers=headers, content=body)
    started = time.time()
    try:
        resp = await http.send(req, stream=True)
    except Exception as exc:
        return 0, {}, b"", int((time.time() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:200]}"
    status = resp.status_code
    hdrs = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() in ("content-type", "content-length", "transfer-encoding", "x-request-id", "cache-control")
    }
    try:
        deadline = time.monotonic() + MAX_SECONDS
        try:
            raw = await asyncio.wait_for(_collect(resp, deadline), timeout=MAX_SECONDS + 2)
            note = ""
        except asyncio.TimeoutError:
            raw, note = b"", "timeout"
    finally:
        await resp.aclose()
    return status, hdrs, raw, int((time.time() - started) * 1000), note


def print_result(label: str, model: str, result: tuple, details: bool) -> dict[str, object]:
    status, hdrs, raw, elapsed, note = result
    stats = _frame_stats(raw)
    frames = stats.pop("frames", [])
    print(
        f"  {label} [{model}] http={status} bytes={len(raw)} elapsed={elapsed}ms "
        f"hdrs={json.dumps(hdrs, ensure_ascii=False)} note={note!r}"
    )
    print(f"  {label} [{model}] stats={json.dumps(stats, ensure_ascii=False)}")
    if details or int(stats["yielded"]) == 0:
        print(f"  {label} [{model}] body={raw[:2000]!r}")
        for idx, frame in enumerate(frames):  # type: ignore[union-attr]
            print(f"  {label} [{model}] frame[{idx}]={json.dumps(frame, ensure_ascii=False)}")
    return stats


async def main() -> None:
    raw_args = sys.argv[1:]
    details = "--details" in raw_args
    variants = "--variants" in raw_args
    via_adapter = "--via-adapter" in raw_args
    repeat = 1
    concurrency = 1
    payload_chars = 0
    account_filter = 0
    for arg in raw_args:
        if arg.startswith("--repeat="):
            repeat = max(1, min(20, int(arg.split("=", 1)[1])))
        if arg.startswith("--concurrency="):
            concurrency = max(1, min(12, int(arg.split("=", 1)[1])))
        if arg.startswith("--payload-chars="):
            payload_chars = max(0, min(400000, int(arg.split("=", 1)[1])))
        if arg.startswith("--account-id="):
            account_filter = int(arg.split("=", 1)[1])
    args = [a for a in raw_args if not a.startswith("--")]
    provider_id = args[0] if args else "qoder-cn"
    models = args[1:] or ["dfmodel"]

    settings.ensure_dirs()
    db = Database(settings.db_path)
    box = SecretBox(load_or_create_secret(settings))
    http = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(60, connect=20),
        limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
        headers={"Accept-Encoding": "identity"},
    )
    providers = ProviderService(settings, db, box, http)
    provider = providers.get_provider(provider_id)
    if not provider:
        print(f"provider {provider_id} not found")
        return
    accounts = providers.accounts(provider_id, include_disabled=False)
    if account_filter:
        accounts = [a for a in accounts if int(a["id"]) == account_filter]
    q = QoderProvider(settings, http)
    model_ids = [m.get("id") for m in provider.get("models") or []]
    print(f"provider={provider_id} base={provider.get('base_url')} region={provider.get('region')}")
    print(f"catalog_model_ids={json.dumps(model_ids, ensure_ascii=False)}")
    print(f"requested_models={json.dumps(models, ensure_ascii=False)} accounts={len(accounts)}")
    print(f"repeat={repeat} concurrency={concurrency} payload_chars={payload_chars} variants={variants}")

    # 模拟真实 IDE 客户端常见的请求形态，逐一试探上游是否返回「200 但无 choices 帧」。
    variant_payloads: list[tuple[str, dict]] = [
        ("baseline", {}),
        ("tools", {"tools": [{"type": "function", "function": {"name": "get_time", "description": "get time", "parameters": {"type": "object", "properties": {}}}}], "tool_choice": "auto"}),
        ("stream_options", {"stream_options": {"include_usage": True}}),
        ("max_tokens_1", {"max_tokens": 1}),
        ("max_tokens_0", {"max_tokens": 0}),
        ("empty_messages", {"messages": []}),
        ("system_only", {"messages": [{"role": "system", "content": "you are helpful"}]}),
        ("bad_field", {"response_format": {"type": "json_object"}, "n": 1, "frequency_penalty": 0.0, "presence_penalty": 0.0, "top_p": 0.95, "user": "ide-user", "metadata": {"conversation_id": "diag"}}),
        ("multimodal", {"messages": [{"role": "user", "content": [{"type": "text", "text": "只回复 OK"}]}]}),
        ("reasoning", {"reasoning_effort": "high", "max_completion_tokens": 512}),
    ]

    total = 0
    empty_hits = 0
    for account in accounts:
        cred = account.get("credential") or {}
        print(f"--- account id={account['id']} name={account.get('name','')!r} cred_keys={sorted(cred.keys())}")
        for model in models:
            if via_adapter:
                # 走真实 QoderProvider.chat_stream()，验证修复后是否抛 ProviderError / 产出 [DONE]。
                from app.providers.base import ProviderError

                cases = [
                    ("normal", {"model": model, "messages": [{"role": "user", "content": "只回复 OK"}], "stream": True}),
                    ("empty_messages", {"model": model, "messages": [], "stream": True}),
                ]
                for cname, payload in cases:
                    try:
                        lines = [c.line async for c in q.chat_stream(provider, account, payload)]
                    except ProviderError as exc:
                        print(
                            f"  adapter:{cname} [{model}] ProviderError status={exc.status_code} "
                            f"retryable={exc.retryable} msg={str(exc)[:300]!r}"
                        )
                        continue
                    print(f"  adapter:{cname} [{model}] frames={len(lines)} done={sum('[DONE]' in x for x in lines)}")
                    for idx, line in enumerate(lines[:4]):
                        print(f"  adapter:{cname} [{model}] frame[{idx}]={line[:200]!r}")
                continue
            if variants:
                for vname, extra in variant_payloads:
                    payload = {
                        "model": model,
                        "messages": [{"role": "user", "content": "只回复 OK"}],
                        "stream": True,
                        "temperature": 0.1,
                        **extra,
                    }
                    result = await request_raw(http, q, provider, account, model, 0, payload)
                    stats = print_result(f"variant:{vname}", model, result, details)
                    total += 1
                    empty_hits += int(int(stats["yielded"]) == 0)
                continue
            jobs = [
                (run + 1, model, request_raw(http, q, provider, account, model, payload_chars))
                for run in range(repeat * concurrency)
            ]
            if concurrency > 1:
                # 分批并发，每批 concurrency 个同时打同一账号
                for start in range(0, len(jobs), concurrency):
                    batch = jobs[start : start + concurrency]
                    results = await asyncio.gather(*(item[2] for item in batch))
                    for (run, mdl, _), result in zip(batch, results):
                        stats = print_result(f"run{run}/c{concurrency}", mdl, result, details)
                        total += 1
                        empty_hits += int(int(stats["yielded"]) == 0)
            else:
                for run, mdl, coro in jobs:
                    result = await coro
                    stats = print_result(f"run{run}", mdl, result, details)
                    total += 1
                    empty_hits += int(int(stats["yielded"]) == 0)
    print(f"SUMMARY total={total} yielded0(empty_stream)={empty_hits}")
    await http.aclose()
    db.close()


if __name__ == "__main__":
    asyncio.run(main())
