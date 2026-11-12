from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from ..config import BASE_DIR, Settings
from ..db import Database
from .accounts import AccountService
from .platforms import PlatformService
from .providers import ProviderService
from .workbuddy_growth import WorkbuddyGrowthService
from .usage import UsageService

log = logging.getLogger("aiall.scheduler")


def _beijing_tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Asia/Shanghai")
        except Exception:
            pass
    return timezone(timedelta(hours=8), name="Asia/Shanghai")


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        accounts: AccountService,
        usage: UsageService,
        platforms: PlatformService | None = None,
        providers: ProviderService | None = None,
        http=None,
    ):
        self.settings = settings
        self.db = db
        self.accounts = accounts
        self.usage = usage
        self.platforms = platforms
        self.providers = providers
        self.growth = (
            WorkbuddyGrowthService(settings, db, providers, http)
            if providers is not None and http is not None
            else None
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_cleanup_day = ""
        self._manual_tasks: dict[str, asyncio.Task] = {}

    def start(self) -> None:
        if self.settings.disable_scheduler:
            log.info("调度器已禁用")
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="traeapi-scheduler")
        log.info("调度器已启动")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("调度器执行失败: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def _refresh_models(self, now: datetime, today: str, force: bool = False) -> None:
        """每个平台每天自动同步一次模型列表，默认开启；force=True 用于人工补跑。"""
        if not force and self.db.get_setting("models_auto_refresh", "1") != "1":
            return
        if not force and self.db.get_setting("last_models_refresh", "") == today:
            if not self._retry_due("model_refresh", now):
                return
        scheduled = self.db.get_setting("models_refresh_time", "10:10") or "10:10"
        # force=True 是人工补跑，不应再受「每天 10:10」时间窗限制。
        if not force and now < self._scheduled_at(now, scheduled, "models"):
            return
        self.db.set_setting("last_models_refresh", today)
        started_at = int(time.time())
        results: list[dict[str, Any]] = []
        try:
            models = await self.accounts.all_models(force=True)
            results.append(
                {
                    "account": "Trae",
                    "status": "success",
                    "message": f"{len(models)} 个模型",
                }
            )
        except Exception as exc:
            results.append({"account": "Trae", "status": "failed", "message": str(exc)[:300]})
        if self.providers is not None:
            for provider in self.providers.list_providers(include_disabled=False):
                if str(provider.get("kind") or "") == "trae":
                    continue
                name = str(provider.get("name") or provider.get("id") or "")
                try:
                    models = await self.providers.sync_models(str(provider["id"]))
                    results.append(
                        {
                            "account": name,
                            "status": "success" if models else "skipped",
                            "message": f"{len(models)} 个模型" if models else "上游未返回模型",
                        }
                    )
                except ValueError as exc:
                    results.append({"account": name, "status": "skipped", "message": str(exc)[:300]})
                except Exception as exc:
                    results.append({"account": name, "status": "failed", "message": str(exc)[:300]})
        self.record_task_run("model_refresh", started_at, results)
        for item in results:
            log.info("模型同步 %s: %s %s", item.get("account"), item.get("status"), item.get("message", ""))

    async def _maybe_growth_tasks(self, now: datetime, today: str) -> None:
        tasks = (
            ("activity", "schedule.activity_enabled", "schedule.activity_time", "10:00"),
            ("travel", "schedule.travel_enabled", "schedule.travel_time", "09:00"),
            ("school", "schedule.school_enabled", "schedule.school_time", "12:00"),
            ("cat", "schedule.cat_enabled", "schedule.cat_time", "01:00"),
        )
        for name, enabled_key, time_key, default_time in tasks:
            if self.db.get_setting(enabled_key, "0") != "1":
                continue
            if self.db.get_setting(f"last_{name}_run", "") == today:
                if self._retry_due(name, now):
                    await self._run_growth_task(name, source="retry")
                continue
            scheduled = self.db.get_setting(time_key, default_time) or default_time
            if now < self._scheduled_at(now, scheduled, name):
                continue
            self.db.set_setting(f"last_{name}_run", today)
            await self._run_growth_task(name)

    async def _run_growth_task(self, name: str, source: str = "schedule") -> None:
        if self.growth is None:
            return
        started = int(time.time())
        try:
            if name == "activity":
                results = await self.growth.run_activity()
            elif name == "travel":
                results = await self.growth.run_travel()
            elif name == "school":
                results = await self.growth.run_school()
            elif name == "cat":
                results = await self.growth.run_cat()
            else:
                return
        except Exception as exc:
            results = [{"account": name, "status": "error", "message": str(exc)}]
        self.record_task_run(name, started, results, source=source)

    def _scheduled_at(self, now: datetime, scheduled: str, task: str) -> datetime:
        try:
            hour, minute = [int(x) for x in scheduled.split(":", 1)]
        except Exception:
            hour, minute = 9, 0
        try:
            jitter = max(0, int(float(self.db.get_setting("schedule.jitter_minutes", "0") or 0)))
        except Exception:
            jitter = 0
        offset = 0
        if jitter:
            seed = f"{task}|{now.strftime('%Y-%m-%d')}|{hour:02d}"
            offset = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % (jitter + 1)
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(minutes=offset)

    async def _tick(self) -> None:
        now = datetime.now(_beijing_tz())
        today = now.strftime("%Y-%m-%d")
        if now.hour == 3 and now.minute < 1 and self._last_cleanup_day != today:
            self._last_cleanup_day = today
            days = int(self.db.get_setting("log_retention_days", "30") or 30)
            removed = self.usage.cleanup(days)
            if removed:
                log.info("清理 %d 条过期使用日志", removed)

        await self._refresh_models(now, today)
        await self._maybe_growth_tasks(now, today)

        if self.db.get_setting("auto_checkin", "1") != "1":
            return
        scheduled = self.db.get_setting("checkin_time", self.settings.checkin_time) or "09:00"
        if now < self._scheduled_at(now, scheduled, "checkin"):
            return
        last_run = self.db.get_setting("last_checkin_run", "")
        if last_run == today:
            if not self._retry_due("checkin", now):
                return
            await self._run_checkin(source="retry")
            return
        self.db.set_setting("last_checkin_run", today)
        await self._run_checkin(source="schedule")

    async def _run_checkin(self, source: str = "schedule") -> None:
        log.info("开始每日自动签到 (%s)", source)
        started_at = int(time.time())
        results = await self.accounts.checkin_all()
        self.record_task_run("trae_checkin", started_at, results, source=source)
        for item in results:
            log.info("Trae 签到 %s: %s %s", item.get("account"), item.get("status"), item.get("message", ""))
        if self.platforms is not None:
            platform_started_at = int(time.time())
            platform_results = await self.platforms.checkin_all()
            self.record_task_run("platform_checkin", platform_started_at, platform_results, source=source)
            for item in platform_results:
                log.info("平台签到 %s: %s %s", item.get("account"), item.get("status"), item.get("message", ""))

    def manual_running(self, name: str) -> bool:
        task = self._manual_tasks.get(name)
        return bool(task and not task.done())

    def start_manual(self, name: str) -> None:
        if name not in (
            "checkin",
            "trae_checkin",
            "platform_checkin",
            "model_refresh",
            "activity",
            "travel",
            "school",
            "cat",
        ):
            raise ValueError(f"当前项目未接入任务: {name}")
        if self.manual_running(name):
            raise RuntimeError(f"任务正在执行: {name}")
        task = asyncio.create_task(self._run_manual(name), name=f"manual-{name}")
        self._manual_tasks[name] = task

        def done(_task: asyncio.Task) -> None:
            self._manual_tasks.pop(name, None)
            try:
                _task.result()
            except Exception as exc:
                log.warning("人工任务 %s 执行失败: %s", name, exc)

        task.add_done_callback(done)

    async def _run_manual(self, name: str) -> None:
        if name == "model_refresh":
            now = datetime.now(_beijing_tz())
            await self._refresh_models(now, now.strftime("%Y-%m-%d"), force=True)
            return
        if name in ("activity", "travel", "school", "cat"):
            await self._run_growth_task(name, source="manual")
            return
        if name == "trae_checkin":
            started = int(time.time())
            results = await self.accounts.checkin_all()
            self.record_task_run(name, started, results, source="manual")
            return
        if name == "platform_checkin":
            if self.platforms is None:
                raise RuntimeError("平台签到服务未初始化")
            started = int(time.time())
            results = await self.platforms.checkin_all()
            self.record_task_run(name, started, results, source="manual")
            return
        started = int(time.time())
        trae_results = await self.accounts.checkin_all()
        self.record_task_run("trae_checkin", started, trae_results, source="manual")
        if self.platforms is not None:
            platform_started = int(time.time())
            platform_results = await self.platforms.checkin_all()
            self.record_task_run("platform_checkin", platform_started, platform_results, source="manual")

    def _retry_due(self, task: str, now: datetime) -> bool:
        key = "checkin" if task in ("trae_checkin", "platform_checkin") else task
        try:
            at = int(self.db.get_setting(f"retry_{key}_at", "0") or 0)
        except Exception:
            at = 0
        if at <= 0 or int(now.timestamp()) < at:
            return False
        self.db.set_setting(f"retry_{key}_at", "0")
        return True

    def _schedule_retry(self, task: str, finished_at: int) -> None:
        try:
            delay = max(0, int(self.db.get_setting("schedule.retry_delay_minutes", "0") or 0))
            maximum = max(0, int(self.db.get_setting("schedule.retry_max_per_day", "1") or 1))
        except Exception:
            return
        if delay <= 0 or maximum <= 0:
            return
        key = "checkin" if task in ("trae_checkin", "platform_checkin") else task
        now = datetime.now(_beijing_tz())
        day = now.strftime("%Y-%m-%d")
        if self.db.get_setting(f"retry_{key}_day", "") != day:
            self.db.set_setting(f"retry_{key}_day", day)
            self.db.set_setting(f"retry_{key}_count", "0")
        try:
            count = int(self.db.get_setting(f"retry_{key}_count", "0") or 0)
        except Exception:
            count = 0
        if count >= maximum:
            log.warning("任务 %s 当日重试已达上限 %s", key, maximum)
            return
        self.db.set_setting(f"retry_{key}_count", str(count + 1))
        self.db.set_setting(f"retry_{key}_at", str(finished_at + delay * 60))
        log.warning("任务 %s 本轮全灭，%s 分钟后自动重试", key, delay)

    def record_task_run(
        self,
        task: str,
        started_at: int,
        results: list[dict[str, Any]],
        message: str = "",
        source: str = "schedule",
    ) -> None:
        success = failed = skipped = 0
        for item in results:
            status = str(item.get("status") or "").lower()
            if status in ("success", "already", "pending", "ok"):
                success += 1
            elif status in ("skipped", "disabled", "no_account"):
                skipped += 1
            else:
                failed += 1
        total = len(results)
        all_failed = 1 if total > 0 and success == 0 and failed > 0 else 0
        details = [
            {
                "account": str(item.get("account") or "")[:120],
                "status": str(item.get("status") or "unknown")[:24],
                "message": str(item.get("message") or "")[:300],
            }
            for item in results[:50]
            if isinstance(item, dict)
        ]
        finished_at = int(time.time())
        if all_failed and source != "manual":
            self._schedule_retry(task, finished_at)
        self.db.execute(
            "INSERT INTO task_runs(task, started_at, finished_at, total, success, failed, skipped, all_failed, "
            "message, source, details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task) DO UPDATE SET "
            "started_at=excluded.started_at, finished_at=excluded.finished_at, total=excluded.total, "
            "success=excluded.success, failed=excluded.failed, skipped=excluded.skipped, "
            "all_failed=excluded.all_failed, message=excluded.message, source=excluded.source, "
            "details_json=excluded.details_json",
            (
                task,
                int(started_at),
                finished_at,
                total,
                success,
                failed,
                skipped,
                all_failed,
                message[:500],
                source[:32],
                json.dumps(details, ensure_ascii=False),
            ),
        )
        ledger_path = str(self.db.get_setting("schedule.ledger_file", "") or "").strip()
        if not ledger_path:
            return
        try:
            path = Path(ledger_path)
            if not path.is_absolute():
                path = BASE_DIR / path
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "task": task,
                "source": source,
                "started_at": int(started_at),
                "finished_at": finished_at,
                "duration_ms": max(0, (finished_at - int(started_at)) * 1000),
                "total": total,
                "success": success,
                "failed": failed,
                "skipped": skipped,
                "all_failed": bool(all_failed),
                "message": message[:500],
                "details": details,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception as exc:
            log.warning("任务台账写盘失败: %s", exc)

    def task_ledger(self, limit: int = 0) -> list[dict[str, Any]]:
        sql = (
            "SELECT task, source, started_at, finished_at, total, success, failed, skipped, all_failed, "
            "message, details_json FROM task_runs ORDER BY finished_at DESC"
        )
        params: tuple[Any, ...] = ()
        if limit > 0:
            sql += " LIMIT ?"
            params = (int(limit),)
        rows = []
        for row in self.db.query(sql, params):
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json", "[]") or "[]")
            except Exception:
                item["details"] = []
            rows.append(item)
        return rows
