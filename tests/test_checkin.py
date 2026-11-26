from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import checkin as checkin_module
from app.accounts.models import build_device
from app.main import create_app
from app.upstream import activity
from tests.conftest import make_jwt
from tests.test_api import setup_admin


def run(coroutine):
    return asyncio.run(coroutine)


class Upstream:
    """模拟签到上游：按账号记住状态，并记下每次提交用的设备号。"""

    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.claims: list[dict] = []

    def add(self, user_id: str, *, limited_times: int = 0) -> str:
        token = make_jwt({"data": {"id": user_id}, "exp": 4102444800})
        self.accounts[token] = {
            "user_id": user_id,
            "checked_in": False,
            "limited_times": limited_times,
        }
        return token

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Cloud-IDE-JWT ")
        state = self.accounts[token]
        if request.url.path.endswith("/checkin_credits/status"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "checked_in": state["checked_in"],
                    "did_checked_in": state["checked_in"],
                    "enable": True,
                    "credits": 100,
                    "extra_credits": 100,
                },
            )
        assert request.url.path.endswith("/checkin_credits/claim")
        self.claims.append(
            {
                "user_id": state["user_id"],
                "device_id": request.headers.get("x-device-id"),
                "body": json.loads(request.content),
            }
        )
        if state["limited_times"] > 0:
            state["limited_times"] -= 1
            return httpx.Response(
                200, json={"code": 9074, "message": "当前参与用户太多，请稍后再试"}
            )
        state["checked_in"] = True
        return httpx.Response(200, json={"code": 0, "message": "success"})


@pytest.fixture
def env(config, monkeypatch):
    """一个带上游模拟的应用，外加添加账号的辅助函数。"""
    monkeypatch.setattr(checkin_module, "ACCOUNT_GAP_SECONDS", 0)
    upstream = Upstream()
    app = create_app(config, transport=httpx.MockTransport(upstream))
    client = TestClient(app)

    def add(user_id: str, *, limited_times: int = 0) -> str:
        token = upstream.add(user_id, limited_times=limited_times)
        record, _ = app.state.accounts.add_manual(
            token=token,
            user_id=user_id,
            refresh_token=f"refresh-{user_id}",
            token_expires_at=int(time.time()) + 86400,
            device=build_device(),
        )
        return record.id

    return SimpleNamespace(app=app, client=client, upstream=upstream, add=add)


MORNING = dt.datetime(2026, 11, 26, 9, 0)


def test_checkin_device_id_is_stable_and_rotates() -> None:
    first = activity.checkin_device_id("user-1")
    assert len(first) == 16 and first.isdigit()
    assert activity.checkin_device_id("user-1", 0) == first
    assert activity.checkin_device_id("user-1", 1) != first
    assert activity.checkin_device_id("user-2") != first


@pytest.mark.parametrize(
    "value, expected",
    [("09:00", "09:00"), (" 23:59 ", "23:59"), ("9:00", ""), ("24:00", ""), ("09:60", "")],
)
def test_normalize_time(value, expected) -> None:
    assert checkin_module.normalize_time(value) == expected


def test_checkin_uses_derived_device_and_records_credit(config, env) -> None:
    account_id = env.add("user-1")
    with env.client:
        headers = setup_admin(env.client)
        response = env.client.post(
            "/admin/api/activity/checkin/account",
            json={"account_id": account_id},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["claimed"] is True and data["already"] is False
        assert data["rate_limited"] is False
        # 提交成功的响应不带积分数，金额来自随后的一次状态查询
        assert data["income"] == 100
        assert env.upstream.claims == [
            {
                "user_id": "user-1",
                "device_id": activity.checkin_device_id("user-1"),
                "body": {},
            }
        ]
        with env.app.state.db.read() as conn:
            rows = conn.execute(
                "SELECT amount FROM credit_events WHERE reason = 'checkin'"
            ).fetchall()
        assert [row["amount"] for row in rows] == [100]

        again = env.client.post(
            "/admin/api/activity/checkin/account",
            json={"account_id": account_id},
            headers=headers,
        ).json()
        assert again["already"] is True and again["claimed"] is False
        assert again["income"] == 0


def test_checkin_rate_limit_rotates_device_and_cools_down(config, env) -> None:
    account_id = env.add("user-1", limited_times=1)
    service = env.app.state.checkin
    with env.client:
        headers = setup_admin(env.client)
        first = env.client.post(
            "/admin/api/activity/checkin/account",
            json={"account_id": account_id},
            headers=headers,
        ).json()

        assert first["rate_limited"] is True and first["claimed"] is False
        assert first["code"] == 9074
        # 冷却 15 分钟，并且已经换到下一代设备号
        assert first["retry_in"] == 15
        assert first["generation"] == 1
        assert service.generation(account_id) == 1
        assert env.upstream.claims[-1]["device_id"] == activity.checkin_device_id("user-1")

        # 冷却期内不再向上游发请求
        blocked = env.client.post(
            "/admin/api/activity/checkin/account",
            json={"account_id": account_id},
            headers=headers,
        ).json()
        assert blocked["rate_limited"] is True
        assert 0 < blocked["retry_in"] <= 15
        assert len(env.upstream.claims) == 1

        # 冷却结束后用新的设备号提交
        env.app.state.db.set_setting(
            f"checkin:blocked_until:{account_id}", json.dumps(int(time.time()) - 1)
        )
        retried = env.client.post(
            "/admin/api/activity/checkin/account",
            json={"account_id": account_id},
            headers=headers,
        ).json()
        assert retried["claimed"] is True
        assert env.upstream.claims[-1]["device_id"] == activity.checkin_device_id("user-1", 1)


def test_auto_checkin_runs_at_set_time_and_retries_pending(config, env) -> None:
    plain = env.add("user-1")
    limited = env.add("user-2", limited_times=1)
    auto = checkin_module.AutoCheckin(env.app.state.checkin)
    config.checkin_auto_time = "09:00"

    # 未开启时不运行
    assert run(auto.tick(MORNING + dt.timedelta(minutes=5))) is False
    assert auto.next_run(MORNING) is None

    config.checkin_auto = True
    # 到点之前不运行
    assert run(auto.tick(MORNING.replace(hour=8))) is False
    assert auto.next_run(MORNING.replace(hour=8)) == int(MORNING.timestamp())
    assert run(auto.tick(MORNING + dt.timedelta(minutes=5))) is True
    first = auto.status()
    assert (first["last"]["signed"], first["last"]["rate_limited"]) == (1, 1)
    assert first["pending"] == [limited]
    assert 0 < first["cooldown"][limited] <= checkin_module.COOLDOWN_SECONDS

    # 补签间隔之内不重复运行
    assert run(auto.tick(MORNING + dt.timedelta(minutes=10))) is False
    assert auto.next_run(MORNING + dt.timedelta(minutes=10)) == int(
        (MORNING + dt.timedelta(minutes=35)).timestamp()
    )

    # 冷却期满、间隔到了再补签，这次全部签成（冷却按真实时间计，这里直接把冷却置为已过期）
    later = MORNING + dt.timedelta(minutes=36)
    env.app.state.db.set_setting(
        f"checkin:blocked_until:{limited}", json.dumps(int(time.time()) - 1)
    )
    assert run(auto.tick(later)) is True
    done = auto.status()
    assert (done["last"]["signed"], done["pending"]) == (2, [])
    assert done["cooldown"] == {}
    # 全部签成后当天不再运行，下一次是明天同一时刻
    assert run(auto.tick(later + dt.timedelta(hours=1))) is False
    assert auto.next_run(later) == int((MORNING + dt.timedelta(days=1)).timestamp())
    assert [item["user_id"] for item in env.upstream.claims] == ["user-1", "user-2", "user-2"]
    assert env.upstream.claims[-1]["device_id"] == activity.checkin_device_id("user-2", 1)
    assert plain  # 未被限流的账号只在第一轮提交过一次


def test_auto_checkin_skips_account_still_in_cooldown(config, env) -> None:
    account_id = env.add("user-1", limited_times=99)
    auto = checkin_module.AutoCheckin(env.app.state.checkin)
    config.checkin_auto = True
    config.checkin_auto_time = "09:00"

    assert run(auto.tick(MORNING)) is True
    # 账号还在冷却期内：这一轮跳过它，不向上游提交，也不换设备号
    summary = run(auto.tick(MORNING + dt.timedelta(minutes=31)))
    assert summary is True
    assert env.upstream.claims == env.upstream.claims[:1]
    assert env.app.state.checkin.generation(account_id) == 1
    last = auto.status()["last"]
    assert (last["rate_limited"], last["pending"], last["signed"]) == (1, 1, 0)


def test_auto_checkin_resumes_progress_across_restart(config, env) -> None:
    env.add("user-1")
    config.checkin_auto = True
    config.checkin_auto_time = "09:00"
    run(checkin_module.AutoCheckin(env.app.state.checkin).tick(MORNING))
    # 模拟服务重启：新建一个调度器，沿用数据库里的进度
    restarted = checkin_module.AutoCheckin(env.app.state.checkin)
    assert run(restarted.tick(MORNING + dt.timedelta(minutes=1))) is False
    assert restarted.status()["pending"] == []
    assert restarted.status()["last"]["signed"] == 1


def test_auto_checkin_waits_past_last_run_time(config, env) -> None:
    env.add("user-1")
    auto = checkin_module.AutoCheckin(env.app.state.checkin)
    config.checkin_auto = True
    config.checkin_auto_time = "09:00"
    late = dt.datetime(2026, 11, 26, 23, 40)
    assert run(auto.tick(late)) is False
    assert auto.next_run(late) == int(dt.datetime(2026, 11, 27, 9, 0).timestamp())


def test_settings_manage_auto_checkin(config, env) -> None:
    with env.client:
        headers = setup_admin(env.client)
        status = env.client.get("/admin/api/checkin/auto").json()
        assert status["enabled"] is False
        assert status["next_run"] is None and status["time"] == "09:00"

        patched = env.client.patch(
            "/admin/api/settings",
            json={"values": {"checkin_auto": True, "checkin_auto_time": "08:30"}},
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
        assert config.checkin_auto is True and config.checkin_auto_time == "08:30"
        assert env.app.state.db.get_setting("config:checkin_auto_time") == '"08:30"'
        settings = env.client.get("/admin/api/settings").json()["settings"]
        assert settings["checkin_auto"] is True
        assert settings["checkin_auto_time"] == "08:30"
        enabled = env.client.get("/admin/api/checkin/auto").json()
        assert enabled["enabled"] is True and enabled["next_run"]

        invalid = env.client.patch(
            "/admin/api/settings",
            json={"values": {"checkin_auto_time": "8点半"}},
            headers=headers,
        )
        assert invalid.status_code == 400
        assert config.checkin_auto_time == "08:30"
