from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from ..config import Settings
from ..db import Database
from .observability import ObservabilityService

log = logging.getLogger("aiall.alerting")


class AlertingService:
    """池健康度边沿告警：连续 for_ticks 拍触发，连续 clear_ticks 拍恢复。"""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        observability: ObservabilityService,
        http: httpx.AsyncClient,
    ):
        self.settings = settings
        self.db = db
        self.observability = observability
        self.http = http
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._started_at = time.time()
        self._last_tick = 0.0
        self._states: dict[str, dict[str, Any]] = {}
        self._deliveries: set[asyncio.Task] = set()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="aiall-alerting")
        log.info("告警评估器已启动")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for task in list(self._deliveries):
            task.cancel()
        if self._deliveries:
            await asyncio.gather(*self._deliveries, return_exceptions=True)
            self._deliveries.clear()

    async def _run(self) -> None:
        while not self._stop.is_set():
            interval = self.observability.alerting_interval()
            now = time.time()
            if now - self._last_tick >= interval:
                self._last_tick = now
                try:
                    self.evaluate_once()
                except Exception as exc:
                    log.warning("告警评估失败: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(5, interval))
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ rules
    def _rules(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []
        for realm in ("cn", "global"):
            threshold = self.observability.alerting_min_healthy(realm)
            if threshold <= 0:
                continue
            healthy = int((snapshot["realms"].get(realm) or {}).get("ready") or 0)
            rules.append(
                {
                    "key": f"min_healthy:{realm}",
                    "rule": "min_healthy_accounts",
                    "realm": realm,
                    "severity": "critical",
                    "condition": healthy < threshold,
                    "value": healthy,
                    "threshold": threshold,
                    "summary": f"{realm} 健康账号 {healthy} 个，低于阈值 {threshold}",
                    "details": {"healthy": healthy, "min_healthy": threshold},
                }
            )

        breaker_threshold = self.observability.alerting_breaker_threshold()
        if breaker_threshold > 0:
            breaker = int((snapshot["realms"].get("all") or {}).get("breaker") or 0)
            rules.append(
                {
                    "key": "breaker:all",
                    "rule": "breaker_accounts",
                    "realm": "all",
                    "severity": "warning",
                    "condition": breaker >= breaker_threshold,
                    "value": breaker,
                    "threshold": breaker_threshold,
                    "summary": f"熔断账号 {breaker} 个，达到阈值 {breaker_threshold}",
                    "details": {"breaker": breaker, "threshold": breaker_threshold},
                }
            )

        rules.append(
            {
                "key": "waf:all",
                "rule": "waf_ip_blocked",
                "realm": "all",
                "severity": "critical",
                "condition": bool(snapshot.get("waf_blocked")),
                "value": 1 if snapshot.get("waf_blocked") else 0,
                "threshold": 1,
                "summary": "检测到上游 WAF/IP 拦截信号",
                "details": {"waf_ip_blocked": bool(snapshot.get("waf_blocked"))},
            }
        )
        return rules

    def evaluate_once(self) -> list[dict[str, Any]]:
        if not self.observability.alerting_enabled():
            return []
        if time.time() - self._started_at < self.observability.alerting_startup_grace():
            return []
        snapshot = self.observability.pool_snapshot()
        fired: list[dict[str, Any]] = []
        for rule in self._rules(snapshot):
            state = self._states.setdefault(
                rule["key"], {"active": False, "pending": 0, "clear": 0}
            )
            if rule["condition"]:
                state["clear"] = 0
                if state["active"]:
                    continue
                state["pending"] = int(state["pending"] or 0) + 1
                if state["pending"] >= self.observability.alerting_for_ticks():
                    state["active"] = True
                    state["pending"] = 0
                    self._emit("alert", rule)
                    fired.append(rule)
            else:
                state["pending"] = 0
                if state["active"]:
                    state["clear"] = int(state["clear"] or 0) + 1
                    if state["clear"] >= self.observability.alerting_clear_ticks():
                        state["active"] = False
                        state["clear"] = 0
                        if self.observability.alerting_send_resolve():
                            self._emit("resolve", rule)
        return fired

    # ------------------------------------------------------------------ delivery
    def _emit(self, event: str, rule: dict[str, Any]) -> None:
        webhook = self.observability.alerting_webhook()
        if not webhook:
            log.warning("告警已触发但未配置 alerting.webhook_url: %s", rule["summary"])
            return
        payload = {
            "service": self.settings.app_name,
            "version": self.settings.version,
            "event": event,
            "rule": rule["rule"],
            "realm": rule["realm"],
            "severity": rule["severity"],
            "summary": rule["summary"],
            "details": rule["details"],
            "timestamp": int(time.time()),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = self.observability.alerting_secret()
        if secret:
            digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-WB2A-Signature"] = "sha256=" + digest
        task = asyncio.create_task(self._post(webhook, body, headers))
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    async def _post(self, url: str, body: bytes, headers: dict[str, str]) -> None:
        try:
            response = await self.http.post(
                url,
                content=body,
                headers=headers,
                timeout=self.observability.alerting_timeout(),
            )
            if response.status_code >= 400:
                log.warning("告警投递失败 HTTP %s: %s", response.status_code, response.text[:200])
        except Exception as exc:
            log.warning("告警投递异常: %s", exc)

    # ------------------------------------------------------------------ report
    def state_report(self) -> list[dict[str, Any]]:
        return [
            {"rule": key, **value}
            for key, value in sorted(self._states.items())
        ]
