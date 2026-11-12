from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings, new_secret_key


PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64 + "=" * (-len(salt_b64) % 4))
        expected = base64.urlsafe_b64decode(digest_b64 + "=" * (-len(digest_b64) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_api_key() -> str:
    return "sk-trae-" + secrets.token_urlsafe(32).replace("-", "").replace("_", "")[:40]


def safe_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def load_or_create_secret(settings: Settings) -> str:
    settings.ensure_dirs()
    env_secret = (os.getenv("AIALL_SECRET_KEY") or os.getenv("TRAEAPI_SECRET_KEY") or "").strip()
    if env_secret:
        return env_secret
    path = settings.secret_path
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = new_secret_key()
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


class SecretBox:
    """Fernet encryption for upstream credentials stored in SQLite."""

    def __init__(self, secret: str):
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, value: Optional[str]) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: Optional[str]) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return ""


class LoginRateLimiter:
    """Tiny in-memory limiter; good enough for a single-process BT deployment."""

    def __init__(self, max_attempts: int = 8, window_seconds: int = 600):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def allowed(self, key: str) -> bool:
        now = time.time()
        bucket = [t for t in self._attempts.get(key, []) if now - t < self.window_seconds]
        self._attempts[key] = bucket
        return len(bucket) < self.max_attempts

    def fail(self, key: str) -> None:
        self._attempts.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)
