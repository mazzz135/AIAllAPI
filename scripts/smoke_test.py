#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

import httpx


def main() -> int:
    parser = argparse.ArgumentParser(description="AIAll API 冒烟测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:2999")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--chat", action="store_true", help="额外发一条真实聊天请求（会消耗积分）")
    parser.add_argument("--model", default="DeepSeek-V4-Flash")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    with httpx.Client(timeout=30) as client:
        health = client.get(base + "/health")
        print("health", health.status_code, health.text[:300])
        if health.status_code != 200:
            return 1
        if not args.api_key:
            print("未提供 --api-key，跳过 /v1/models 与聊天测试")
            return 0
        headers = {"Authorization": "Bearer " + args.api_key}
        models = client.get(base + "/v1/models", headers=headers)
        print("models", models.status_code, models.text[:500])
        if models.status_code != 200:
            return 1
        if args.chat:
            response = client.post(
                base + "/v1/chat/completions",
                headers=headers,
                json={
                    "model": args.model,
                    "messages": [{"role": "user", "content": "请只回复：ok"}],
                    "stream": False,
                },
                timeout=180,
            )
            print("chat", response.status_code)
            try:
                print(json.dumps(response.json(), ensure_ascii=False, indent=2)[:2000])
            except Exception:
                print(response.text[:2000])
            return 0 if response.status_code == 200 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
