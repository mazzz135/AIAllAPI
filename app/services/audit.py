from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import BASE_DIR
from ..db import Database

log = logging.getLogger("aiall.audit")

_ACCOUNT_PATH = re.compile(r"^/admin/accounts/(\d+)/([a-z0-9_-]+)$")
_PLATFORM_ACCOUNT_PATH = re.compile(r"^/admin/platforms/accounts/(\d+)/([a-z0-9_-]+)$")
_PLATFORM_PATH = re.compile(r"^/admin/platforms/([a-z0-9_-]+)/([a-z0-9_-]+)$")
_VIRTUAL_PATH = re.compile(r"^/admin/virtuals/(\d+)/([a-z0-9_-]+)$")


def _setting(db: Database, keys: tuple[str, ...], default: str = "") -> str:
    for key in keys:
        value = db.get_setting(key, "")
        if value not in (None, ""):
            return str(value)
    return default


def _setting_bool(db: Database, keys: tuple[str, ...], default: bool = False) -> bool:
    value = _setting(db, keys, "1" if default else "0").strip().lower()
    return value in {"1", "true", "yes", "on", "y"}


def fingerprint(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class AdminAuditService:
    """管理动作 JSONL 审计；每条记录独立开-写-关，失败不阻断请求。"""

    def __init__(self, db: Database):
        self.db = db
        self.path = self._resolve_path()
        if self.enabled:
            self._validate()

    @property
    def enabled(self) -> bool:
        return _setting_bool(self.db, ("admin.audit_enabled", "admin_audit_enabled"), False) and _setting_bool(
            self.db, ("admin.enabled", "admin_enabled"), True
        )

    def _resolve_path(self) -> Path:
        raw = _setting(
            self.db,
            ("admin.audit_file", "admin_audit_file"),
            "./data/admin_audit.log",
        ).strip()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return path.resolve()

    def _validate(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8"):
                pass
        except OSError as exc:
            raise RuntimeError(
                f"管理操作审计已开启，但审计文件不可写: {self.path} ({exc})"
            ) from exc

    def record(
        self,
        *,
        action: str,
        object_name: str,
        status_code: int,
        ip: str,
        api_key_fp: str,
        reason: str = "",
        method: str = "",
        path: str = "",
        ok: bool | None = None,
    ) -> None:
        if not self.enabled:
            return
        now = time.time()
        entry = {
            "ts": int(now),
            "time": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
            "action": action,
            "object": object_name,
            "status": int(status_code),
            "ok": int(status_code) < 400 if ok is None else bool(ok),
            "ip": ip,
            "api_key_fp": api_key_fp,
            "reason": reason,
            "method": method,
            "path": path,
        }
        try:
            path = self._resolve_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError as exc:
            log.warning("管理审计写入失败（不影响请求）: %s", exc)

    @staticmethod
    def action_for(path: str, method: str) -> tuple[str, str]:
        """返回 (动作名, 对象)；对象优先由端点通过 request.state 覆盖。"""
        clean = path.rstrip("/") or "/admin"
        match = _PLATFORM_ACCOUNT_PATH.match(clean)
        if match:
            return f"platform_account.{match.group(2)}", match.group(1)
        match = _ACCOUNT_PATH.match(clean)
        if match:
            return f"account.{match.group(2)}", match.group(1)
        match = _PLATFORM_PATH.match(clean)
        if match:
            return f"platform.{match.group(2)}", match.group(1)
        match = _VIRTUAL_PATH.match(clean)
        if match:
            return f"virtual.{match.group(2)}", match.group(1)
        if clean == "/admin/checkins/run-all":
            return "task.run_all", "checkins"
        if clean == "/admin/checkins/settings":
            return "task.settings", "checkins"
        if clean == "/admin/settings/password":
            return "admin.password", "admin"
        if clean.startswith("/admin/settings"):
            return "settings.update", "settings"
        if clean == "/admin/login":
            return "admin.login", "admin"
        if clean == "/admin/logout":
            return "admin.logout", "admin"
        suffix = clean.removeprefix("/admin/").replace("/", ".")
        return f"admin.{method.lower()}.{suffix or 'root'}", clean


def request_fingerprint(request: Any) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return fingerprint(auth[7:].strip())
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        return fingerprint(api_key)
    token = request.cookies.get("aiall_session", "") or request.cookies.get("traeapi_session", "")
    return fingerprint(token)
