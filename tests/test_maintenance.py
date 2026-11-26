from __future__ import annotations

import datetime as dt
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import maintenance as maintenance_module
from app.accounts.models import build_device
from app.main import create_app
from tests.conftest import make_jwt
from tests.test_api import setup_admin

CLIENT_ID = "ono9krqynydwx5"
EXCHANGE = "/oauth/ExchangeToken"
USER_INFO = "/cloudide/api/v3/trae/GetUserInfo"
ENT_USAGE = "/trae/api/v2/pay/ide_user_ent_usage"


def iso_later(seconds: int) -> str:
    moment = dt.datetime.fromtimestamp(int(time.time()) + seconds, dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Upstream:
    """模拟与账号维护相关的上游接口，并记下每次请求。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.native = "ok"  # ok | mismatch | fail
        self.exchange_fails = False
        self.names: dict[str, str] = {}
        self.balance = 250.0
        self.consumed = 40.0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        token = (
            request.headers.get("authorization", "").removeprefix("Cloud-IDE-JWT ")
            or request.headers.get("x-cloudide-token", "")
        )
        self.requests.append((path, token))
        if path.endswith(EXCHANGE):
            if self.exchange_fails:
                return httpx.Response(500, text="upstream down")
            native = "/trae/api/v3" in path
            if native and self.native == "mismatch":
                return httpx.Response(
                    401,
                    json={
                        "ResponseMetadata": {
                            "Error": {"Code": "20403", "Message": "Token device not match."}
                        }
                    },
                )
            if native and self.native == "fail":
                return httpx.Response(500, text="native broken")
            return httpx.Response(
                200,
                json={
                    "Result": {
                        "Token": "native-token" if native else "legacy-token",
                        "RefreshToken": "refresh-new",
                        "TokenExpireAt": iso_later(14 * 86400),
                        "RefreshExpireAt": iso_later(180 * 86400),
                        "ClientID": CLIENT_ID,
                        "UserInfo": json.dumps({"UserID": "user"}),
                    }
                },
            )
        if path.endswith(USER_INFO):
            return httpx.Response(200, json={"Result": {"Name": self.names.get(token, "旧昵称")}})
        if path.endswith(ENT_USAGE):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "total_balance": self.balance,
                    "usage_summary": {"consumed_amount": self.consumed},
                    "user_entitlement_pack_list": [
                        {
                            "entitlement_base_info": {"product_type": 0},
                            "display_desc": "Pro",
                        }
                    ],
                },
            )
        return httpx.Response(500, text="unexpected request")

    def paths(self, suffix: str) -> list[str]:
        return [path for path, _ in self.requests if path.endswith(suffix)]


@pytest.fixture
def env(config):
    """带上游模拟的应用；间隔与抖动设为 0，让测试不必等待。"""
    config.account_stagger_seconds = 0
    config.account_jitter_seconds = 0
    config.refresh_device_info = True
    upstream = Upstream()
    app = create_app(config, transport=httpx.MockTransport(upstream))
    client = TestClient(app)

    def add(user_id: str, *, token_seconds: int = 13 * 86400, refresh: bool = True) -> str:
        token = make_jwt({"data": {"id": user_id}, "exp": 4102444800})
        record, _ = app.state.accounts.add_manual(
            token=token,
            user_id=user_id,
            refresh_token=f"refresh-{user_id}" if refresh else "",
            token_expires_at=int(time.time()) + token_seconds,
            device=build_device(),
        )
        return record.id

    return SimpleNamespace(app=app, client=client, upstream=upstream, add=add)


MORNING = dt.datetime(2026, 11, 26, 9, 0)


def run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_normalize_times() -> None:
    assert maintenance_module.normalize_times("14:30, 08:30;08:30") == ["08:30", "14:30"]
    assert maintenance_module.normalize_times(" 08:30，20:00 ") == ["08:30", "20:00"]
    assert maintenance_module.normalize_times("") == []
    assert maintenance_module.normalize_times("8点半, 09:00") == []


def test_extract_plan_and_balance() -> None:
    data = {
        "user_entitlement_pack_list": [
            {"entitlement_base_info": {"product_type": 0}, "display_desc": "免费"},
        ],
        "balance_summary": [{"total_balance": 100}, {"total_balance": 50}],
    }
    assert maintenance_module.extract_plan(data) == "Free"
    assert maintenance_module.extract_plan({}, "Free") == "Free"
    assert maintenance_module.read_balance(data) == 150
    assert maintenance_module.read_balance({"total_balance": True}) is None


def test_credit_job_updates_cache_and_plan(config, env) -> None:
    account_id = env.add("user-1")
    env.app.state.accounts.update_profile(account_id, plan="Free")
    with env.client:
        setup_admin(env.client)
        summary = run(env.app.state.maintenance.run("credits", "manual"))

        assert (summary["ok"], summary["failed"]) == (1, 0)
        assert summary["items"][0]["detail"] == "余额 250"
        assert env.upstream.paths(ENT_USAGE) == [ENT_USAGE]
        cached = maintenance_module.cached_credit(env.app.state.db, account_id)
        assert cached["balance"] == 250 and cached["used_today"] == 0
        assert env.app.state.accounts.get(account_id).plan == "Pro"

        # 页面轮询积分列表不再请求上游
        before = len(env.upstream.requests)
        listed = env.client.get("/admin/api/credits").json()
        assert listed["accounts"][0]["balance"] == 250
        assert len(env.upstream.requests) == before


def test_balance_increase_is_recorded_once(config, env) -> None:
    account_id = env.add("user-1")
    db = env.app.state.db
    maintenance_module.apply_credit_usage(
        db, account_id, {"total_balance": 100, "usage_summary": {"consumed_amount": 10}}
    )
    # 同一天用量上涨只影响今日已用
    _, used = maintenance_module.apply_credit_usage(
        db, account_id, {"total_balance": 100, "usage_summary": {"consumed_amount": 35}}
    )
    assert used == 25
    # 余额上涨且没有对应的签到/充值记录时，记为充值
    maintenance_module.apply_credit_usage(
        db, account_id, {"total_balance": 200, "usage_summary": {"consumed_amount": 35}}
    )
    with db.read() as conn:
        rows = conn.execute(
            "SELECT reason, amount FROM credit_events WHERE account_id = ?", (account_id,)
        ).fetchall()
    assert [(row["reason"], row["amount"]) for row in rows] == [("recharge", 100)]


def test_scheduled_jobs_run_once_per_slot_and_not_before(config, env) -> None:
    env.add("user-1")
    env.add("user-2")
    maintenance = env.app.state.maintenance
    config.keepalive_time = "09:00"
    config.credit_refresh_times = "09:00, 21:00"
    config.token_refresh_time = "09:00"

    # 到点之前只做整点的兜底检查，令牌都还早，不发请求
    assert run(maintenance.tick(MORNING.replace(hour=8))) == ["safety"]
    assert env.upstream.requests == []
    assert maintenance.next_run("keepalive", MORNING.replace(hour=8)) == int(MORNING.timestamp())

    # 到点后保活、积分、令牌各跑一轮；换了一个小时，兜底检查也跑一轮
    assert sorted(run(maintenance.tick(MORNING))) == ["credits", "keepalive", "safety", "token"]
    assert len(env.upstream.paths(USER_INFO)) == 2
    assert len(env.upstream.paths(ENT_USAGE)) == 2
    # 令牌还有 13 天，定时刷新和兜底检查都不发请求
    assert env.upstream.paths(EXCHANGE) == []

    # 同一个时刻当天不重复
    assert run(maintenance.tick(MORNING + dt.timedelta(minutes=5))) == []
    assert len(env.upstream.paths(USER_INFO)) == 2
    status = maintenance.status(MORNING + dt.timedelta(minutes=5))
    assert status["jobs"]["keepalive"]["next_run"] == int(
        (MORNING + dt.timedelta(days=1)).timestamp()
    )
    assert status["jobs"]["credits"]["next_run"] == int(
        MORNING.replace(hour=21).timestamp()
    )
    assert status["jobs"]["keepalive"]["last"]["ok"] == 2

    # 第二天的同一时刻再跑
    tomorrow = MORNING + dt.timedelta(days=1)
    run(maintenance.tick(tomorrow))
    assert len(env.upstream.paths(USER_INFO)) == 4


def test_token_job_refreshes_only_accounts_close_to_expiry(config, env) -> None:
    fresh = env.add("user-fresh", token_seconds=13 * 86400)
    expiring = env.add("user-expiring", token_seconds=2 * 3600)
    maintenance = env.app.state.maintenance
    # 不需要发请求的账号不占用错峰间隔：间隔设成 5 秒也应当很快结束
    config.account_stagger_seconds = 5
    started = time.monotonic()
    summary = run(maintenance.run("token", "auto"))
    assert time.monotonic() - started < 4
    assert [item["status"] for item in summary["items"] if item["account_id"] == fresh] == ["skipped"]
    assert [item["status"] for item in summary["items"] if item["account_id"] == expiring] == ["ok"]
    # 原生接口成功后，这个账号以后都记为走原生接口
    assert env.upstream.paths(EXCHANGE) == ["/trae/api/v3" + EXCHANGE]
    assert env.app.state.db.get_setting(f"refresh:endpoint:{expiring}") == "native"
    assert "原生接口" in summary["items"][0]["detail"] + summary["items"][1]["detail"]
    assert maintenance.endpoint_summary() == {"native": 1, "legacy": 0, "unknown": 1}

    # 兜底检查：令牌还有 5 小时的账号会被刷新，其余两个不会
    later = env.add("user-later", token_seconds=5 * 3600)
    safety = run(maintenance.run("token", "safety"))
    assert sorted(item["status"] for item in safety["items"]) == ["ok", "skipped", "skipped"]
    assert len(env.upstream.paths(EXCHANGE)) == 2
    assert env.app.state.accounts.get(later).token_expires_at > time.time() + 13 * 86400


def test_mismatched_device_falls_back_to_legacy_and_stops_retrying_native(config, env) -> None:
    env.upstream.native = "mismatch"
    account_id = env.add("user-1", token_seconds=3600)
    refresher = env.app.state.refresher
    db = env.app.state.db

    first = refresher.refresh(account_id)
    assert first.ok and first.endpoint == "legacy"
    native_calls = [p for p in env.upstream.paths(EXCHANGE) if p.startswith("/trae/api/v3")]
    assert len(native_calls) == 1
    assert db.get_setting(f"refresh:endpoint:{account_id}") == "legacy"
    with db.read() as conn:
        detail = conn.execute(
            "SELECT detail FROM account_events WHERE event = 'token_refresh_ok' ORDER BY id DESC LIMIT 1"
        ).fetchone()["detail"]
    assert detail == "endpoint=legacy (token not bound to this device)"

    second = refresher.refresh(account_id, force=True)
    assert second.ok and second.endpoint == "legacy"
    # 第二次不再尝试原生接口
    native_calls = [p for p in env.upstream.paths(EXCHANGE) if p.startswith("/trae/api/v3")]
    assert len(native_calls) == 1


def test_failed_refresh_waits_before_retrying(config, env) -> None:
    env.upstream.exchange_fails = True
    account_id = env.add("user-1", token_seconds=3600)
    refresher = env.app.state.refresher

    failed = refresher.refresh(account_id)
    assert not failed.ok and failed.retry_after == 300

    # 退避期内不再请求上游
    waiting = refresher.refresh(account_id)
    assert waiting.skipped and waiting.message == "waiting to retry"
    assert len(env.upstream.paths(EXCHANGE)) == 2  # 原生一次、旧接口一次

    env.upstream.exchange_fails = False
    env.app.state.db.set_setting(f"refresh:retry_at:{account_id}", "0")
    recovered = refresher.refresh(account_id)
    assert recovered.ok and recovered.endpoint == "native"
    assert env.app.state.db.get_setting(f"refresh:failures:{account_id}") == "0"


def test_manual_refresh_runs_in_background_with_stagger(config, env) -> None:
    # 账号之间留 1 秒，保证第二轮请求发出时第一轮还在进行
    config.account_stagger_seconds = 1
    for index in range(3):
        env.add(f"user-{index}", token_seconds=3600)
    with env.client:
        headers = setup_admin(env.client)
        credits = env.client.get("/admin/api/credits?refresh=1").json()
        assert credits["refreshing"] is True and credits["started"] is True
        # 同一个任务进行中时不重复启动
        again = env.client.get("/admin/api/credits?refresh=1").json()
        assert again["refreshing"] is True and again["started"] is False
        config.account_stagger_seconds = 0
        for _ in range(100):
            status = env.client.get("/admin/api/maintenance").json()
            if not status["jobs"]["credits"]["running"]:
                break
            time.sleep(0.05)
        assert status["jobs"]["credits"]["last"]["ok"] == 3
        assert len(env.upstream.paths(ENT_USAGE)) == 3

        refreshed = env.client.post("/admin/api/accounts/refresh-all", headers=headers).json()
        assert refreshed["started"] is True
        for _ in range(100):
            status = env.client.get("/admin/api/maintenance").json()
            if not status["jobs"]["sync"]["running"]:
                break
            time.sleep(0.05)
        last = status["jobs"]["sync"]["last"]
        assert (last["ok"], last["total"]) == (3, 3)
        assert "余额 250" in last["items"][0]["detail"]
        # 全部刷新：令牌（原生）、资料、积分各一次
        assert len(env.upstream.paths(EXCHANGE)) == 3
        assert len(env.upstream.paths(USER_INFO)) == 3
        assert len(env.upstream.paths(ENT_USAGE)) == 6


def test_stagger_order_is_shuffled_and_gap_includes_jitter(config, env) -> None:
    maintenance = env.app.state.maintenance
    config.account_stagger_seconds = 10
    config.account_jitter_seconds = 60
    for index in range(8):
        env.add(f"user-{index}")
    gaps = {round(maintenance.gap()) for _ in range(200)}
    assert all(10 <= gap <= 70 for gap in gaps)
    assert len(gaps) > 1
    first_positions = {
        maintenance.order(maintenance.active())[0].id for _ in range(20)
    }
    assert len(first_positions) > 1


def test_settings_manage_maintenance(config, env) -> None:
    with env.client:
        headers = setup_admin(env.client)
        patched = env.client.patch(
            "/admin/api/settings",
            json={
                "values": {
                    "account_stagger_seconds": 15,
                    "account_jitter_seconds": 120,
                    "token_refresh_time": "05:10",
                    "token_refresh_margin_hours": 30,
                    "keepalive_time": "",
                    "credit_refresh_times": "07:00, 19:00, 07:00",
                }
            },
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
        assert config.account_stagger_seconds == 15
        assert config.token_refresh_margin_hours == 30
        # 提前量换成秒交给刷新器
        assert env.app.state.refresher.refresh_window == 30 * 3600
        assert config.keepalive_time == ""
        assert config.credit_refresh_times == "07:00,19:00"
        assert env.app.state.db.get_setting("config:token_refresh_time") == '"05:10"'
        status = env.client.get("/admin/api/maintenance").json()
        assert status["stagger_seconds"] == 15
        assert status["token_refresh_margin_hours"] == 30
        assert status["credit_refresh_times"] == ["07:00", "19:00"]
        assert status["jobs"]["keepalive"]["next_run"] is None
        assert status["jobs"]["token"]["next_run"]

        for key, value in (
            ("token_refresh_time", "5点"),
            ("keepalive_time", "25:00"),
            ("credit_refresh_times", "中午"),
            ("token_refresh_margin_hours", 0),
        ):
            invalid = env.client.patch(
                "/admin/api/settings", json={"values": {key: value}}, headers=headers
            )
            assert invalid.status_code == 400, key


def test_deleting_account_removes_its_remembered_state(config, env) -> None:
    account_id = env.add("user-1", token_seconds=3600)
    env.app.state.refresher.refresh(account_id)
    env.app.state.checkin.rotate(account_id)
    env.app.state.accounts.delete(account_id, actor="test")
    with env.app.state.db.read() as conn:
        left = conn.execute(
            "SELECT key FROM settings WHERE key LIKE 'refresh:%' OR key LIKE 'checkin:%'"
        ).fetchall()
    assert left == []
