"""账号维护：按错峰方式执行令牌刷新、账号保活、积分刷新和全部刷新。

真实客户端没有固定时刻的批量刷新：一个设备只对应一个账号，请求由使用行为
自然分散。账号池要贴近这一点，所有针对全部账号的操作都打乱顺序，并在账号
之间留出间隔和随机抖动。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import random
import time
from typing import Any, Callable, Optional

import httpx

from .accounts.store import STATUS_ACTIVE
from .checkin import normalize_time
from .upstream import activity

logger = logging.getLogger(__name__)

STATE_KEY = "maintenance_state"
# 每小时一次的兜底检查：令牌剩余不足这个时间的账号立即刷新
SAFETY_WINDOW = 6 * 3600
MAX_SLOTS = 12
# 每个任务对单个账号依次做什么。模型目录没有固定时刻，只按需在后台刷新。
JOB_ACTIONS: dict[str, tuple[str, ...]] = {
    "token": ("token",),
    "keepalive": ("profile",),
    "credits": ("credits",),
    "sync": ("token", "profile", "credits"),
    "catalogs": ("catalog",),
}
JOB_LABELS = {
    "token": "令牌刷新",
    "keepalive": "账号保活",
    "credits": "积分刷新",
    "sync": "全部刷新",
    "catalogs": "模型目录",
}
SCHEDULED_JOBS = ("token", "keepalive", "credits")
PROFILE_NAME_KEYS = ("ScreenName", "screenName", "NickName", "nickName", "Name")
# 一个账号做了几件事时，结果取最差的那个
_STATUS_RANK = {"ok": 0, "skipped": 1, "failed": 2}
WORSE_STATUS = {
    (left, right): max((left, right), key=lambda name: _STATUS_RANK[name])
    for left in _STATUS_RANK
    for right in _STATUS_RANK
}


def normalize_times(value: Any) -> list[str]:
    """把「08:30, 14:30」这样的写法拆成去重排序后的时刻列表；有不合法项时返回空列表。"""
    parts = [
        part.strip()
        for part in str(value or "").replace(";", ",").replace("，", ",").split(",")
    ]
    slots: list[str] = []
    for part in parts:
        if not part:
            continue
        text = normalize_time(part)
        if not text:
            return []
        if text not in slots:
            slots.append(text)
    return sorted(slots)[:MAX_SLOTS]


def extract_plan(data: Any, fallback: str = "") -> str:
    """从权益接口的返回里认出套餐名。"""
    packs = data.get("user_entitlement_pack_list") if isinstance(data, dict) else None
    plan = ""
    if isinstance(packs, list):
        for pack in packs:
            if not isinstance(pack, dict):
                continue
            base = pack.get("entitlement_base_info") or {}
            if base.get("product_type") != 0:
                continue
            desc = str(pack.get("display_desc") or "").strip()
            group = str(pack.get("group_name") or "").strip()
            if desc and desc != "免费":
                plan = desc
                break
            if group and group != "免费":
                plan = group
                break
        if not plan:
            for pack in packs:
                if not isinstance(pack, dict):
                    continue
                desc = str(pack.get("display_desc") or "").strip()
                group = str(pack.get("group_name") or "").strip()
                if desc == "免费" or group == "免费":
                    plan = "Free"
                    break
    return plan or fallback


def read_balance(data: Any) -> Optional[float]:
    if not isinstance(data, dict):
        return None
    balance = data.get("total_balance")
    if isinstance(balance, (int, float)) and not isinstance(balance, bool):
        return float(balance)
    summary = data.get("balance_summary")
    if isinstance(summary, list):
        values = [
            item.get("total_balance")
            for item in summary
            if isinstance(item, dict)
            and isinstance(item.get("total_balance"), (int, float))
        ]
        if values:
            return float(sum(values))
    return None


def apply_credit_usage(db: Any, account_id: str, data: Any) -> tuple[Optional[float], Optional[float]]:
    """把一次权益接口的返回记入本地：当日用量、余额，以及余额上涨中没被记过的部分。

    返回 (余额, 今日已用)。
    """
    usage = data.get("usage_summary") if isinstance(data, dict) else None
    consumed = usage.get("consumed_amount") if isinstance(usage, dict) else None
    if isinstance(consumed, bool) or not isinstance(consumed, (int, float)):
        consumed = None
    used_today: Optional[float] = None
    day = dt.date.today().isoformat()
    now = int(time.time())
    if consumed is not None:
        with db.tx() as conn:
            row = conn.execute(
                "SELECT baseline, latest FROM credit_daily "
                "WHERE account_id = ? AND day = ?",
                (account_id, day),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO credit_daily(account_id, day, baseline, latest, updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (account_id, day, float(consumed), float(consumed), now),
                )
            else:
                conn.execute(
                    "UPDATE credit_daily SET latest = ?, updated_at = ? "
                    "WHERE account_id = ? AND day = ?",
                    (float(consumed), now, account_id, day),
                )
        baseline = float(row["baseline"]) if row is not None else float(consumed)
        used_today = max(0.0, float(consumed) - baseline)
    balance = read_balance(data)
    if balance is not None:
        with db.read() as conn:
            previous = conn.execute(
                "SELECT balance, updated_at FROM credit_balance WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if previous is None:
            with db.tx() as conn:
                conn.execute(
                    "INSERT INTO credit_balance(account_id, balance, updated_at) "
                    "VALUES(?,?,?)",
                    (account_id, balance, now),
                )
        else:
            increased = balance - float(previous["balance"])
            if increased > 0.01:
                with db.read() as conn:
                    recorded = float(
                        conn.execute(
                            "SELECT COALESCE(SUM(amount), 0) FROM credit_events "
                            "WHERE account_id = ? AND created_at >= ?",
                            (account_id, int(previous["updated_at"])),
                        ).fetchone()[0]
                    )
                recharge = increased - recorded
                if recharge > 0.01:
                    with db.tx() as conn:
                        conn.execute(
                            "INSERT INTO credit_events(account_id, amount, reason, created_at) "
                            "VALUES(?,?,?,?)",
                            (account_id, recharge, "recharge", now),
                        )
            with db.tx() as conn:
                conn.execute(
                    "UPDATE credit_balance SET balance = ?, updated_at = ? "
                    "WHERE account_id = ?",
                    (balance, now, account_id),
                )
    return balance, used_today


def cached_credit(db: Any, account_id: str) -> dict[str, Any]:
    """只读本地记录的积分信息，不请求上游。"""
    with db.read() as conn:
        balance_row = conn.execute(
            "SELECT balance, updated_at FROM credit_balance WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        daily_row = conn.execute(
            "SELECT baseline, latest FROM credit_daily "
            "WHERE account_id = ? AND day = ?",
            (account_id, dt.date.today().isoformat()),
        ).fetchone()
    used = None
    if daily_row is not None:
        used = max(0.0, float(daily_row["latest"]) - float(daily_row["baseline"]))
    return {
        "balance": float(balance_row["balance"]) if balance_row else None,
        "used_today": used,
        "updated_at": int(balance_row["updated_at"]) if balance_row else 0,
    }


class AccountMaintenance:
    """给全部账号做的定时与手动操作，统一走这里以保持错峰。"""

    def __init__(
        self,
        *,
        config: Any,
        db: Any,
        accounts: Any,
        refresher: Any,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.config = config
        self.db = db
        self.accounts = accounts
        self.refresher = refresher
        self.transport = transport
        self._running: set[str] = set()
        self._manual: set[str] = set()
        # 正在进行的任务已处理的账号数，给页面显示进度
        self._progress: dict[str, int] = {}
        # 模型目录的刷新入口和缓存由应用工厂在创建后接上
        self.catalog_for: Optional[Callable[[str], Any]] = None
        self.catalog_cache: Any = None

    # -- settings ---------------------------------------------------------

    @property
    def stagger_seconds(self) -> int:
        return max(0, int(getattr(self.config, "account_stagger_seconds", 0) or 0))

    @property
    def jitter_seconds(self) -> int:
        return max(0, int(getattr(self.config, "account_jitter_seconds", 0) or 0))

    @property
    def margin_seconds(self) -> int:
        hours = int(getattr(self.config, "token_refresh_margin_hours", 24) or 24)
        return max(1, min(24 * 14, hours)) * 3600

    def slots(self, job: str) -> list[str]:
        if job == "token":
            text = normalize_time(getattr(self.config, "token_refresh_time", ""))
            return [text] if text else []
        if job == "keepalive":
            text = normalize_time(getattr(self.config, "keepalive_time", ""))
            return [text] if text else []
        if job == "credits":
            return normalize_times(getattr(self.config, "credit_refresh_times", ""))
        return []

    # -- stagger ----------------------------------------------------------

    def active(self) -> list[Any]:
        return [
            record for record in self.accounts.list() if record.status == STATUS_ACTIVE
        ]

    @staticmethod
    def order(records: list[Any]) -> list[Any]:
        # 每次打乱顺序，避免总是同一个账号先发请求
        shuffled = list(records)
        random.shuffle(shuffled)
        return shuffled

    def gap(self) -> float:
        return self.stagger_seconds + random.uniform(0, self.jitter_seconds)

    # -- persisted progress -----------------------------------------------

    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.db.get_setting(STATE_KEY))
        except (TypeError, ValueError):
            data = None
        state: dict[str, Any] = {"day": "", "done": {}, "safety": "", "last": {}}
        if isinstance(data, dict):
            for key in state:
                if isinstance(data.get(key), type(state[key])):
                    state[key] = data[key]
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        self.db.set_setting(STATE_KEY, json.dumps(state, ensure_ascii=False))

    def _today_state(self, now: dt.datetime) -> dict[str, Any]:
        """当天的进度；跨天后清空已完成清单，但保留上次结果用于展示。"""
        state = self._state()
        today = now.date().isoformat()
        if state["day"] != today:
            state = {
                "day": today,
                "done": {},
                "safety": "",
                "last": state["last"],
            }
        return state

    @staticmethod
    def _at(day: dt.date, text: str) -> int:
        hour, minute = (int(part) for part in text.split(":"))
        return int(dt.datetime.combine(day, dt.time(hour, minute)).timestamp())

    def next_run(self, job: str, now: Optional[dt.datetime] = None) -> Optional[int]:
        """这个任务下一次开始的时刻；没有配置时刻时为 None。"""
        slots = self.slots(job)
        if not slots:
            return None
        now = now or dt.datetime.now()
        state = self._today_state(now)
        done = state["done"].get(job, [])
        for slot in slots:
            if slot in done:
                continue
            start = self._at(now.date(), slot)
            return max(start, int(now.timestamp()))
        return self._at(now.date() + dt.timedelta(days=1), slots[0])

    def safety_due(self, now: dt.datetime, state: dict[str, Any]) -> bool:
        return state["safety"] != f"{state['day']} {now.hour}"

    def next_safety(self, now: Optional[dt.datetime] = None) -> int:
        """下一次兜底检查的时刻：本小时还没做过就是现在，否则是下一个整点。"""
        now = now or dt.datetime.now()
        if self.safety_due(now, self._today_state(now)):
            return int(now.timestamp())
        return int(
            (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)).timestamp()
        )

    # -- scheduling -------------------------------------------------------

    async def tick(self, now: Optional[dt.datetime] = None) -> list[str]:
        """定时调用：到点的任务跑一轮。返回这次启动了哪些任务。"""
        now = now or dt.datetime.now()
        self.refresher.clear_expired_cooldowns()
        state = self._today_state(now)
        started: list[str] = []
        for job in SCHEDULED_JOBS:
            if job in self._running:
                continue
            done = state["done"].get(job, [])
            slot = next(
                (
                    text
                    for text in self.slots(job)
                    if text not in done
                    and self._at(now.date(), text) <= now.timestamp()
                ),
                None,
            )
            if slot is None:
                continue
            await self.run(job, "auto")
            state = self._today_state(now)
            state["done"].setdefault(job, []).append(slot)
            self._save_state(state)
            started.append(job)
        if "token" not in self._running and self.safety_due(now, state):
            await self.run("token", "safety")
            state = self._today_state(now)
            state["safety"] = f"{state['day']} {now.hour}"
            self._save_state(state)
            started.append("safety")
        return started

    def start_manual(self, job: str, *, force: bool = False) -> bool:
        """在后台启动一轮手动任务；同一任务已在进行时返回 False。

        ``force`` 用于模型目录：连仍在有效期内的账号也重新拉取。
        """
        if job not in JOB_ACTIONS or job in self._running or job in self._manual:
            return False
        self._manual.add(job)
        trigger = "force" if force else "manual"
        asyncio.get_running_loop().create_task(self._manual_run(job, trigger))
        return True

    async def _manual_run(self, job: str, trigger: str) -> None:
        self._manual.discard(job)
        try:
            await self.run(job, trigger)
        except Exception:
            logger.exception("manual %s failed", job)

    async def run(self, job: str, trigger: str = "auto") -> dict[str, Any]:
        """给全部启用账号各执行一遍，账号之间错峰。"""
        actions = JOB_ACTIONS[job]
        self._running.add(job)
        self._progress[job] = 0
        started = int(time.time())
        try:
            items: list[dict[str, Any]] = []
            previous = False
            for record in self.order(self.active()):
                self._progress[job] = len(items)
                # 不需要发请求的账号直接记为跳过，不占用错峰间隔
                noop = self._noop(actions, record, trigger)
                if noop:
                    items.append(self._item(record, "skipped", noop))
                    continue
                if previous:
                    await asyncio.sleep(self.gap())
                previous = True
                status = "ok"
                details: list[str] = []
                for action in actions:
                    action_status, detail = await self._act(action, record, job, trigger)
                    status = WORSE_STATUS[(status, action_status)]
                    if detail:
                        details.append(detail)
                items.append(self._item(record, status, "；".join(details)))
            summary = {
                "job": job,
                "trigger": trigger,
                "label": JOB_LABELS[job],
                "at": started,
                "finished": int(time.time()),
                "total": len(items),
                "ok": sum(1 for item in items if item["status"] == "ok"),
                "failed": sum(1 for item in items if item["status"] == "failed"),
                "skipped": sum(1 for item in items if item["status"] == "skipped"),
                "items": items,
            }
            # 兜底检查的结果单独存，不覆盖当天定时刷新的结果
            result_key = "safety" if trigger == "safety" else job
            state = self._state()
            state.setdefault("last", {})[result_key] = summary
            self._save_state(state)
            logger.info(
                "maintenance %s (%s): ok=%s failed=%s skipped=%s",
                job, trigger, summary["ok"], summary["failed"], summary["skipped"],
            )
            return summary
        finally:
            # 结果保存之后才解除「进行中」，否则状态查询会看到既没在跑也没有结果
            self._running.discard(job)
            self._progress.pop(job, None)

    @staticmethod
    def _item(record: Any, status: str, detail: str) -> dict[str, Any]:
        return {
            "account_id": record.id,
            "label": str(record.label or record.username or record.id),
            "status": status,
            "detail": detail,
        }

    def _noop(self, actions: tuple[str, ...], record: Any, trigger: str) -> str:
        """这一轮对这个账号是否完全不用请求上游；是的话返回跳过原因。"""
        if "token" in actions:
            return self._token_skip_reason(record, trigger)
        if "catalog" in actions and trigger != "force" and self.catalog_cache is not None:
            if self.catalog_cache.get(record.id):
                return "目录仍在有效期内"
        return ""

    async def _act(
        self, action: str, record: Any, job: str, trigger: str
    ) -> tuple[str, str]:
        try:
            if action == "token":
                return await self._token(record, trigger)
            if action == "catalog":
                return await self._catalog(record)
            secrets = self.accounts.get_secrets(record.id)
            if not secrets.token:
                return "failed", "账号没有令牌"
            if action == "profile":
                return await self._profile(record, secrets.token)
            return await self._credits(record, secrets.token)
        except Exception as exc:
            logger.exception("maintenance action %s failed for %s", action, record.id)
            return "failed", f"{type(exc).__name__}: {str(exc)[:120]}"

    def _token_skip_reason(self, record: Any, trigger: str) -> str:
        """定时刷新和兜底检查只处理快到期的令牌；返回空串表示需要刷新。"""
        if trigger == "manual":
            return ""
        if not record.has_refresh_token:
            return "没有刷新令牌"
        limit = SAFETY_WINDOW if trigger == "safety" else self.margin_seconds
        secrets = self.accounts.get_secrets(record.id)
        remaining = int(secrets.token_expires_at or 0) - int(time.time())
        if remaining > limit:
            return f"令牌还有 {remaining // 3600} 小时才到期"
        return ""

    async def _catalog(self, record: Any) -> tuple[str, str]:
        if self.catalog_for is None:
            return "skipped", "模型目录刷新没有接入"
        result = await asyncio.to_thread(self.catalog_for, record.id)
        count = int(result.get("model_count") or 0) if isinstance(result, dict) else 0
        if not count:
            return "failed", "没有取到模型目录"
        return "ok", f"目录已更新（{count} 个条目）"

    async def _token(self, record: Any, trigger: str) -> tuple[str, str]:
        force = trigger == "manual"
        result = await asyncio.to_thread(self.refresher.refresh, record.id, force=force)
        if result.ok and not result.skipped:
            how = "原生接口" if result.endpoint == "native" else "旧接口"
            return "ok", f"令牌已刷新（{how}）"
        if result.skipped:
            detail = result.message
            if result.message == "waiting to retry":
                detail = f"上次刷新失败，{max(1, result.retry_after // 60)} 分钟后重试"
            elif result.message == "not due":
                detail = "令牌未到刷新时间"
            return "skipped", detail
        detail = result.message or "刷新失败"
        if result.retry_after:
            detail = f"{detail}（{max(1, result.retry_after // 60)} 分钟后重试）"
        return "failed", detail

    async def _profile(self, record: Any, token: str) -> tuple[str, str]:
        result = await activity.user_info(
            self.config, token, transport=self.transport
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if not result.get("ok"):
            return "failed", str(result.get("message") or "资料获取失败")
        name = ""
        for key in PROFILE_NAME_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        if name and name != record.username:
            self.accounts.update_profile(record.id, username=name)
        return "ok", f"账号正常（{name or record.username or '未命名'}）"

    async def _credits(self, record: Any, token: str) -> tuple[str, str]:
        device = self.accounts.get_device(record.id)
        result = await activity.credit_usage(
            self.config, token, device, transport=self.transport
        )
        if not result.get("ok"):
            return "failed", str(result.get("message") or "积分获取失败")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        balance, _ = apply_credit_usage(self.db, record.id, data)
        plan = extract_plan(data, record.plan)
        if plan and plan != record.plan:
            self.accounts.update_profile(record.id, plan=plan)
        if balance is None:
            return "ok", "积分已更新"
        return "ok", f"余额 {balance:g}"

    # -- status -----------------------------------------------------------

    def endpoint_summary(self) -> dict[str, int]:
        """各账号的令牌刷新走哪个接口。"""
        summary = {"native": 0, "legacy": 0, "unknown": 0}
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key LIKE 'refresh:endpoint:%'"
            ).fetchall()
        remembered = {str(row["key"]).rsplit(":", 1)[-1]: str(row["value"]) for row in rows}
        for record in self.active():
            value = remembered.get(record.id, "")
            if value == "native":
                summary["native"] += 1
            elif value == "legacy":
                summary["legacy"] += 1
            else:
                summary["unknown"] += 1
        return summary

    def status(self, now: Optional[dt.datetime] = None) -> dict[str, Any]:
        now = now or dt.datetime.now()
        last = self._state().get("last") or {}
        total = len(self.active())
        jobs = {}
        for job, label in JOB_LABELS.items():
            running = self.is_running(job)
            jobs[job] = {
                "label": label,
                "slots": self.slots(job) if job in SCHEDULED_JOBS else [],
                "next_run": self.next_run(job, now) if job in SCHEDULED_JOBS else None,
                "running": running,
                "progress": {"done": self._progress.get(job, 0), "total": total}
                if running
                else None,
                "last": last.get(job),
            }
        return {
            "ok": True,
            "stagger_seconds": self.stagger_seconds,
            "jitter_seconds": self.jitter_seconds,
            "token_refresh_time": getattr(self.config, "token_refresh_time", ""),
            "token_refresh_margin_hours": self.margin_seconds // 3600,
            "keepalive_time": getattr(self.config, "keepalive_time", ""),
            "credit_refresh_times": self.slots("credits"),
            "invalid_times": {
                "token_refresh_time": bool(getattr(self.config, "token_refresh_time", ""))
                and not self.slots("token"),
                "keepalive_time": bool(getattr(self.config, "keepalive_time", ""))
                and not self.slots("keepalive"),
                "credit_refresh_times": bool(getattr(self.config, "credit_refresh_times", ""))
                and not self.slots("credits"),
            },
            "safety": {"next_run": self.next_safety(now), "last": last.get("safety")},
            "jobs": jobs,
            "endpoints": self.endpoint_summary(),
        }

    def is_running(self, job: str) -> bool:
        return job in self._running or job in self._manual
