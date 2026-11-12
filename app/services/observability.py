from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..db import Database


REALMS = ("cn", "global", "all")
POOL_STATUSES = ("total", "enabled", "ready", "manual_disabled", "cooling", "breaker", "disabled")
MODEL_STATUSES = ("success", "failure")
TOKEN_TYPES = ("prompt", "completion", "total")
CACHE_TYPES = ("hit", "miss", "write")
TASK_METRICS = (
    "last_run_timestamp_seconds",
    "last_run_total",
    "last_run_success",
    "last_run_failed",
    "last_run_skipped",
    "last_run_all_failed",
)


def _setting(db: Database, keys: Iterable[str], default: str = "") -> str:
    for key in keys:
        value = db.get_setting(key, "")
        if value not in (None, ""):
            return str(value)
    return default


def _setting_int(db: Database, keys: Iterable[str], default: int) -> int:
    try:
        return int(float(_setting(db, keys, str(default))))
    except Exception:
        return default


def _setting_float(db: Database, keys: Iterable[str], default: float) -> float:
    try:
        return float(_setting(db, keys, str(default)))
    except Exception:
        return default


def _setting_bool(db: Database, keys: Iterable[str], default: bool = False) -> bool:
    value = _setting(db, keys, "1" if default else "0").strip().lower()
    return value in {"1", "true", "yes", "on", "y"}


def _beijing_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=8)))


def _realm(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("global", "intl", "international", "oversea", "overseas"):
        return "global"
    return "cn"


def _escape(value: Any) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def _number(value: Any) -> str:
    try:
        number = float(value or 0)
    except Exception:
        number = 0.0
    if number == int(number):
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


class ObservabilityService:
    """统一聚合 /status 与 /metrics 使用的池与请求口径。

    只读取聚合结果，不输出账号 uid、token、cookie 等敏感维度。
    """

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ config
    def metrics_enabled(self) -> bool:
        return _setting_bool(self.db, ("metrics.enabled", "metrics_enabled"), False)

    def alerting_enabled(self) -> bool:
        return _setting_bool(self.db, ("alerting.enabled", "alerting_enabled"), False)

    def alerting_interval(self) -> int:
        return max(5, _setting_int(self.db, ("alerting.interval_seconds", "alerting_interval_seconds"), 30))

    def alerting_min_healthy(self, realm: str) -> int:
        return max(
            0,
            _setting_int(
                self.db,
                (f"alerting.min_healthy_{realm}", f"alerting_min_healthy_{realm}"),
                1 if realm == "cn" else 0,
            ),
        )

    def alerting_breaker_threshold(self) -> int:
        return max(
            0,
            _setting_int(
                self.db,
                ("alerting.breaker_threshold", "alerting_breaker_threshold"),
                0,
            ),
        )

    def alerting_for_ticks(self) -> int:
        return max(1, _setting_int(self.db, ("alerting.for_ticks", "alerting_for_ticks"), 3))

    def alerting_clear_ticks(self) -> int:
        return max(1, _setting_int(self.db, ("alerting.clear_ticks", "alerting_clear_ticks"), 3))

    def alerting_startup_grace(self) -> int:
        return max(
            0,
            _setting_int(
                self.db,
                ("alerting.startup_grace_seconds", "alerting_startup_grace_seconds"),
                30,
            ),
        )

    def alerting_timeout(self) -> float:
        return max(
            0.5,
            _setting_float(self.db, ("alerting.timeout_seconds", "alerting_timeout_seconds"), 5.0),
        )

    def alerting_send_resolve(self) -> bool:
        return _setting_bool(self.db, ("alerting.send_resolve", "alerting_send_resolve"), False)

    def alerting_webhook(self) -> str:
        return _setting(self.db, ("alerting.webhook_url", "alerting_webhook_url"), "").strip()

    def alerting_secret(self) -> str:
        return _setting(self.db, ("alerting.secret", "alerting_secret"), "").strip()

    # ------------------------------------------------------------------ aggregate
    def _provider_map(self) -> dict[str, dict[str, Any]]:
        return {
            str(row["id"]): dict(row)
            for row in self.db.query("SELECT id, name, kind, region, enabled FROM providers")
        }

    def _empty_realm(self) -> dict[str, Any]:
        return {
            "total": 0,
            "enabled": 0,
            "ready": 0,
            "manual_disabled": 0,
            "cooling": 0,
            "breaker": 0,
            "disabled": 0,
            "in_flight": 0,
        }

    def _waf_blocked(self, now: int) -> bool:
        since = now - 300
        patterns = (
            "%waf%",
            "%cloudflare%",
            "%cf-ray%",
            "%1020%",
            "%web application firewall%",
            "%请求被拦截%",
            "%安全拦截%",
        )
        clause = " OR ".join(["lower(error) LIKE ?"] * len(patterns))
        row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM usage_logs WHERE created_at>=? AND ({clause})",
            (since, *patterns),
        )
        if row and int(row["n"] or 0) > 0:
            return True
        account_clause = " OR ".join(["lower(last_error) LIKE ?"] * len(patterns))
        row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM accounts WHERE last_error<>'' AND ({account_clause})",
            patterns,
        )
        if row and int(row["n"] or 0) > 0:
            return True
        row = self.db.query_one(
            f"SELECT COUNT(*) AS n FROM provider_accounts WHERE last_error<>'' AND ({account_clause})",
            patterns,
        )
        return bool(row and int(row["n"] or 0) > 0)

    def pool_snapshot(self) -> dict[str, Any]:
        """一次查询完成池规模、状态、在途、粘性、探索与任务聚合。"""
        now = int(time.time())
        providers = self._provider_map()
        realms: dict[str, dict[str, Any]] = {realm: self._empty_realm() for realm in ("cn", "global")}

        def add(realm: str, status: str, in_flight: int, enabled: bool) -> None:
            bucket = realms.setdefault(realm, self._empty_realm())
            bucket["total"] += 1
            if enabled:
                bucket["enabled"] += 1
            bucket[status] = int(bucket.get(status) or 0) + 1
            bucket["in_flight"] = int(bucket.get("in_flight") or 0) + max(0, int(in_flight or 0))

        # 聚合平台账号
        rows = self.db.query(
            "SELECT pa.*, p.region AS provider_region, p.enabled AS provider_enabled, "
            "p.kind AS provider_kind FROM provider_accounts pa "
            "JOIN providers p ON p.id=pa.provider_id"
        )
        for raw in rows:
            row = dict(raw)
            realm = _realm(row.get("region") or row.get("provider_region"))
            enabled = bool(int(row.get("enabled") or 0)) and bool(int(row.get("provider_enabled") or 0))
            manual = bool(int(row.get("manual_disabled") or 0))
            cooling = int(row.get("cooldown_until") or 0) > now
            breaker = cooling and str(row.get("cooldown_kind") or "") == "breaker"
            if not enabled:
                status = "disabled"
            elif manual:
                status = "manual_disabled"
            elif cooling:
                status = "breaker" if breaker else "cooling"
            else:
                status = "ready"
            add(realm, status, int(row.get("in_flight") or 0), enabled)

        # Trae 账号（独立表）
        for raw in self.db.query("SELECT * FROM accounts"):
            row = dict(raw)
            realm = _realm(row.get("region"))
            enabled = bool(int(row.get("enabled") or 0))
            manual = bool(int(row.get("manual_disabled") or 0))
            if not enabled:
                status = "disabled"
            elif manual:
                status = "manual_disabled"
            else:
                status = "ready"
            add(realm, status, 0, enabled)

        # all = cn + global
        total = self._empty_realm()
        for realm in ("cn", "global"):
            for key in total:
                total[key] += int(realms[realm].get(key) or 0)
        realms["all"] = total

        # 会话粘性
        sessions = {realm: 0 for realm in ("cn", "global")}
        for row in self.db.query(
            "SELECT provider_id, COUNT(*) AS n FROM session_bindings WHERE expires_at>? GROUP BY provider_id",
            (now,),
        ):
            provider = providers.get(str(row["provider_id"])) or {}
            sessions[_realm(provider.get("region"))] += int(row["n"] or 0)
        sessions["all"] = sessions["cn"] + sessions["global"]

        # 成本探索触发（当日 / 累计）
        today = _beijing_now().strftime("%Y-%m-%d")
        explores = {realm: 0 for realm in ("cn", "global")}
        explores_all = {realm: 0 for realm in ("cn", "global")}
        for row in self.db.query(
            "SELECT provider_id, COALESCE(SUM(count),0) AS n FROM cost_explore GROUP BY provider_id"
        ):
            provider = providers.get(str(row["provider_id"])) or {}
            explores_all[_realm(provider.get("region"))] += int(row["n"] or 0)
        for row in self.db.query(
            "SELECT provider_id, COALESCE(SUM(count),0) AS n FROM cost_explore WHERE day=? GROUP BY provider_id",
            (today,),
        ):
            provider = providers.get(str(row["provider_id"])) or {}
            explores[_realm(provider.get("region"))] += int(row["n"] or 0)
        explores["all"] = explores["cn"] + explores["global"]
        explores_all["all"] = explores_all["cn"] + explores_all["global"]

        # 按模型的当日请求聚合
        start = int(
            _beijing_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        model_rows = self.db.query(
            """
            SELECT model,
                   COUNT(*) AS requests,
                   COALESCE(SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END),0) AS success,
                   COALESCE(SUM(CASE WHEN status<>'ok' THEN 1 ELSE 0 END),0) AS failure,
                   COALESCE(SUM(stream),0) AS streams,
                   COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                   COALESCE(SUM(total_tokens),0) AS total_tokens,
                   COALESCE(SUM(cache_read_tokens),0) AS cache_read,
                   COALESCE(SUM(
                       CASE WHEN cache_miss_tokens=0 AND cache_read_tokens=0 AND cache_write_tokens=0
                            THEN prompt_tokens ELSE cache_miss_tokens END
                   ),0) AS cache_miss,
                   COALESCE(SUM(cache_write_tokens),0) AS cache_write,
                   COALESCE(SUM(credits_delta),0) AS credits,
                   COALESCE(SUM(CASE WHEN ttft_ms>0 THEN ttft_ms ELSE 0 END),0) AS ttft_sum,
                   COALESCE(SUM(CASE WHEN ttft_ms>0 THEN 1 ELSE 0 END),0) AS ttft_count,
                   COALESCE(SUM(latency_ms),0) AS latency_sum,
                   COUNT(*) AS latency_count,
                   COALESCE(SUM(
                       CASE WHEN completion_ms>0 OR latency_ms>ttft_ms THEN completion_tokens ELSE 0 END
                   ),0) AS gen_tokens,
                   COALESCE(SUM(
                       CASE WHEN completion_ms>0 THEN completion_ms
                            WHEN latency_ms>ttft_ms THEN latency_ms-ttft_ms ELSE 0 END
                   ),0) AS gen_ms
            FROM usage_logs
            WHERE created_at>=? AND model<>''
            GROUP BY model
            ORDER BY model
            """,
            (start,),
        )
        models = [dict(row) for row in model_rows]

        tasks = [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM task_runs WHERE finished_at>0 ORDER BY task"
            )
        ]
        return {
            "generated_at": now,
            "today": today,
            "realms": realms,
            "sessions": sessions,
            "explores": explores,
            "explores_all": explores_all,
            "waf_blocked": self._waf_blocked(now),
            "models": models,
            "tasks": tasks,
        }

    # ------------------------------------------------------------------ prometheus
    def render_prometheus(self, snapshot: dict[str, Any] | None = None) -> str:
        data = snapshot or self.pool_snapshot()
        lines: list[str] = []

        def family(name: str, kind: str, help_text: str, samples: list[str]) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            lines.extend(samples)

        pool_samples = []
        for realm in REALMS:
            bucket = data["realms"].get(realm) or self._empty_realm()
            for status in POOL_STATUSES:
                pool_samples.append(
                    f'aiall_pool_accounts{{realm="{realm}",status="{status}"}} '
                    f'{_number(bucket.get(status) or 0)}'
                )
        family(
            "aiall_pool_accounts",
            "gauge",
            "Account pool size and status distribution by realm.",
            pool_samples,
        )

        family(
            "aiall_pool_in_flight_requests",
            "gauge",
            "Requests currently leased to an account, by realm.",
            [
                f'aiall_pool_in_flight_requests{{realm="{realm}"}} '
                f'{_number((data["realms"].get(realm) or {}).get("in_flight") or 0)}'
                for realm in REALMS
            ],
        )
        family(
            "aiall_session_bindings",
            "gauge",
            "Active sticky session bindings, by realm.",
            [
                f'aiall_session_bindings{{realm="{realm}"}} {_number(data["sessions"].get(realm) or 0)}'
                for realm in REALMS
            ],
        )
        family(
            "aiall_cost_explore_triggers_total",
            "gauge",
            "Cost exploration triggers accumulated all time, by realm.",
            [
                f'aiall_cost_explore_triggers_total{{realm="{realm}"}} '
                f'{_number(data.get("explores_all", {}).get(realm) or 0)}'
                for realm in REALMS
            ],
        )
        family(
            "aiall_cost_explore_triggers_today",
            "gauge",
            "Cost exploration triggers accumulated today, by realm.",
            [
                f'aiall_cost_explore_triggers_today{{realm="{realm}"}} '
                f'{_number(data["explores"].get(realm) or 0)}'
                for realm in REALMS
            ],
        )
        family(
            "aiall_waf_ip_blocked",
            "gauge",
            "1 when a recent upstream WAF/IP-block signature is observed.",
            [f"aiall_waf_ip_blocked {1 if data.get('waf_blocked') else 0}"],
        )

        model_samples: dict[str, list[str]] = {
            "requests": [],
            "streams": [],
            "tokens": [],
            "cache": [],
            "credits": [],
            "ttft": [],
            "latency": [],
            "throughput": [],
        }
        for row in data["models"]:
            model = _escape(row["model"])
            for status in MODEL_STATUSES:
                value = row.get("success") if status == "success" else row.get("failure")
                model_samples["requests"].append(
                    f'aiall_model_requests_total{{model="{model}",status="{status}"}} {_number(value or 0)}'
                )
            model_samples["streams"].append(
                f'aiall_model_stream_requests_total{{model="{model}"}} {_number(row.get("streams") or 0)}'
            )
            for token_type, field in (
                ("prompt", "prompt_tokens"),
                ("completion", "completion_tokens"),
                ("total", "total_tokens"),
            ):
                model_samples["tokens"].append(
                    f'aiall_model_tokens_total{{model="{model}",type="{token_type}"}} '
                    f'{_number(row.get(field) or 0)}'
                )
            for cache_type, field in (
                ("hit", "cache_read"),
                ("miss", "cache_miss"),
                ("write", "cache_write"),
            ):
                model_samples["cache"].append(
                    f'aiall_model_cache_tokens_total{{model="{model}",type="{cache_type}"}} '
                    f'{_number(row.get(field) or 0)}'
                )
            model_samples["credits"].append(
                f'aiall_model_credits_total{{model="{model}"}} {_number(row.get("credits") or 0)}'
            )
            ttft_count = int(row.get("ttft_count") or 0)
            latency_count = int(row.get("latency_count") or 0)
            ttft_avg = float(row.get("ttft_sum") or 0) / ttft_count / 1000.0 if ttft_count else 0.0
            latency_avg = (
                float(row.get("latency_sum") or 0) / latency_count / 1000.0 if latency_count else 0.0
            )
            gen_ms = float(row.get("gen_ms") or 0)
            throughput = (
                float(row.get("gen_tokens") or 0) / (gen_ms / 1000.0) if gen_ms > 0 else 0.0
            )
            model_samples["ttft"].append(
                f'aiall_model_ttft_seconds_avg{{model="{model}"}} {_number(ttft_avg)}'
            )
            model_samples["latency"].append(
                f'aiall_model_latency_seconds_avg{{model="{model}"}} {_number(latency_avg)}'
            )
            model_samples["throughput"].append(
                f'aiall_model_generation_tokens_per_second{{model="{model}"}} {_number(throughput)}'
            )

        family(
            "aiall_model_requests_total",
            "gauge",
            "Model requests today, split by success/failure.",
            model_samples["requests"],
        )
        family(
            "aiall_model_stream_requests_total",
            "gauge",
            "Streaming model requests today.",
            model_samples["streams"],
        )
        family(
            "aiall_model_tokens_total",
            "gauge",
            "Model tokens today, split by prompt/completion/total.",
            model_samples["tokens"],
        )
        family(
            "aiall_model_cache_tokens_total",
            "gauge",
            "Model cache tokens today, split by hit/miss/write.",
            model_samples["cache"],
        )
        family(
            "aiall_model_credits_total",
            "gauge",
            "Model credit consumption today.",
            model_samples["credits"],
        )
        family(
            "aiall_model_ttft_seconds_avg",
            "gauge",
            "Average time to first token today, in seconds.",
            model_samples["ttft"],
        )
        family(
            "aiall_model_latency_seconds_avg",
            "gauge",
            "Average end-to-end latency today, in seconds.",
            model_samples["latency"],
        )
        family(
            "aiall_model_generation_tokens_per_second",
            "gauge",
            "Average generation throughput today, tokens per second.",
            model_samples["throughput"],
        )

        task_values: dict[str, list[str]] = {name: [] for name in TASK_METRICS}
        for row in data["tasks"]:
            task = _escape(row["task"])
            task_values["last_run_timestamp_seconds"].append(
                f'aiall_task_last_run_timestamp_seconds{{task="{task}"}} '
                f'{_number(row.get("finished_at") or 0)}'
            )
            for metric, field in (
                ("last_run_total", "total"),
                ("last_run_success", "success"),
                ("last_run_failed", "failed"),
                ("last_run_skipped", "skipped"),
                ("last_run_all_failed", "all_failed"),
            ):
                task_values[metric].append(
                    f'aiall_task_{metric}{{task="{task}"}} {_number(row.get(field) or 0)}'
                )
        for metric in TASK_METRICS:
            family(
                f"aiall_task_{metric}",
                "gauge",
                "Last completed scheduled task run.",
                task_values[metric],
            )

        output = "\n".join(lines) + "\n"
        try:
            legacy = str(self.db.get_setting("metrics.legacy_prefix", "1")) == "1"
        except Exception:
            legacy = True
        if legacy:
            def _legacy(line: str) -> str:
                if "aiall_task_" in line:
                    return line.replace("aiall_task_", "wb2api_task_")
                return line.replace("aiall_", "traeapi_")

            output += "\n".join(_legacy(line) for line in lines) + "\n"
        return output
