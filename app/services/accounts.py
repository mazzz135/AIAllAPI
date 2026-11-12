from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import secrets
import time
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from ..config import Settings
from ..credit_packs import dumps as packs_dumps, expiring as packs_expiring, loads as packs_loads
from ..db import Database
from ..security import SecretBox
from ..upstream.client import TraeClient, TraeError
from ..upstream.login import generate_device, make_pkce

log = logging.getLogger("aiall.accounts")

MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")


def _region_hosts(region: str) -> tuple[str, str]:
    if (region or "cn").lower() in ("global", "intl", "international"):
        return "https://trae-api-sg.mchost.guru", "https://www.trae.ai"
    return "https://trae-api-cn.mchost.guru", "https://api.trae.com.cn"


def _console_host(region: str) -> str:
    return "https://www.trae.ai" if (region or "cn").lower() in ("global", "intl", "international") else "https://www.trae.cn"


def _session_hosts(region: str) -> list[str]:
    if (region or "cn").lower() in ("global", "intl", "international"):
        return ["https://api.trae.ai", "https://www.trae.ai"]
    return ["https://api.trae.cn", "https://www.trae.cn"]


def _now() -> int:
    return int(time.time())


def _random_digits(n: int = 16) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(n))


def _row_account(row, box: SecretBox) -> dict[str, Any]:
    account = dict(row)
    account["access_token"] = box.decrypt(account.get("access_token", ""))
    account["refresh_token"] = box.decrypt(account.get("refresh_token", ""))
    account["session_cookie"] = box.decrypt(account.get("session_cookie", ""))
    account["device_private_key"] = box.decrypt(account.get("device_private_key", ""))
    account["packs"] = packs_loads(account.get("credit_packs_json"))
    return account


def parse_callback_fields(callback_url: str) -> dict[str, str]:
    parsed = urlparse(callback_url.strip())
    query = parse_qs(parsed.query, keep_blank_values=True)
    return {key: values[0] for key, values in query.items() if values}


def extract_auth_code(fields: dict[str, str]) -> str:
    raw = fields.get("authCodeInfo") or fields.get("auth_code_info") or ""
    if raw:
        try:
            data = json.loads(raw)
            code = data.get("AuthCode") or data.get("authCode") or data.get("code")
            if code:
                return str(code)
        except Exception:
            pass
    for key in ("AuthCode", "authCode", "code"):
        if fields.get(key):
            return str(fields[key])
    return ""


def deep_code(data: Any) -> int:
    if isinstance(data, dict):
        for key in ("code", "status_code", "error_code"):
            if key in data:
                try:
                    return int(data[key])
                except (TypeError, ValueError):
                    pass
        for value in data.values():
            code = deep_code(value)
            if code:
                return code
    elif isinstance(data, list):
        for value in data:
            code = deep_code(value)
            if code:
                return code
    return 0


class AccountService:
    def __init__(self, settings: Settings, db: Database, box: SecretBox, client: TraeClient):
        self.settings = settings
        self.db = db
        self.box = box
        self.client = client
        self._token_locks: dict[int, asyncio.Lock] = {}
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._catalog: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._catalog_lock = asyncio.Lock()

    # ------------------------------------------------------------------ storage
    def _encrypt(self, value: str) -> str:
        return self.box.encrypt(value)

    def list_accounts(self, include_disabled: bool = True, region: str = "") -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if not include_disabled:
            clauses.append("enabled=1")
        if region:
            clauses.append("COALESCE(NULLIF(region,''),'cn')=?")
            params.append(region)
        sql = "SELECT * FROM accounts"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY enabled DESC, credits DESC, id ASC"
        return [_row_account(row, self.box) for row in self.db.query(sql, params)]

    def get_account(self, account_id: int) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM accounts WHERE id=?", (account_id,))
        return _row_account(row, self.box) if row else None

    def get_by_uid(self, uid: str, account_type: str = "personal") -> Optional[dict[str, Any]]:
        row = self.db.query_one(
            "SELECT * FROM accounts WHERE uid=? AND account_type=?", (uid, account_type)
        )
        return _row_account(row, self.box) if row else None

    def _upsert(
        self,
        *,
        uid: str,
        name: str,
        auth_type: str,
        access_token: str,
        refresh_token: str,
        session_cookie: str,
        expires_at: int,
        raw: dict[str, Any],
        device_id: str,
        machine_id: str,
        account_type: str = "personal",
        device_private_key: str = "",
        device_info_json: str = "",
        region: str = "",
    ) -> dict[str, Any]:
        existing = self.get_by_uid(uid, account_type)
        now = _now()
        if existing:
            self.db.execute(
                """
                UPDATE accounts SET name=?, auth_type=?, access_token=?, refresh_token=?,
                    session_cookie=?, token_expires_at=?, raw_json=?, device_id=?, machine_id=?,
                    device_private_key=?, device_info_json=?, region=?,
                    enabled=1, last_error='', updated_at=?
                WHERE id=?
                """,
                (
                    name or existing["name"],
                    auth_type,
                    self._encrypt(access_token),
                    self._encrypt(refresh_token),
                    self._encrypt(session_cookie),
                    expires_at,
                    json.dumps(raw, ensure_ascii=False),
                    device_id or existing.get("device_id", ""),
                    machine_id or existing.get("machine_id", ""),
                    self._encrypt(device_private_key),
                    device_info_json,
                    region or existing.get("region", "cn"),
                    now,
                    existing["id"],
                ),
            )
            account_id = existing["id"]
        else:
            account_id = self.db.execute(
                """
                INSERT INTO accounts(
                    name, uid, account_type, auth_type, access_token, refresh_token, session_cookie,
                    token_expires_at, device_id, machine_id, client_id, host, oauth_host, agent_host,
                    raw_json, device_private_key, device_info_json, region, enabled, auto_checkin, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    name or f"Trae-{uid[-4:]}",
                    uid,
                    account_type,
                    auth_type,
                    self._encrypt(access_token),
                    self._encrypt(refresh_token),
                    self._encrypt(session_cookie),
                    expires_at,
                    device_id,
                    machine_id,
                    self.settings.client_id,
                    _region_hosts(region)[0],
                    _region_hosts(region)[1],
                    _region_hosts(region)[0],
                    json.dumps(raw, ensure_ascii=False),
                    self._encrypt(device_private_key),
                    device_info_json,
                    region or "cn",
                    1,
                    1,
                    now,
                    now,
                ),
            )
        self._catalog.pop(account_id, None)
        account = self.get_account(account_id)
        assert account is not None
        return account

    # ------------------------------------------------------------------ login
    async def add_from_callback(self, callback_url: str, region: str = "") -> dict[str, Any]:
        fields = parse_callback_fields(callback_url)
        for key in ("refreshToken", "refresh_token"):
            if fields.get(key):
                return await self.add_from_refresh(fields[key])
        auth_code = extract_auth_code(fields)
        if not auth_code:
            raise ValueError("回调链接中没有 refreshToken 或 AuthCode，请复制登录后跳转的完整地址")
        trace_id = (
            fields.get("login_trace_id")
            or fields.get("loginTraceID")
            or fields.get("trace_id")
            or ""
        )
        state = str(fields.get("state") or fields.get("login_state") or "")
        session = None
        if state:
            session = self.db.query_one(
                "SELECT * FROM login_sessions WHERE state=? AND status='pending'", (state,)
            )
            if session and trace_id and str(session["trace_id"]) != trace_id:
                raise ValueError("登录会话校验失败（state 与 trace 不匹配），请重新生成登录链接")
        elif trace_id:
            session = self.db.query_one(
                "SELECT * FROM login_sessions WHERE trace_id=? AND status='pending'", (trace_id,)
            )
        else:
            pending = self.db.query(
                "SELECT * FROM login_sessions WHERE status='pending' AND created_at>=? "
                "ORDER BY created_at DESC LIMIT 2",
                (_now() - 1800,),
            )
            if len(pending) != 1:
                raise ValueError("回调链接缺少 state，请使用管理台重新生成的登录链接")
            session = pending[0]
        if not session:
            raise ValueError("登录会话不存在或已过期，请重新生成登录链接")
        device_info = json.loads(session["device_info_json"] or "{}")
        region = region or str(session["region"] or "cn")
        oauth_host = _region_hosts(region)[1]
        exchanged = await self.client.exchange_code(
            auth_code,
            session["verifier"],
            device_info,
            ide_version=self.settings.ide_version,
            oauth_host=oauth_host,
            client_id=self.settings.client_id,
        )
        info = await self.client.get_user_info(exchanged["access_token"], oauth_host=oauth_host)
        uid = str(info.get("UserID") or "")
        if not uid:
            raise ValueError("无法获取 Trae UserID")
        name = str(info.get("ScreenName") or info.get("Username") or f"Trae-{uid[-4:]}")
        account = self._upsert(
            uid=uid,
            name=name,
            auth_type="refresh",
            access_token=exchanged["access_token"],
            refresh_token=exchanged.get("refresh_token") or "",
            session_cookie="",
            expires_at=int(exchanged.get("expires_at") or 0),
            raw=info,
            device_id=str(device_info.get("DeviceID") or ""),
            machine_id=str(device_info.get("MachineID") or ""),
            device_private_key=self.box.decrypt(session["private_key"]),
            device_info_json=json.dumps(device_info, ensure_ascii=False),
            region=region,
        )
        self.db.execute(
            "UPDATE login_sessions SET status='ok', message='登录成功', state='' WHERE trace_id=?",
            (session["trace_id"],),
        )
        return account

    async def add_from_refresh(self, refresh_token: str, uid: str = "", region: str = "cn") -> dict[str, Any]:
        refresh_token = refresh_token.strip()
        if not refresh_token:
            raise ValueError("refreshToken 不能为空")
        oauth_host = _region_hosts(region)[1]
        exchanged = await self.client.exchange_token(refresh_token, uid, oauth_host=oauth_host)
        info = await self.client.get_user_info(exchanged["access_token"], oauth_host=oauth_host)
        uid = str(info.get("UserID") or uid or "")
        if not uid:
            raise ValueError("无法获取 Trae UserID")
        name = str(info.get("ScreenName") or info.get("Username") or f"Trae-{uid[-4:]}")
        return self._upsert(
            uid=uid,
            name=name,
            auth_type="refresh",
            access_token=exchanged["access_token"],
            refresh_token=exchanged.get("refresh_token") or refresh_token,
            session_cookie="",
            expires_at=int(exchanged.get("expires_at") or 0),
            raw=info,
            device_id=_random_digits(15),
            machine_id=_random_digits(16) + _random_digits(16) + _random_digits(16) + _random_digits(16),
            region=region,
        )

    async def _session_token(
        self,
        session_cookie: str,
        region: str,
        console_host: str,
    ) -> tuple[dict[str, Any], str]:
        last_error: TraeError | None = None
        for host in _session_hosts(region):
            try:
                token = await self.client.get_token_from_session(
                    session_cookie,
                    oauth_host=host,
                    console_host=console_host,
                )
                return token, host
            except TraeError as exc:
                last_error = exc
                if exc.http_status in (401, 403, 404):
                    continue
                raise
        if last_error:
            raise last_error
        raise TraeError("网页会话换取 token 失败", retryable=False)

    async def add_from_session(self, session_cookie: str, region: str = "cn") -> dict[str, Any]:
        session_cookie = session_cookie.strip()
        if ";" in session_cookie:
            for part in session_cookie.split(";"):
                part = part.strip()
                if part.lower().startswith("x-cloudide-session="):
                    session_cookie = part.split("=", 1)[1].strip()
                    break
        if session_cookie.lower().startswith("x-cloudide-session="):
            session_cookie = session_cookie.split("=", 1)[1].strip()
        session_cookie = session_cookie.strip().strip('"').strip("'")
        if not session_cookie:
            raise ValueError("会话 Cookie 为空")
        console_host = _console_host(region)
        token, token_host = await self._session_token(session_cookie, region, console_host)
        info = await self.client.get_user_info(token["access_token"], oauth_host=token_host)
        uid = str(info.get("UserID") or "")
        if not uid:
            raise ValueError("无法获取 Trae UserID")
        name = str(info.get("ScreenName") or info.get("Username") or f"Trae-{uid[-4:]}")
        return self._upsert(
            uid=uid,
            name=name,
            auth_type="session",
            access_token=token["access_token"],
            refresh_token="",
            session_cookie=session_cookie,
            expires_at=int(token.get("expires_at") or 0),
            raw=info,
            device_id=_random_digits(16),
            machine_id=_random_digits(16),
            region=region,
        )

    def start_login(self, callback_url: str = "", region: str = "cn") -> tuple[str, str, str, str]:
        # 官方授权页只接受 http://127.0.0.1:<port>/authorize
        if not re.match(r"^http://127\.0\.0\.1:\d+/authorize$", (callback_url or "").strip()):
            callback_url = f"http://127.0.0.1:{self.settings.trae_callback_port}/authorize"
        device_info, private_pem = generate_device(self.settings.ide_version)
        verifier, challenge = make_pkce()
        trace_id = secrets.token_hex(8)
        state = secrets.token_urlsafe(24)
        device_id = str(device_info["DeviceID"])
        machine_id = str(device_info["MachineID"])
        self.db.execute("DELETE FROM login_sessions WHERE created_at < ?", (_now() - 86400,))
        self.db.execute(
            "INSERT INTO login_sessions(trace_id, verifier, challenge, device_info_json, private_key, "
            "callback_url, region, created_at, status, state) VALUES(?,?,?,?,?,?,?,?, 'pending', ?)",
            (
                trace_id,
                verifier,
                challenge,
                json.dumps(device_info, ensure_ascii=False),
                self.box.encrypt(private_pem),
                callback_url,
                region or "cn",
                _now(),
                state,
            ),
        )
        sep = "&" if "?" in callback_url else "?"
        url = self.client.build_login_url(
            f"{callback_url}{sep}state={state}", trace_id, device_id, machine_id, challenge,
            console_host="https://www.trae.ai" if (region or "cn") == "global" else "https://www.trae.cn",
        )
        return url, trace_id, device_id, machine_id

    def build_login_url(self, callback_url: str) -> tuple[str, str, str, str]:
        return self.start_login(callback_url)

    # ------------------------------------------------------------------ token
    def _token_lock(self, account_id: int) -> asyncio.Lock:
        if account_id not in self._token_locks:
            self._token_locks[account_id] = asyncio.Lock()
        return self._token_locks[account_id]

    async def ensure_token(self, account: dict[str, Any], force: bool = False) -> dict[str, Any]:
        account_id = int(account["id"])
        lock = self._token_lock(account_id)
        async with lock:
            fresh = self.get_account(account_id)
            if not fresh:
                raise TraeError("账号不存在", retryable=False)
            now = _now()
            if not force and fresh.get("access_token"):
                expires = int(fresh.get("token_expires_at") or 0)
                if expires == 0 or expires > now + 1800:
                    return fresh
            try:
                if fresh.get("refresh_token"):
                    device_info = {}
                    try:
                        device_info = json.loads(fresh.get("device_info_json") or "{}")
                    except Exception:
                        device_info = {}
                    exchanged = await self.client.exchange_token(
                        fresh["refresh_token"],
                        fresh.get("uid", ""),
                        oauth_host=fresh.get("oauth_host", ""),
                        client_id=fresh.get("client_id", ""),
                        device_info=device_info or None,
                        private_key_pem=fresh.get("device_private_key", ""),
                        ide_version=self.settings.ide_version,
                    )
                    self.db.execute(
                        "UPDATE accounts SET access_token=?, refresh_token=?, token_expires_at=?, "
                        "last_error='', updated_at=? WHERE id=?",
                        (
                            self._encrypt(exchanged["access_token"]),
                            self._encrypt(exchanged.get("refresh_token") or fresh["refresh_token"]),
                            int(exchanged.get("expires_at") or 0),
                            _now(),
                            account_id,
                        ),
                    )
                elif fresh.get("session_cookie"):
                    region = str(fresh.get("region") or "cn")
                    token, _ = await self._session_token(
                        fresh["session_cookie"],
                        region,
                        _console_host(region),
                    )
                    self.db.execute(
                        "UPDATE accounts SET access_token=?, token_expires_at=?, last_error='', updated_at=? "
                        "WHERE id=?",
                        (self._encrypt(token["access_token"]), int(token["expires_at"]), _now(), account_id),
                    )
                elif fresh.get("access_token"):
                    return fresh
                else:
                    raise TraeError("账号没有可用的登录凭据", retryable=False)
            except TraeError as exc:
                self.mark_error(account_id, str(exc))
                raise
            updated = self.get_account(account_id)
            assert updated is not None
            return updated

    def mark_error(self, account_id: int, message: str) -> None:
        self.db.execute(
            "UPDATE accounts SET last_error=?, updated_at=? WHERE id=?",
            (message[:1000], _now(), account_id),
        )

    # ------------------------------------------------------------------ selection
    def _sem(self, account_id: int) -> asyncio.Semaphore:
        if account_id not in self._semaphores:
            self._semaphores[account_id] = asyncio.Semaphore(self.settings.max_concurrent_per_account)
        return self._semaphores[account_id]

    async def pick_account(
        self, *, exclude: set[int] | None = None, require_model: str = "", region: str = ""
    ) -> tuple[dict[str, Any], asyncio.Semaphore]:
        exclude = exclude or set()
        accounts = [
            a
            for a in self.list_accounts(include_disabled=False, region=region)
            if int(a["id"]) not in exclude and not int(a.get("manual_disabled") or 0)
        ]
        accounts.sort(key=lambda a: (float(a.get("credits") or -1), -int(a["id"])), reverse=True)
        errors: list[str] = []
        for account in accounts:
            sem = self._sem(int(account["id"]))
            if sem.locked():
                continue
            try:
                await asyncio.wait_for(sem.acquire(), timeout=0.15)
            except asyncio.TimeoutError:
                continue
            try:
                fresh = await self.ensure_token(account)
            except TraeError as exc:
                sem.release()
                errors.append(f"{account['name']}: {exc}")
                continue
            if require_model:
                try:
                    models = await self.models(fresh)
                except TraeError as exc:
                    sem.release()
                    errors.append(f"{account['name']}: {exc}")
                    continue
                if not self._find_model(models, require_model):
                    sem.release()
                    continue
            return fresh, sem
        detail = "；".join(errors[-3:]) if errors else "没有已启用且可用的账号"
        raise TraeError("没有可用 Trae 账号：" + detail, http_status=503, retryable=False)

    @staticmethod
    def release(sem: asyncio.Semaphore) -> None:
        try:
            sem.release()
        except ValueError:
            pass

    # ------------------------------------------------------------------ models
    async def models(self, account: dict[str, Any], force: bool = False) -> list[dict[str, Any]]:
        account_id = int(account["id"])
        cached = self._catalog.get(account_id)
        if cached and not force and time.time() - cached[0] < 3600:
            return cached[1]
        async with self._catalog_lock:
            cached = self._catalog.get(account_id)
            if cached and not force and time.time() - cached[0] < 3600:
                return cached[1]
            fresh = await self.ensure_token(account)
            models = await self.client.get_models_all(fresh)
            self._catalog[account_id] = (time.time(), models)
            return models

    async def all_models(self, force: bool = False, region: str = "") -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        for account in self.list_accounts(include_disabled=False, region=region):
            try:
                models = await self.models(account, force=force)
            except TraeError as exc:
                errors.append(str(exc))
                continue
            for model in models:
                key = model["id"].lower()
                if key not in merged:
                    merged[key] = model
        if not merged:
            raise TraeError("无法获取模型列表：" + ("；".join(errors[-2:]) or "没有可用账号"))
        return list(merged.values())

    @staticmethod
    def _find_model(models: list[dict[str, Any]], requested: str) -> Optional[dict[str, Any]]:
        want = requested.strip().lower()
        for model in models:
            if model["id"].lower() == want:
                return model
        for model in models:
            if str(model.get("display_name", "")).lower() == want:
                return model
        for model in models:
            if want and want in str(model.get("display_name", "")).lower():
                return model
        return None

    async def resolve_model(self, account: dict[str, Any], requested: str) -> tuple[str, dict[str, Any]]:
        requested = (requested or self.db.get_setting("default_model", self.settings.default_model)).strip()
        if not MODEL_ID_RE.match(requested):
            requested = self.db.get_setting("default_model", self.settings.default_model)
        try:
            models = await self.models(account)
        except TraeError:
            return requested, {"id": requested, "ide_function": self.db.get_setting("chat_function", "solo_work_remote")}
        found = self._find_model(models, requested)
        if found:
            return found["id"], found
        return requested, {"id": requested, "ide_function": self.db.get_setting("chat_function", "solo_work_remote")}

    def estimate_credits(self, model: dict[str, Any], prompt: int, completion: int) -> float:
        candidates = [
            float(model.get("rate_activity_member") or 0),
            float(model.get("rate_member") or 0),
            float(model.get("rate_activity") or 0),
            float(model.get("rate_base") or 0),
        ]
        rates = [v for v in candidates if v > 0]
        if not rates:
            return 0.0
        rate = min(rates)
        # Trae's rate is a consumption multiplier per 1K tokens. Keep this labelled
        # as an estimate; the authoritative number is the upstream credit delta.
        return round((prompt + completion) * rate / 1000.0, 4)

    # ------------------------------------------------------------------ credits
    async def refresh_credits(self, account: dict[str, Any]) -> dict[str, Any]:
        fresh = await self.ensure_token(account)
        detail = await self.client.get_credit_detail(fresh)
        identity = await self.client.get_pay_identity(fresh)
        if detail is not None:
            try:
                days = max(0, int(float(self.db.get_setting("pool_expiring_soon_days", "7") or 7)))
            except Exception:
                days = 7
            packs = packs_loads(detail.get("packs"))
            expiring = packs_expiring(packs, days)
            self.db.execute(
                "UPDATE accounts SET credits=?, credits_used=?, credits_total=?, credits_expiring=?, "
                "credit_packs_json=?, credits_updated_at=?, pay_identity=?, last_error='', updated_at=? WHERE id=?",
                (
                    float(detail.get("remaining", -1)),
                    float(detail.get("used", -1)),
                    float(detail.get("total", -1)),
                    expiring,
                    packs_dumps(packs),
                    _now(),
                    int(identity),
                    _now(),
                    int(fresh["id"]),
                ),
            )
        return self.get_account(int(fresh["id"])) or fresh

    async def refresh_all_credits(self) -> list[dict[str, Any]]:
        results = []
        for account in self.list_accounts(include_disabled=False):
            try:
                results.append(await self.refresh_credits(account))
            except TraeError as exc:
                self.mark_error(int(account["id"]), str(exc))
                results.append({**account, "last_error": str(exc)})
        return results

    # ------------------------------------------------------------------ check-in
    async def checkin(self, account: dict[str, Any]) -> dict[str, Any]:
        fresh = await self.ensure_token(account)
        today = datetime.now().strftime("%Y-%m-%d")
        existing = self.db.query_one(
            "SELECT * FROM checkins WHERE account_id=? AND day=?", (int(fresh["id"]), today)
        )
        if existing and existing["status"] in ("success", "already"):
            return {
                "status": existing["status"],
                "credits": float(existing["credits"]),
                "message": existing["message"],
                "account": fresh["name"],
            }
        try:
            status = await self.client.checkin_status(fresh)
        except TraeError as exc:
            self._save_checkin(int(fresh["id"]), today, "failed", 0, str(exc))
            raise
        code = deep_code(status)
        checked = bool(status.get("checked_in") or status.get("did_checked_in"))
        enabled = status.get("enable", True)
        if code not in (0,) and code != 9004:
            message = str(status.get("message") or f"签到状态异常 code={code}")
            result = "failed"
            self._save_checkin(int(fresh["id"]), today, result, 0, message)
            return {"status": result, "credits": 0, "message": message, "account": fresh["name"]}
        if checked or code == 9004:
            message = "今日已签到"
            self._save_checkin(int(fresh["id"]), today, "already", 0, message)
            return {"status": "already", "credits": 0, "message": message, "account": fresh["name"]}
        if enabled is False:
            message = "该账号未开放签到"
            self._save_checkin(int(fresh["id"]), today, "disabled", 0, message)
            return {"status": "disabled", "credits": 0, "message": message, "account": fresh["name"]}

        claim = await self.client.checkin_claim(fresh)
        claim_code = deep_code(claim)
        if claim_code == 9004:
            message = "今日已签到"
            self._save_checkin(int(fresh["id"]), today, "already", 0, message)
            return {"status": "already", "credits": 0, "message": message, "account": fresh["name"]}
        if claim_code == 9074:
            message = "触发上游风控 code=9074，请稍后重试"
            self._save_checkin(int(fresh["id"]), today, "failed", 0, message)
            return {"status": "failed", "credits": 0, "message": message, "account": fresh["name"]}
        if claim_code not in (0,):
            message = str(claim.get("message") or f"签到失败 code={claim_code}")
            self._save_checkin(int(fresh["id"]), today, "failed", 0, message)
            return {"status": "failed", "credits": 0, "message": message, "account": fresh["name"]}

        credits = float(claim.get("credits") or 0) + float(claim.get("extra_credits") or 0)
        try:
            after = await self.client.checkin_status(fresh)
            if bool(after.get("checked_in")):
                if credits <= 0:
                    credits = float(after.get("credits") or 0) + float(after.get("extra_credits") or 0)
                message = "签到成功"
                self._save_checkin(int(fresh["id"]), today, "success", credits, message)
                await self.refresh_credits(fresh)
                return {"status": "success", "credits": credits, "message": message, "account": fresh["name"]}
        except TraeError:
            pass
        message = "签到已提交，积分到账待确认"
        self._save_checkin(int(fresh["id"]), today, "pending", credits, message)
        return {"status": "pending", "credits": credits, "message": message, "account": fresh["name"]}

    def _save_checkin(self, account_id: int, day: str, status: str, credits: float, message: str) -> None:
        self.db.execute(
            """
            INSERT INTO checkins(account_id, day, status, credits, message, created_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(account_id, day) DO UPDATE SET
                status=excluded.status, credits=excluded.credits,
                message=excluded.message, created_at=excluded.created_at
            """,
            (account_id, day, status, credits, message[:1000], _now()),
        )

    async def checkin_all(self) -> list[dict[str, Any]]:
        results = []
        accounts = self.list_accounts(include_disabled=False)
        for index, account in enumerate(accounts):
            if index:
                await asyncio.sleep(random.uniform(2.0, 4.0))
            if account.get("account_type") == "saas":
                results.append({"account": account["name"], "status": "skipped", "message": "企业账号不参与签到"})
                continue
            try:
                results.append(await self.checkin(account))
            except TraeError as exc:
                results.append({"account": account["name"], "status": "failed", "message": str(exc)})
        return results

    def checkin_history(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT c.*, a.name AS account_name FROM checkins c
            JOIN accounts a ON a.id=c.account_id
            ORDER BY c.created_at DESC LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ stats
    def snapshot(self) -> dict[str, Any]:
        accounts = self.list_accounts(include_disabled=False)
        today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        usage = self.db.query_one(
            """
            SELECT COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens),0) AS completion_tokens,
                   COALESCE(SUM(total_tokens),0) AS total_tokens,
                   COALESCE(SUM(credits_delta),0) AS credits
            FROM usage_logs WHERE created_at>=?
            """,
            (today_start,),
        )
        total = self.db.query_one(
            """
            SELECT COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS tokens,
                   COALESCE(SUM(credits_delta),0) AS credits
            FROM usage_logs
            """
        )
        return {
            "accounts": len(accounts),
            "credits": sum(max(0.0, float(a.get("credits") or 0)) for a in accounts),
            "today_requests": int(usage["requests"] if usage else 0),
            "today_prompt": int(usage["prompt_tokens"] if usage else 0),
            "today_completion": int(usage["completion_tokens"] if usage else 0),
            "today_tokens": int(usage["total_tokens"] if usage else 0),
            "today_credits": float(usage["credits"] if usage else 0),
            "total_requests": int(total["requests"] if total else 0),
            "total_tokens": int(total["tokens"] if total else 0),
            "total_credits": float(total["credits"] if total else 0),
        }

