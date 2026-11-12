from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ..db import Database
from .accounts import AccountService
from .pool import AccountPool
from .providers import ProviderService
from .virtuals import VirtualService


def _today_start() -> int:
    return int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


class StatsService:
    """跨平台统计：概览页与平台详情共用同一口径。"""

    def __init__(
        self,
        db: Database,
        accounts: AccountService,
        providers: ProviderService,
        pool: AccountPool,
        virtuals: VirtualService,
    ):
        self.db = db
        self.accounts = accounts
        self.providers = providers
        self.pool = pool
        self.virtuals = virtuals

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _sum(values: list[float]) -> Optional[float]:
        return sum(values) if values else None

    def _trae_stats(self, region: str) -> dict[str, Any]:
        rows = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM accounts WHERE COALESCE(NULLIF(region,''),'cn')=?", (region or "cn",)
            )
        ]
        if not rows:
            return {
                "accounts": 0,
                "enabled": 0,
                "cooling": 0,
                "remaining": None,
                "used": None,
                "total": None,
                "in_flight": 0,
                "errors": [],
            }

        def total(field: str) -> Optional[float]:
            values = [float(r.get(field) or 0) for r in rows if float(r.get(field) or -1) >= 0]
            return sum(values) if values else None

        return {
            "accounts": len(rows),
            "enabled": len([r for r in rows if int(r.get("enabled") or 0)]),
            "cooling": len([r for r in rows if str(r.get("last_error") or "")]),
            "remaining": total("credits"),
            "used": total("credits_used"),
            "total": total("credits_total"),
            "in_flight": 0,
            "errors": [str(r.get("last_error")) for r in rows if r.get("last_error")][:3],
        }

    def platform_stats(self, provider: dict[str, Any]) -> dict[str, Any]:
        if provider.get("kind") == "trae":
            return self._trae_stats(provider.get("region") or "cn")
        return self.pool.provider_stats(provider["id"])

    # ------------------------------------------------------------------ overview
    def overview(self) -> dict[str, Any]:
        start = _today_start()
        providers = self.providers.list_providers()
        today_by_provider = {
            str(r["provider_id"] or ""): dict(r)
            for r in self.db.query(
                "SELECT provider_id, COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS tokens, "
                "COALESCE(SUM(credits_delta),0) AS credits FROM usage_logs WHERE created_at>=? "
                "GROUP BY provider_id",
                (start,),
            )
        }
        platform_rows: list[dict[str, Any]] = []
        totals = {"accounts": 0, "enabled": 0, "cooling": 0, "remaining": 0.0, "used": 0.0, "total": 0.0}
        has_remaining = has_used = has_total = False
        for provider in providers:
            stats = self.platform_stats(provider)
            usage = today_by_provider.get(provider["id"], {})
            models = provider.get("models") or []
            row = {
                "id": provider["id"],
                "name": provider["name"],
                "kind": provider["kind"],
                "region": provider.get("region") or "",
                "enabled": bool(provider.get("enabled")),
                "accounts": int(stats.get("accounts") or 0),
                "enabled_accounts": int(stats.get("enabled") or 0),
                "cooling": int(stats.get("cooling") or 0),
                "models": len(models),
                "remaining": stats.get("remaining"),
                "used": stats.get("used"),
                "total": stats.get("total"),
                "in_flight": int(stats.get("in_flight") or 0),
                "today_requests": int(usage.get("requests") or 0),
                "today_tokens": int(usage.get("tokens") or 0),
                "today_credits": float(usage.get("credits") or 0),
                "last_error": (stats.get("errors") or [""])[0] if stats.get("errors") else provider.get("last_error", ""),
            }
            platform_rows.append(row)
            totals["accounts"] += row["accounts"]
            totals["enabled"] += row["enabled_accounts"]
            totals["cooling"] += row["cooling"]
            if row["remaining"] is not None:
                totals["remaining"] += float(row["remaining"])
                has_remaining = True
            if row["used"] is not None:
                totals["used"] += float(row["used"])
                has_used = True
            if row["total"] is not None:
                totals["total"] += float(row["total"])
                has_total = True

        today_row = self.db.query_one(
            "SELECT COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS tokens, "
            "COALESCE(SUM(credits_delta),0) AS credits FROM usage_logs WHERE created_at>=?",
            (start,),
        )
        today = dict(today_row) if today_row else {}
        all_time_row = self.db.query_one(
            "SELECT COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS tokens FROM usage_logs"
        )
        all_time = dict(all_time_row) if all_time_row else {}
        groups = self.virtuals.list_groups(include_disabled=False)
        return {
            "accounts": totals["accounts"],
            "enabled_accounts": totals["enabled"],
            "cooling_accounts": totals["cooling"],
            "platforms": len(providers),
            "enabled_platforms": len([p for p in providers if p.get("enabled")]),
            "models": sum(len(p.get("models") or []) for p in providers),
            "virtual_groups": len(groups),
            "credits": totals["remaining"] if has_remaining else -1,
            "credits_used": totals["used"] if has_used else -1,
            "credits_total": totals["total"] if has_total else -1,
            "today_requests": int(today.get("requests") or 0),
            "today_tokens": int(today.get("tokens") or 0),
            "today_credits": float(today.get("credits") or 0),
            "total_requests": int(all_time.get("requests") or 0),
            "total_tokens": int(all_time.get("tokens") or 0),
            "platform_rows": platform_rows,
        }

    # ------------------------------------------------------------------ daily
    def daily_series(self, days: int = 7) -> list[dict[str, Any]]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        rows = self.db.query(
            "SELECT strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') AS day, "
            "COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS tokens, "
            "COALESCE(SUM(credits_delta),0) AS credits FROM usage_logs WHERE created_at>=? "
            "GROUP BY day ORDER BY day",
            (int(start.timestamp()),),
        )
        by_day = {r["day"]: dict(r) for r in rows}
        result = []
        for i in range(days):
            day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            result.append(by_day.get(day, {"day": day, "requests": 0, "tokens": 0, "credits": 0}))
        return result
