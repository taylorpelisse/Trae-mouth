"""每日签到：按设备号限流的规则处理与自动签到。"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import math
import re
import time
from typing import Any, Optional

import httpx

from .accounts.store import STATUS_ACTIVE
from .upstream import activity

logger = logging.getLogger(__name__)

# 收到限流后这段时间内不再提交：紧接着重试会延长上游的限流窗口
COOLDOWN_SECONDS = 15 * 60
# 自动签到当天给未签成的账号补签的间隔
RETRY_SECONDS = 30 * 60
# 账号之间的间隔，避免同一出口集中提交
ACCOUNT_GAP_SECONDS = 3
# 当天最晚的发起时间，过了就等第二天
LAST_RUN = dt.time(23, 30)
DEFAULT_TIME = "09:00"
STATE_KEY = "checkin_auto_state"
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def normalize_time(value: Any) -> str:
    """校验「HH:MM」写法，不合法时返回空串。"""
    text = str(value or "").strip()
    return text if _TIME_RE.match(text) else ""


def _label(record: Any) -> str:
    return str(record.label or record.username or record.id)


class CheckinService:
    """给一个账号签到，并处理上游按设备号的限流。"""

    def __init__(
        self,
        *,
        config: Any,
        db: Any,
        accounts: Any,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.accounts = accounts
        self.transport = transport

    def generation(self, account_id: str) -> int:
        """这个账号当前的签到设备号用到第几代。"""
        raw = self.db.get_setting(f"checkin:generation:{account_id}")
        try:
            return max(0, int(json.loads(raw)))
        except (TypeError, ValueError):
            return 0

    def rotate(self, account_id: str) -> int:
        generation = self.generation(account_id) + 1
        self.db.set_setting(
            f"checkin:generation:{account_id}", json.dumps(generation)
        )
        return generation

    def cooldown_remaining(self, account_id: str, now: Optional[int] = None) -> int:
        """距离可以再次提交还有多少秒，0 表示现在就可以。"""
        raw = self.db.get_setting(f"checkin:blocked_until:{account_id}")
        try:
            until = int(json.loads(raw))
        except (TypeError, ValueError):
            return 0
        return max(0, until - int(now or time.time()))

    def _block(self, account_id: str) -> int:
        until = int(time.time()) + COOLDOWN_SECONDS
        self.db.set_setting(f"checkin:blocked_until:{account_id}", json.dumps(until))
        return until

    def record_credit(self, account_id: str, amount: Any) -> float:
        """记一笔签到收入，当天只记一次；返回实际记入的金额。"""
        try:
            value = float(amount)
        except (TypeError, ValueError):
            return 0
        if not math.isfinite(value) or value <= 0:
            return 0
        start = int(dt.datetime.combine(dt.date.today(), dt.time.min).timestamp())
        with self.db.tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM credit_events WHERE account_id = ? "
                "AND reason = 'checkin' AND created_at >= ? LIMIT 1",
                (account_id, start),
            ).fetchone()
            if exists:
                return 0
            conn.execute(
                "INSERT INTO credit_events(account_id, amount, reason, created_at) "
                "VALUES(?,?,?,?)",
                (account_id, value, "checkin", int(time.time())),
            )
        return value

    async def run(self, record: Any) -> dict[str, Any]:
        """查状态并在未签时提交签到。

        限流冷却期内直接返回剩余时间，不向上游发请求；被限流时换下一代设备号
        并进入冷却。
        """
        base: dict[str, Any] = {
            "account_id": record.id,
            "account_label": _label(record),
            "ok": False,
            "already": False,
            "claimed": False,
            "rate_limited": False,
            "code": 0,
            "message": "",
            "income": 0,
            "generation": self.generation(record.id),
        }
        remaining = self.cooldown_remaining(record.id)
        if remaining > 0:
            return {
                **base,
                "rate_limited": True,
                "retry_in": max(1, math.ceil(remaining / 60)),
                "message": "签到设备号被限流",
            }
        secrets = self.accounts.get_secrets(record.id)
        if not secrets.token:
            return {**base, "message": "account has no token"}
        identity = str(record.user_id or record.id)
        token = secrets.token
        generation = base["generation"]
        transport = self.transport

        async def query() -> dict[str, Any]:
            status = await activity.checkin_status(
                self.config, token, identity, generation=generation, transport=transport
            )
            return status.get("data") if isinstance(status.get("data"), dict) else {}

        data = await query()
        if data.get("checked_in") or data.get("did_checked_in"):
            amount = data.get("credits") or data.get("extra_credits")
            return {
                **base,
                "ok": True,
                "already": True,
                "message": "already checked in",
                "income": self.record_credit(record.id, amount),
            }
        claim = await activity.checkin_claim(
            self.config, token, identity, generation=generation, transport=transport
        )
        claim_data = claim.get("data") if isinstance(claim.get("data"), dict) else {}
        if claim.get("rate_limited"):
            generation = self.rotate(record.id)
            until = self._block(record.id)
            return {
                **base,
                "rate_limited": True,
                "code": claim.get("code") or 0,
                "message": claim.get("message") or "",
                "generation": generation,
                "retry_in": max(1, math.ceil((until - time.time()) / 60)),
            }
        claimed = bool(claim.get("ok")) and not claim_data.get("risk")
        amount = claim_data.get("credits") or claim_data.get("extra_credits")
        if claimed and not amount:
            # 提交成功的响应里不带积分数，再查一次状态拿到金额
            refreshed = await query()
            amount = refreshed.get("credits") or refreshed.get("extra_credits")
        return {
            **base,
            "ok": bool(claim.get("ok")),
            "claimed": claimed,
            "code": claim.get("code") or 0,
            "message": claim.get("message") or "",
            "risk": bool(claim_data.get("risk")),
            "income": self.record_credit(record.id, amount) if claimed else 0,
        }


class AutoCheckin:
    """每天在设定时刻给全部启用账号签到，当天没签成的按间隔补签。

    进度和结果保存在数据库里，服务重启后接着当天的进度走。
    """

    def __init__(self, service: CheckinService) -> None:
        self.service = service
        self._running = False

    @property
    def config(self) -> Any:
        return self.service.config

    def scheduled_time(self) -> Optional[dt.time]:
        text = normalize_time(
            getattr(self.config, "checkin_auto_time", "") or DEFAULT_TIME
        )
        if not text:
            return None
        hour, minute = (int(part) for part in text.split(":"))
        return dt.time(hour, minute)

    def _enabled(self) -> bool:
        return bool(getattr(self.config, "checkin_auto", False))

    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.service.db.get_setting(STATE_KEY))
        except (TypeError, ValueError):
            data = None
        state: dict[str, Any] = {"day": "", "last_run": 0, "done": False, "last": None}
        if isinstance(data, dict):
            for key in state:
                if key in data:
                    state[key] = data[key]
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        self.service.db.set_setting(
            STATE_KEY, json.dumps(state, ensure_ascii=False)
        )

    @staticmethod
    def _run_at(day: dt.date, at: dt.time) -> int:
        return int(dt.datetime.combine(day, at).timestamp())

    def next_run(self, now: Optional[dt.datetime] = None) -> Optional[int]:
        """下一次发起签到的时间；未开启或时刻无效时为 None。"""
        at = self.scheduled_time()
        if not self._enabled() or not at:
            return None
        now = now or dt.datetime.now()
        state = self._state()
        if state["day"] == now.date().isoformat() and state["done"]:
            return self._run_at(now.date() + dt.timedelta(days=1), at)
        today_run = self._run_at(now.date(), at)
        if now.timestamp() < today_run:
            return today_run
        # 今天已到点：首次立即执行，之后按补签间隔
        if state["day"] == now.date().isoformat() and state["last_run"]:
            candidate = int(state["last_run"]) + RETRY_SECONDS
        else:
            candidate = int(now.timestamp())
        if candidate >= self._run_at(now.date(), LAST_RUN):
            return self._run_at(now.date() + dt.timedelta(days=1), at)
        return candidate

    async def tick(self, now: Optional[dt.datetime] = None) -> bool:
        """定时调用：到点就跑一轮。返回这次是否发起了签到。"""
        at = self.scheduled_time()
        if not self._enabled() or not at or self._running:
            return False
        now = now or dt.datetime.now()
        if now.time() < at or now.time() >= LAST_RUN:
            return False
        state = self._state()
        today = now.date().isoformat()
        if state["day"] == today:
            if state["done"]:
                return False
            if state["last_run"] and int(now.timestamp()) - int(state["last_run"]) < RETRY_SECONDS:
                return False
        await self.run_pass("auto", now=now)
        return True

    async def run_pass(
        self, trigger: str = "auto", now: Optional[dt.datetime] = None
    ) -> dict[str, Any]:
        """给全部启用账号各签到一次，返回这一轮的结果。"""
        self._running = True
        moment = now or dt.datetime.now()
        started = int(moment.timestamp())
        today = moment.date().isoformat()
        items: list[dict[str, Any]] = []
        pending: list[str] = []
        previous = False
        try:
            for record in self.service.accounts.list():
                if record.status != STATUS_ACTIVE:
                    continue
                if previous:
                    await asyncio.sleep(ACCOUNT_GAP_SECONDS)
                previous = True
                remaining = self.service.cooldown_remaining(record.id)
                if remaining > 0:
                    items.append(
                        {
                            "account_id": record.id,
                            "label": _label(record),
                            "status": "cooldown",
                            "detail": f"限流冷却中，{math.ceil(remaining / 60)} 分钟后再试",
                        }
                    )
                    pending.append(record.id)
                    continue
                result = await self.service.run(record)
                if result["claimed"] or result["already"]:
                    status = "signed"
                    detail = (
                        "今日已签"
                        if result["already"]
                        else f"签到成功，获得 {result['income']:g} 积分"
                    )
                elif result["rate_limited"]:
                    status = "rate_limited"
                    detail = (
                        f"设备号被限流，已更换设备号，{result['retry_in']} 分钟后再试"
                    )
                else:
                    status = "failed"
                    detail = result.get("message") or "签到失败"
                items.append(
                    {
                        "account_id": record.id,
                        "label": _label(record),
                        "status": status,
                        "detail": detail,
                    }
                )
                if status != "signed":
                    pending.append(record.id)
        finally:
            self._running = False
        summary = {
            "at": started,
            "trigger": trigger,
            "day": today,
            "total": len(items),
            "signed": sum(1 for item in items if item["status"] == "signed"),
            "rate_limited": sum(
                1 for item in items if item["status"] in {"rate_limited", "cooldown"}
            ),
            "failed": sum(1 for item in items if item["status"] == "failed"),
            "pending": len(pending),
            "items": items,
        }
        state = self._state()
        if state.get("day") != today:
            state = {"day": today, "last_run": 0, "done": False}
        state["last"] = summary
        if trigger == "auto":
            state["last_run"] = started
            state["done"] = not pending
        self._save_state(state)
        logger.info(
            "check-in pass finished: trigger=%s signed=%s rate_limited=%s failed=%s pending=%s",
            trigger, summary["signed"], summary["rate_limited"], summary["failed"], summary["pending"],
        )
        return summary

    def status(self) -> dict[str, Any]:
        """给设置页看的自动签到状态。"""
        state = self._state()
        today = dt.date.today().isoformat()
        cooldown = {
            record.id: self.service.cooldown_remaining(record.id)
            for record in self.service.accounts.list()
            if record.status == STATUS_ACTIVE
            and self.service.cooldown_remaining(record.id) > 0
        }
        pending: list[str] = []
        if state.get("day") == today and not state["done"]:
            last = state.get("last") or {}
            pending = [
                item["account_id"]
                for item in last.get("items") or []
                if item.get("status") in {"rate_limited", "cooldown", "failed"}
            ]
        return {
            "ok": True,
            "enabled": self._enabled(),
            "time": getattr(self.config, "checkin_auto_time", "") or DEFAULT_TIME,
            "time_valid": self.scheduled_time() is not None,
            "next_run": self.next_run(),
            "running": self._running,
            "last": state.get("last"),
            "pending": pending,
            "cooldown": cooldown,
        }
