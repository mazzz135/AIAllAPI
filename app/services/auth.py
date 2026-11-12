from __future__ import annotations

import secrets
import time
from typing import Any, Optional

from ..db import Database
from ..security import hash_password, new_session_token, token_hash, verify_password


class AdminAuthService:
    def __init__(self, db: Database, session_hours: int = 72):
        self.db = db
        self.session_hours = session_hours

    def authenticate(self, username: str, password: str) -> Optional[dict[str, Any]]:
        row = self.db.query_one("SELECT * FROM admins WHERE username=?", (username.strip(),))
        if not row or not verify_password(password, row["password_hash"]):
            return None
        return dict(row)

    def create_session(self, admin_id: int) -> tuple[str, str]:
        token = new_session_token()
        csrf = secrets.token_urlsafe(24)
        now = int(time.time())
        expires = now + self.session_hours * 3600
        self.db.execute(
            "INSERT INTO admin_sessions(token_hash, admin_id, csrf, expires_at, created_at, last_seen_at) "
            "VALUES(?,?,?,?,?,?)",
            (token_hash(token), admin_id, csrf, expires, now, now),
        )
        return token, csrf

    def get_session(self, token: str) -> Optional[dict[str, Any]]:
        if not token:
            return None
        row = self.db.query_one(
            """
            SELECT s.*, a.username FROM admin_sessions s
            JOIN admins a ON a.id=s.admin_id WHERE s.token_hash=?
            """,
            (token_hash(token),),
        )
        if not row:
            return None
        now = int(time.time())
        if int(row["expires_at"]) < now:
            self.db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (row["token_hash"],))
            return None
        self.db.execute(
            "UPDATE admin_sessions SET last_seen_at=? WHERE token_hash=?", (now, row["token_hash"])
        )
        return dict(row)

    def destroy_session(self, token: str) -> None:
        if token:
            self.db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash(token),))

    def change_password(self, admin_id: int, old_password: str, new_password: str) -> bool:
        row = self.db.query_one("SELECT * FROM admins WHERE id=?", (admin_id,))
        if not row or not verify_password(old_password, row["password_hash"]):
            return False
        if len(new_password) < 8:
            raise ValueError("新密码至少 8 位")
        self.db.execute(
            "UPDATE admins SET password_hash=? WHERE id=?", (hash_password(new_password), admin_id)
        )
        self.db.execute("DELETE FROM admin_sessions WHERE admin_id=?", (admin_id,))
        return True
