from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from ..db import Database
from ..security import SecretBox, hash_token, new_token, now_ts
from .models import (
    AccountProfile,
    DeviceProfile,
    ImportedAccount,
    TokenBundle,
    normalize_device,
)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUS_COOLDOWN = "cooldown"
STATUS_RELOGIN = "relogin_required"
STATUS_INVALID = "invalid"


@dataclass
class AccountRecord:
    id: str
    user_id: str
    label: str
    note: str
    username: str
    avatar_url: str
    edition: str
    host: str
    client_id: str
    plan: str
    status: str
    status_reason: str
    cooldown_until: Optional[int]
    concurrency_limit: int
    last_used_at: Optional[int]
    last_refreshed_at: Optional[int]
    last_error: str
    source: str
    created_at: int
    updated_at: int
    token_expires_at: Optional[int] = None
    refresh_expires_at: Optional[int] = None
    has_refresh_token: bool = False

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "label": self.label,
            "note": self.note,
            "username": self.username,
            "avatar_url": self.avatar_url,
            "edition": self.edition,
            "host": self.host,
            "client_id": self.client_id,
            "plan": self.plan,
            "status": self.status,
            "status_reason": self.status_reason,
            "cooldown_until": self.cooldown_until,
            "concurrency_limit": self.concurrency_limit,
            "last_used_at": self.last_used_at,
            "last_refreshed_at": self.last_refreshed_at,
            "last_error": self.last_error,
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "token_expires_at": self.token_expires_at,
            "refresh_expires_at": self.refresh_expires_at,
            "has_refresh_token": self.has_refresh_token,
        }


def _record_from_row(row: sqlite3.Row) -> AccountRecord:
    keys = row.keys()
    return AccountRecord(
        id=row["id"],
        user_id=row["user_id"],
        label=row["label"],
        note=row["note"] if "note" in keys else "",
        username=row["username"],
        avatar_url=row["avatar_url"],
        edition=row["edition"],
        host=row["host"],
        client_id=row["client_id"],
        plan=row["plan"],
        status=row["status"],
        status_reason=row["status_reason"],
        cooldown_until=row["cooldown_until"],
        concurrency_limit=row["concurrency_limit"],
        last_used_at=row["last_used_at"],
        last_refreshed_at=row["last_refreshed_at"],
        last_error=row["last_error"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        token_expires_at=row["token_expires_at"] if "token_expires_at" in keys else None,
        refresh_expires_at=(
            row["refresh_expires_at"] if "refresh_expires_at" in keys else None
        ),
        has_refresh_token=bool(row["has_refresh_token"]) if "has_refresh_token" in keys else False,
    )


class AccountStore:
    def __init__(
        self,
        db: Database,
        secrets: SecretBox,
        default_concurrency: int = 3,
    ) -> None:
        self.db = db
        self.secrets = secrets
        self.default_concurrency = max(1, int(default_concurrency))

    # -- reads ---------------------------------------------------------
    def list(self) -> list[AccountRecord]:
        with self.db.read() as conn:
            rows = conn.execute(
                """
                SELECT a.*, s.token_expires_at, s.refresh_expires_at,
                       CASE WHEN COALESCE(s.refresh_token_enc, '') <> '' THEN 1 ELSE 0 END
                           AS has_refresh_token
                FROM accounts a
                LEFT JOIN account_secrets s ON s.account_id = a.id
                ORDER BY a.created_at ASC
                """
            ).fetchall()
        return [_record_from_row(row) for row in rows]

    def get(self, account_id: str) -> Optional[AccountRecord]:
        with self.db.read() as conn:
            row = conn.execute(
                """
                SELECT a.*, s.token_expires_at, s.refresh_expires_at,
                       CASE WHEN COALESCE(s.refresh_token_enc, '') <> '' THEN 1 ELSE 0 END
                           AS has_refresh_token
                FROM accounts a
                LEFT JOIN account_secrets s ON s.account_id = a.id
                WHERE a.id = ?
                """,
                (account_id,),
            ).fetchone()
        return _record_from_row(row) if row else None

    def conversation_account(
        self, conversation_key: str, max_age: int = 0
    ) -> Optional[str]:
        if not conversation_key:
            return None
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT account_id, updated_at FROM conversation_accounts "
                "WHERE conversation_key = ?",
                (conversation_key,),
            ).fetchone()
        if not row:
            return None
        if max_age > 0 and now_ts() - int(row["updated_at"]) > max_age:
            self.unbind_conversation_account(conversation_key)
            return None
        return str(row["account_id"])

    def bind_conversation_account(
        self, conversation_key: str, account_id: str, *, purge_before: int = 0
    ) -> None:
        if not conversation_key or not account_id:
            return
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO conversation_accounts(conversation_key, account_id, updated_at) "
                "VALUES(?,?,?) ON CONFLICT(conversation_key) DO UPDATE SET "
                "account_id=excluded.account_id, updated_at=excluded.updated_at",
                (conversation_key, account_id, now),
            )
            if purge_before > 0:
                conn.execute(
                    "DELETE FROM conversation_accounts WHERE updated_at < ?",
                    (purge_before,),
                )

    def unbind_conversation_account(self, conversation_key: str) -> None:
        if not conversation_key:
            return
        with self.db.tx() as conn:
            conn.execute(
                "DELETE FROM conversation_accounts WHERE conversation_key = ?",
                (conversation_key,),
            )

    def get_secrets(self, account_id: str) -> TokenBundle:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM account_secrets WHERE account_id = ?", (account_id,)
            ).fetchone()
        if not row:
            return TokenBundle()
        return TokenBundle(
            token=self.secrets.decrypt(row["token_enc"]),
            refresh_token=self.secrets.decrypt(row["refresh_token_enc"]),
            token_expires_at=row["token_expires_at"],
            refresh_expires_at=row["refresh_expires_at"],
        )

    def get_device(self, account_id: str) -> Optional[DeviceProfile]:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM account_devices WHERE account_id = ?", (account_id,)
            ).fetchone()
        if not row:
            return None
        return DeviceProfile.from_dict({key: row[key] for key in row.keys()})

    def get_device_private_key(self, account_id: str) -> str:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT private_key_enc FROM account_device_keys WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if not row:
            return ""
        return self.secrets.decrypt(row["private_key_enc"])

    def events(self, account_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT id, event, detail, created_at FROM account_events "
                "WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                (account_id, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    # -- writes --------------------------------------------------------
    def add_imported(
        self,
        imported: ImportedAccount,
        *,
        label: str = "",
        actor: str = "system",
    ) -> tuple[AccountRecord, bool]:
        profile = imported.profile
        if not profile.user_id:
            raise ValueError("account has no user id")
        now = now_ts()
        with self.db.tx() as conn:
            existing = conn.execute(
                "SELECT * FROM accounts WHERE user_id = ?", (profile.user_id,)
            ).fetchone()
            if existing:
                account_id = existing["id"]
                fields: dict[str, Any] = {
                    "updated_at": now,
                    "username": profile.username or existing["username"],
                    "avatar_url": profile.avatar_url or existing["avatar_url"],
                    "edition": profile.edition or existing["edition"],
                    "host": profile.host or existing["host"],
                    "client_id": profile.client_id or existing["client_id"],
                    "plan": profile.plan or existing["plan"],
                    "source": profile.source or existing["source"],
                    "last_error": "",
                }
                if label:
                    fields["label"] = label[:120]
                if existing["status"] in {STATUS_RELOGIN, STATUS_COOLDOWN, STATUS_INVALID}:
                    fields["status"] = STATUS_ACTIVE
                    fields["status_reason"] = ""
                    fields["cooldown_until"] = None
                    fields["last_error"] = ""
                assignments = ", ".join(f"{key} = ?" for key in fields)
                conn.execute(
                    f"UPDATE accounts SET {assignments} WHERE id = ?",
                    (*fields.values(), account_id),
                )
                self._write_secrets(conn, account_id, imported.tokens, now)
                # Keep the original device fingerprint when one exists.
                device_row = conn.execute(
                    "SELECT account_id FROM account_devices WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                if not device_row:
                    self._write_device(conn, account_id, imported.device, now)
                self._log(conn, account_id, "account_reimported", actor)
                created = False
            else:
                account_id = new_token(18)
                conn.execute(
                    """
                    INSERT INTO accounts(
                        id, user_id, label, username, avatar_url, edition, host,
                        client_id, plan, status, status_reason, cooldown_until,
                        concurrency_limit, last_used_at, last_refreshed_at,
                        last_error, source, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        account_id,
                        profile.user_id,
                        label[:120],
                        profile.username,
                        profile.avatar_url,
                        profile.edition or "cn",
                        profile.host or "https://api.trae.cn",
                        profile.client_id,
                        profile.plan,
                        STATUS_ACTIVE,
                        "",
                        None,
                        self.default_concurrency,
                        None,
                        None,
                        "",
                        profile.source,
                        now,
                        now,
                    ),
                )
                self._write_secrets(conn, account_id, imported.tokens, now)
                self._write_device(conn, account_id, imported.device, now)
                self._log(conn, account_id, "account_imported", actor)
                created = True
        record = self.get(account_id)
        if record is None:
            raise RuntimeError("account disappeared after write")
        return record, created

    def add_manual(
        self,
        *,
        token: str,
        user_id: str,
        refresh_token: str = "",
        host: str = "https://api.trae.cn",
        client_id: str = "",
        label: str = "",
        username: str = "",
        token_expires_at: Optional[int] = None,
        refresh_expires_at: Optional[int] = None,
        device: DeviceProfile | None = None,
        actor: str = "admin",
    ) -> tuple[AccountRecord, bool]:
        imported = ImportedAccount(
            profile=AccountProfile(
                user_id=user_id,
                username=username,
                host=host,
                client_id=client_id,
                source="manual",
            ),
            tokens=TokenBundle(
                token=token,
                refresh_token=refresh_token,
                token_expires_at=token_expires_at,
                refresh_expires_at=refresh_expires_at,
            ),
            device=normalize_device(device.to_dict() if device else None),
        )
        return self.add_imported(imported, label=label, actor=actor)

    def update_tokens(
        self,
        account_id: str,
        tokens: TokenBundle,
        *,
        username: str = "",
        plan: str = "",
    ) -> None:
        now = now_ts()
        with self.db.tx() as conn:
            self._write_secrets(conn, account_id, tokens, now)
            fields = ["last_refreshed_at = ?", "updated_at = ?"]
            values: list[Any] = [now, now]
            if username:
                fields.append("username = ?")
                values.append(username)
            if plan:
                fields.append("plan = ?")
                values.append(plan)
            values.append(account_id)
            conn.execute(
                f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?",
                tuple(values),
            )
            self._log(conn, account_id, "token_refreshed", "")

    def update_profile(
        self,
        account_id: str,
        *,
        username: str = "",
        avatar_url: str = "",
        plan: str = "",
    ) -> None:
        now = now_ts()
        updates: dict[str, Any] = {"updated_at": now}
        if username:
            updates["username"] = username
        if avatar_url:
            updates["avatar_url"] = avatar_url
        if plan:
            updates["plan"] = plan
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE accounts SET {assignments} WHERE id = ?",
                (*updates.values(), account_id),
            )

    def set_status(
        self,
        account_id: str,
        status: str,
        reason: str = "",
        *,
        cooldown_until: Optional[int] = None,
        actor: str = "system",
    ) -> None:
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE accounts SET status = ?, status_reason = ?, "
                "cooldown_until = ?, updated_at = ? WHERE id = ?",
                (status, reason, cooldown_until, now, account_id),
            )
            self._log(conn, account_id, f"status_{status}", reason or actor)

    def mark_error(self, account_id: str, message: str) -> None:
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE accounts SET last_error = ?, updated_at = ? WHERE id = ?",
                (message[:1000], now, account_id),
            )

    def set_label(self, account_id: str, label: str) -> None:
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE accounts SET label = ?, updated_at = ? WHERE id = ?",
                (label[:120], now, account_id),
            )

    def set_note(self, account_id: str, note: str) -> None:
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE accounts SET note = ?, updated_at = ? WHERE id = ?",
                (note[:200], now, account_id),
            )

    def set_concurrency(self, account_id: str, value: int) -> None:
        value = max(1, min(int(value), 32))
        now = now_ts()
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE accounts SET concurrency_limit = ?, updated_at = ? WHERE id = ?",
                (value, now, account_id),
            )

    def set_device(self, account_id: str, device: DeviceProfile) -> None:
        now = now_ts()
        with self.db.tx() as conn:
            self._write_device(conn, account_id, device, now)

    def delete(self, account_id: str, actor: str = "admin") -> None:
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT user_id FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if not row:
                return
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            self._log(conn, None, "account_deleted", f"{account_id}:{row['user_id']} {actor}")

    def log_event(self, account_id: Optional[str], event: str, detail: str = "") -> None:
        with self.db.tx() as conn:
            self._log(conn, account_id, event, detail)

    # -- internals -----------------------------------------------------
    def _write_secrets(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        tokens: TokenBundle,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO account_secrets(
                account_id, token_enc, refresh_token_enc, token_expires_at,
                refresh_expires_at, updated_at
            ) VALUES(?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                token_enc=excluded.token_enc,
                refresh_token_enc=excluded.refresh_token_enc,
                token_expires_at=excluded.token_expires_at,
                refresh_expires_at=excluded.refresh_expires_at,
                updated_at=excluded.updated_at
            """,
            (
                account_id,
                self.secrets.encrypt(tokens.token),
                self.secrets.encrypt(tokens.refresh_token),
                tokens.token_expires_at,
                tokens.refresh_expires_at,
                now,
            ),
        )

    def _write_device(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        device: DeviceProfile,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO account_devices(
                account_id, machine_id, device_id, brand, cpu, device_type,
                os_version, app_version, ide_version, ide_version_code, web_id,
                device_name, device_model, device_manufacturer, cpu_manufacturer,
                cpu_brand, cpu_model, cpu_family, cpu_stepping, cpu_speed,
                cpu_count, memory, os_name, os_release, os_kernel, platform,
                platform_code, device_class, screen_width, screen_height,
                language, public_key, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                machine_id=excluded.machine_id,
                device_id=excluded.device_id,
                brand=excluded.brand,
                cpu=excluded.cpu,
                device_type=excluded.device_type,
                os_version=excluded.os_version,
                app_version=excluded.app_version,
                ide_version=excluded.ide_version,
                ide_version_code=excluded.ide_version_code,
                web_id=excluded.web_id,
                device_name=excluded.device_name,
                device_model=excluded.device_model,
                device_manufacturer=excluded.device_manufacturer,
                cpu_manufacturer=excluded.cpu_manufacturer,
                cpu_brand=excluded.cpu_brand,
                cpu_model=excluded.cpu_model,
                cpu_family=excluded.cpu_family,
                cpu_stepping=excluded.cpu_stepping,
                cpu_speed=excluded.cpu_speed,
                cpu_count=excluded.cpu_count,
                memory=excluded.memory,
                os_name=excluded.os_name,
                os_release=excluded.os_release,
                os_kernel=excluded.os_kernel,
                platform=excluded.platform,
                platform_code=excluded.platform_code,
                device_class=excluded.device_class,
                screen_width=excluded.screen_width,
                screen_height=excluded.screen_height,
                language=excluded.language,
                public_key=excluded.public_key,
                updated_at=excluded.updated_at
            """,
            (
                account_id,
                device.machine_id,
                device.device_id,
                device.brand,
                device.cpu,
                device.device_type,
                device.os_version,
                device.app_version,
                device.ide_version,
                device.ide_version_code,
                device.web_id,
                device.device_name,
                device.device_model,
                device.device_manufacturer,
                device.cpu_manufacturer,
                device.cpu_brand,
                device.cpu_model,
                device.cpu_family,
                device.cpu_stepping,
                device.cpu_speed,
                device.cpu_count,
                device.memory,
                device.os_name,
                device.os_release,
                device.os_kernel,
                device.platform,
                device.platform_code,
                device.device_class,
                device.screen_width,
                device.screen_height,
                device.language,
                device.public_key,
                now,
            ),
        )
        if device.private_key_pem:
            conn.execute(
                """
                INSERT INTO account_device_keys(account_id, private_key_enc, created_at)
                VALUES(?,?,?)
                ON CONFLICT(account_id) DO UPDATE SET
                    private_key_enc=excluded.private_key_enc,
                    created_at=excluded.created_at
                """,
                (
                    account_id,
                    self.secrets.encrypt(device.private_key_pem),
                    now,
                ),
            )

    def _log(
        self,
        conn: sqlite3.Connection,
        account_id: Optional[str],
        event: str,
        detail: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO account_events(account_id, event, detail, created_at) "
            "VALUES(?,?,?,?)",
            (account_id, event, detail[:2000], now_ts()),
        )

    # -- import codes --------------------------------------------------
    def create_import_code(
        self, created_by: str = "admin", ttl: int = 600
    ) -> tuple[str, int]:
        code = "tmimp_" + new_token(24)
        now = now_ts()
        expires_at = now + max(30, int(ttl))
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE import_codes SET used_at = ? WHERE used_at IS NULL",
                (now,),
            )
            conn.execute(
                "INSERT INTO import_codes(code_hash, created_by, created_at, "
                "expires_at, used_at) VALUES(?,?,?,?,NULL)",
                (hash_token(code), created_by, now, expires_at),
            )
            conn.execute(
                "DELETE FROM import_codes WHERE expires_at < ?", (now - 86400,)
            )
        return code, expires_at

    def consume_import_code(self, code: str) -> bool:
        if not code:
            return False
        now = now_ts()
        with self.db.tx() as conn:
            cursor = conn.execute(
                "UPDATE import_codes SET used_at = ? "
                "WHERE code_hash = ? AND used_at IS NULL AND expires_at > ?",
                (now, hash_token(code), now),
            )
            return cursor.rowcount == 1

    def validate_import_code(self, code: str) -> bool:
        return self.check_import_code(code) is not None

    def check_import_code(self, code: str) -> Optional[int]:
        if not code:
            return None
        now = now_ts()
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT expires_at FROM import_codes WHERE code_hash = ? "
                "AND used_at IS NULL AND expires_at > ?",
                (hash_token(code), now),
            ).fetchone()
        return int(row["expires_at"]) if row else None
