from __future__ import annotations

import time

from app.accounts.models import build_device
from app.accounts.router import AccountRouter
from app.accounts.store import (
    AccountStore,
    STATUS_ACTIVE,
    STATUS_COOLDOWN,
    STATUS_RELOGIN,
)
from app.db import Database
from app.security import SecretBox
from tests.conftest import make_jwt


def make_store(tmp_path) -> tuple[AccountStore, str, str]:
    db = Database(tmp_path / "router.db")
    db.migrate()
    store = AccountStore(db, SecretBox(b"r" * 32), default_concurrency=2)
    first, _ = store.add_manual(
        token=make_jwt({"data": {"id": "user-1"}, "exp": 4102444800}),
        user_id="user-1",
        refresh_token="r1",
        token_expires_at=int(time.time()) + 86400,
        device=build_device(),
    )
    second, _ = store.add_manual(
        token=make_jwt({"data": {"id": "user-2"}, "exp": 4102444800}),
        user_id="user-2",
        refresh_token="r2",
        token_expires_at=int(time.time()) + 86400,
        device=build_device(),
    )
    return store, first.id, second.id


def test_router_rotates_new_conversations_and_keeps_affinity(tmp_path) -> None:
    store, first, second = make_store(tmp_path)
    router = AccountRouter(store, affinity_ttl=3600, cooldown=60)

    chosen = [router.choose().id, router.choose().id]
    assert set(chosen) == {first, second}

    router.bind("conv-1", first)
    assert router.affinity("conv-1") == first
    assert router.choose(preferred=router.affinity("conv-1")).id == first

    # Affinity survives a new router instance (DB-backed).
    restarted = AccountRouter(store, affinity_ttl=3600, cooldown=60)
    assert restarted.affinity("conv-1") == first
    assert restarted.choose(preferred=first).id == first


def test_router_marks_failures_and_skips_cooldown(tmp_path) -> None:
    store, first, second = make_store(tmp_path)
    router = AccountRouter(store, affinity_ttl=3600, cooldown=60)
    first_record = store.get(first)
    second_record = store.get(second)
    assert first_record and second_record

    router.acquire(first)
    assert router.inflight(first) == 1
    router.release(first)
    assert router.inflight(first) == 0

    router.mark_failure(first_record, 429, "rate limited")
    assert store.get(first).status == STATUS_COOLDOWN
    assert router.choose().id == second

    router.mark_failure(second_record, 401, "expired")
    assert store.get(second).status == STATUS_RELOGIN
    assert router.choose() is None


def test_router_recovers_expired_cooldown(tmp_path) -> None:
    store, first, _second = make_store(tmp_path)
    router = AccountRouter(store, affinity_ttl=3600, cooldown=60)
    store.set_status(
        first,
        STATUS_COOLDOWN,
        "rate limited",
        cooldown_until=int(time.time()) - 1,
        actor="test",
    )
    chosen = router.choose()
    assert chosen is not None and chosen.id == first
    assert store.get(first).status == STATUS_ACTIVE
