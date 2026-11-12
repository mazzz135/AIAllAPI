from __future__ import annotations

import inspect
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from ..config import Settings
from ..credit_packs import dumps as packs_dumps, expiring as packs_expiring, loads as packs_loads, normalize_packs
from ..db import Database
from ..providers.base import ProviderError
from .providers import ProviderService


def _now() -> int:
    return int(time.time())


def _today() -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


class PlatformService:
    def __init__(self, settings: Settings, db: Database, providers: ProviderService):
        self.settings = settings
        self.db = db
        self.providers = providers

    # ------------------------------------------------------------------ login
    async def start_login(self, provider_id: str) -> dict[str, Any]:
        provider = self.providers.get_provider(provider_id)
        if not provider:
            raise ValueError("平台不存在")
        adapter = self.providers.adapter(provider)
        if adapter is None or not hasattr(adapter, "start_login"):
            raise ValueError("该平台不支持网页授权，请使用凭据导入")
        result = adapter.start_login(provider)
        if inspect.isawaitable(result):
            result = await result
        login_id = secrets.token_hex(12)
        ttl = 1800 if provider["kind"] == "lobster" else 900
        now = _now()
        self.db.execute(
            "INSERT INTO platform_logins(id, provider_id, state, auth_url, extra_json, created_at, expires_at, status, message) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                login_id,
                provider_id,
                str(result.get("state") or ""),
                str(result.get("auth_url") or ""),
                json.dumps(result, ensure_ascii=False),
                now,
                now + ttl,
                "pending",
                "等待浏览器授权",
            ),
        )
        return {"id": login_id, **result}

    def cleanup_expired(self) -> int:
        now = _now()
        self.db.execute(
            "UPDATE platform_logins SET expires_at=created_at+900 WHERE status='pending' AND expires_at=0"
        )
        self.db.execute(
            "UPDATE platform_logins SET expires_at=created_at+900 WHERE status='pending' AND expires_at=0"
        )
        self.db.execute(
            "UPDATE platform_logins SET status='expired', message='登录会话已过期，请重新发起' "
            "WHERE status='pending' AND expires_at>0 AND expires_at<=?",
            (now,),
        )
        self.db.execute("DELETE FROM platform_logins WHERE created_at < ?", (now - 7 * 86400,))
        return 0

    def get_login(self, login_id: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM platform_logins WHERE id=?", (login_id,))
        if not row:
            return None
        item = dict(row)
        if item["status"] == "pending" and int(item.get("expires_at") or 0) and int(item["expires_at"]) <= _now():
            self.db.execute(
                "UPDATE platform_logins SET status='expired', message='登录会话已过期，请重新发起' WHERE id=?",
                (login_id,),
            )
            item["status"] = "expired"
            item["message"] = "登录会话已过期，请重新发起"
        try:
            item["extra"] = json.loads(item.get("extra_json") or "{}")
        except Exception:
            item["extra"] = {}
        return item

    async def poll_login(self, login_id: str) -> dict[str, Any]:
        login = self.get_login(login_id)
        if not login:
            raise ValueError("登录会话不存在或已过期")
        if login["status"] == "success":
            return {"status": "success", "message": login["message"], "account_id": login["extra"].get("account_id")}
        if login["status"] == "expired":
            return {"status": "error", "message": "登录会话已过期，请重新发起登录"}
        provider = self.providers.get_provider(login["provider_id"])
        if not provider:
            raise ValueError("平台不存在")
        adapter = self.providers.adapter(provider)
        result = await adapter.poll_login(provider, login["state"], login.get("extra") or {})
        if result.get("pending"):
            return {"status": "pending", "message": result.get("message") or "等待浏览器授权"}
        cred = result["credential"]
        account_id = self._save_credential_account(provider, cred)
        self.db.execute(
            "UPDATE platform_logins SET status='success', message=?, extra_json=? WHERE id=?",
            ("登录成功", json.dumps({**login["extra"], "account_id": account_id}, ensure_ascii=False), login_id),
        )
        return {"status": "success", "message": "登录成功", "account_id": account_id}

    async def complete_callback(self, login_id: str, callback_url: str) -> dict[str, Any]:
        login = self.get_login(login_id)
        if not login:
            raise ValueError("登录会话不存在或已过期")
        if login["status"] == "expired":
            raise ValueError("登录会话已过期，请重新发起登录")
        provider = self.providers.get_provider(login["provider_id"])
        if not provider:
            raise ValueError("平台不存在")
        query = parse_qs(urlparse(callback_url.strip()).query)
        code = (query.get("code") or query.get("authCode") or [""])[0]
        if not code:
            raise ValueError("回调链接中没有 code，请粘贴完整回调地址")
        state = (query.get("state") or [""])[0]
        if login["state"] and state and state != login["state"]:
            raise ValueError("state 不匹配，请重新开始授权")
        adapter = self.providers.adapter(provider)
        cred = await adapter.exchange(provider, code, login["extra"])
        account_id = self._save_credential_account(provider, cred)
        self.db.execute(
            "UPDATE platform_logins SET status='success', message=?, extra_json=? WHERE id=?",
            ("登录成功", json.dumps({**login["extra"], "account_id": account_id}, ensure_ascii=False), login_id),
        )
        return {"status": "success", "message": "登录成功", "account_id": account_id}

    def _save_credential_account(self, provider: dict[str, Any], cred: dict[str, Any]) -> int:
        uid = str(cred.get("uid") or "")
        name = str(cred.get("nickname") or "").strip() or (
            f"{provider['name']}-{uid[-6:]}" if uid else provider["name"]
        )
        existing = None
        if uid:
            existing = self.db.query_one(
                "SELECT id FROM provider_accounts WHERE provider_id=? AND uid=?", (provider["id"], uid)
            )
        if existing:
            self.providers.update_credential(int(existing["id"]), cred, int(cred.get("expires_at") or 0))
            return int(existing["id"])
        return self.providers.add_credential_account(
            provider["id"],
            name,
            cred,
            uid=uid,
            region=str(cred.get("region") or provider.get("region") or ""),
            expires_at=int(cred.get("expires_at") or 0),
            extra={"nickname": cred.get("nickname") or ""},
        )

    # ------------------------------------------------------------------ manual
    def add_manual(self, provider_id: str, name: str, text: str, uid: str = "") -> int:
        provider = self.providers.get_provider(provider_id)
        if not provider:
            raise ValueError("平台不存在")
        raw = (text or "").strip()
        if not raw:
            raise ValueError("请粘贴凭据")
        cred: dict[str, Any] = {}
        if raw.startswith("{"):
            try:
                data = json.loads(raw)
            except Exception as exc:
                raise ValueError(f"JSON 解析失败: {exc}") from exc
            if isinstance(data.get("auth"), dict):
                auth = data["auth"]
                account = data.get("account") or {}
                cred = {
                    "access_token": auth.get("accessToken") or auth.get("access_token") or "",
                    "refresh_token": auth.get("refreshToken") or auth.get("refresh_token") or "",
                    "expires_at": int(auth.get("expiresAt") or auth.get("expires_at") or 0),
                    "uid": account.get("uid") or data.get("uid") or "",
                    "user_id": account.get("userId") or data.get("userId") or "",
                    "nickname": account.get("nickname") or data.get("nickname") or "",
                    "uuid": auth.get("uuid") or data.get("uuid") or "",
                    "first_keyfrom": auth.get("firstKeyfrom") or data.get("firstKeyfrom") or "",
                    "security_oauth_token": auth.get("security_oauth_token") or "",
                }
            else:
                cred = data
        else:
            cred = {"access_token": raw}
        if provider["kind"] == "qoder":
            cred.setdefault("uid", uid)
            cred.setdefault("access_token", cred.get("security_oauth_token") or "")
            if not cred.get("uid"):
                raise ValueError("Qoder 需要 uid；请在凭据中提供 uid 或在下方单独填写")
        if provider["kind"] in ("workbuddy", "codebuddy"):
            cred.setdefault("uid", uid)
            cred.setdefault("domain", "workbuddy.ai" if provider.get("region") == "global" else "codebuddy.cn")
            if not cred.get("uid"):
                raise ValueError("WorkBuddy / CodeBuddy 需要 uid")
        if provider["kind"] == "lobster":
            cred.setdefault("uid", uid)
            if not cred.get("uuid"):
                cred["uuid"] = secrets.token_hex(16)
            if not cred.get("first_keyfrom"):
                cred["first_keyfrom"] = str(int(time.time() * 1000))
        expires = int(cred.get("expires_at") or 0)
        if expires > 1_000_000_000_000:
            expires //= 1000
        cred["expires_at"] = expires
        return self._save_credential_account(provider, cred)

    # ------------------------------------------------------------------ runtime
    async def refresh_quota(self, account_id: int) -> dict[str, Any]:
        account = self.providers.get_account(account_id)
        if not account:
            raise ValueError("账号不存在")
        provider = self.providers.get_provider(account["provider_id"])
        adapter = self.providers.adapter(provider)
        if adapter is None or not hasattr(adapter, "quota"):
            raise ValueError("该平台暂不支持额度查询")
        try:
            result = await adapter.quota(provider, account)
        except Exception as exc:
            # 查询失败时保留上一次已知的积分，避免界面被 -1 覆盖
            self.db.execute(
                "UPDATE provider_accounts SET last_error=?, updated_at=? WHERE id=?",
                (str(exc)[:1000], _now(), account_id),
            )
            raise
        credits = result.get("credits")
        used = result.get("used")
        total = result.get("total")
        if credits is None or float(credits) < 0:
            self.db.execute(
                "UPDATE provider_accounts SET last_error=?, updated_at=? WHERE id=?",
                (str(result.get("message") or "额度接口不可用")[:1000], _now(), account_id),
            )
            return result
        try:
            days = max(0, int(float(self.db.get_setting("pool_expiring_soon_days", "7") or 7)))
        except Exception:
            days = 7
        packs = packs_loads(result.get("packs"))
        if not packs and (float(credits) or float(used or 0) or float(total or 0)):
            from ..credit_packs import make_pack

            packs = [
                make_pack(
                    "总积分",
                    credits,
                    total if total is not None else 0,
                    used if used is not None else 0,
                    0,
                    f"{provider.get('kind', 'platform')}-summary",
                )
            ]
        expiring = packs_expiring(packs, days)
        self.db.execute(
            "UPDATE provider_accounts SET credits=?, credits_used=?, credits_total=?, credits_expiring=?, "
            "credit_packs_json=?, credits_updated_at=?, last_error='', updated_at=? WHERE id=?",
            (
                float(credits),
                float(used) if used is not None else -1,
                float(total) if total is not None else -1,
                expiring,
                packs_dumps(packs),
                _now(),
                _now(),
                account_id,
            ),
        )
        return result

    async def checkin_all(self, kinds: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        supported = kinds or ("workbuddy", "codebuddy", "lobster", "qoder")
        placeholders = ",".join("?" for _ in supported)
        rows = self.db.query(
            "SELECT pa.id, pa.name, p.name AS platform_name FROM provider_accounts pa "
            "JOIN providers p ON p.id = pa.provider_id "
            f"WHERE pa.enabled=1 AND p.enabled=1 AND p.kind IN ({placeholders})",
            supported,
        )
        for row in rows:
            try:
                result = await self.checkin(int(row["id"]))
                results.append({"account": f"{row['platform_name']}/{row['name']}", **result})
            except Exception as exc:
                results.append(
                    {"account": f"{row['platform_name']}/{row['name']}", "status": "error", "message": str(exc)}
                )
        return results

    async def checkin(self, account_id: int) -> dict[str, Any]:
        account = self.providers.get_account(account_id)
        if not account:
            raise ValueError("账号不存在")
        provider = self.providers.get_provider(account["provider_id"])
        adapter = self.providers.adapter(provider)
        if adapter is None or not hasattr(adapter, "checkin"):
            raise ValueError("该平台暂不支持签到")
        try:
            result = await adapter.checkin(provider, account)
        except ProviderError as exc:
            self.db.execute(
                "UPDATE provider_accounts SET last_error=?, updated_at=? WHERE id=?",
                (str(exc)[:1000], _now(), account_id),
            )
            raise
        self.db.execute(
            "UPDATE provider_accounts SET last_checkin_at=?, status=?, last_error='', updated_at=? WHERE id=?",
            (_now(), "ready", _now(), account_id),
        )
        valid_until = int(result.get("valid_until") or 0)
        self.db.execute(
            "INSERT INTO platform_checkins(provider_id, account_id, account_name, day, status, credits, valid_until, message, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(provider_id, account_id, day) DO UPDATE SET "
            "status=excluded.status, credits=excluded.credits, valid_until=excluded.valid_until, "
            "message=excluded.message, created_at=excluded.created_at",
            (
                provider["id"],
                account_id,
                str(account.get("name") or ""),
                _today(),
                str(result.get("status") or "unknown"),
                float(result.get("credits") or 0),
                valid_until,
                str(result.get("message") or "")[:1000],
                _now(),
            ),
        )
        checkin_packs = packs_loads(result.get("packs"))
        if checkin_packs:
            current = self.providers.get_account(account_id) or {}
            merged = normalize_packs((current.get("packs") or []) + checkin_packs)[:100]
            try:
                days = max(0, int(float(self.db.get_setting("pool_expiring_soon_days", "7") or 7)))
            except Exception:
                days = 7
            self.db.execute(
                "UPDATE provider_accounts SET credit_packs_json=?, credits_expiring=?, updated_at=? WHERE id=?",
                (packs_dumps(merged), packs_expiring(merged, days), _now(), account_id),
            )
        return result

    def checkin_history(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT c.*, p.name AS provider_name FROM platform_checkins c "
            "LEFT JOIN providers p ON p.id = c.provider_id "
            "ORDER BY c.created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def checkin_streak(self, today: str = "") -> int:
        """连续签到天数：今天未签到时从昨天开始回溯，避免早上显示 0。"""
        rows = self.db.query(
            "SELECT day FROM ("
            "SELECT day FROM checkins WHERE status IN ('success','already') "
            "UNION "
            "SELECT day FROM platform_checkins WHERE status IN ('success','already')"
            ") ORDER BY day DESC"
        )
        days = {str(row["day"]) for row in rows if row["day"]}
        cursor = datetime.strptime(today or _today(), "%Y-%m-%d").date()
        if cursor.isoformat() not in days:
            cursor -= timedelta(days=1)
        count = 0
        while cursor.isoformat() in days:
            count += 1
            cursor -= timedelta(days=1)
        return count
