from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is optional at import time
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent.parent

if load_dotenv:
    load_dotenv(BASE_DIR / ".env", override=False)


def _env(name: str, default: str = "") -> str:
    """读取环境变量；TRAEAPI_* 为历史前缀，AIALL_* 优先。"""
    value = None
    if name.startswith("TRAEAPI_"):
        value = os.getenv("AIALL_" + name[len("TRAEAPI_"):])
    if value is None:
        value = os.getenv(name)
    return (value or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name, "1" if default else "0").lower()
    return value in {"1", "true", "yes", "on", "y"}


@dataclass
class Settings:
    app_name: str = field(default_factory=lambda: _env("AIALL_APP_NAME", "AIAll API"))
    version: str = "0.01"
    host: str = field(default_factory=lambda: _env("TRAEAPI_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("TRAEAPI_PORT", 2999))
    data_dir: Path = field(
        default_factory=lambda: Path(_env("TRAEAPI_DATA_DIR", str(BASE_DIR / "data"))).resolve()
    )
    admin_username: str = field(default_factory=lambda: _env("TRAEAPI_ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: _env("TRAEAPI_ADMIN_PASSWORD", ""))
    session_hours: int = field(default_factory=lambda: _env_int("TRAEAPI_SESSION_HOURS", 72))
    allow_any_key: bool = field(default_factory=lambda: _env_bool("TRAEAPI_ALLOW_ANY_KEY", False))
    default_model: str = field(default_factory=lambda: _env("TRAEAPI_DEFAULT_MODEL", "deepseek-v4.1-flash"))
    checkin_time: str = field(default_factory=lambda: _env("TRAEAPI_CHECKIN_TIME", "10:05"))
    auto_checkin: bool = field(default_factory=lambda: _env_bool("TRAEAPI_AUTO_CHECKIN", True))
    log_retention_days: int = field(default_factory=lambda: _env_int("TRAEAPI_LOG_RETENTION_DAYS", 30))
    max_concurrent_per_account: int = field(
        default_factory=lambda: max(1, min(8, _env_int("TRAEAPI_MAX_CONCURRENT_PER_ACCOUNT", 2)))
    )
    upstream_timeout: float = field(default_factory=lambda: float(_env("TRAEAPI_UPSTREAM_TIMEOUT", "180")))
    first_event_timeout: float = field(
        default_factory=lambda: float(_env("TRAEAPI_FIRST_EVENT_TIMEOUT", "60"))
    )
    ide_version: str = field(default_factory=lambda: _env("TRAEAPI_IDE_VERSION", "1.107.1"))
    ide_version_code: str = field(default_factory=lambda: _env("TRAEAPI_IDE_VERSION_CODE", "20261110"))
    app_id: str = field(
        default_factory=lambda: _env("TRAEAPI_APP_ID", "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8")
    )
    client_id: str = field(default_factory=lambda: _env("TRAEAPI_CLIENT_ID", "ono9krqynydwx5"))
    oauth_host: str = field(default_factory=lambda: _env("TRAEAPI_OAUTH_HOST", "https://api.trae.cn"))
    agent_host: str = field(
        default_factory=lambda: _env("TRAEAPI_AGENT_HOST", "https://trae-api-cn.mchost.guru")
    )
    ug_host: str = field(default_factory=lambda: _env("TRAEAPI_UG_HOST", "https://api.trae.cn"))
    console_host: str = field(default_factory=lambda: _env("TRAEAPI_CONSOLE_HOST", "https://www.trae.cn"))
    trae_callback_port: int = field(default_factory=lambda: int(_env("TRAEAPI_TRAE_CALLBACK_PORT", "57321")))
    disable_scheduler: bool = field(
        default_factory=lambda: _env_bool("TRAEAPI_DISABLE_SCHEDULER", False)
    )
    trust_proxy: bool = field(default_factory=lambda: _env_bool("TRAEAPI_TRUST_PROXY", True))

    @property
    def db_path(self) -> Path:
        # 文件名保持 traeapi.db 以兼容已有部署数据，可用 AIALL_DB_FILE 改名
        return self.data_dir / (_env("AIALL_DB_FILE", "traeapi.db") or "traeapi.db")

    @property
    def secret_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def initial_admin_path(self) -> Path:
        return self.data_dir / "initial_admin.txt"

    @property
    def initial_api_key_path(self) -> Path:
        return self.data_dir / "initial_api_key.txt"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_dir, 0o700)
        except OSError:
            pass


settings = Settings()


def new_secret_key() -> str:
    return secrets.token_urlsafe(48)
