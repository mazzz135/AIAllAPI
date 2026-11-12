from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, openai
from .config import Settings, settings as default_settings
from .deps import AdminRedirect, client_ip
from .errors import OpenAIError
from .runtime import build_state
from .services.audit import AdminAuditService, request_fingerprint

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("aiall")


def create_app(custom_settings: Settings | None = None) -> FastAPI:
    cfg = custom_settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = await build_state(cfg)
        app.state.ctx = ctx
        ctx.scheduler.start()
        ctx.alerting.start()
        log.info("%s %s 已启动，数据目录 %s", cfg.app_name, cfg.version, cfg.data_dir)
        try:
            yield
        finally:
            await ctx.close()
            log.info("AIAll API 已停止")

    app = FastAPI(
        title="AIAll API",
        version=cfg.version,
        description="Trae 国服账号的本地 OpenAI 兼容 API",
        lifespan=lifespan,
        # 默认关闭自动文档与 OpenAPI schema，避免公开部署时暴露全部接口清单。
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    @app.middleware("http")
    async def daily_budget_guard(request: Request, call_next):
        """当日积分预算闸：在读请求体之前判定，超限直接 429。"""
        if request.method == "POST" and request.url.path in ("/v1/chat/completions", "/v1/responses"):
            ctx = getattr(request.app.state, "ctx", None)
            if ctx is not None:
                status = ctx.pool.check_budget()
                if not status["allowed"]:
                    return JSONResponse(
                        {
                            "error": {
                                "message": (
                                    f"已达当日积分上限：已用 {status['used']:.2f} / 上限 {status['limit']:.2f}"
                                ),
                                "type": "rate_limit_error",
                                "code": "daily_budget_exceeded",
                            }
                        },
                        status_code=429,
                        headers={"Cache-Control": "no-store"},
                    )
        return await call_next(request)

    @app.middleware("http")
    async def admin_audit(request: Request, call_next):
        """只记录已通过管理员鉴权的写操作；审计失败不影响请求。"""
        ctx = getattr(request.app.state, "ctx", None)
        should_audit = (
            ctx is not None
            and request.url.path.startswith("/admin")
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and ctx.audit.enabled
        )
        admin = None
        if should_audit:
            token = request.cookies.get("aiall_session", "") or request.cookies.get("traeapi_session", "")
            admin = ctx.admin_auth.get_session(token) if token else None
        response = await call_next(request)
        if should_audit and admin:
            action, object_name = AdminAuditService.action_for(request.url.path, request.method)
            action = getattr(request.state, "audit_action", "") or action
            object_name = getattr(request.state, "audit_object", "") or object_name
            reason = getattr(request.state, "audit_reason", "") or ""
            location = response.headers.get("location", "")
            ok = response.status_code < 400 and "ok=0" not in location
            ctx.audit.record(
                action=action,
                object_name=str(object_name),
                status_code=response.status_code,
                ok=ok,
                ip=client_ip(request, ctx.settings.trust_proxy),
                api_key_fp=request_fingerprint(request),
                reason=reason,
                method=request.method,
                path=request.url.path,
            )
        return response

    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
    app.include_router(openai.router)
    app.include_router(admin.router, prefix="/admin")

    @app.get("/")
    async def index():
        return RedirectResponse("/admin", status_code=303)

    @app.exception_handler(OpenAIError)
    async def openai_error_handler(request: Request, exc: OpenAIError):
        return JSONResponse(exc.body(), status_code=exc.status_code)

    @app.exception_handler(AdminRedirect)
    async def admin_redirect_handler(request: Request, exc: AdminRedirect):
        return RedirectResponse("/admin/login", status_code=303)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            {
                "error": {
                    "message": "请求参数校验失败",
                    "type": "invalid_request_error",
                    "code": "validation_error",
                    "details": exc.errors(),
                }
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def generic_handler(request: Request, exc: Exception):
        log.exception("未处理异常 %s %s", request.method, request.url.path)
        if request.url.path.startswith("/v1") or request.url.path in ("/models", "/health"):
            return JSONResponse(
                {"error": {"message": str(exc), "type": "api_error", "code": "internal_error"}},
                status_code=500,
            )
        return JSONResponse({"detail": "服务器内部错误，请查看日志"}, status_code=500)

    return app


app = create_app()
