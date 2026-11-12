from __future__ import annotations

import json
import logging
import os
import secrets
import time

from .config import Settings
from .db import Database
from .security import SecretBox, hash_password, load_or_create_secret, new_api_key, token_hash

log = logging.getLogger("aiall.bootstrap")

DEFAULT_SETTINGS = {
    "default_model": "deepseek-v4.1-flash",
    "metrics.legacy_prefix": "1",
    "models_auto_refresh": "1",
    "models_refresh_time": "10:10",
    "last_models_refresh": "",
    "prompt.mode": "passthrough",
    "prompt.file": "",
    "privacy.sanitize_fingerprint": "1",
    "schedule.jitter_minutes": "0",
    "schedule.ledger_file": "",
    "schedule.retry_delay_minutes": "0",
    "schedule.retry_max_per_day": "1",
    "schedule.activity_enabled": "0",
    "schedule.activity_time": "10:00",
    "schedule.travel_enabled": "0",
    "schedule.travel_time": "09:00",
    "schedule.school_enabled": "0",
    "schedule.school_time": "12:00",
    "schedule.cat_enabled": "0",
    "schedule.cat_time": "01:00",
    "checkin_time": "10:05",
    "auto_checkin": "1",
    "log_retention_days": "30",
    "allow_any_key": "0",
    "max_concurrent_per_account": "2",
    "upstream_timeout": "180",
    "first_event_timeout": "60",
    "ide_version": "3.4.0",
    "ide_version_code": "20260716",
    "chat_function": "solo_work_remote",
    "public_base_url": "",
    "last_checkin_run": "",
    # 账号池（移植自 workbuddy2api 的选号 / 冷却 / 熔断语义）
    "pool_max_in_flight": "2",
    "pool_min_pick_gap_ms": "100",
    "pool_expiring_weight": "8",
    "pool_idle_weight_per_hour": "0.1",
    "pool_idle_weight_max": "3",
    "pool_soft_rate_base": "600",
    "pool_soft_rate_max": "7200",
    "pool_breaker_threshold": "3",
    "pool_breaker_base": "60",
    "pool_breaker_max": "21600",
    "pool_session_ttl": "1800",
    "pool_expiring_soon_days": "7",
    "pool_cost_explore_interval": "1800",
    "pool_cost_explore_max_per_day": "48",
    "daily_credit_limit": "0",
    "upstash_redis_rest_url": "",
    "upstash_redis_rest_token": "",
    # 可观测与告警（缺省关闭）
    "metrics_enabled": "0",
    "alerting_enabled": "0",
    "alerting_interval_seconds": "30",
    "alerting_webhook_url": "",
    "alerting_secret": "",
    "alerting_min_healthy_cn": "1",
    "alerting_min_healthy_global": "0",
    "alerting_breaker_threshold": "0",
    "alerting_for_ticks": "3",
    "alerting_clear_ticks": "3",
    "alerting_startup_grace_seconds": "30",
    "alerting_timeout_seconds": "5",
    "alerting_send_resolve": "0",
    # 管理操作审计（缺省关闭）
    "admin_enabled": "1",
    "admin_audit_enabled": "0",
    "admin_audit_file": "./data/admin_audit.log",
}

# 内置平台。base_url / config 均可在管理台「聚合平台」中修改。
BUILTIN_PLATFORMS = [
    {
        "id": "trae-cn",
        "name": "Trae 国内版",
        "kind": "trae",
        "protocol": "trae",
        "region": "cn",
        "base_url": "https://trae-api-cn.mchost.guru",
        "config": {
            "oauth_host": "https://api.trae.com.cn",
            "console_host": "https://www.trae.cn",
            "usage_host": "https://api.trae.cn",
            "chat_path": "/api/agent/v3/llm_utils_chat",
            "models_path": "/api/ide/v1/get_detail_param",
        },
    },
    {
        "id": "trae-global",
        "name": "Trae 国际版",
        "kind": "trae",
        "protocol": "trae",
        "region": "global",
        "base_url": "https://trae-api-sg.mchost.guru",
        "config": {
            "oauth_host": "https://api.trae.ai",
            "console_host": "https://www.trae.ai",
            "usage_host": "https://api.trae.ai",
            "chat_path": "/api/agent/v3/llm_utils_chat",
            "models_path": "/api/ide/v1/get_detail_param",
        },
    },
    {
        "id": "workbuddy",
        "name": "WorkBuddy 国际版",
        "kind": "workbuddy",
        "protocol": "workbuddy",
        "region": "global",
        "base_url": "https://www.workbuddy.ai",
        "config": {"origin": "https://www.workbuddy.ai"},
    },
    {
        "id": "codebuddy",
        "name": "CodeBuddy 国内版",
        "kind": "codebuddy",
        "protocol": "workbuddy",
        "region": "cn",
        "base_url": "https://copilot.tencent.com",
        "config": {"origin": "https://www.codebuddy.cn", "billing_base": "https://www.codebuddy.cn"},
    },
    {
        "id": "lobster",
        "name": "LobsterAI",
        "kind": "lobster",
        "protocol": "lobster",
        "region": "cn",
        "base_url": "https://lobsterai-server.youdao.com",
        "config": {"portal": "https://lobsterai.youdao.com"},
    },
    {
        "id": "qoder-cn",
        "name": "Qoder 国内版",
        "kind": "qoder",
        "protocol": "qoder",
        "region": "cn",
        "base_url": "https://gateway.qoder.com.cn",
        "config": {"openapi": "https://openapi.qoder.com.cn", "portal": "https://qoder.com.cn"},
    },
    {
        "id": "qoder-global",
        "name": "Qoder 国际版",
        "kind": "qoder",
        "protocol": "qoder",
        "region": "global",
        "base_url": "https://api1.qoder.sh",
        "config": {"openapi": "https://openapi.qoder.sh", "portal": "https://qoder.com"},
    },
]


def bootstrap(settings: Settings, db: Database) -> SecretBox:
    settings.ensure_dirs()
    secret = load_or_create_secret(settings)
    box = SecretBox(secret)

    for key, value in DEFAULT_SETTINGS.items():
        if db.query_one("SELECT 1 FROM settings WHERE key=?", (key,)) is None:
            db.set_setting(key, value)

    # 签到统一为全局开关 + 时间，清理旧版 Qoder 独立配置与任务记录。
    db.execute(
        "DELETE FROM settings WHERE key IN ('qoder_checkin_time','qoder_auto_checkin','last_qoder_checkin_run')"
    )
    db.execute("DELETE FROM task_runs WHERE task='qoder_checkin'")

    now = int(time.time())
    for platform in BUILTIN_PLATFORMS:
        if db.query_one("SELECT 1 FROM providers WHERE id=?", (platform["id"],)) is not None:
            continue
        db.execute(
            "INSERT INTO providers(id, name, kind, protocol, base_url, region, config_json, models_json, "
            "enabled, weight, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                platform["id"],
                platform["name"],
                platform["kind"],
                platform["protocol"],
                platform["base_url"],
                platform["region"],
                json_dumps(platform["config"]),
                "[]",
                1,
                1,
                now,
                now,
            ),
        )

    # 修复历史数据：国际版 portal 使用 qoder.com；OpenAPI 必须走 openapi.qoder.sh
    row = db.query_one("SELECT config_json FROM providers WHERE id='qoder-global'")
    if row:
        cfg = {}
        try:
            cfg = json.loads(row["config_json"] or "{}")
        except Exception:
            cfg = {}
        changed = False
        if "qoder.sh" in str(cfg.get("portal") or ""):
            cfg["portal"] = "https://qoder.com"
            changed = True
        if not cfg.get("openapi") or "openapi.qoder.com" in str(cfg.get("openapi") or ""):
            cfg["openapi"] = "https://openapi.qoder.sh"
            changed = True
        if changed:
            db.execute(
                "UPDATE providers SET config_json=?, updated_at=? WHERE id='qoder-global'",
                (json_dumps(cfg), int(time.time())),
            )
    row = db.query_one("SELECT config_json FROM providers WHERE id='qoder-cn'")
    if row:
        cfg = {}
        try:
            cfg = json.loads(row["config_json"] or "{}")
        except Exception:
            cfg = {}
        if not cfg.get("openapi") or "qoder.sh" in str(cfg.get("openapi") or ""):
            cfg["openapi"] = "https://openapi.qoder.com.cn"
            db.execute(
                "UPDATE providers SET config_json=?, updated_at=? WHERE id='qoder-cn'",
                (json_dumps(cfg), int(time.time())),
            )

    db.execute("UPDATE providers SET region='cn' WHERE id='trae-cn' AND COALESCE(region,'')=''")
    db.execute("UPDATE accounts SET region='cn' WHERE COALESCE(region,'')=''")

    if db.query_one("SELECT 1 FROM admins LIMIT 1") is None:
        password = settings.admin_password or secrets.token_urlsafe(14)
        db.execute(
            "INSERT INTO admins(username, password_hash, created_at) VALUES(?,?,?)",
            (settings.admin_username, hash_password(password), int(time.time())),
        )
        content = (
            "AIAll API 初始管理员账号\n"
            f"用户名: {settings.admin_username}\n"
            f"密码: {password}\n\n"
            "登录后请立即在「设置」页面修改密码，并删除本文件。\n"
        )
        settings.initial_admin_path.write_text(content, encoding="utf-8")
        try:
            os.chmod(settings.initial_admin_path, 0o600)
        except OSError:
            pass
        log.warning("已生成初始管理员账号，凭据保存在 %s", settings.initial_admin_path)

    if db.query_one("SELECT 1 FROM api_keys LIMIT 1") is None:
        key = new_api_key()
        db.execute(
            "INSERT INTO api_keys(name, key_hash, key_prefix, key_encrypted, enabled, created_at) "
            "VALUES(?,?,?,?,1,?)",
            ("default", token_hash(key), key[:12] + "…", box.encrypt(key), int(time.time())),
        )
        settings.initial_api_key_path.write_text(
            "AIAll API 初始 API Key\n" + key + "\n\n请在「设置」页面重新生成后删除本文件。\n",
            encoding="utf-8",
        )
        try:
            os.chmod(settings.initial_api_key_path, 0o600)
        except OSError:
            pass
        log.warning("已生成初始 API Key，保存在 %s", settings.initial_api_key_path)

    return box


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
