from __future__ import annotations

import threading
from typing import Iterable, Optional

from ..security import now_ts
from .store import (
    STATUS_ACTIVE,
    STATUS_COOLDOWN,
    STATUS_RELOGIN,
    AccountRecord,
    AccountStore,
)


class AccountRouter:
    """Sticky, capacity-aware account selection with failure cooldowns."""

    def __init__(
        self,
        store: AccountStore,
        *,
        affinity_ttl: int = 7 * 86400,
        cooldown: int = 300,
        rotation_idle_seconds: int = 1800,
        rotation_credit_share: float = 0.2,
    ) -> None:
        self.store = store
        self.affinity_ttl = max(0, int(affinity_ttl))
        self.cooldown = max(30, int(cooldown))
        self.rotation_idle_seconds = max(0, int(rotation_idle_seconds))
        self.rotation_credit_share = max(0.0, min(1.0, float(rotation_credit_share)))
        self._lock = threading.Lock()
        self._inflight: dict[str, int] = {}
        self._cursor = 0

    def active(self) -> list[AccountRecord]:
        now = now_ts()
        result: list[AccountRecord] = []
        for record in self.store.list():
            if record.status == STATUS_ACTIVE:
                result.append(record)
                continue
            if (
                record.status == STATUS_COOLDOWN
                and record.cooldown_until
                and record.cooldown_until <= now
            ):
                # Cooldowns expire on their own; do not wait for the refresher.
                self.store.set_status(
                    record.id, STATUS_ACTIVE, "", actor="account-router"
                )
                record.status = STATUS_ACTIVE
                record.status_reason = ""
                record.cooldown_until = None
                result.append(record)
        return result

    def acquire(self, account_id: str) -> None:
        with self._lock:
            self._inflight[account_id] = self._inflight.get(account_id, 0) + 1

    def release(self, account_id: str) -> None:
        with self._lock:
            current = self._inflight.get(account_id, 0)
            if current <= 1:
                self._inflight.pop(account_id, None)
            else:
                self._inflight[account_id] = current - 1

    def inflight(self, account_id: str) -> int:
        with self._lock:
            return self._inflight.get(account_id, 0)

    def choose(
        self,
        *,
        preferred: str = "",
        exclude: Iterable[str] = (),
    ) -> Optional[AccountRecord]:
        excluded = set(exclude)
        records = [record for record in self.active() if record.id not in excluded]
        if not records:
            return None
        by_id = {record.id: record for record in records}
        if preferred and preferred in by_id:
            # A conversation must keep its account even when that account is
            # momentarily busy; tool state is more important than balance.
            return by_id[preferred]

        with self._lock:
            inflight = dict(self._inflight)
            cursor = self._cursor
        capacities = {
            record.id: max(
                1,
                int(record.concurrency_limit or self.store.default_concurrency),
            )
            for record in records
        }
        available = [
            record
            for record in records
            if inflight.get(record.id, 0) < capacities[record.id]
        ]
        pool = available or records
        minimum = min(inflight.get(record.id, 0) for record in pool)
        ordered = pool[cursor % len(pool) :] + pool[: cursor % len(pool)]
        chosen = next(
            record for record in ordered if inflight.get(record.id, 0) == minimum
        )
        index = records.index(chosen)
        with self._lock:
            self._cursor = (index + 1) % max(1, len(records))
        return chosen

    def affinity(self, conversation_key: str) -> Optional[str]:
        return self.store.conversation_account(
            conversation_key,
            max_age=self.affinity_ttl,
        )

    def affinity_or_rotate(self, conversation_key: str) -> Optional[str]:
        """对话绑定的账号；该轮转时返回 None 让调用方重新选号。

        两种轮转条件：

        1. 空闲太久。实测同一段前缀隔 9 小时仍能命中缓存，所以阈值给到
           半小时；真正该换的是长时间没动、缓存大概已经凉了的对话。
        2. 这个对话已经把该账号的积分消耗掉一大截。与缓存无关，纯粹是
           别让一个长任务把单个账号的额度吃干。

        连续任务（两次请求间隔很短）一律保持粘性，否则工具调用链会因为
        换号而丢掉上下文。
        """

        bound = self.store.conversation_affinity(conversation_key)
        if bound is None:
            return None
        account_id, updated_at = bound
        if self.affinity_ttl > 0 and now_ts() - updated_at > self.affinity_ttl:
            self.store.unbind_conversation_account(conversation_key)
            return None
        if self.rotation_idle_seconds > 0:
            idle = now_ts() - updated_at
            if idle > self.rotation_idle_seconds:
                return None
        if self.rotation_credit_share > 0:
            used = self.store.conversation_credits(conversation_key, account_id)
            if used > 0:
                balance = self.store.account_credit_balance(account_id)
                if balance is not None and balance > 0:
                    if used >= balance * self.rotation_credit_share:
                        return None
        return account_id

    def bind(self, conversation_key: str, account_id: str) -> None:
        purge_before = (
            now_ts() - self.affinity_ttl if self.affinity_ttl > 0 else 0
        )
        self.store.bind_conversation_account(
            conversation_key,
            account_id,
            purge_before=purge_before,
        )

    def mark_failure(
        self,
        account: AccountRecord,
        status_code: int,
        reason: str,
    ) -> None:
        reason = str(reason or "")[:500]
        if status_code in {401, 403}:
            self.store.set_status(
                account.id,
                STATUS_RELOGIN,
                reason or f"HTTP {status_code}",
                actor="account-router",
            )
            return
        if status_code == 429 or status_code >= 500 or status_code in {0, 502, 503, 504}:
            self.store.set_status(
                account.id,
                STATUS_COOLDOWN,
                reason or f"HTTP {status_code}",
                cooldown_until=now_ts() + self.cooldown,
                actor="account-router",
            )
