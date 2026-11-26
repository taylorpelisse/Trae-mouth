from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    edition TEXT NOT NULL DEFAULT 'cn',
    host TEXT NOT NULL DEFAULT 'https://api.trae.cn',
    client_id TEXT NOT NULL DEFAULT '',
    plan TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    status_reason TEXT NOT NULL DEFAULT '',
    cooldown_until INTEGER,
    concurrency_limit INTEGER NOT NULL DEFAULT 2,
    last_used_at INTEGER,
    last_refreshed_at INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_secrets (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    token_enc TEXT NOT NULL,
    refresh_token_enc TEXT NOT NULL DEFAULT '',
    token_expires_at INTEGER,
    refresh_expires_at INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_devices (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    machine_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    brand TEXT NOT NULL,
    cpu TEXT NOT NULL,
    device_type TEXT NOT NULL,
    os_version TEXT NOT NULL,
    app_version TEXT NOT NULL,
    ide_version TEXT NOT NULL,
    ide_version_code TEXT NOT NULL,
    web_id TEXT NOT NULL DEFAULT '',
    device_name TEXT NOT NULL DEFAULT '',
    device_model TEXT NOT NULL DEFAULT '',
    device_manufacturer TEXT NOT NULL DEFAULT '',
    cpu_manufacturer TEXT NOT NULL DEFAULT '',
    cpu_brand TEXT NOT NULL DEFAULT '',
    cpu_model TEXT NOT NULL DEFAULT '',
    cpu_family TEXT NOT NULL DEFAULT '',
    cpu_stepping TEXT NOT NULL DEFAULT '',
    cpu_speed REAL NOT NULL DEFAULT 0,
    cpu_count INTEGER NOT NULL DEFAULT 0,
    memory INTEGER NOT NULL DEFAULT 0,
    os_name TEXT NOT NULL DEFAULT '',
    os_release TEXT NOT NULL DEFAULT '',
    os_kernel TEXT NOT NULL DEFAULT '',
    platform TEXT NOT NULL DEFAULT '',
    platform_code TEXT NOT NULL DEFAULT '',
    device_class TEXT NOT NULL DEFAULT '',
    screen_width INTEGER NOT NULL DEFAULT 0,
    screen_height INTEGER NOT NULL DEFAULT 0,
    language TEXT NOT NULL DEFAULT '',
    public_key TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_device_keys (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    private_key_enc TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS import_codes (
    code_hash TEXT PRIMARY KEY,
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at INTEGER
);

CREATE TABLE IF NOT EXISTS conversation_accounts (
    conversation_key TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT 'chat,models',
    allowed_ips TEXT NOT NULL DEFAULT '',
    quota_tokens INTEGER NOT NULL DEFAULT 0,
    quota_requests INTEGER NOT NULL DEFAULT 0,
    used_tokens INTEGER NOT NULL DEFAULT 0,
    used_requests INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_used_at INTEGER,
    expires_at INTEGER,
    disabled INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    api_key_id TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT '',
    status INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    first_token_ms INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    source_ip TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT '',
    reasoning_effort TEXT NOT NULL DEFAULT '',
    context_window INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_events_account ON account_events(account_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON admin_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_import_codes_expiry ON import_codes(expires_at);
CREATE INDEX IF NOT EXISTS idx_conversation_accounts_updated
    ON conversation_accounts(updated_at);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_key ON usage_events(api_key_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_events(account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_events(model, created_at DESC);

CREATE TABLE IF NOT EXISTS credit_daily (
    account_id TEXT NOT NULL,
    day TEXT NOT NULL,
    baseline REAL NOT NULL,
    latest REAL NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(account_id, day)
);

CREATE TABLE IF NOT EXISTS credit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_balance (
    account_id TEXT PRIMARY KEY,
    balance REAL NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS model_catalog_snapshot (
    scope TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    account_id TEXT NOT NULL DEFAULT ''
);
"""

DEVICE_COLUMNS = {
    "device_name": "TEXT NOT NULL DEFAULT ''",
    "device_model": "TEXT NOT NULL DEFAULT ''",
    "device_manufacturer": "TEXT NOT NULL DEFAULT ''",
    "cpu_manufacturer": "TEXT NOT NULL DEFAULT ''",
    "cpu_brand": "TEXT NOT NULL DEFAULT ''",
    "cpu_model": "TEXT NOT NULL DEFAULT ''",
    "cpu_family": "TEXT NOT NULL DEFAULT ''",
    "cpu_stepping": "TEXT NOT NULL DEFAULT ''",
    "cpu_speed": "REAL NOT NULL DEFAULT 0",
    "cpu_count": "INTEGER NOT NULL DEFAULT 0",
    "memory": "INTEGER NOT NULL DEFAULT 0",
    "os_name": "TEXT NOT NULL DEFAULT ''",
    "os_release": "TEXT NOT NULL DEFAULT ''",
    "os_kernel": "TEXT NOT NULL DEFAULT ''",
    "platform": "TEXT NOT NULL DEFAULT ''",
    "platform_code": "TEXT NOT NULL DEFAULT ''",
    "device_class": "TEXT NOT NULL DEFAULT ''",
    "screen_width": "INTEGER NOT NULL DEFAULT 0",
    "screen_height": "INTEGER NOT NULL DEFAULT 0",
    "language": "TEXT NOT NULL DEFAULT ''",
    "public_key": "TEXT NOT NULL DEFAULT ''",
}

ACCOUNT_COLUMNS = {
    "note": "TEXT NOT NULL DEFAULT ''",
}

API_KEY_COLUMNS = {
    "note": "TEXT NOT NULL DEFAULT ''",
}

USAGE_COLUMNS = {
    "source_ip": "TEXT NOT NULL DEFAULT ''",
    "client": "TEXT NOT NULL DEFAULT ''",
    "reasoning_effort": "TEXT NOT NULL DEFAULT ''",
    "context_window": "INTEGER NOT NULL DEFAULT 0",
}


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def migrate(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            existing = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(account_devices)").fetchall()
            }
            for name, declaration in DEVICE_COLUMNS.items():
                if name not in existing:
                    conn.execute(
                        f"ALTER TABLE account_devices ADD COLUMN {name} {declaration}"
                    )
            for table, columns in (
                ("accounts", ACCOUNT_COLUMNS),
                ("api_keys", API_KEY_COLUMNS),
                ("usage_events", USAGE_COLUMNS),
            ):
                present = {
                    row["name"]
                    for row in conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                for name, declaration in columns.items():
                    if name not in present:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                        )
            conn.commit()
        finally:
            conn.close()

    def get_setting(self, key: str, default: str = "") -> str:
        with self.read() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        import time

        now = int(time.time())
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (key, value, now),
            )
