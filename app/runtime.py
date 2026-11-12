from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .bootstrap import bootstrap
from .config import Settings, settings as default_settings
from .db import Database
from .security import SecretBox
from .services.accounts import AccountService
from .services.alerting import AlertingService
from .services.audit import AdminAuditService
from .services.auth import AdminAuthService
from .services.chat import ChatService
from .services.observability import ObservabilityService
from .services.platforms import PlatformService
from .services.pool import AccountPool
from .services.providers import ProviderService
from .services.router import RouterService
from .services.stats import StatsService
from .services.virtuals import VirtualService
from .services.scheduler import Scheduler
from .services.usage import ApiKeyService, UsageService
from .upstream.client import TraeClient

log = logging.getLogger("aiall.runtime")


@dataclass
class AppState:
    settings: Settings
    db: Database
    box: SecretBox
    http: httpx.AsyncClient
    client: TraeClient
    accounts: AccountService
    api_keys: ApiKeyService
    usage: UsageService
    admin_auth: AdminAuthService
    chat: ChatService
    providers: ProviderService
    platforms: PlatformService
    pool: AccountPool
    virtuals: VirtualService
    router: RouterService
    stats: StatsService
    scheduler: Scheduler
    observability: ObservabilityService
    alerting: AlertingService
    audit: AdminAuditService

    async def close(self) -> None:
        await self.scheduler.stop()
        await self.alerting.stop()
        await self.http.aclose()
        self.db.close()


async def build_state(settings: Settings | None = None) -> AppState:
    settings = settings or default_settings
    settings.ensure_dirs()
    db = Database(settings.db_path)
    box = bootstrap(settings, db)
    http = httpx.AsyncClient(
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        timeout=httpx.Timeout(connect=20.0, read=settings.upstream_timeout, write=30.0, pool=20.0),
        headers={"Accept-Encoding": "identity"},
    )
    client = TraeClient(settings, http)
    accounts = AccountService(settings, db, box, client)
    api_keys = ApiKeyService(db, box)
    usage = UsageService(db)
    admin_auth = AdminAuthService(db, settings.session_hours)
    chat = ChatService(settings, db, accounts, usage, client)
    providers = ProviderService(settings, db, box, http)
    pool = AccountPool(db)
    platforms = PlatformService(settings, db, providers)
    virtuals = VirtualService(db)
    router = RouterService(settings, db, accounts, chat, usage, providers, virtuals, http, pool)
    stats = StatsService(db, accounts, providers, pool, virtuals)
    scheduler = Scheduler(settings, db, accounts, usage, platforms, providers=providers, http=http)
    observability = ObservabilityService(db)
    alerting = AlertingService(settings, db, observability, http)
    audit = AdminAuditService(db)
    return AppState(
        settings=settings,
        db=db,
        box=box,
        http=http,
        client=client,
        accounts=accounts,
        api_keys=api_keys,
        usage=usage,
        admin_auth=admin_auth,
        chat=chat,
        providers=providers,
        platforms=platforms,
        pool=pool,
        virtuals=virtuals,
        router=router,
        stats=stats,
        scheduler=scheduler,
        observability=observability,
        alerting=alerting,
        audit=audit,
    )
