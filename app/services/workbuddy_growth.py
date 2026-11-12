from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from ..config import BASE_DIR, Settings
from ..db import Database
from .providers import ProviderService

TASK_DIR = BASE_DIR / "scripts" / "workbuddy_tasks"
CHAT_BASE = "https://copilot.tencent.com"


class WorkbuddyGrowthService:
    """WorkBuddy / CodeBuddy 成长任务与猫猫旅行适配层。"""

    def __init__(self, settings: Settings, db: Database, providers: ProviderService, http: httpx.AsyncClient):
        self.settings = settings
        self.db = db
        self.providers = providers
        self.http = http

    def _accounts(self) -> list[tuple[dict[str, Any], dict[str, Any], Any]]:
        result = []
        for provider_id in ("workbuddy", "codebuddy"):
            provider = self.providers.get_provider(provider_id)
            if not provider or not provider.get("enabled"):
                continue
            adapter = self.providers.adapter(provider)
            if adapter is None:
                continue
            for account in self.providers.accounts(provider_id, include_disabled=False):
                result.append((provider, account, adapter))
        return result

    async def _auth(self, provider: dict[str, Any], account: dict[str, Any], adapter) -> dict[str, Any] | None:
        cred = account.get("credential") or {}
        if not cred.get("access_token"):
            return None
        if hasattr(adapter, "ensure"):
            try:
                cred = await adapter.ensure(provider, account)
            except Exception:
                return None
        uid = str(cred.get("uid") or account.get("uid") or "")
        if not uid:
            return None
        region = str(provider.get("region") or "cn").lower()
        return {
            "token": str(cred.get("access_token") or ""),
            "uid": uid,
            "nick": str(account.get("name") or ""),
            "domain": str(cred.get("domain") or ("workbuddy.ai" if region == "global" else "codebuddy.cn")),
            "realm": "global" if region == "global" else "cn",
        }

    async def _export_auths(self, directory: Path) -> int:
        count = 0
        for provider, account, adapter in self._accounts():
            auth = await self._auth(provider, account, adapter)
            if not auth:
                continue
            filename = directory / f"workbuddy-{auth['uid'][:8]}-{count}.json"
            payload = {
                "auth": {"accessToken": auth["token"], "domain": auth["domain"], "realm": auth["realm"]},
                "account": {"uid": auth["uid"], "nickname": auth["nick"]},
                "realm": auth["realm"],
            }
            filename.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.chmod(filename, 0o600)
            count += 1
        return count

    async def _run_script(self, args: list[str]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="traeapi-wb-tasks-") as tmp:
            auth_dir = Path(tmp)
            count = await self._export_auths(auth_dir)
            if not count:
                return {"status": "unavailable", "message": "没有可用的 WorkBuddy / CodeBuddy 账号"}
            env = os.environ.copy()
            env["WB2A_AUTHS"] = str(auth_dir)
            env["WB2A_PYTHON"] = sys.executable
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                *args,
                cwd=str(TASK_DIR),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await proc.communicate()
            text = output.decode("utf-8", "replace")[-4000:]
            status = "success" if proc.returncode == 0 else "error"
            return {"status": status, "message": text[-1500:], "accounts": count}

    async def run_activity(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for provider, account, adapter in self._accounts():
            if str(provider.get("region") or "cn").lower() == "global":
                results.append({"account": account.get("name"), "status": "skipped", "message": "global 不适用 CN 活跃任务"})
                continue
            try:
                auth = await self._auth(provider, account, adapter)
                if not auth:
                    results.append({"account": account.get("name"), "status": "skipped", "message": "缺少 token"})
                    continue
                results.append({"account": account.get("name"), **await self._activity_one(provider, account, adapter, auth)})
            except Exception as exc:
                results.append({"account": account.get("name"), "status": "error", "message": str(exc)[:300]})
        return results

    async def _activity_one(self, provider, account, adapter, auth) -> dict[str, Any]:
        headers = adapter._identity(provider, (account.get("credential") or {}))
        headers["Authorization"] = "Bearer " + auth["token"]
        headers["X-User-Id"] = auth["uid"]
        origin = adapter.origin(provider)
        billing = origin
        chat = CHAT_BASE
        now_ms = int(time.time() * 1000)

        async def call(method: str, base: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
            resp = await self.http.request(method, base + path, headers=headers, json=body, timeout=30)
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:300]}
            return resp.status_code, data if isinstance(data, dict) else {}

        for i in range(5):
            conv = f"traeapi-{uuid.uuid4().hex}"
            event = {
                "eventCode": "chat_request_send",
                "timestamp": now_ms + i,
                "reportDelay": 0,
                "mode": "craft",
                "conversationId": conv,
                "requestId": f"{conv}-{i}",
                "inputLength": 12,
                "requestModelId": "deepseek-v4-flash",
                "requestModelName": "DeepSeek V4 Flash",
                "isPlan": False,
                "isAutoExecuteTerminal": False,
                "isAutoModify": False,
                "codebaseEnable": False,
                "maxToken": 0,
                "maxSteps": 0,
                "temperature": 0,
                "maxRetries": 0,
                "mentionContexts": [],
                "knowledgeId": [],
                "knowledgeName": [],
                "codebaseId": "",
                "mentionContextCount": 0,
                "command": "",
                "expertId": "",
                "recommendId": "",
                "skillId": "",
                "skillCount": 0,
                "totalCount": 0,
                "fileUri": "",
                "presentAt": now_ms + i,
                "traceId": "",
                "rootRequestId": conv,
                "parentConversationId": conv,
                "agentName": "default",
                "agentType": "conversation",
                "userId": auth["uid"],
            }
            status, data = await call("POST", billing, "/v2/report", [event])
            if status >= 300 or int(data.get("code") or 0) != 0:
                raise RuntimeError(f"活跃上报失败 HTTP {status}: {data}")

        streak_status, streak_data = await call("GET", chat, "/activity/growth/streak")
        days = 0
        redemption: dict[str, Any] = {}
        if streak_status < 300 and int(streak_data.get("code") or 0) == 0:
            data = streak_data.get("data") if isinstance(streak_data.get("data"), dict) else {}
            days = int((data.get("streak") or {}).get("days") or 0)
            redemption = data.get("redemption_status") if isinstance(data.get("redemption_status"), dict) else {}
        for tier in ("7d", "14d", "28d"):
            status_key = f"tier_{tier}_status"
            if redemption.get(status_key) == "claimed":
                continue
            await call(
                "POST",
                chat,
                "/activity/growth/redeem",
                {"tier": tier, "client_token": f"redeem-{tier}-{uuid.uuid4().hex}"},
            )
        chances_status, chances_data = await call("GET", chat, "/activity/growth/lottery/chances")
        chances = 0
        if chances_status < 300 and int(chances_data.get("code") or 0) == 0:
            data = chances_data.get("data") if isinstance(chances_data.get("data"), dict) else {}
            chances = int(data.get("balance") or 0)
        for _ in range(min(max(chances, 0), 10)):
            status, data = await call(
                "POST",
                chat,
                "/activity/growth/lottery/draw",
                {"client_token": f"draw-{uuid.uuid4().hex}"},
            )
            if status >= 300 or int(data.get("code") or 0) != 0:
                break
        await call("POST", billing, "/billing/meter/claim-gift", {})
        await call("POST", billing, "/billing/meter/claim-compensation", {})
        return {
            "status": "success",
            "credits": 0,
            "message": f"活跃上报 5 次，连登 {days} 天，抽奖 {chances} 次",
        }

    async def run_school(self) -> list[dict[str, Any]]:
        result = await self._run_script(["school_open_day_2026.py", "ALL", "--run", "--yes"])
        return [{"account": "WorkBuddy/CodeBuddy", **result}]

    async def run_cat(self) -> list[dict[str, Any]]:
        result = await self._run_script(
            ["task_runner.py", "ALL", "--yes", "--only", "black_cat"]
        )
        return [{"account": "WorkBuddy/CodeBuddy", **result}]

    async def run_travel(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for provider, account, adapter in self._accounts():
            if str(provider.get("region") or "cn").lower() == "global":
                results.append({"account": account.get("name"), "status": "skipped", "message": "global 无旅行任务"})
                continue
            try:
                auth = await self._auth(provider, account, adapter)
                if not auth:
                    results.append({"account": account.get("name"), "status": "skipped", "message": "缺少 token"})
                    continue
                outcome = await self._travel_one(provider, account, adapter, auth)
                results.append({"account": account.get("name"), **outcome})
            except Exception as exc:
                results.append({"account": account.get("name"), "status": "error", "message": str(exc)[:300]})
        return results

    async def _travel_one(self, provider, account, adapter, auth) -> dict[str, Any]:
        headers = adapter._identity(provider, (account.get("credential") or {}))
        headers["Authorization"] = "Bearer " + auth["token"]
        headers["X-User-Id"] = auth["uid"]

        async def request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
            resp = await self.http.request(method, CHAT_BASE + path, headers=headers, json=body, timeout=30)
            if resp.status_code >= 300:
                text = resp.text[:300]
                if resp.status_code == 400 and "first_buddy task not completed yet" in text:
                    raise RuntimeError("领养前置对话任务未完成")
                raise RuntimeError(f"HTTP {resp.status_code}: {text}")
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError("响应不是 JSON 对象")
            if int(data.get("code") or 0) != 0:
                raise RuntimeError(str(data.get("msg") or data.get("message") or "上游业务错误"))
            return data.get("data") if isinstance(data.get("data"), dict) else {}

        buddy = await request("GET", "/activity/growth/buddy/info")
        if not buddy.get("buddy"):
            await request("POST", "/activity/growth/buddy/agreement", {"agree": True})
            try:
                await request("POST", "/activity/growth/buddy/first", {})
                return {"status": "success", "message": "领养成功"}
            except RuntimeError as exc:
                if "领养前置" in str(exc):
                    return {"status": "skipped", "message": str(exc)}
                raise
        state = await request("GET", "/activity/growth/buddy/travel/status")
        status = str(state.get("state") or "")
        if status == "arrived":
            record_id = int(state.get("record_id") or 0)
            if not record_id:
                return {"status": "skipped", "message": "到站但缺少 record_id"}
            data = await request("POST", "/activity/growth/buddy/travel/claim", {"record_id": record_id})
            return {"status": "success", "credits": int(data.get("reward_credit") or 0), "message": "旅行领奖成功"}
        if status == "idle":
            if state.get("daily_limit_reached"):
                return {"status": "skipped", "message": "今日已派出"}
            await request("POST", "/activity/growth/buddy/travel/depart", {"location_id": 4})
            return {"status": "success", "message": "旅行派出成功"}
        return {"status": "skipped", "message": f"旅行状态: {status or 'unknown'}"}
