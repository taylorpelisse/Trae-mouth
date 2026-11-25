from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Optional

from .db import Database
from .security import (
    hash_password,
    hash_token,
    new_token,
    now_ts,
    verify_password,
)


class AdminStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def has_admin(self) -> bool:
        with self.db.read() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM admin_users").fetchone()
        return bool(row and row["n"])

    def create_admin(self, username: str, password: str) -> str:
        username = (username or "admin").strip() or "admin"
        if self.has_admin():
            raise ValueError("admin already exists")
        user_id = new_token(16)
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO admin_users(id, username, password_hash, created_at) "
                "VALUES(?,?,?,?)",
                (user_id, username, hash_password(password), now),
            )
        return user_id

    def verify(self, username: str, password: str) -> Optional[str]:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT id, password_hash FROM admin_users WHERE username = ?",
                ((username or "").strip(),),
            ).fetchone()
        if not row:
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return row["id"]

    def create_session(self, user_id: str, ttl: int = 7 * 86400) -> str:
        token = new_token(32)
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO admin_sessions(token_hash, user_id, created_at, "
                "expires_at, last_seen_at) VALUES(?,?,?,?,?)",
                (hash_token(token), user_id, now, now + ttl, now),
            )
            conn.execute(
                "DELETE FROM admin_sessions WHERE expires_at < ?", (now,)
            )
        return token

    def get_session(self, token: str) -> Optional[str]:
        if not token:
            return None
        now = now_ts()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT user_id, last_seen_at FROM admin_sessions "
                "WHERE token_hash = ? AND expires_at > ?",
                (hash_token(token), now),
            ).fetchone()
            if not row:
                return None
            if now - int(row["last_seen_at"] or 0) > 300:
                conn.execute(
                    "UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                    (now, hash_token(token)),
                )
            return str(row["user_id"])

    def delete_session(self, token: str) -> None:
        if not token:
            return
        with self.db.tx() as conn:
            conn.execute(
                "DELETE FROM admin_sessions WHERE token_hash = ?",
                (hash_token(token),),
            )

    def username(self, user_id: str) -> str:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT username FROM admin_users WHERE id = ?", (user_id,)
            ).fetchone()
        return str(row["username"]) if row else ""

    def audit(self, user_id: str, action: str, detail: str = "") -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO admin_audit(user_id, username, action, detail, created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    user_id,
                    self.username(user_id),
                    str(action)[:80],
                    str(detail)[:500],
                    now_ts(),
                ),
            )
            conn.execute(
                "DELETE FROM admin_audit WHERE id NOT IN "
                "(SELECT id FROM admin_audit ORDER BY id DESC LIMIT 5000)"
            )

    def audit_events(self, limit: int = 100) -> list[dict[str, object]]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def change_password(self, user_id: str, current: str, new: str) -> bool:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT password_hash FROM admin_users WHERE id = ?", (user_id,)
            ).fetchone()
        if not row or not verify_password(current, row["password_hash"]):
            return False
        new_hash = hash_password(new)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE admin_users SET password_hash = ? WHERE id = ?",
                (new_hash, user_id),
            )
        return True


class LoginLimiter:
    """Simple in-process failure counter to slow down brute force attempts."""

    def __init__(self, limit: int = 5, window: int = 300) -> None:
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque[float]:
        bucket = self._failures[key]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        return bucket

    def allow(self, key: str) -> bool:
        now = time.time()
        bucket = self._trim(key, now)
        return len(bucket) < self.limit

    def fail(self, key: str) -> None:
        now = time.time()
        bucket = self._trim(key, now)
        bucket.append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
