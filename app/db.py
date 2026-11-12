from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    admin_id INTEGER NOT NULL,
    csrf TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    FOREIGN KEY(admin_id) REFERENCES admins(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    key_encrypted TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    uid TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL DEFAULT 'personal',
    auth_type TEXT NOT NULL DEFAULT 'refresh',
    access_token TEXT NOT NULL DEFAULT '',
    refresh_token TEXT NOT NULL DEFAULT '',
    session_cookie TEXT NOT NULL DEFAULT '',
    token_expires_at INTEGER NOT NULL DEFAULT 0,
    device_id TEXT NOT NULL DEFAULT '',
    machine_id TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    oauth_host TEXT NOT NULL DEFAULT '',
    agent_host TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '',
    device_private_key TEXT NOT NULL DEFAULT '',
    device_info_json TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    auto_checkin INTEGER NOT NULL DEFAULT 1,
    credits REAL NOT NULL DEFAULT -1,
    credits_used REAL NOT NULL DEFAULT -1,
    credits_total REAL NOT NULL DEFAULT -1,
    credits_expiring REAL NOT NULL DEFAULT 0,
    credit_packs_json TEXT NOT NULL DEFAULT '[]',
    credits_updated_at INTEGER NOT NULL DEFAULT 0,
    pay_identity INTEGER NOT NULL DEFAULT -1,
    last_error TEXT NOT NULL DEFAULT '',
    last_used_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(uid, account_type)
);

CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    status TEXT NOT NULL,
    credits REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE(account_id, day),
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    api_key_id INTEGER,
    api_key_name TEXT NOT NULL DEFAULT '',
    account_id INTEGER,
    account_name TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    stream INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    credits_before REAL NOT NULL DEFAULT -1,
    credits_after REAL NOT NULL DEFAULT -1,
    credits_delta REAL NOT NULL DEFAULT 0,
    credits_estimated REAL NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ttft_ms INTEGER NOT NULL DEFAULT 0,
    completion_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_logs(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_logs(model, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_checkins_day ON checkins(day, status);

CREATE TABLE IF NOT EXISTS platform_checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    account_name TEXT NOT NULL DEFAULT '',
    day TEXT NOT NULL,
    status TEXT NOT NULL,
    credits REAL NOT NULL DEFAULT 0,
    valid_until INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE(provider_id, account_id, day)
);
CREATE INDEX IF NOT EXISTS idx_platform_checkins_day ON platform_checkins(day, status);

CREATE TABLE IF NOT EXISTS model_cooldowns (
    provider_id TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    until INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(provider_id, account_id, model)
);

CREATE TABLE IF NOT EXISTS model_costs (
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_per_1k REAL NOT NULL DEFAULT 0,
    samples INTEGER NOT NULL DEFAULT 0,
    last_credits REAL NOT NULL DEFAULT 0,
    last_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(provider_id, model)
);

CREATE TABLE IF NOT EXISTS account_costs (
    account_id INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    cost_per_1k REAL NOT NULL DEFAULT 0,
    samples INTEGER NOT NULL DEFAULT 0,
    last_credits REAL NOT NULL DEFAULT 0,
    last_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, model)
);
CREATE INDEX IF NOT EXISTS idx_account_costs_model ON account_costs(provider_id, model);

CREATE TABLE IF NOT EXISTS cost_explore (
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    last_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(provider_id, model, day)
);

CREATE TABLE IF NOT EXISTS session_bindings (
    session_key TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    task TEXT PRIMARY KEY,
    started_at INTEGER NOT NULL DEFAULT 0,
    finished_at INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    all_failed INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '[]'
);


CREATE TABLE IF NOT EXISTS login_sessions (
    trace_id TEXT PRIMARY KEY,
    verifier TEXT NOT NULL,
    challenge TEXT NOT NULL,
    device_info_json TEXT NOT NULL,
    private_key TEXT NOT NULL,
    callback_url TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT 'cn',
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    protocol TEXT NOT NULL,
    base_url TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    models_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    weight INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL DEFAULT '',
    last_synced_at INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    credential_json TEXT NOT NULL DEFAULT '',
    auth_type TEXT NOT NULL DEFAULT 'api_key',
    uid TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    expires_at INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ready',
    weight INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    credits REAL NOT NULL DEFAULT -1,
    credits_used REAL NOT NULL DEFAULT -1,
    credits_total REAL NOT NULL DEFAULT -1,
    credits_expiring REAL NOT NULL DEFAULT 0,
    credit_packs_json TEXT NOT NULL DEFAULT '[]',
    credits_updated_at INTEGER NOT NULL DEFAULT 0,
    last_checkin_at INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_used_at INTEGER NOT NULL DEFAULT 0,
    extra_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(provider_id) REFERENCES providers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS platform_logins (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '',
    auth_url TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS virtual_models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS virtual_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    virtual_id INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(virtual_id) REFERENCES virtual_models(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_provider_accounts ON provider_accounts(provider_id);
CREATE INDEX IF NOT EXISTS idx_virtual_members ON virtual_members(virtual_id);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._ensure_column("accounts", "device_private_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("accounts", "device_info_json", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("accounts", "region", "TEXT NOT NULL DEFAULT 'cn'")
            self._ensure_column("accounts", "credits_used", "REAL NOT NULL DEFAULT -1")
            self._ensure_column("accounts", "credits_total", "REAL NOT NULL DEFAULT -1")
            self._ensure_column("accounts", "credits_expiring", "REAL NOT NULL DEFAULT 0")
            self._ensure_column("accounts", "credit_packs_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column("accounts", "manual_disabled", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("accounts", "manual_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("login_sessions", "region", "TEXT NOT NULL DEFAULT 'cn'")
            self._ensure_column("login_sessions", "state", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("platform_logins", "expires_at", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("usage_logs", "provider_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("usage_logs", "virtual_model", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("usage_logs", "cache_read_tokens", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("usage_logs", "cache_write_tokens", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("usage_logs", "cache_miss_tokens", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("usage_logs", "ttft_ms", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("usage_logs", "completion_ms", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("platform_checkins", "valid_until", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("task_runs", "source", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("task_runs", "details_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column("providers", "region", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("providers", "config_json", "TEXT NOT NULL DEFAULT '{}'")
            # 历史 models 表已废弃（模型实际存于 providers.models_json）：
            # 先把仍然存在的映射迁成虚拟模型（保证老客户端别名继续可用），再删除旧表
            legacy = self.query_one(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='models'"
            )
            if legacy:
                pending = 0
                now_ts = int(time.time())
                try:
                    legacy_rows = self.query(
                        "SELECT name, provider_id, upstream_model, enabled FROM models"
                    )
                except Exception:
                    legacy_rows = []
                for row in legacy_rows:
                    name = str(row["name"] or "").strip()
                    provider_id = str(row["provider_id"] or "").strip()
                    upstream = str(row["upstream_model"] or "").strip() or name
                    if not name or not provider_id:
                        continue
                    if self.query_one(
                        "SELECT 1 FROM providers WHERE id=?", (provider_id,)
                    ) is None:
                        pending += 1
                        continue
                    if self.query_one(
                        "SELECT 1 FROM virtual_models WHERE lower(name)=lower(?)", (name,)
                    ) is None:
                        vid = self.execute(
                            "INSERT INTO virtual_models(name, display_name, enabled, created_at, updated_at) "
                            "VALUES(?,?,?,?,?)",
                            (name, name, int(row["enabled"] or 1), now_ts, now_ts),
                        )
                        self.execute(
                            "INSERT INTO virtual_members(virtual_id, provider_id, model, weight, enabled) "
                            "VALUES(?,?,?,?,1)",
                            (vid, provider_id, upstream, 1),
                        )
                if pending == 0:
                    self._conn.execute("DROP TABLE models")
            for column, definition in (
                ("credential_json", "TEXT NOT NULL DEFAULT ''"),
                ("auth_type", "TEXT NOT NULL DEFAULT 'api_key'"),
                ("uid", "TEXT NOT NULL DEFAULT ''"),
                ("region", "TEXT NOT NULL DEFAULT ''"),
                ("expires_at", "INTEGER NOT NULL DEFAULT 0"),
                ("status", "TEXT NOT NULL DEFAULT 'ready'"),
                ("credits", "REAL NOT NULL DEFAULT -1"),
                ("credits_used", "REAL NOT NULL DEFAULT -1"),
                ("credits_total", "REAL NOT NULL DEFAULT -1"),
                ("credits_updated_at", "INTEGER NOT NULL DEFAULT 0"),
                ("last_checkin_at", "INTEGER NOT NULL DEFAULT 0"),
                ("extra_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("updated_at", "INTEGER NOT NULL DEFAULT 0"),
                ("credits_expiring", "REAL NOT NULL DEFAULT 0"),
                ("credit_packs_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("cooldown_until", "INTEGER NOT NULL DEFAULT 0"),
                ("cooldown_kind", "TEXT NOT NULL DEFAULT ''"),
                ("fail_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("soft_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("in_flight", "INTEGER NOT NULL DEFAULT 0"),
                ("last_picked_at", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_disabled", "INTEGER NOT NULL DEFAULT 0"),
                ("manual_reason", "TEXT NOT NULL DEFAULT ''"),
            ):
                self._ensure_column("provider_accounts", column, definition)
            self._conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        return row[0]

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), now),
            )
            self._conn.commit()

    def get_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.query("SELECT key, value FROM settings")}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
