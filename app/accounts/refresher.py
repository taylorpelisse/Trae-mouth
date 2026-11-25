from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

import httpx

from ..security import now_ts
from . import oauth
from .models import TokenBundle, generate_keypair, normalize_device
from .store import (
    STATUS_ACTIVE,
    STATUS_COOLDOWN,
    STATUS_DISABLED,
    STATUS_INVALID,
    STATUS_RELOGIN,
    AccountRecord,
    AccountStore,
)

logger = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    account_id: str
    ok: bool
    skipped: bool = False
    message: str = ""


class TokenRefresher:
    def __init__(
        self,
        store: AccountStore,
        *,
        refresh_window: int = 1800,
        interval: int = 300,
        transport: Optional[httpx.BaseTransport] = None,
        client_id: str = "ono9krqynydwx5",
        app_version: str = "0.1.7",
        ide_version: str = "3.4.22",
        ide_version_code: str = "20261118",
        use_device_info: bool = True,
    ) -> None:
        self.store = store
        self.refresh_window = max(60, int(refresh_window))
        self.interval = max(30, int(interval))
        self.transport = transport
        self.client_id = client_id
        self.app_version = app_version
        self.ide_version = ide_version
        self.ide_version_code = ide_version_code
        self.use_device_info = use_device_info
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _lock_for(self, account_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(account_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[account_id] = lock
            return lock

    def refresh(self, account_id: str, *, force: bool = False) -> RefreshResult:
        record = self.store.get(account_id)
        if not record:
            return RefreshResult(account_id, False, message="account not found")
        if record.status == STATUS_DISABLED:
            return RefreshResult(account_id, True, skipped=True, message="disabled")

        lock = self._lock_for(account_id)
        acquired = lock.acquire(timeout=45)
        if not acquired:
            return RefreshResult(account_id, False, message="refresh already running")
        try:
            # Re-read after acquiring: another worker may have refreshed already.
            record = self.store.get(account_id)
            if not record:
                return RefreshResult(account_id, False, message="account removed")
            secrets = self.store.get_secrets(account_id)
            now = now_ts()
            if (
                not force
                and secrets.token_expires_at
                and secrets.token_expires_at - now > self.refresh_window
            ):
                return RefreshResult(account_id, True, skipped=True, message="not due")
            if not secrets.refresh_token:
                if secrets.token_expires_at and secrets.token_expires_at > now:
                    return RefreshResult(
                        account_id,
                        True,
                        skipped=True,
                        message="no refresh token; current token still valid",
                    )
                self.store.set_status(
                    account_id,
                    STATUS_RELOGIN,
                    "no refresh token",
                    actor="refresher",
                )
                return RefreshResult(
                    account_id, False, message="refresh token missing; re-login required"
                )

            logger.info("refreshing account %s", account_id)
            device = self.store.get_device(account_id)
            private_key = self.store.get_device_private_key(account_id)
            if device:
                device.private_key_pem = private_key
            if (
                not device
                or not device.device_name
                or not device.device_model
                or not device.public_key
                or not private_key
            ):
                device = normalize_device(
                    device.to_dict() if device else None,
                    app_version=self.app_version,
                    ide_version=self.ide_version,
                    ide_version_code=self.ide_version_code,
                )
                if not device.private_key_pem and private_key and device.public_key:
                    device.private_key_pem = private_key
                if not device.public_key or not device.private_key_pem:
                    device.public_key, device.private_key_pem = generate_keypair()
                self.store.set_device(account_id, device)
                private_key = (
                    device.private_key_pem
                    or self.store.get_device_private_key(account_id)
                )
            try:
                exchanged = oauth.exchange_refresh_token(
                    secrets.refresh_token,
                    client_id=record.client_id or self.client_id,
                    user_id=record.user_id,
                    host=record.host,
                    transport=self.transport,
                    device=device,
                    private_key_pem=private_key,
                    ide_version=self.app_version,
                    use_device_info=self.use_device_info,
                )
            except oauth.ImportError_ as exc:
                message = str(exc)
                auth_failure = any(
                    marker in message
                    for marker in (
                        "HTTP 400",
                        "HTTP 401",
                        "HTTP 403",
                        "invalid_grant",
                        "did not return a token",
                    )
                )
                if "HTTP 429" in message:
                    self.store.set_status(
                        account_id,
                        STATUS_COOLDOWN,
                        message,
                        cooldown_until=now + 300,
                        actor="refresher",
                    )
                elif auth_failure:
                    self.store.set_status(
                        account_id,
                        STATUS_RELOGIN,
                        message,
                        actor="refresher",
                    )
                else:
                    self.store.mark_error(account_id, message)
                    self.store.log_event(account_id, "token_refresh_failed", message)
                return RefreshResult(account_id, False, message=message)

            tokens = TokenBundle(
                token=exchanged["token"],
                refresh_token=exchanged.get("refresh_token") or secrets.refresh_token,
                token_expires_at=exchanged.get("token_expires_at")
                or secrets.token_expires_at,
                refresh_expires_at=exchanged.get("refresh_expires_at")
                or secrets.refresh_expires_at,
            )
            self.store.update_tokens(account_id, tokens)
            if record.status in {STATUS_RELOGIN, STATUS_COOLDOWN}:
                self.store.set_status(
                    account_id, STATUS_ACTIVE, "", actor="refresher"
                )
            self.store.log_event(
                account_id,
                "token_refresh_ok",
                f"endpoint={exchanged.get('endpoint', '')}",
            )
            logger.info("account %s token refreshed", account_id)
            return RefreshResult(account_id, True, message="refreshed")
        finally:
            lock.release()

    def due_accounts(self) -> list[AccountRecord]:
        now = now_ts()
        due: list[AccountRecord] = []
        for record in self.store.list():
            if record.status in {STATUS_DISABLED, STATUS_RELOGIN, STATUS_INVALID}:
                continue
            if not record.has_refresh_token:
                continue
            expiry = record.token_expires_at
            if expiry is None or expiry - now <= self.refresh_window:
                due.append(record)
        return due

    def refresh_due(self) -> list[RefreshResult]:
        results: list[RefreshResult] = []
        for record in self.due_accounts():
            results.append(self.refresh(record.id))
        return results

    def clear_expired_cooldowns(self) -> None:
        now = now_ts()
        for record in self.store.list():
            if (
                record.status == STATUS_COOLDOWN
                and record.cooldown_until
                and record.cooldown_until <= now
            ):
                self.store.set_status(
                    record.id, STATUS_ACTIVE, "", actor="refresher"
                )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.clear_expired_cooldowns()
                self.refresh_due()
            except Exception:
                logger.exception("token refresher iteration failed")
            self._stop.wait(self.interval)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="token-refresher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
