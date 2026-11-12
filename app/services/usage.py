from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from ..db import Database
from ..security import SecretBox, new_api_key, token_hash


class ApiKeyService:
    def __init__(self, db: Database, box: SecretBox):
        self.db = db
        self.box = box

    def list_keys(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM api_keys ORDER BY id ASC")
        result = []
        for row in rows:
            item = dict(row)
            item["plaintext"] = self.box.decrypt(item.pop("key_encrypted", ""))
            result.append(item)
        return result

    def create_key(self, name: str = "default") -> dict[str, Any]:
        key = new_api_key()
        now = int(time.time())
        key_id = self.db.execute(
            "INSERT INTO api_keys(name, key_hash, key_prefix, key_encrypted, enabled, created_at) "
            "VALUES(?,?,?,?,1,?)",
            (name[:64] or "default", token_hash(key), key[:12] + "…", self.box.encrypt(key), now),
        )
        return {
            "id": key_id,
            "name": name,
            "key": key,
            "key_prefix": key[:12] + "…",
            "created_at": now,
        }

    def revoke_key(self, key_id: int) -> None:
        self.db.execute("UPDATE api_keys SET enabled=0 WHERE id=?", (key_id,))

    def verify(self, key: str) -> Optional[dict[str, Any]]:
        if not key:
            return None
        row = self.db.query_one(
            "SELECT * FROM api_keys WHERE key_hash=? AND enabled=1", (token_hash(key),)
        )
        if not row:
            return None
        self.db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (int(time.time()), row["id"]))
        item = dict(row)
        item["plaintext"] = self.box.decrypt(item.pop("key_encrypted", ""))
        return item


class UsageService:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        *,
        request_id: str,
        api_key_id: Optional[int],
        api_key_name: str,
        account_id: Optional[int],
        account_name: str,
        endpoint: str,
        model: str,
        stream: bool,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        cached_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cache_miss_tokens: int = 0,
        reasoning_tokens: int = 0,
        credits_before: float = -1,
        credits_after: float = -1,
        credits_delta: float = 0,
        credits_estimated: float = 0,
        latency_ms: int = 0,
        ttft_ms: int = 0,
        completion_ms: int = 0,
        status: str = "ok",
        error: str = "",
        ip: str = "",
        provider_id: str = "",
        virtual_model: str = "",
    ) -> int:
        cache_read = int(cache_read_tokens or cached_tokens or 0)
        cache_write = int(cache_write_tokens or 0)
        cache_miss = int(cache_miss_tokens or 0)
        if not cache_miss:
            cache_miss = max(0, int(prompt_tokens) - cache_read - cache_write)
        return self.db.execute(
            """
            INSERT INTO usage_logs(
                request_id, api_key_id, api_key_name, account_id, account_name, endpoint, model, stream,
                prompt_tokens, completion_tokens, total_tokens, cached_tokens,
                cache_read_tokens, cache_write_tokens, cache_miss_tokens,
                reasoning_tokens, credits_before, credits_after, credits_delta, credits_estimated,
                latency_ms, ttft_ms, completion_ms, status, error, ip, provider_id, virtual_model, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                request_id,
                api_key_id,
                api_key_name,
                account_id,
                account_name,
                endpoint,
                model,
                1 if stream else 0,
                int(prompt_tokens),
                int(completion_tokens),
                int(total_tokens or prompt_tokens + completion_tokens),
                int(cached_tokens),
                cache_read,
                cache_write,
                cache_miss,
                int(reasoning_tokens),
                float(credits_before),
                float(credits_after),
                float(credits_delta),
                float(credits_estimated),
                int(latency_ms),
                int(ttft_ms),
                int(completion_ms),
                status,
                error[:2000],
                ip[:64],
                provider_id[:64],
                virtual_model[:128],
                int(time.time()),
            ),
        )

    def page(
        self,
        *,
        page: int = 1,
        limit: int = 50,
        model: str = "",
        status: str = "",
        account_id: int = 0,
        api_key_id: int = 0,
        days: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where = []
        params: list[Any] = []
        if model:
            where.append("model LIKE ?")
            params.append(f"%{model}%")
        if status:
            where.append("status=?")
            params.append(status)
        if account_id:
            where.append("account_id=?")
            params.append(account_id)
        if api_key_id:
            where.append("api_key_id=?")
            params.append(api_key_id)
        if days:
            where.append("created_at>=?")
            params.append(int((datetime.now() - timedelta(days=days)).timestamp()))
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = int(self.db.scalar("SELECT COUNT(*) FROM usage_logs" + clause, params, 0))
        offset = max(0, page - 1) * limit
        rows = self.db.query(
            "SELECT * FROM usage_logs" + clause + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [dict(r) for r in rows], total

    def recent(self, limit: int = 12) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM usage_logs ORDER BY id DESC LIMIT ?", (limit,))]

    def summary(self, api_key_id: Optional[int] = None) -> dict[str, Any]:
        start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        where = " WHERE created_at>=?"
        params: list[Any] = [start]
        if api_key_id:
            where += " AND api_key_id=?"
            params.append(api_key_id)
        row = self.db.query_one(
            f"""
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                   COALESCE(SUM(total_tokens),0) AS total_tokens,
                   COALESCE(SUM(credits_delta),0) AS credits_delta,
                   COALESCE(SUM(credits_estimated),0) AS credits_estimated
            FROM usage_logs{where}
            """,
            params,
        )
        return dict(row) if row else {}

    def daily_series(self, days: int = 7) -> list[dict[str, Any]]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
        rows = self.db.query(
            """
            SELECT strftime('%Y-%m-%d', created_at, 'unixepoch', 'localtime') AS day,
                   COUNT(*) AS requests,
                   COALESCE(SUM(total_tokens),0) AS tokens,
                   COALESCE(SUM(credits_delta),0) AS credits
            FROM usage_logs WHERE created_at>=? GROUP BY day ORDER BY day
            """,
            (int(start.timestamp()),),
        )
        by_day = {r["day"]: dict(r) for r in rows}
        result = []
        for i in range(days):
            day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            result.append(by_day.get(day, {"day": day, "requests": 0, "tokens": 0, "credits": 0}))
        return result

    def cleanup(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = int((datetime.now() - timedelta(days=days)).timestamp())
        before = int(self.db.scalar("SELECT COUNT(*) FROM usage_logs WHERE created_at<?", (cutoff,), 0))
        self.db.execute("DELETE FROM usage_logs WHERE created_at<?", (cutoff,))
        return before

    def export_csv(self, **filters) -> str:
        rows, _ = self.page(page=1, limit=100000, **filters)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "time",
                "request_id",
                "api_key",
                "account",
                "endpoint",
                "model",
                "provider",
                "virtual_model",
                "stream",
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "cached_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cache_miss_tokens",
                "reasoning_tokens",
                "credits_before",
                "credits_after",
                "credits_delta",
                "credits_estimated",
                "latency_ms",
                "ttft_ms",
                "completion_ms",
                "status",
                "error",
                "ip",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d %H:%M:%S"),
                    row["request_id"],
                    row["api_key_name"],
                    row["account_name"],
                    row["endpoint"],
                    row["model"],
                    row.get("provider_id", ""),
                    row.get("virtual_model", ""),
                    "yes" if row["stream"] else "no",
                    row["prompt_tokens"],
                    row["completion_tokens"],
                    row["total_tokens"],
                    row["cached_tokens"],
                    row.get("cache_read_tokens", 0),
                    row.get("cache_write_tokens", 0),
                    row.get("cache_miss_tokens", 0),
                    row["reasoning_tokens"],
                    row["credits_before"],
                    row["credits_after"],
                    row["credits_delta"],
                    row["credits_estimated"],
                    row["latency_ms"],
                    row.get("ttft_ms", 0),
                    row.get("completion_ms", 0),
                    row["status"],
                    row["error"],
                    row["ip"],
                ]
            )
        return output.getvalue()
