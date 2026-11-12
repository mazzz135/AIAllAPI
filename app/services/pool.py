from __future__ import annotations

import hashlib
import random
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from ..db import Database


def _now() -> int:
    return int(time.time())


def _setting_int(db: Database, key: str, default: int) -> int:
    try:
        return int(float(db.get_setting(key, str(default))))
    except Exception:
        return default


def _setting_float(db: Database, key: str, default: float) -> float:
    try:
        return float(db.get_setting(key, str(default)))
    except Exception:
        return default


def _today() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _next_reset_at() -> int:
    """上游积分耗尽后一般次日 04:00（CST）重置。"""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = None
    now = datetime.now(tz) if tz else datetime.now()
    target = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return int(target.timestamp())


CREDIT_EXHAUSTED_PATTERN = re.compile(
    r"credit|quota|余额|积分|insufficient|exhaust|402|payment required", re.IGNORECASE
)
MODEL_LIMIT_PATTERN = re.compile(r"6004|model.{0,12}(limit|exceed)|该模型", re.IGNORECASE)
AUTH_PATTERN = re.compile(r"401|403|unauthorized|invalid token|token expired|session", re.IGNORECASE)

COST_TTL_SECONDS = 6 * 3600
EQUAL_WEIGHT_EPSILON = 1e-6


class AccountPool:
    """账号池治理：三因子加权选号 + 冷却/熔断 + 会话粘性 + 账号级成本账本 + 成本分层探索。

    选号顺序（与 workbuddy2api 语义一致）：
      会话粘性 → 成本分层（免费 > 未知 > 收费）→ 三因子加权 Top-5 → 短名单加权抽签
    三因子权重：
      credits 比例 ×10 + 快过期积分占比 ×8 + 闲置时长补偿（上限 3.0）
    失败账号由冷却/熔断状态机处置，不参与权重公式。
    """

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ config
    def _max_in_flight(self) -> int:
        return max(1, _setting_int(self.db, "pool_max_in_flight", 2))

    def _session_ttl(self) -> int:
        return max(60, _setting_int(self.db, "pool_session_ttl", 1800))

    def expiring_soon_days(self) -> int:
        return max(0, _setting_int(self.db, "pool_expiring_soon_days", 7))

    def explore_interval(self) -> int:
        return max(0, _setting_int(self.db, "pool_cost_explore_interval", 1800))

    def explore_max_per_day(self) -> int:
        return max(0, _setting_int(self.db, "pool_cost_explore_max_per_day", 48))

    # ------------------------------------------------------------------ session
    @staticmethod
    def session_key(payload: dict[str, Any], headers: Optional[dict[str, str]] = None) -> str:
        """会话粘性键：conversation_id 四键 → prompt_cache_key → 首条 user 消息哈希。"""
        headers = headers or {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        for key in ("conversation_id", "conversationId"):
            value = payload.get(key) or metadata.get(key)
            if value:
                return "conv:" + str(value)[:128]
        if payload.get("prompt_cache_key"):
            return "cache:" + str(payload["prompt_cache_key"])[:128]
        header_key = headers.get("x-conversation-id") or headers.get("x-conversation-request-id")
        if header_key:
            return "hdr:" + str(header_key)[:128]
        # user_id 粒度太粗，带 user_id 时不派生兜底键（避免把所有会话钉到同一账号）
        if payload.get("user_id") or metadata.get("user_id"):
            return ""
        for message in payload.get("messages") or []:
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(part.get("text") or "") for part in content if isinstance(part, dict)
                    )
                text = str(content or "").strip()
                if text:
                    return "msg:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
                break
        return ""

    def bind_session(self, key: str, provider_id: str, account_id: int) -> None:
        if not key:
            return
        now = _now()
        self.db.execute(
            "INSERT INTO session_bindings(session_key, provider_id, account_id, expires_at, updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET "
            "provider_id=excluded.provider_id, account_id=excluded.account_id, "
            "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (key, provider_id, int(account_id), now + self._session_ttl(), now),
        )

    def session_account(self, key: str, provider_id: str) -> int:
        if not key:
            return 0
        row = self.db.query_one(
            "SELECT account_id, expires_at FROM session_bindings WHERE session_key=? AND provider_id=?",
            (key, provider_id),
        )
        if not row:
            return 0
        if int(row["expires_at"] or 0) <= _now():
            self.db.execute("DELETE FROM session_bindings WHERE session_key=?", (key,))
            return 0
        return int(row["account_id"] or 0)

    # ------------------------------------------------------------------ pick
    def candidates(
        self,
        provider_id: str,
        model: str = "",
        exclude: Optional[set[int]] = None,
        session_key: str = "",
        rows: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        exclude = exclude or set()
        now = _now()
        if rows is None:
            rows = [
                dict(r)
                for r in self.db.query(
                    "SELECT * FROM provider_accounts WHERE provider_id=? AND enabled=1 ORDER BY id",
                    (provider_id,),
                )
            ]
        rows = [
            dict(r)
            for r in rows
            if int(r["id"]) not in exclude
            and int(r.get("enabled", 1) or 0)
            and not int(r.get("manual_disabled", 0) or 0)
        ]
        if not rows:
            return []
        max_flight = self._max_in_flight()
        cooled_models = self._model_cooldowns(provider_id, model, now)

        def cooling(row: dict[str, Any]) -> bool:
            return int(row.get("cooldown_until") or 0) > now

        def model_cooled(row: dict[str, Any]) -> bool:
            return int(row["id"]) in cooled_models

        def saturated(row: dict[str, Any]) -> bool:
            return int(row.get("in_flight") or 0) >= max_flight

        healthy = [r for r in rows if not cooling(r) and not model_cooled(r) and not saturated(r)]

        sticky_id = self.session_account(session_key, provider_id)
        sticky: list[dict[str, Any]] = []
        if sticky_id:
            sticky = [r for r in healthy if int(r["id"]) == sticky_id]

        # 成本分层：0=实测免费，1=无观测，2=实测收费
        tiers: dict[int, tuple[int, float]] = {}
        if model:
            for row in healthy:
                tiers[int(row["id"])] = self.cost_tier(int(row["id"]), model, now)
        best_tier = min([t for t, _ in tiers.values()], default=1) if tiers else 1
        tier1 = [r for r in healthy if tiers.get(int(r["id"]), (1, 0.0))[0] == 1]

        explore_row: Optional[dict[str, Any]] = None
        pool_rows = healthy
        if model and tiers:
            if best_tier == 0 and tier1 and self._should_explore(provider_id, model, now):
                explore_row = random.choice(tier1)
                self._record_explore(provider_id, model, now)
            else:
                pool_rows = [r for r in healthy if tiers[int(r["id"])][0] == best_tier]

        max_credits = max([max(0.0, float(r.get("credits") or 0)) for r in pool_rows] + [0.0])
        scored = [(self._weight(r, max_credits, now), r) for r in pool_rows]
        scored.sort(key=lambda item: item[0], reverse=True)

        min_gap = _setting_float(self.db, "pool_min_pick_gap_ms", 100) / 1000.0
        fresh = [r for _, r in scored if (now - int(r.get("last_picked_at") or 0)) >= min_gap]
        lru_fallback = False
        if not fresh:
            # 全都在 min_pick_gap 内被选过：按最近最少使用（LRU）兜底，覆盖全部候选
            fresh = [
                r
                for _, r in sorted(scored, key=lambda item: int(item[1].get("last_picked_at") or 0))
            ]
            lru_fallback = True
        else:
            fresh = [r for _, r in scored if r in fresh]

        top = fresh[:5]
        ordered: list[dict[str, Any]] = []
        ordered.extend(sticky)
        if explore_row is not None:
            ordered.append(explore_row)
        if lru_fallback:
            # LRU 兜底顺序即语义本身，不能再被等权重洗牌打乱
            ordered.extend(top)
        else:
            ordered.extend(self._weighted_order(top))
        ordered.extend([r for _, r in scored if r not in top])

        seen: set[int] = set()
        result: list[dict[str, Any]] = []
        for row in ordered:
            rid = int(row["id"])
            if rid in seen:
                continue
            seen.add(rid)
            result.append(row)

        # 全冷却兜底：按冷却到期时间从早到晚追加，避免直接 503
        fallback = sorted(
            [r for r in rows if int(r["id"]) not in seen and not saturated(r)],
            key=lambda r: int(r.get("cooldown_until") or 0) or 2**31,
        )
        result.extend(fallback)
        if result:
            self.db.execute(
                "UPDATE provider_accounts SET last_picked_at=? WHERE id=?",
                (now, int(result[0]["id"])),
            )
        return result

    def _model_cooldowns(self, provider_id: str, model: str, now: int) -> set[int]:
        if not model:
            return set()
        self.db.execute("DELETE FROM model_cooldowns WHERE until<=?", (now,))
        rows = self.db.query(
            "SELECT account_id FROM model_cooldowns WHERE provider_id=? AND model=? AND until>?",
            (provider_id, model, now),
        )
        return {int(r["account_id"]) for r in rows}

    def _weight(self, row: dict[str, Any], max_credits: float, now: int) -> float:
        weight = 1.0
        credits = max(0.0, float(row.get("credits") or 0))
        if max_credits > 0:
            weight += credits / max_credits * 10.0
        expiring = max(0.0, float(row.get("credits_expiring") or 0))
        if credits > 0 and expiring > 0:
            weight += min(1.0, expiring / credits) * _setting_float(self.db, "pool_expiring_weight", 8.0)
        last_used = int(row.get("last_used_at") or 0)
        per_hour = _setting_float(self.db, "pool_idle_weight_per_hour", 0.1)
        idle_max = _setting_float(self.db, "pool_idle_weight_max", 3.0)
        if last_used <= 0:
            weight += idle_max
        else:
            weight += max(0.0, min(idle_max, (now - last_used) / 3600.0 * per_hour))
        weight *= max(1, int(row.get("weight") or 1)) ** 0.5
        return weight

    def _weighted_order(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(rows) <= 1:
            return list(rows)
        max_credits = max([max(0.0, float(r.get("credits") or 0)) for r in rows] + [0.0])
        now = _now()
        weights = [max(0.001, self._weight(r, max_credits, now)) for r in rows]
        # 等权重候选先随机打乱，避免固定顺序在短名单内形成偏差
        if max(weights) - min(weights) <= EQUAL_WEIGHT_EPSILON * max(1.0, max(weights)):
            shuffled = list(rows)
            random.shuffle(shuffled)
            return shuffled
        result: list[dict[str, Any]] = []
        pool = list(zip(weights, rows))
        while pool:
            total = sum(w for w, _ in pool)
            pick = random.uniform(0, total)
            acc = 0.0
            index = len(pool) - 1
            for i, (w, _) in enumerate(pool):
                acc += w
                if pick <= acc:
                    index = i
                    break
            result.append(pool.pop(index)[1])
        return result

    # ------------------------------------------------------------------ inflight
    def acquire(self, account_id: int) -> None:
        self.db.execute(
            "UPDATE provider_accounts SET in_flight=in_flight+1 WHERE id=?", (int(account_id),)
        )

    def release(self, account_id: int) -> None:
        self.db.execute(
            "UPDATE provider_accounts SET in_flight=MAX(0, in_flight-1) WHERE id=?", (int(account_id),)
        )

    # ------------------------------------------------------------------ outcome
    def mark_success(
        self,
        account_id: int,
        provider_id: str,
        model: str,
        credits_delta: float = -1,
        total_tokens: int = 0,
    ) -> None:
        now = _now()
        self.db.execute(
            "UPDATE provider_accounts SET fail_streak=0, soft_streak=0, cooldown_until=0, "
            "cooldown_kind='', last_used_at=?, updated_at=? WHERE id=?",
            (now, now, int(account_id)),
        )
        if model and credits_delta is not None and float(credits_delta) >= 0 and int(total_tokens or 0) > 0:
            self.note_model_cost(int(account_id), provider_id, model, float(credits_delta), int(total_tokens))
        self.persist_state()

    def note_model_cost(
        self,
        account_id: int,
        provider_id: str,
        model: str,
        credits: float,
        tokens: int,
    ) -> None:
        """记录 (账号, 模型) 的实测单价（积分 / 1K tokens），EMA 平滑。"""
        if not model or tokens <= 0 or credits < 0:
            return
        cost = credits / (tokens / 1000.0)
        now = _now()
        row = self.db.query_one(
            "SELECT cost_per_1k, samples FROM account_costs WHERE account_id=? AND model=?",
            (int(account_id), model),
        )
        if row:
            samples = int(row["samples"] or 0)
            value = float(row["cost_per_1k"] or 0) * 0.7 + cost * 0.3
            self.db.execute(
                "UPDATE account_costs SET provider_id=?, cost_per_1k=?, samples=?, last_credits=?, "
                "last_tokens=?, updated_at=? WHERE account_id=? AND model=?",
                (provider_id, value, samples + 1, credits, tokens, now, int(account_id), model),
            )
        else:
            self.db.execute(
                "INSERT INTO account_costs(account_id, provider_id, model, cost_per_1k, samples, "
                "last_credits, last_tokens, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (int(account_id), provider_id, model, cost, 1, credits, tokens, now),
            )

    def account_cost(self, account_id: int, model: str, now: Optional[int] = None) -> dict[str, Any]:
        if not model:
            return {}
        row = self.db.query_one(
            "SELECT * FROM account_costs WHERE account_id=? AND model=?", (int(account_id), model)
        )
        if not row:
            return {}
        item = dict(row)
        # 6 小时未更新视为陈旧，避免过期价格长期生效
        if (now or _now()) - int(item.get("updated_at") or 0) > COST_TTL_SECONDS:
            return {}
        return item

    def cost_tier(self, account_id: int, model: str, now: Optional[int] = None) -> tuple[int, float]:
        """0=实测免费，1=无观测（含过期），2=实测收费。"""
        cost = self.account_cost(account_id, model, now)
        if not cost:
            return 1, 0.0
        value = float(cost.get("cost_per_1k") or 0)
        return (0, 0.0) if value <= 0 else (2, value)

    def model_cost(self, provider_id: str, model: str) -> dict[str, Any]:
        """平台 + 模型维度的聚合观测（供展示用）。"""
        rows = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM account_costs WHERE provider_id=? AND model=? AND updated_at>?",
                (provider_id, model, _now() - COST_TTL_SECONDS),
            )
        ]
        if rows:
            total_samples = sum(int(r["samples"] or 0) for r in rows)
            weighted = sum(float(r["cost_per_1k"] or 0) * int(r["samples"] or 0) for r in rows)
            return {
                "cost_per_1k": weighted / total_samples if total_samples else 0.0,
                "samples": total_samples,
                "accounts": len(rows),
                "updated_at": max(int(r["updated_at"] or 0) for r in rows),
            }
        # 兜底：用使用日志里的积分差值估算（覆盖 Trae 等未上报 usage.credits 的平台）
        row = self.db.query_one(
            "SELECT COUNT(*) AS samples, COALESCE(SUM(credits_delta),0) AS credits, "
            "COALESCE(SUM(total_tokens),0) AS tokens, MAX(created_at) AS updated_at "
            "FROM usage_logs WHERE provider_id=? AND model=? AND created_at>? AND credits_delta>0 AND total_tokens>0",
            (provider_id, model, _now() - COST_TTL_SECONDS),
        )
        if not row or int(row["tokens"] or 0) <= 0:
            return {}
        return {
            "cost_per_1k": float(row["credits"] or 0) / (float(row["tokens"]) / 1000.0),
            "samples": int(row["samples"] or 0),
            "accounts": 0,
            "updated_at": int(row["updated_at"] or 0),
            "source": "usage_logs",
        }

    # ------------------------------------------------------------------ explore
    def _should_explore(self, provider_id: str, model: str, now: int) -> bool:
        interval = self.explore_interval()
        limit = self.explore_max_per_day()
        if interval <= 0 or limit <= 0 or not model:
            return False
        row = self.db.query_one(
            "SELECT count, last_at FROM cost_explore WHERE provider_id=? AND model=? AND day=?",
            (provider_id, model, _today()),
        )
        if not row:
            return True
        if int(row["count"] or 0) >= limit:
            return False
        return now - int(row["last_at"] or 0) >= interval

    def _record_explore(self, provider_id: str, model: str, now: int) -> None:
        self.db.execute(
            "INSERT INTO cost_explore(provider_id, model, day, count, last_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(provider_id, model, day) DO UPDATE SET count=count+1, last_at=excluded.last_at",
            (provider_id, model, _today(), 1, now),
        )

    def mark_failure(
        self,
        account_id: int,
        provider_id: str,
        model: str,
        status_code: int = 0,
        message: str = "",
        error_code: str = "",
    ) -> dict[str, Any]:
        now = _now()
        text = f"{error_code} {message}"
        raw = self.db.query_one("SELECT * FROM provider_accounts WHERE id=?", (int(account_id),))
        if not raw:
            return {}
        row = dict(raw)
        soft_streak = int(row.get("soft_streak") or 0)
        fail_streak = int(row.get("fail_streak") or 0) + 1
        kind = ""
        until = 0

        if status_code == 429 or "429" in text:
            soft_streak += 1
            base = _setting_int(self.db, "pool_soft_rate_base", 600)
            cap = _setting_int(self.db, "pool_soft_rate_max", 7200)
            until = now + min(cap, base * (2 ** (soft_streak - 1)))
            kind = "soft_rate"
        elif status_code == 402 or (CREDIT_EXHAUSTED_PATTERN.search(text) and status_code in (0, 400, 403)):
            until = _next_reset_at()
            kind = "credit_exhausted"
        elif status_code == 404:
            until = now + 300
            kind = "not_found"
        elif status_code in (401, 403) or AUTH_PATTERN.search(text):
            until = now + 1800
            kind = "auth"
        elif MODEL_LIMIT_PATTERN.search(text):
            # 模型级冷却：只冷却触发调用的模型，其他模型立即可用
            cap = _setting_int(self.db, "pool_soft_rate_max", 7200)
            until = now + min(cap, 600)
            self.db.execute(
                "INSERT INTO model_cooldowns(provider_id, account_id, model, until, reason) "
                "VALUES(?,?,?,?,?) ON CONFLICT(provider_id, account_id, model) DO UPDATE SET "
                "until=excluded.until, reason=excluded.reason",
                (provider_id, int(account_id), model or "*", until, message[:300]),
            )
            self.db.execute(
                "UPDATE provider_accounts SET last_error=?, updated_at=? WHERE id=?",
                (message[:1000], now, int(account_id)),
            )
            self.persist_state(force=True)
            return {"kind": "model_cooldown", "until": until}

        threshold = _setting_int(self.db, "pool_breaker_threshold", 3)
        if not kind and fail_streak >= threshold:
            base = _setting_int(self.db, "pool_breaker_base", 60)
            cap = _setting_int(self.db, "pool_breaker_max", 21600)
            until = now + min(cap, base * (2 ** max(0, fail_streak - threshold)))
            kind = "breaker"

        self.db.execute(
            "UPDATE provider_accounts SET fail_streak=?, soft_streak=?, cooldown_until=?, cooldown_kind=?, "
            "last_error=?, updated_at=? WHERE id=?",
            (fail_streak, soft_streak, until, kind, message[:1000], now, int(account_id)),
        )
        self.persist_state()
        return {"kind": kind, "until": until, "fail_streak": fail_streak, "soft_streak": soft_streak}

    def clear_cooldown(self, account_id: int) -> None:
        self.db.execute(
            "UPDATE provider_accounts SET cooldown_until=0, cooldown_kind='', fail_streak=0, "
            "soft_streak=0, last_error='', updated_at=? WHERE id=?",
            (_now(), int(account_id)),
        )

    # ------------------------------------------------------------------ budget
    def budget_status(self, refresh: bool = True) -> dict[str, Any]:
        try:
            limit = float(self.db.get_setting("daily_credit_limit", "0") or 0)
        except Exception:
            limit = 0.0
        today = _today()
        if not hasattr(self, "_budget_cache") or self._budget_cache.get("day") != today:
            self._budget_cache = {"day": today, "used": 0.0, "rejected": 0, "at": 0}
        cache = self._budget_cache
        if refresh and (_now() - int(cache.get("at") or 0) >= 5 or int(cache.get("at") or 0) == 0):
            try:
                from zoneinfo import ZoneInfo

                base = datetime.now(ZoneInfo("Asia/Shanghai"))
            except Exception:
                base = datetime.now()
            start = int(base.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            row = self.db.query_one(
                "SELECT COALESCE(SUM(credits_delta),0) AS used FROM usage_logs WHERE created_at>=?",
                (start,),
            )
            cache["used"] = float(row["used"] or 0) if row else 0.0
            cache["at"] = _now()
        return {
            "day": today,
            "used": round(float(cache.get("used") or 0), 4),
            "limit": limit,
            "rejected": int(cache.get("rejected") or 0),
            "enabled": limit > 0,
        }

    def check_budget(self) -> dict[str, Any]:
        """返回 {allowed, used, limit, ...}；超限时累计 rejected。"""
        status = self.budget_status()
        if status["limit"] <= 0:
            return {**status, "allowed": True}
        allowed = status["used"] < status["limit"]
        if not allowed:
            self._budget_cache["rejected"] = int(self._budget_cache.get("rejected") or 0) + 1
            status["rejected"] = self._budget_cache["rejected"]
        return {**status, "allowed": allowed}

    # ------------------------------------------------------------------ manual
    def set_manual_disabled(self, account_id: int, disabled: bool, reason: str = "") -> None:
        self.db.execute(
            "UPDATE provider_accounts SET manual_disabled=?, manual_reason=?, updated_at=? WHERE id=?",
            (1 if disabled else 0, reason[:200] if disabled else "", _now(), int(account_id)),
        )
        self.persist_state(force=True)

    # ------------------------------------------------------------------ cooldowns
    def model_cooldown_report(self, provider_id: str = "") -> list[dict[str, Any]]:
        now = _now()
        self.db.execute("DELETE FROM model_cooldowns WHERE until<=?", (now,))
        sql = (
            "SELECT provider_id, account_id, model, until, reason FROM model_cooldowns WHERE until>?"
        )
        params: list[Any] = [now]
        if provider_id:
            sql += " AND provider_id=?"
            params.append(provider_id)
        sql += " ORDER BY until"
        return [dict(r) for r in self.db.query(sql, params)]

    # ------------------------------------------------------------------ state
    def snapshot_state(self) -> dict[str, Any]:
        accounts = [
            dict(r)
            for r in self.db.query(
                "SELECT id, provider_id, credits, credits_used, credits_total, credits_expiring, "
                "cooldown_until, cooldown_kind, fail_streak, soft_streak, manual_disabled, manual_reason, "
                "last_used_at FROM provider_accounts"
            )
        ]
        cooldowns = [dict(r) for r in self.db.query("SELECT * FROM model_cooldowns WHERE until>?", (_now(),))]
        costs = [dict(r) for r in self.db.query("SELECT * FROM account_costs")]
        explores = [dict(r) for r in self.db.query("SELECT * FROM cost_explore")]
        return {
            "version": 1,
            "saved_at": _now(),
            "accounts": accounts,
            "model_cooldowns": cooldowns,
            "account_costs": costs,
            "cost_explore": explores,
        }

    def persist_state(self, force: bool = False) -> None:
        """原子落盘 data/pool_state.json（节流 5s），可选镜像到 Upstash Redis。"""
        now = _now()
        last = getattr(self, "_persist_at", 0)
        if not force and now - last < 5:
            return
        self._persist_at = now
        try:
            data_dir = Path(self.db.path).parent if hasattr(self.db, "path") else None
        except Exception:
            data_dir = None
        if data_dir is None:
            return
        target = data_dir / "pool_state.json"
        tmp = target.with_suffix(".json.tmp")
        try:
            import json as _json

            payload = _json.dumps(self.snapshot_state(), ensure_ascii=False)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(target)
        except Exception:
            return
        self._mirror_upstash(payload)

    def _mirror_upstash(self, payload: str) -> None:
        try:
            url = self.db.get_setting("upstash_redis_rest_url", "").strip()
            token = self.db.get_setting("upstash_redis_rest_token", "").strip()
        except Exception:
            return
        if not url or not token:
            return
        try:
            import httpx

            httpx.post(
                url.rstrip("/") + "/set/traeapi:pool_state",
                headers={"Authorization": "Bearer " + token, "Content-Type": "text/plain"},
                content=payload.encode("utf-8"),
                timeout=5,
            )
        except Exception:
            return

    # ------------------------------------------------------------------ report
    def provider_stats(self, provider_id: str) -> dict[str, Any]:
        now = _now()
        rows = [
            dict(r)
            for r in self.db.query("SELECT * FROM provider_accounts WHERE provider_id=?", (provider_id,))
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

        cooling = [r for r in rows if int(r.get("cooldown_until") or 0) > now]
        errors = [str(r.get("last_error") or "") for r in rows if r.get("last_error")]
        return {
            "accounts": len(rows),
            "enabled": len([r for r in rows if int(r.get("enabled") or 0)]),
            "cooling": len(cooling),
            "remaining": total("credits"),
            "used": total("credits_used"),
            "total": total("credits_total"),
            "in_flight": sum(int(r.get("in_flight") or 0) for r in rows),
            "manual_disabled": len([r for r in rows if int(r.get("manual_disabled") or 0)]),
            "errors": errors[:3],
        }

    def costs(self, provider_id: str) -> dict[str, dict[str, Any]]:
        """平台下每个模型的最新聚合单价（供模型管理页展示）。"""
        rows = self.db.query(
            "SELECT DISTINCT model FROM account_costs WHERE provider_id=? AND updated_at>?",
            (provider_id, _now() - COST_TTL_SECONDS),
        )
        return {str(r["model"]): self.model_cost(provider_id, str(r["model"])) for r in rows}

    def cost_report(self) -> list[dict[str, Any]]:
        """全部 (平台, 模型) 成本台账，供 /status 透出。"""
        rows = self.db.query(
            "SELECT DISTINCT provider_id, model FROM account_costs WHERE updated_at>? ORDER BY provider_id, model",
            (_now() - COST_TTL_SECONDS,),
        )
        report: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            provider_id = str(row["provider_id"])
            model = str(row["model"])
            cost = self.model_cost(provider_id, model)
            if cost:
                report.append({"provider_id": provider_id, "model": model, **cost})
                seen.add((provider_id, model))
        # 使用日志兜底（Trae 等平台）
        log_rows = self.db.query(
            "SELECT provider_id, model, COUNT(*) AS samples, COALESCE(SUM(credits_delta),0) AS credits, "
            "COALESCE(SUM(total_tokens),0) AS tokens, MAX(created_at) AS updated_at FROM usage_logs "
            "WHERE created_at>? AND credits_delta>0 AND total_tokens>0 GROUP BY provider_id, model",
            (_now() - COST_TTL_SECONDS,),
        )
        for row in log_rows:
            provider_id = str(row["provider_id"] or "")
            model = str(row["model"] or "")
            if not provider_id or not model or (provider_id, model) in seen:
                continue
            tokens = float(row["tokens"] or 0)
            if tokens <= 0:
                continue
            report.append(
                {
                    "provider_id": provider_id,
                    "model": model,
                    "cost_per_1k": float(row["credits"] or 0) / (tokens / 1000.0),
                    "samples": int(row["samples"] or 0),
                    "accounts": 0,
                    "updated_at": int(row["updated_at"] or 0),
                    "source": "usage_logs",
                }
            )
        return report

    def explore_report(self) -> list[dict[str, Any]]:
        day = _today()
        rows = self.db.query("SELECT * FROM cost_explore WHERE day=? ORDER BY last_at DESC", (day,))
        return [dict(r) for r in rows]
