from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..credit_packs import unit_of
from ..deps import SESSION_COOKIE, admin_from_request, client_ip, require_admin, state
from ..config import BASE_DIR
from ..security import LoginRateLimiter, safe_compare
from ..services.platforms import _today
from ..upstream.client import TraeError

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
login_limiter = LoginRateLimiter()
callback_limiter = LoginRateLimiter(max_attempts=20, window_seconds=300)


def _fmt_ts(value: Any) -> str:
    try:
        ts = int(value)
        if ts <= 0:
            return "—"
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "—"


def _fmt_num(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "0"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:.0f}"


def _fmt_credits(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "—"
    if number < 0:
        return "—"
    return f"{number:,.1f}"


def _fmt_money(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return "—"
    if number < 0:
        return "—"
    return f"${number:,.2f}"


templates.env.globals.update(
    fmt_ts=_fmt_ts, fmt_num=_fmt_num, fmt_credits=_fmt_credits, fmt_money=_fmt_money
)


def _base_url(request: Request) -> str:
    ctx = state(request)
    configured = ctx.db.get_setting("public_base_url", "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _render(request: Request, name: str, admin: dict[str, Any] | None = None, **context: Any):
    ctx = state(request)
    admin = admin or context.get("admin")
    context.setdefault("app_name", ctx.settings.app_name)
    context.setdefault("version", ctx.settings.version)
    context.setdefault("admin", admin)
    context.setdefault("csrf", admin.get("csrf") if admin else "")
    context.setdefault("msg", request.query_params.get("msg", ""))
    context.setdefault("ok", request.query_params.get("ok", ""))
    context.setdefault("base_url", _base_url(request))
    return templates.TemplateResponse(request=request, name=name, context=context)


def _wants_json(request: Request) -> bool:
    """前端 AJAX 刷新时返回 JSON，避免整页跳转导致滚动位置丢失。"""
    if request.headers.get("x-requested-with", "").lower() == "xmlhttprequest":
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _credits_payload(value: Any) -> Any:
    try:
        number = float(value)
    except Exception:
        return None
    return number if number >= 0 else None


def _account_credits(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for account in accounts:
        result.append(
            {
                "id": int(account.get("id") or 0),
                "name": str(account.get("name") or ""),
                "credits": _credits_payload(account.get("credits")),
                "credits_used": _credits_payload(account.get("credits_used")),
                "credits_total": _credits_payload(account.get("credits_total")),
                "credits_expiring": _credits_payload(account.get("credits_expiring")),
                "packs": account.get("packs") or [],
                "unit": unit_of(account.get("packs")),
                "updated_at": int(account.get("credits_updated_at") or 0),
                "status": str(account.get("status") or ""),
                "enabled": bool(account.get("enabled", True)),
                "last_error": str(account.get("last_error") or "")[:200],
            }
        )
    return result


def _credit_totals(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    def total(field: str) -> Any:
        values = []
        for account in accounts:
            value = _credits_payload(account.get(field))
            if value is not None:
                values.append(value)
        return sum(values) if values else None

    units = {unit_of(account.get("packs")) for account in accounts}
    return {
        "unit": "USD" if "USD" in units else "credits",
        "remaining": total("credits"),
        "used": total("credits_used"),
        "total": total("credits_total"),
        "expiring": total("credits_expiring"),
    }


def _redirect(path: str, msg: str = "", ok: bool = True) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    if msg:
        path = f"{path}{sep}msg={quote(msg)}&ok={'1' if ok else '0'}"
    return RedirectResponse(path, status_code=303)


async def _csrf_form(request: Request, admin: dict[str, Any]):
    form = await request.form()
    token = str(form.get("_csrf") or request.headers.get("x-csrf-token") or "")
    if not token or not safe_compare(token, admin["csrf"]):
        raise ValueError("CSRF 校验失败，请刷新页面后重试")
    return form


# --------------------------------------------------------------------- auth
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    admin = await admin_from_request(request)
    if admin:
        return RedirectResponse("/admin", status_code=303)
    return _render(request, "login.html")


@router.post("/login")
async def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    ctx = state(request)
    ip = client_ip(request, ctx.settings.trust_proxy)
    if not login_limiter.allowed(ip):
        return _render(request, "login.html", error="尝试次数过多，请 10 分钟后再试")
    admin = ctx.admin_auth.authenticate(username, password)
    if not admin:
        login_limiter.fail(ip)
        return _render(request, "login.html", error="用户名或密码错误")
    login_limiter.reset(ip)
    token, _ = ctx.admin_auth.create_session(int(admin["id"]))
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=ctx.settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@router.post("/logout")
async def logout(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    ctx.admin_auth.destroy_session(request.cookies.get(SESSION_COOKIE, ""))
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# --------------------------------------------------------------------- dashboard
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    snapshot = ctx.stats.overview()
    keys = ctx.api_keys.list_keys()
    active_key = next((k for k in keys if k["enabled"]), None)
    series = ctx.stats.daily_series(7)
    max_tokens = max([int(s["tokens"] or 0) for s in series] + [1])
    return _render(
        request,
        "dashboard.html",
        admin,
        accounts=ctx.accounts.list_accounts(),
        snapshot=snapshot,
        api_key=active_key,
        default_model=ctx.db.get_setting("default_model", ctx.settings.default_model),
        series=series,
        max_tokens=max_tokens,
        recent=ctx.usage.recent(10),
        virtuals=ctx.virtuals.list_groups(),
    )


# --------------------------------------------------------------------- accounts
@router.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request, admin: dict = Depends(require_admin)):
    return RedirectResponse("/admin/platforms", status_code=303)


@router.get("/accounts/login-url", response_class=HTMLResponse)
async def login_url_page(request: Request, admin: dict = Depends(require_admin)):
    region = request.query_params.get("region", "cn")
    return RedirectResponse(f"/admin/platforms/trae/login?region={region}", status_code=303)


@router.post("/accounts/add")
async def account_add(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        mode = str(form.get("mode") or "callback")
        if mode == "session":
            account = await ctx.accounts.add_from_session(
                str(form.get("session_cookie") or ""),
                str(form.get("region") or "cn"),
            )
        elif mode == "refresh":
            account = await ctx.accounts.add_from_refresh(
                str(form.get("refresh_token") or ""), "", str(form.get("region") or "cn")
            )
        else:
            account = await ctx.accounts.add_from_callback(
                str(form.get("callback_url") or ""), str(form.get("region") or "")
            )
        return _redirect("/admin/accounts", f"账号 {account['name']} 已保存")
    except (ValueError, TraeError) as exc:
        return _redirect("/admin/accounts", str(exc), ok=False)
    except Exception as exc:
        return _redirect("/admin/accounts", f"添加失败: {exc}", ok=False)


@router.post("/accounts/{account_id}/refresh")
async def account_refresh(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        account = ctx.accounts.get_account(account_id)
        if not account:
            raise ValueError("账号不存在")
        updated = await ctx.accounts.refresh_credits(account)
        if _wants_json(request):
            accounts = ctx.accounts.list_accounts(
                include_disabled=True, region=str(updated.get("region") or "cn")
            )
            return JSONResponse(
                {
                    "ok": True,
                    "message": f"{updated['name']} 当前积分 {_fmt_credits(updated['credits'])}",
                    "accounts": _account_credits(accounts),
                    "totals": _credit_totals(accounts),
                },
                headers={"Cache-Control": "no-store"},
            )
        return _redirect("/admin/accounts", f"{updated['name']} 当前积分 {_fmt_credits(updated['credits'])}")
    except Exception as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return _redirect("/admin/accounts", str(exc), ok=False)


@router.post("/accounts/{account_id}/checkin")
async def account_checkin(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        account = ctx.accounts.get_account(account_id)
        if not account:
            raise ValueError("账号不存在")
        result = await ctx.accounts.checkin(account)
        message = f"{result['account']}: {result['message']}"
        ok = result["status"] in ("success", "already", "pending")
        if _wants_json(request):
            return JSONResponse(
                {"ok": ok, "message": message, "result": result},
                headers={"Cache-Control": "no-store"},
            )
        return _redirect("/admin/accounts", message, ok=ok)
    except Exception as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return _redirect("/admin/accounts", str(exc), ok=False)


@router.post("/accounts/{account_id}/toggle")
async def account_toggle(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        account = ctx.accounts.get_account(account_id)
        if not account:
            raise ValueError("账号不存在")
        enabled = 0 if account["enabled"] else 1
        ctx.db.execute("UPDATE accounts SET enabled=?, updated_at=? WHERE id=?", (enabled, int(time.time()), account_id))
        return _redirect("/admin/accounts", f"{account['name']} 已{'启用' if enabled else '停用'}")
    except Exception as exc:
        return _redirect("/admin/accounts", str(exc), ok=False)


@router.post("/accounts/{account_id}/delete")
async def account_delete(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        account = ctx.accounts.get_account(account_id)
        if not account:
            raise ValueError("账号不存在")
        ctx.db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        return _redirect("/admin/accounts", f"{account['name']} 已删除")
    except Exception as exc:
        return _redirect("/admin/accounts", str(exc), ok=False)


@router.post("/accounts/{account_id}/rename")
async def account_rename(
    request: Request,
    account_id: int,
    name: str = Form(""),
    admin: dict = Depends(require_admin),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        name = name.strip()
        if not name or len(name) > 80:
            raise ValueError("名称不能为空且最多 80 字符")
        ctx.db.execute("UPDATE accounts SET name=?, updated_at=? WHERE id=?", (name, int(time.time()), account_id))
        return _redirect("/admin/accounts", "名称已更新")
    except Exception as exc:
        return _redirect("/admin/accounts", str(exc), ok=False)


@router.post("/accounts/checkin-all")
async def checkin_all(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        results = await ctx.accounts.checkin_all()
        success = sum(1 for r in results if r.get("status") in ("success", "already", "pending"))
        return _redirect("/admin/accounts", f"批量签到完成：{success}/{len(results)} 成功")
    except Exception as exc:
        return _redirect("/admin/accounts", str(exc), ok=False)


@router.post("/accounts/refresh-all")
async def refresh_all(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        await ctx.accounts.refresh_all_credits()
        return _redirect("/admin/accounts", "积分已刷新")
    except Exception as exc:
        return _redirect("/admin/accounts", str(exc), ok=False)


# --------------------------------------------------------------------- providers (removed)
@router.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request, admin: dict = Depends(require_admin)):
    return RedirectResponse("/admin/platforms", status_code=303)


async def _collect_platform_models(ctx) -> list[dict[str, Any]]:
    """汇总各平台已有模型，供虚拟模型组成员下拉使用。"""
    result: list[dict[str, Any]] = []
    for provider in ctx.providers.list_providers():
        models = provider.get("models") or []
        if provider["kind"] == "trae":
            try:
                live = await ctx.accounts.all_models(region=provider.get("region") or "cn")
                if live:
                    models = live
                    # 缓存到平台模型目录，概览页与模型下拉无需每次打上游
                    ctx.db.execute(
                        "UPDATE providers SET models_json=?, last_synced_at=?, updated_at=? WHERE id=?",
                        (
                            json.dumps(live, ensure_ascii=False),
                            int(time.time()),
                            int(time.time()),
                            provider["id"],
                        ),
                    )
            except Exception:
                models = models or []
        seen: set[str] = set()
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "")
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            result.append(
                {
                    "provider_id": provider["id"],
                    "provider_name": provider["name"],
                    "region": provider.get("region") or "",
                    "model_id": model_id,
                    "display_name": str(item.get("display_name") or model_id),
                    "context_length": int(item.get("context_length") or 131072),
                }
            )
    return result


# --------------------------------------------------------------------- virtual models
@router.get("/virtuals", response_class=HTMLResponse)
async def virtuals_page(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    platforms = ctx.providers.list_providers()
    platform_stats = {p["id"]: ctx.stats.platform_stats(p) for p in platforms}
    return _render(
        request,
        "virtuals.html",
        admin=admin,
        groups=ctx.virtuals.list_groups(),
        providers=platforms,
        platform_models=await _collect_platform_models(ctx),
        provider_regions={p["id"]: p.get("region") or "" for p in platforms},
        provider_names={p["id"]: p["name"] for p in platforms},
        platform_stats=platform_stats,
        costs={p["id"]: ctx.pool.costs(p["id"]) for p in platforms},
        now=int(time.time()),
    )


@router.post("/virtuals/add")
async def virtual_add(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        ctx.virtuals.create(str(form.get("name") or ""), str(form.get("display_name") or ""))
        return _redirect("/admin/virtuals", "虚拟模型组已创建")
    except Exception as exc:
        return _redirect("/admin/virtuals", str(exc), ok=False)


@router.post("/virtuals/{group_id}/toggle")
async def virtual_toggle(request: Request, group_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.virtuals.toggle(group_id)
        return _redirect("/admin/virtuals", "虚拟模型组状态已更新")
    except Exception as exc:
        return _redirect("/admin/virtuals", str(exc), ok=False)


@router.post("/virtuals/{group_id}/delete")
async def virtual_delete(request: Request, group_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.virtuals.delete(group_id)
        return _redirect("/admin/virtuals", "虚拟模型组已删除")
    except Exception as exc:
        return _redirect("/admin/virtuals", str(exc), ok=False)


@router.post("/virtuals/{group_id}/members/add")
async def virtual_member_add(request: Request, group_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        ctx.virtuals.add_member(
            group_id,
            str(form.get("provider_id") or ""),
            str(form.get("model") or ""),
            int(form.get("weight") or 1),
        )
        return _redirect("/admin/virtuals", "成员已添加")
    except Exception as exc:
        return _redirect("/admin/virtuals", str(exc), ok=False)


@router.post("/virtuals/members/{member_id}/toggle")
async def virtual_member_toggle(request: Request, member_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.virtuals.toggle_member(member_id)
        return _redirect("/admin/virtuals", "成员状态已更新")
    except Exception as exc:
        return _redirect("/admin/virtuals", str(exc), ok=False)


@router.post("/virtuals/members/{member_id}/delete")
async def virtual_member_delete(request: Request, member_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.virtuals.delete_member(member_id)
        return _redirect("/admin/virtuals", "成员已删除")
    except Exception as exc:
        return _redirect("/admin/virtuals", str(exc), ok=False)


# --------------------------------------------------------------------- platforms
@router.get("/platforms/trae/login", response_class=HTMLResponse)
async def trae_login_page(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    region = request.query_params.get("region", "cn")
    if (region or "cn").lower() in ("global", "intl", "international"):
        return RedirectResponse("/admin/trae/token?region=global", status_code=303)
    url, trace, device, machine = ctx.accounts.start_login("", region)
    callback = f"http://127.0.0.1:{ctx.settings.trae_callback_port}/authorize"
    return _render(
        request,
        "trae_login.html",
        admin=admin,
        login_url=url,
        callback_url=callback,
        region=region,
        trace_id=trace,
        device_id=device,
        machine_id=machine,
    )


@router.post("/platforms/trae/callback")
async def trae_login_callback(
    request: Request,
    admin: dict = Depends(require_admin),
    callback_url: str = Form(""),
    region: str = Form("cn"),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        account = await ctx.accounts.add_from_callback(callback_url, region)
        return _redirect("/admin/platforms", f"Trae 账号 {account['name']} 已保存")
    except Exception as exc:
        return _redirect(f"/admin/platforms/trae/login?region={region}", str(exc), ok=False)


@router.get("/trae/token", response_class=HTMLResponse)
async def trae_token_page(request: Request, admin: dict = Depends(require_admin)):
    region = request.query_params.get("region", "global")
    response = _render(request, "trae_token.html", admin=admin, region=region, result=None)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/trae/token", response_class=HTMLResponse)
async def trae_token_exchange(
    request: Request,
    admin: dict = Depends(require_admin),
    refresh_token: str = Form(""),
    uid: str = Form(""),
    region: str = Form("global"),
):
    ctx = state(request)
    result = None
    error = ""
    try:
        await _csrf_form(request, admin)
        refresh_token = (refresh_token or "").strip()
        if not refresh_token:
            raise ValueError("请填写 refreshToken")
        account = await ctx.accounts.add_from_refresh(
            refresh_token,
            (uid or "").strip(),
            region or "global",
        )
        result = {
            "access_token": account.get("access_token") or "",
            "refresh_token": account.get("refresh_token") or refresh_token,
            "expires_at": int(account.get("token_expires_at") or 0),
            "uid": str(account.get("uid") or uid or ""),
            "name": str(account.get("name") or ""),
            "saved": True,
        }
    except Exception as exc:
        error = str(exc)
    response = _render(
        request,
        "trae_token.html",
        admin=admin,
        region=region,
        result=result,
        error=error,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/trae/cookie")
async def trae_cookie_login(
    request: Request,
    admin: dict = Depends(require_admin),
    session_cookie: str = Form(""),
    region: str = Form("global"),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        account = await ctx.accounts.add_from_session(session_cookie, region or "global")
        return _redirect("/admin/platforms", f"Trae 账号 {account['name']} 已通过 Cookie 保存")
    except Exception as exc:
        return _redirect(f"/admin/trae/token?region={region or 'global'}", str(exc), ok=False)


@router.get("/platforms", response_class=HTMLResponse)
async def platforms_page(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    ctx.platforms.cleanup_expired()
    platforms = ctx.providers.list_providers()
    platform_models = await _collect_platform_models(ctx)
    counts: dict[str, int] = {}
    for item in platform_models:
        counts[item["provider_id"]] = counts.get(item["provider_id"], 0) + 1
    for provider in platforms:
        provider["model_count"] = counts.get(provider["id"], 0)
        if provider["kind"] == "trae":
            provider["account_count"] = len(
                ctx.accounts.list_accounts(include_disabled=False, region=provider.get("region") or "cn")
            )
    trae_accounts = {
        "cn": ctx.accounts.list_accounts(include_disabled=True, region="cn"),
        "global": ctx.accounts.list_accounts(include_disabled=True, region="global"),
    }
    for rows in trae_accounts.values():
        for account in rows:
            account["credit_unit"] = unit_of(account.get("packs"))
    for provider in platforms:
        for account in provider.get("accounts") or []:
            account["credit_unit"] = unit_of(account.get("packs"))
    return _render(
        request,
        "platforms.html",
        admin=admin,
        platforms=platforms,
        trae_accounts=trae_accounts,
        pending=ctx.db.query(
            "SELECT * FROM platform_logins WHERE status='pending' AND (expires_at=0 OR expires_at>?) "
            "ORDER BY created_at DESC LIMIT 10",
            (int(time.time()),),
        ),
    )


@router.get("/platforms/{provider_id}", response_class=HTMLResponse)
async def platform_detail(request: Request, provider_id: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    provider = ctx.providers.get_provider(provider_id)
    if not provider:
        return _redirect("/admin/platforms", "平台不存在", ok=False)
    if provider["kind"] == "trae":
        accounts = ctx.accounts.list_accounts(include_disabled=True, region=provider.get("region") or "cn")
    else:
        accounts = provider.get("accounts") or []
    models = [m for m in await _collect_platform_models(ctx) if m["provider_id"] == provider_id]

    def _sum(field: str) -> float:
        values = []
        for account in accounts:
            try:
                value = float(account.get(field))
            except Exception:
                continue
            if value >= 0:
                values.append(value)
        return sum(values) if values else -1

    costs = ctx.pool.costs(provider_id)
    return _render(
        request,
        "platform_detail.html",
        admin=admin,
        provider=provider,
        accounts=accounts,
        models=models,
        costs=costs,
        now=int(time.time()),
        totals={
            "remaining": _sum("credits"),
            "used": _sum("credits_used"),
            "total": _sum("credits_total"),
            "expiring": _sum("credits_expiring"),
            "unit": "USD" if any(unit_of(a.get("packs")) == "USD" for a in accounts) else "credits",
        },
    )


@router.post("/platforms/{provider_id}/quota-all")
async def platform_quota_all(request: Request, provider_id: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        provider = ctx.providers.get_provider(provider_id)
        if not provider:
            raise ValueError("平台不存在")
        count = 0
        if provider["kind"] == "trae":
            for account in ctx.accounts.list_accounts(include_disabled=False, region=provider.get("region") or "cn"):
                try:
                    await ctx.accounts.refresh_credits(account)
                    count += 1
                except Exception:
                    continue
        else:
            for account in ctx.providers.accounts(provider_id, include_disabled=False):
                try:
                    await ctx.platforms.refresh_quota(int(account["id"]))
                    count += 1
                except Exception:
                    continue
        if _wants_json(request):
            if provider["kind"] == "trae":
                accounts = ctx.accounts.list_accounts(
                    include_disabled=True, region=provider.get("region") or "cn"
                )
            else:
                accounts = ctx.providers.accounts(provider_id, include_disabled=True)
            return JSONResponse(
                {
                    "ok": True,
                    "message": f"已刷新 {count} 个账号积分",
                    "accounts": _account_credits(accounts),
                    "totals": _credit_totals(accounts),
                },
                headers={"Cache-Control": "no-store"},
            )
        return _redirect(f"/admin/platforms/{provider_id}", f"已刷新 {count} 个账号积分")
    except Exception as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return _redirect(f"/admin/platforms/{provider_id}", str(exc), ok=False)


@router.post("/platforms/{provider_id}/checkin-all")
async def platform_checkin_all(request: Request, provider_id: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        provider = ctx.providers.get_provider(provider_id)
        if not provider:
            raise ValueError("平台不存在")
        accounts = ctx.providers.accounts(provider_id, include_disabled=False)
        if not accounts:
            return _redirect("/admin/platforms", f"{provider['name']} 没有启用账号", ok=False)
        results = []
        for account in accounts:
            try:
                result = await ctx.platforms.checkin(int(account["id"]))
                results.append({"account": account.get("name"), "status": result.get("status"), "message": result.get("message")})
            except Exception as exc:
                results.append({"account": account.get("name"), "status": "error", "message": str(exc)[:300]})
        success = sum(1 for item in results if item.get("status") in ("success", "already"))
        message = f"{provider['name']} 签到完成：{success}/{len(results)} 个账号成功或已签到"
        if _wants_json(request):
            return JSONResponse({"ok": success == len(results), "message": message, "results": results}, headers={"Cache-Control": "no-store"})
        return _redirect("/admin/platforms", message)
    except Exception as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return _redirect("/admin/platforms", str(exc), ok=False)


@router.post("/platforms/{provider_id}/toggle")
async def platforms_toggle(request: Request, provider_id: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.providers.toggle_provider(provider_id)
        return _redirect("/admin/platforms", "平台状态已更新")
    except Exception as exc:
        return _redirect("/admin/platforms", str(exc), ok=False)


@router.post("/platforms/{provider_id}/config")
async def platforms_config(
    request: Request,
    provider_id: str,
    admin: dict = Depends(require_admin),
    base_url: str = Form(""),
    config_json: str = Form("{}"),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        try:
            config = json.loads(config_json or "{}")
        except Exception as exc:
            raise ValueError(f"配置 JSON 无效: {exc}") from exc
        ctx.providers.update_config(provider_id, base_url, config)
        return _redirect("/admin/platforms", "平台配置已保存")
    except Exception as exc:
        return _redirect("/admin/platforms", str(exc), ok=False)


@router.post("/platforms/{provider_id}/login/start")
async def platforms_login_start(request: Request, provider_id: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        login = await ctx.platforms.start_login(provider_id)
        return RedirectResponse(f"/admin/platforms/login/{login['id']}", status_code=303)
    except Exception as exc:
        return _redirect("/admin/platforms", str(exc), ok=False)


@router.get("/platforms/login/{login_id}", response_class=HTMLResponse)
async def platforms_login_page(request: Request, login_id: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    login = ctx.platforms.get_login(login_id)
    if not login:
        return _redirect("/admin/platforms", "登录会话不存在", ok=False)
    provider = ctx.providers.get_provider(login["provider_id"])
    return _render(request, "platform_login.html", admin=admin, login=login, provider=provider)


@router.post("/platforms/login/{login_id}/poll")
async def platforms_login_poll(request: Request, login_id: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        return JSONResponse(await ctx.platforms.poll_login(login_id))
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


@router.post("/platforms/login/{login_id}/callback")
async def platforms_login_callback(
    request: Request,
    login_id: str,
    admin: dict = Depends(require_admin),
    callback_url: str = Form(""),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        result = await ctx.platforms.complete_callback(login_id, callback_url)
        return _redirect("/admin/platforms", result.get("message") or "登录成功")
    except Exception as exc:
        return _redirect(f"/admin/platforms/login/{login_id}", str(exc), ok=False)


@router.post("/platforms/{provider_id}/accounts/manual")
async def platforms_add_manual(
    request: Request,
    provider_id: str,
    admin: dict = Depends(require_admin),
    name: str = Form(""),
    uid: str = Form(""),
    credential: str = Form(""),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.platforms.add_manual(provider_id, name, credential, uid)
        return _redirect("/admin/platforms", "账号已导入")
    except Exception as exc:
        return _redirect("/admin/platforms", str(exc), ok=False)


@router.post("/platforms/accounts/{account_id}/toggle")
async def platforms_account_toggle(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.providers.toggle_account(account_id)
        return _redirect("/admin/platforms", "账号状态已更新")
    except Exception as exc:
        return _redirect("/admin/platforms", str(exc), ok=False)


async def _manual_action(request: Request, admin: dict, scope: str, account_id: int, action: str):
    ctx = state(request)
    form = await _csrf_form(request, admin)
    reason = str(form.get("reason") or "").strip()
    now = int(time.time())
    if scope == "provider":
        row = ctx.providers.get_account(account_id)
    else:
        row = ctx.accounts.get_account(account_id)
    if not row:
        raise ValueError("账号不存在")
    request.state.audit_reason = reason
    request.state.audit_object = str(row.get("uid") or row.get("name") or account_id)
    request.state.audit_action = (
        f"platform_account.{action}" if scope == "provider" else f"account.{action}"
    )
    if action == "disable":
        if scope == "provider":
            ctx.pool.set_manual_disabled(account_id, True, reason)
        else:
            ctx.db.execute(
                "UPDATE accounts SET manual_disabled=1, manual_reason=?, updated_at=? WHERE id=?",
                (reason[:200], now, account_id),
            )
        message = f"{row.get('name') or account_id} 已摘除对话流量（签到与保活不受影响）"
    elif action == "enable":
        if scope == "provider":
            ctx.pool.set_manual_disabled(account_id, False)
        else:
            ctx.db.execute(
                "UPDATE accounts SET manual_disabled=0, manual_reason='', updated_at=? WHERE id=?",
                (now, account_id),
            )
        message = f"{row.get('name') or account_id} 已恢复对话流量"
    elif action == "revive":
        if scope == "provider":
            ctx.db.execute(
                "UPDATE provider_accounts SET manual_disabled=0, manual_reason='', cooldown_until=0, "
                "cooldown_kind='', fail_streak=0, soft_streak=0, last_error='', updated_at=? WHERE id=?",
                (now, account_id),
            )
        else:
            ctx.db.execute(
                "UPDATE accounts SET manual_disabled=0, manual_reason='', last_error='', updated_at=? WHERE id=?",
                (now, account_id),
            )
        message = f"{row.get('name') or account_id} 已复活（清除停用与冷却状态）"
    else:
        raise ValueError("未知操作")
    ctx.pool.persist_state(force=True)
    return message


def _manual_response(request: Request, message: str, fallback: str, ok: bool = True):
    if _wants_json(request):
        return JSONResponse({"ok": ok, "message": message}, headers={"Cache-Control": "no-store"})
    return _redirect(fallback, message, ok=ok)


@router.post("/platforms/accounts/{account_id}/disable")
async def platform_account_disable(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    try:
        message = await _manual_action(request, admin, "provider", account_id, "disable")
        return _manual_response(request, message, "/admin/platforms")
    except Exception as exc:
        return _manual_response(request, str(exc), "/admin/platforms", ok=False)


@router.post("/platforms/accounts/{account_id}/enable")
async def platform_account_enable(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    try:
        message = await _manual_action(request, admin, "provider", account_id, "enable")
        return _manual_response(request, message, "/admin/platforms")
    except Exception as exc:
        return _manual_response(request, str(exc), "/admin/platforms", ok=False)


@router.post("/platforms/accounts/{account_id}/revive")
async def platform_account_revive(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    try:
        message = await _manual_action(request, admin, "provider", account_id, "revive")
        return _manual_response(request, message, "/admin/platforms")
    except Exception as exc:
        return _manual_response(request, str(exc), "/admin/platforms", ok=False)


@router.post("/accounts/{account_id}/disable")
async def trae_account_disable(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    try:
        message = await _manual_action(request, admin, "trae", account_id, "disable")
        return _manual_response(request, message, "/admin/platforms")
    except Exception as exc:
        return _manual_response(request, str(exc), "/admin/platforms", ok=False)


@router.post("/accounts/{account_id}/enable")
async def trae_account_enable(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    try:
        message = await _manual_action(request, admin, "trae", account_id, "enable")
        return _manual_response(request, message, "/admin/platforms")
    except Exception as exc:
        return _manual_response(request, str(exc), "/admin/platforms", ok=False)


@router.post("/accounts/{account_id}/revive")
async def trae_account_revive(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    try:
        message = await _manual_action(request, admin, "trae", account_id, "revive")
        return _manual_response(request, message, "/admin/platforms")
    except Exception as exc:
        return _manual_response(request, str(exc), "/admin/platforms", ok=False)


@router.post("/platforms/accounts/{account_id}/delete")
async def platforms_account_delete(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.providers.delete_account(account_id)
        return _redirect("/admin/platforms", "账号已删除")
    except Exception as exc:
        return _redirect("/admin/platforms", str(exc), ok=False)


@router.post("/platforms/accounts/{account_id}/quota")
async def platforms_account_quota(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        result = await ctx.platforms.refresh_quota(account_id)
        if _wants_json(request):
            account = ctx.providers.get_account(account_id) or {}
            provider_id = str(account.get("provider_id") or "")
            accounts = ctx.providers.accounts(provider_id, include_disabled=True) if provider_id else []
            return JSONResponse(
                {
                    "ok": True,
                    "message": f"额度已刷新: {_fmt_credits(result.get('credits'))}",
                    "accounts": _account_credits(accounts),
                    "totals": _credit_totals(accounts),
                },
                headers={"Cache-Control": "no-store"},
            )
        return _redirect("/admin/platforms", f"额度已刷新: {result.get('credits', '—')}")
    except Exception as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return _redirect("/admin/platforms", str(exc), ok=False)


@router.post("/platforms/accounts/{account_id}/checkin")
async def platforms_account_checkin(request: Request, account_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        result = await ctx.platforms.checkin(account_id)
        message = str(result.get("message") or "签到完成")
        if _wants_json(request):
            return JSONResponse(
                {"ok": str(result.get("status") or "") != "error", "message": message, "result": result},
                headers={"Cache-Control": "no-store"},
            )
        return _redirect("/admin/platforms", message)
    except Exception as exc:
        if _wants_json(request):
            return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
        return _redirect("/admin/platforms", str(exc), ok=False)


@router.post("/platforms/{provider_id}/models/add")
async def platform_model_add(
    request: Request,
    provider_id: str,
    admin: dict = Depends(require_admin),
    model_id: str = Form(""),
    display_name: str = Form(""),
    context_length: str = Form("131072"),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        added = ctx.providers.add_model(provider_id, model_id, display_name, int(context_length or 131072))
        msg = f"已添加模型 {model_id}" if added else f"模型 {model_id} 已存在"
        return _redirect(f"/admin/platforms/{provider_id}", msg)
    except Exception as exc:
        return _redirect(f"/admin/platforms/{provider_id}", str(exc), ok=False)


@router.post("/platforms/{provider_id}/models/remove")
async def platform_model_remove(
    request: Request,
    provider_id: str,
    admin: dict = Depends(require_admin),
    model_id: str = Form(""),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.providers.remove_model(provider_id, model_id)
        return _redirect(f"/admin/platforms/{provider_id}", f"已移除模型 {model_id}")
    except Exception as exc:
        return _redirect(f"/admin/platforms/{provider_id}", str(exc), ok=False)


@router.post("/platforms/{provider_id}/test")
async def platform_test(request: Request, provider_id: str, admin: dict = Depends(require_admin)):
    """连通性测试：对平台做一次只读探测，不写库、不消耗额度。"""
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "message": str(exc)}, status_code=400, headers={"Cache-Control": "no-store"}
        )
    provider = ctx.providers.get_provider(provider_id)
    if not provider:
        return JSONResponse(
            {"ok": False, "message": "平台不存在"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        if str(provider.get("kind") or "") == "trae":
            region = provider.get("region") or "cn"
            accounts = ctx.accounts.list_accounts(include_disabled=False, region=region)
            if not accounts:
                return JSONResponse(
                    {"ok": False, "latency_ms": elapsed(), "message": "没有已启用的 Trae 账号"},
                    headers={"Cache-Control": "no-store"},
                )
            models = await asyncio.wait_for(ctx.accounts.models(accounts[0], force=True), timeout=25)
            return JSONResponse(
                {
                    "ok": True,
                    "latency_ms": elapsed(),
                    "message": f"连通正常，账号 {accounts[0]['name']} 返回 {len(models)} 个模型",
                },
                headers={"Cache-Control": "no-store"},
            )
        accounts = ctx.providers.accounts(provider_id, include_disabled=False)
        if not accounts:
            return JSONResponse(
                {"ok": False, "latency_ms": elapsed(), "message": "没有已启用的账号"},
                headers={"Cache-Control": "no-store"},
            )
        account = accounts[0]
        adapter = ctx.providers.adapter(provider)
        if adapter is not None:
            models = await asyncio.wait_for(adapter.list_models(provider, account), timeout=25)
        elif str(provider.get("protocol") or "") == "anthropic":
            models = await asyncio.wait_for(
                ctx.providers.anthropic.list_models(provider, account), timeout=25
            )
        else:
            models = await asyncio.wait_for(
                ctx.providers.openai.list_models(provider, account), timeout=25
            )
        return JSONResponse(
            {
                "ok": True,
                "latency_ms": elapsed(),
                "message": f"连通正常，账号 {account['name']} 返回 {len(models or [])} 个模型",
            },
            headers={"Cache-Control": "no-store"},
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            {"ok": False, "latency_ms": elapsed(), "message": "连通测试超时（25s）"},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "latency_ms": elapsed(), "message": str(exc)[:300]},
            headers={"Cache-Control": "no-store"},
        )


@router.post("/platforms/{provider_id}/sync")
async def platforms_sync_models(request: Request, provider_id: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        models = await ctx.providers.sync_models(provider_id)
        return _redirect(f"/admin/platforms/{provider_id}", f"已同步 {len(models)} 个模型")
    except Exception as exc:
        return _redirect("/admin/platforms", str(exc), ok=False)


# --------------------------------------------------------------------- models (merged into virtual models)
@router.get("/models", response_class=HTMLResponse)
async def models_page(request: Request, admin: dict = Depends(require_admin)):
    return RedirectResponse("/admin/virtuals", status_code=303)


# --------------------------------------------------------------------- logs
@router.get("/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    admin: dict = Depends(require_admin),
    page: int = 1,
    model: str = "",
    status: str = "",
    days: int = 0,
):
    ctx = state(request)
    page = max(1, page)
    rows, total = ctx.usage.page(page=page, limit=50, model=model, status=status, days=days)
    pages = max(1, (total + 49) // 50)
    return _render(
        request,
        "logs.html",
        admin,
        rows=rows,
        total=total,
        page=page,
        pages=pages,
        model=model,
        status=status,
        days=days,
        summary=ctx.usage.summary(),
        ledger=ctx.scheduler.task_ledger(30),
        task_labels=TASK_LABELS,
    )


@router.get("/logs/export")
async def logs_export(
    request: Request,
    admin: dict = Depends(require_admin),
    model: str = "",
    status: str = "",
    days: int = 0,
):
    ctx = state(request)
    content = ctx.usage.export_csv(model=model, status=status, days=days)
    return Response(
        content="\ufeff" + content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="aiall-usage.csv"'},
    )


@router.post("/tasks/{name}/run")
async def task_run(request: Request, name: str, admin: dict = Depends(require_admin)):
    ctx = state(request)
    redirect_to = ""
    try:
        form = await _csrf_form(request, admin)
        redirect_to = str(form.get("redirect") or "").strip()
        ctx.scheduler.start_manual(name)
        if redirect_to:
            return _redirect(redirect_to, f"任务 {name} 已受理，正在后台执行")
        return JSONResponse(
            {"ok": True, "message": f"任务 {name} 已受理", "task": name},
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )
    except RuntimeError as exc:
        if redirect_to:
            return _redirect(redirect_to, str(exc), ok=False)
        return JSONResponse(
            {"ok": False, "message": str(exc)},
            status_code=409,
            headers={"Cache-Control": "no-store"},
        )
    except ValueError as exc:
        if redirect_to:
            return _redirect(redirect_to, str(exc), ok=False)
        return JSONResponse(
            {"ok": False, "message": str(exc)},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:
        if redirect_to:
            return _redirect(redirect_to, str(exc), ok=False)
        return JSONResponse(
            {"ok": False, "message": str(exc)},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )


TASK_DEFINITIONS = (
    {
        "name": "activity",
        "label": "活跃地图",
        "desc": "对话事件上报点亮活跃地图，连登兑换 + 抽奖 + 礼包/补偿领取",
        "endpoints": "POST /v2/report · GET /activity/growth/streak · POST redeem / lottery/draw · claim-gift",
        "time": ("schedule.activity_time", "10:00"),
        "enabled": "schedule.activity_enabled",
    },
    {
        "name": "travel",
        "label": "猫猫旅行",
        "desc": "领养 / 派出 / 到站领奖闭环推进",
        "endpoints": "GET buddy/info · POST agreement / first · GET travel/status · POST travel/depart / travel/claim",
        "time": ("schedule.travel_time", "09:00"),
        "enabled": "schedule.travel_enabled",
    },
    {
        "name": "school",
        "label": "开学季",
        "desc": "任务点亮 + claim + 自动抽空抽奖余额，活动下线自动跳过",
        "endpoints": "scripts/workbuddy_tasks/school_open_day_2026.py",
        "time": ("schedule.school_time", "12:00"),
        "enabled": "schedule.school_enabled",
    },
    {
        "name": "cat",
        "label": "夜猫子",
        "desc": "夜猫窗口（23:00–08:00 CST）内补一次 black_cat 任务",
        "endpoints": "scripts/workbuddy_tasks/task_runner.py --only black_cat",
        "time": ("schedule.cat_time", "01:00"),
        "enabled": "schedule.cat_enabled",
    },
)

TASK_LABELS = {
    "trae_checkin": "Trae 签到",
    "platform_checkin": "平台签到",
    "checkin": "全部签到",
    "activity": "活跃地图",
    "travel": "猫猫旅行",
    "school": "开学季",
    "cat": "夜猫子",
    "model_refresh": "模型同步",
}


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    settings = ctx.db.get_settings()
    today = _today()
    ledger_rows = ctx.scheduler.task_ledger(50)
    latest: dict[str, dict[str, Any]] = {}
    for row in ledger_rows:
        latest.setdefault(str(row.get("task")), row)
    tasks = []
    for item in TASK_DEFINITIONS:
        time_key, default_time = item["time"]
        enabled = settings.get(item["enabled"], "0") == "1"
        last = latest.get(item["name"])
        tasks.append(
            {
                "name": item["name"],
                "label": item["label"],
                "desc": item["desc"],
                "endpoints": item["endpoints"],
                "enabled": enabled,
                "time": settings.get(time_key, default_time),
                "today_run": settings.get(f"last_{item['name']}_run", "") == today,
                "last": last,
            }
        )
    return _render(
        request,
        "tasks.html",
        admin,
        settings=settings,
        tasks=tasks,
        ledger=ledger_rows,
        task_labels=TASK_LABELS,
    )


def _time_value(form, name: str, default: str) -> str:
    value = str(form.get(name) or default).strip()
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value):
        raise ValueError(f"时间格式应为 HH:MM: {name}={value}")
    return value


@router.post("/tasks/settings")
async def tasks_settings_save(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        values = {}
        for item in TASK_DEFINITIONS:
            values[item["enabled"]] = "1" if form.get(f"{item['name']}_enabled") == "1" else "0"
            values[item["time"][0]] = _time_value(
                form, f"{item['name']}_time", item["time"][1]
            )
        for key, value in values.items():
            ctx.db.set_setting(key, value)
        return _redirect("/admin/tasks", "任务开关与时间已保存")
    except Exception as exc:
        return _redirect("/admin/tasks", str(exc), ok=False)


@router.post("/tasks/schedule")
async def tasks_schedule_save(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        values = {
            "schedule.jitter_minutes": str(
                max(0, min(120, int(form.get("schedule_jitter_minutes") or 0)))
            ),
            "schedule.retry_delay_minutes": str(
                max(0, min(1440, int(form.get("schedule_retry_delay_minutes") or 0)))
            ),
            "schedule.retry_max_per_day": str(
                max(0, min(10, int(form.get("schedule_retry_max_per_day") or 1)))
            ),
            "schedule.ledger_file": str(form.get("schedule_ledger_file") or "").strip(),
        }
        for key, value in values.items():
            ctx.db.set_setting(key, value)
        return _redirect("/admin/tasks", "任务调度设置已保存")
    except Exception as exc:
        return _redirect("/admin/tasks", str(exc), ok=False)


@router.get("/checkins", response_class=HTMLResponse)
async def checkins_page(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    records = [
        {**row, "platform_name": "Trae"}
        for row in ctx.accounts.checkin_history(200)
    ] + ctx.platforms.checkin_history(200)
    records.sort(key=lambda r: int(r.get("created_at") or 0), reverse=True)
    today = _today()
    last_run = ctx.db.get_setting("last_checkin_run", "")
    return _render(
        request,
        "checkins.html",
        admin=admin,
        checkins=records[:200],
        settings=ctx.db.get_settings(),
        today=today,
        today_run=last_run == today,
        streak=ctx.platforms.checkin_streak(today),
    )


@router.post("/checkins/settings")
async def checkins_settings(
    request: Request,
    admin: dict = Depends(require_admin),
    auto_checkin: str = Form("0"),
    checkin_time: str = Form("10:05"),
):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", checkin_time or ""):
            raise ValueError("签到时间格式应为 HH:MM")
        ctx.db.set_setting("auto_checkin", "1" if auto_checkin in ("1", "on", "true") else "0")
        ctx.db.set_setting("checkin_time", checkin_time)
        return _redirect("/admin/checkins", "自动签到设置已保存")
    except Exception as exc:
        return _redirect("/admin/checkins", str(exc), ok=False)


@router.post("/checkins/run-all")
async def checkins_run_all(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        started_at = int(time.time())
        trae_results = await ctx.accounts.checkin_all()
        ctx.scheduler.record_task_run("trae_checkin", started_at, trae_results, source="manual")
        platform_started_at = int(time.time())
        platform_results = await ctx.platforms.checkin_all()
        ctx.scheduler.record_task_run("platform_checkin", platform_started_at, platform_results, source="manual")
        request.state.audit_object = "checkins"
        request.state.audit_action = "task.run_all"
        try:
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        except Exception:
            today = datetime.now().strftime("%Y-%m-%d")
        ctx.db.set_setting("last_checkin_run", today)
        return _redirect(
            "/admin/checkins",
            f"签到完成：Trae {len(trae_results)} 个账号，平台 {len(platform_results)} 个账号",
        )
    except Exception as exc:
        return _redirect("/admin/checkins", str(exc), ok=False)


# --------------------------------------------------------------------- settings
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    return _render(
        request,
        "settings.html",
        admin,
        settings=ctx.db.get_settings(),
        callback_url=_base_url(request) + "/trae/callback",
        data_dir=str(ctx.settings.data_dir),
        port=ctx.settings.port,
    )


@router.post("/settings")
async def settings_save(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        prompt_mode = str(form.get("prompt_mode") or "passthrough").strip().lower()
        if prompt_mode not in ("passthrough", "custom", "append"):
            prompt_mode = "passthrough"
        values = {
            "default_model": str(form.get("default_model") or "").strip() or "DeepSeek-V4-Flash",
            "models_auto_refresh": "1" if str(form.get("models_auto_refresh") or "1") == "1" else "0",
            "models_refresh_time": _time_value(form, "models_refresh_time", "10:10"),
            "prompt.mode": prompt_mode,
            "prompt.file": str(form.get("prompt_file") or "").strip(),
            "privacy.sanitize_fingerprint": "1" if form.get("sanitize_fingerprint") else "0",
            "log_retention_days": str(max(1, min(3650, int(form.get("log_retention_days") or 30)))),
            "max_concurrent_per_account": str(max(1, min(8, int(form.get("max_concurrent_per_account") or 2)))),
            "upstream_timeout": str(max(30, min(900, int(form.get("upstream_timeout") or 180)))),
            "first_event_timeout": str(max(10, min(300, int(form.get("first_event_timeout") or 60)))),
            "chat_function": str(form.get("chat_function") or "solo_work_remote"),
            "public_base_url": str(form.get("public_base_url") or "").strip().rstrip("/"),
            "allow_any_key": "1" if form.get("allow_any_key") else "0",
            "pool_max_in_flight": str(max(1, min(16, int(form.get("pool_max_in_flight") or 2)))),
            "pool_breaker_threshold": str(max(1, min(20, int(form.get("pool_breaker_threshold") or 3)))),
            "pool_session_ttl": str(max(60, min(86400, int(form.get("pool_session_ttl") or 1800)))),
            "pool_expiring_soon_days": str(max(0, min(60, int(form.get("pool_expiring_soon_days") or 7)))),
            "pool_cost_explore_interval": str(
                max(0, min(86400, int(form.get("pool_cost_explore_interval") or 1800)))
            ),
            "pool_cost_explore_max_per_day": str(
                max(0, min(500, int(form.get("pool_cost_explore_max_per_day") or 48)))
            ),
            "daily_credit_limit": str(max(0.0, float(form.get("daily_credit_limit") or 0))),
        }
        for key, value in values.items():
            ctx.db.set_setting(key, value)
        return _redirect("/admin/settings", "设置已保存")
    except Exception as exc:
        return _redirect("/admin/settings", str(exc), ok=False)


@router.post("/settings/observability")
async def settings_observability(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        audit_file = str(form.get("admin_audit_file") or "./data/admin_audit.log").strip()
        audit_enabled = "1" if form.get("admin_audit_enabled") else "0"
        alerting_secret = str(form.get("alerting_secret") or "").strip()
        if alerting_secret == "-":
            alerting_secret = ""
        elif not alerting_secret:
            alerting_secret = ctx.db.get_setting("alerting_secret", "")
        if audit_enabled == "1":
            path = Path(audit_file).expanduser()
            if not path.is_absolute():
                path = BASE_DIR / path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8"):
                    pass
            except OSError as exc:
                raise ValueError(f"审计文件不可写: {path} ({exc})")
        values = {
            "metrics_enabled": "1" if form.get("metrics_enabled") else "0",
            "metrics.legacy_prefix": "1" if form.get("metrics_legacy_prefix") else "0",
            "alerting_enabled": "1" if form.get("alerting_enabled") else "0",
            "alerting_interval_seconds": str(
                max(5, min(3600, int(form.get("alerting_interval_seconds") or 30)))
            ),
            "alerting_webhook_url": str(form.get("alerting_webhook_url") or "").strip(),
            "alerting_secret": alerting_secret,
            "alerting_min_healthy_cn": str(
                max(0, min(100, int(form.get("alerting_min_healthy_cn") or 0)))
            ),
            "alerting_min_healthy_global": str(
                max(0, min(100, int(form.get("alerting_min_healthy_global") or 0)))
            ),
            "alerting_breaker_threshold": str(
                max(0, min(100, int(form.get("alerting_breaker_threshold") or 0)))
            ),
            "alerting_for_ticks": str(max(1, min(100, int(form.get("alerting_for_ticks") or 3)))),
            "alerting_clear_ticks": str(max(1, min(100, int(form.get("alerting_clear_ticks") or 3)))),
            "alerting_startup_grace_seconds": str(
                max(0, min(3600, int(form.get("alerting_startup_grace_seconds") or 0)))
            ),
            "alerting_timeout_seconds": str(
                max(1.0, min(60.0, float(form.get("alerting_timeout_seconds") or 5)))
            ),
            "alerting_send_resolve": "1" if form.get("alerting_send_resolve") else "0",
            "admin_audit_enabled": audit_enabled,
            "admin_audit_file": audit_file,
        }
        for key, value in values.items():
            ctx.db.set_setting(key, value)
        return _redirect("/admin/settings", "可观测设置已保存")
    except Exception as exc:
        return _redirect("/admin/settings", str(exc), ok=False)


@router.post("/settings/password")
async def settings_password(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        old = str(form.get("old_password") or "")
        new = str(form.get("new_password") or "")
        confirm = str(form.get("confirm_password") or "")
        if new != confirm:
            raise ValueError("两次输入的新密码不一致")
        if not ctx.admin_auth.change_password(int(admin["admin_id"]), old, new):
            raise ValueError("原密码错误")
        response = RedirectResponse("/admin/login?msg=%E5%AF%86%E7%A0%81%E5%B7%B2%E4%BF%AE%E6%94%B9%EF%BC%8C%E8%AF%B7%E9%87%8D%E6%96%B0%E7%99%BB%E5%BD%95", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response
    except Exception as exc:
        return _redirect("/admin/settings", str(exc), ok=False)


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    return _render(
        request,
        "api_keys.html",
        admin,
        keys=ctx.api_keys.list_keys(),
    )


@router.post("/settings/api-key")
async def settings_api_key(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        for key in ctx.api_keys.list_keys():
            ctx.api_keys.revoke_key(int(key["id"]))
        ctx.api_keys.create_key("default")
        return _redirect("/admin/api-keys", "API Key 已重新生成")
    except Exception as exc:
        return _redirect("/admin/api-keys", str(exc), ok=False)


@router.post("/settings/api-key/add")
async def settings_api_key_add(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        form = await _csrf_form(request, admin)
        name = str(form.get("name") or "client").strip() or "client"
        ctx.api_keys.create_key(name)
        return _redirect("/admin/api-keys", "已新增 API Key")
    except Exception as exc:
        return _redirect("/admin/api-keys", str(exc), ok=False)


@router.post("/settings/api-key/{key_id}/revoke")
async def settings_api_key_revoke(request: Request, key_id: int, admin: dict = Depends(require_admin)):
    ctx = state(request)
    try:
        await _csrf_form(request, admin)
        ctx.api_keys.revoke_key(key_id)
        return _redirect("/admin/api-keys", "API Key 已停用")
    except Exception as exc:
        return _redirect("/admin/api-keys", str(exc), ok=False)


@router.get("/api/status")
async def api_status(request: Request, admin: dict = Depends(require_admin)):
    ctx = state(request)
    return JSONResponse(
        {
            "snapshot": ctx.accounts.snapshot(),
            "accounts": [
                {
                    "id": a["id"],
                    "name": a["name"],
                    "enabled": bool(a["enabled"]),
                    "credits": a["credits"],
                    "last_error": a["last_error"],
                }
                for a in ctx.accounts.list_accounts()
            ],
        }
    )


# --------------------------------------------------------------------- Trae callback
@router.get("/authorize", response_class=HTMLResponse)
@router.get("/trae/callback", response_class=HTMLResponse)
async def trae_callback(request: Request):
    ctx = state(request)
    ip = client_ip(request)
    if not callback_limiter.allowed(ip):
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>AIAll API 登录失败</title>"
            "<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;"
            "place-items:center;height:100vh;margin:0}.card{background:#111c33;padding:32px 40px;"
            "border-radius:18px;border:1px solid #23324f;max-width:520px}h1{color:#f87171}</style>"
            "<div class='card'><h1>尝试过于频繁</h1><p>请 5 分钟后再试，或回到管理台重新生成登录链接。</p></div>",
            status_code=429,
        )
    try:
        url = str(request.url)
        account = await ctx.accounts.add_from_callback(url)
        callback_limiter.reset(ip)
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>AIAll API 登录成功</title>"
            "<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;"
            "place-items:center;height:100vh;margin:0}.card{background:#111c33;padding:32px 40px;"
            "border-radius:18px;border:1px solid #23324f;max-width:520px}h1{color:#38bdf8}"
            "a{color:#7dd3fc}</style><div class='card'><h1>登录成功</h1>"
            f"<p>账号 <b>{account['name']}</b> 已保存到 AIAll API。</p>"
            "<p>可以关闭此页面，返回管理台继续操作。</p>"
            "<p><a href='/admin/accounts'>进入账号管理</a></p></div>"
        )
    except Exception as exc:
        callback_limiter.fail(ip)
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><title>AIAll API 登录失败</title>"
            "<style>body{font-family:system-ui;background:#0f172a;color:#e2e8f0;display:grid;"
            "place-items:center;height:100vh;margin:0}.card{background:#111c33;padding:32px 40px;"
            "border-radius:18px;border:1px solid #23324f;max-width:520px}h1{color:#f87171}</style>"
            f"<div class='card'><h1>登录未完成</h1><p>{exc}</p>"
            "<p>请回到管理台复制回调链接并粘贴，或重新生成登录链接。</p></div>",
            status_code=400,
        )
