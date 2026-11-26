from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import os
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .accounts import importer, oauth
from .accounts.device_import import load_device_override
from .accounts.router import AccountRouter
from .accounts.models import (
    ImportedAccount,
    build_device,
    generate_keypair,
    normalize_device,
)
from .accounts.refresher import TokenRefresher
from .accounts.store import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    AccountStore,
)
from .admin import AdminStore, LoginLimiter
from . import update as update_service
from . import checkin as checkin_service
from . import maintenance as maintenance_service
from .admin_console import create_admin_console_router
from .api.openai_routes import create_openai_router
from .api_keys import ApiKeyStore
from .config import Config
from .db import Database
from . import import_handoff
from .events import EventBus
from .oneclick import build_bat
from .security import (
    SecretBox,
    jwt_expiry,
    jwt_user_id,
    new_token,
    now_ts,
)
from .upstream import catalog
from .upstream import model_catalog

logger = logging.getLogger("trae_mouth")
VERSION = "0.3.1"

SESSION_COOKIE = "tm_session"
CSRF_COOKIE = "tm_csrf"


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------
class SetupBody(BaseModel):
    username: str = "admin"
    password: str


class LoginBody(BaseModel):
    username: str = "admin"
    password: str


class ImportLocalBody(BaseModel):
    path: str = ""


class ManualImportBody(BaseModel):
    token: str = ""
    refresh_token: str = ""
    user_id: str = ""
    host: str = ""
    client_id: str = ""
    label: str = ""
    username: str = ""


class CodeBody(BaseModel):
    ttl: int = Field(default=600, ge=30, le=86400)


class CodeCheckBody(BaseModel):
    import_code: str


class PatchAccountBody(BaseModel):
    label: Optional[str] = None
    note: Optional[str] = None
    disabled: Optional[bool] = None
    concurrency_limit: Optional[int] = Field(default=None, ge=1, le=32)


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class ApiKeyCreateBody(BaseModel):
    name: str = ""
    note: str = ""
    scopes: str = "chat,models"
    allowed_ips: str = ""
    quota_tokens: int = Field(default=0, ge=0)
    quota_requests: int = Field(default=0, ge=0)
    expires_at: Optional[int] = None


class ApiKeyPatchBody(BaseModel):
    name: Optional[str] = None
    note: Optional[str] = None
    scopes: Optional[str] = None
    allowed_ips: Optional[str] = None
    quota_tokens: Optional[int] = Field(default=None, ge=0)
    quota_requests: Optional[int] = Field(default=None, ge=0)
    expires_at: Optional[int] = None
    disabled: Optional[bool] = None


class PublicOAuthBody(BaseModel):
    import_code: str
    callback: dict[str, Any] = Field(default_factory=dict)
    device: Optional[dict[str, Any]] = None
    client_id: str = ""
    auth_url: str = ""
    login_trace_id: str = ""
    helper_version: str = ""


class PublicStorageBody(BaseModel):
    import_code: str
    encrypted_value: str
    edition: str = "cn"
    storage_path: str = ""
    server_data: str = ""
    source: str = "local_storage"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _client_key(request: Request, username: str = "") -> str:
    host = request.client.host if request.client else "unknown"
    return f"{host}:{username.strip().lower()}"


def _is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


def _relay_base(request: Request, config: Config) -> str:
    if config.public_base_url:
        return config.public_base_url
    proto = request.url.scheme
    host = request.headers.get("host", "")
    if config.trust_proxy:
        proto = request.headers.get("x-forwarded-proto", proto).split(",")[0].strip()
        host = request.headers.get("x-forwarded-host", host).split(",")[0].strip()
    base = f"{proto}://{host}".rstrip("/") if host else str(request.base_url).rstrip("/")
    parsed = urlsplit(base)
    if parsed.hostname in {"0.0.0.0", "::", "[::]"}:
        port = f":{parsed.port}" if parsed.port else ""
        base = urlunsplit((parsed.scheme, f"127.0.0.1{port}", "", "", ""))
    return base.rstrip("/")


def _set_session(response: Response, config: Config, session: str, csrf: str) -> None:
    max_age = config.session_ttl
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=config.cookie_secure,
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=config.cookie_secure,
        path="/",
    )


def _clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _ensure_device(store: AccountStore, account_id: str, config: Config):
    record = store.get(account_id)
    device = store.get_device(account_id)
    private_key = store.get_device_private_key(account_id)
    if record and record.source in {"local_import", "upload"}:
        override = load_device_override(config)
        if override:
            if device and device.web_id:
                override.web_id = device.web_id
            if not override.public_key and device and device.public_key:
                override.public_key = device.public_key
            if not override.private_key_pem and private_key:
                override.private_key_pem = private_key
            if not override.public_key or not override.private_key_pem:
                override.public_key, override.private_key_pem = generate_keypair()
            baseline = device.to_dict() if device else None
            key_changed = bool(override.private_key_pem) and (
                override.private_key_pem != private_key
            )
            if baseline is None or override.to_dict() != baseline or key_changed:
                store.set_device(account_id, override)
            return override
    if (
        device
        and device.public_key
        and private_key
        and device.device_name
        and device.device_model
    ):
        device.private_key_pem = private_key
        if (
            device.ide_version != config.ide_version
            or device.ide_version_code != config.ide_version_code
        ):
            device.ide_version = config.ide_version
            device.ide_version_code = config.ide_version_code
            store.set_device(account_id, device)
        return device
    before: Optional[dict[str, Any]] = None
    if device:
        before = device.to_dict()
        device = normalize_device(
            device.to_dict(),
            app_version=config.login_app_version,
            ide_version=config.ide_version,
            ide_version_code=config.ide_version_code,
        )
        if not device.private_key_pem and private_key and device.public_key:
            device.private_key_pem = private_key
    else:
        device = build_device(
            app_version=config.login_app_version,
            ide_version=config.ide_version,
            ide_version_code=config.ide_version_code,
        )
    if not device.public_key or not device.private_key_pem:
        device.public_key, device.private_key_pem = generate_keypair()
    if before is None or device.to_dict() != before:
        store.set_device(account_id, device)
    return device


def _import_kwargs(config: Config) -> dict[str, Any]:
    return {
        "default_client_id": config.trae_client_id,
        "default_host": config.trae_auth_host,
        "app_version": config.login_app_version,
        "ide_version": config.ide_version,
        "ide_version_code": config.ide_version_code,
    }


# --------------------------------------------------------------------------
# app factory
# --------------------------------------------------------------------------
def _acquire_instance_lock(path: os.PathLike[str] | str) -> int:
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"1")
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise RuntimeError(
            "another Trae Mouth instance is already using this data directory"
        ) from exc
    return fd


def _release_instance_lock(fd: int) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


PERSISTED_SETTING_KEYS = (
    "global_ide",
    "native_tools",
    "default_concurrency",
    "account_cooldown",
    "account_failover_limit",
    "first_token_timeout",
    "model_catalog_ttl",
    "refresh_interval",
    "reasoning_as_content",
    "strict_tool_args",
    "tool_duplicate_warning",
    "prewarm_catalogs",
    "max_request_bytes",
    "tool_result_max_chars",
    "history_max_chars",
    "history_max_messages",
    "model_gateway",
    "web_api_base",
    "checkin_auto",
    "checkin_auto_time",
    "account_stagger_seconds",
    "account_jitter_seconds",
    "token_refresh_time",
    "token_refresh_margin_hours",
    "keepalive_time",
    "credit_refresh_times",
)


def _apply_persisted_settings(config: Config, db: Database) -> None:
    for key in PERSISTED_SETTING_KEYS:
        raw = db.get_setting("config:" + key)
        if raw == "":
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if hasattr(config, key):
            setattr(config, key, value)
    # 提前量按小时设置，刷新器用的是秒
    hours = int(getattr(config, "token_refresh_margin_hours", 24) or 24)
    config.refresh_window = max(1, min(24 * 14, hours)) * 3600


# Web 目录只给非 IDE 通道和推理档位提示用，最多这么久更新一次
WEB_CATALOG_TTL = 24 * 3600
WEB_CATALOG_GROUPS = ("remote", "work", "design")


def _carry_web_catalog(
    result: dict[str, Any], previous: Optional[dict[str, Any]]
) -> None:
    """这次没拉 Web 目录时，把上次的 Web 部分和推理档位提示接到新目录上。"""
    if not previous:
        return
    groups = result.setdefault("groups", {})
    old_groups = previous.get("groups") or {}
    for key in WEB_CATALOG_GROUPS:
        if isinstance(old_groups.get(key), dict):
            groups[key] = old_groups[key]
    for key in ("ide", "solo"):
        old_models = {
            str(model.get("config_name") or ""): model
            for model in (old_groups.get(key) or {}).get("models") or []
            if isinstance(model, dict)
        }
        group = groups.get(key)
        for model in (group.get("models") or []) if isinstance(group, dict) else []:
            reasoning = model.get("reasoning") or {}
            hint = (old_models.get(str(model.get("config_name") or "")) or {}).get(
                "reasoning"
            ) or {}
            if reasoning.get("supported") and not reasoning.get("levels") and hint.get("levels"):
                model["reasoning"] = dict(hint)
    result["model_count"] = sum(
        int(group.get("model_count") or 0)
        for group in groups.values()
        if isinstance(group, dict)
    )


async def _scheduled_update_check() -> None:
    while True:
        now = dt.datetime.now()
        next_run = (now + dt.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep(max(1.0, (next_run - now).total_seconds()))
        try:
            await asyncio.to_thread(update_service.check_update, VERSION, force=True)
        except Exception:
            logger.exception("scheduled update check failed")


async def _auto_checkin_loop(service: Any) -> None:
    auto = checkin_service.AutoCheckin(service)
    while True:
        await asyncio.sleep(20)
        try:
            await auto.tick()
        except Exception:
            logger.exception("auto check-in failed")


async def _maintenance_loop(service: Any) -> None:
    while True:
        await asyncio.sleep(20)
        try:
            await service.tick()
        except Exception:
            logger.exception("account maintenance failed")


def create_app(
    config: Optional[Config] = None,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> FastAPI:
    config = config or Config.load()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db = Database(config.db_path)
    db.migrate()
    _apply_persisted_settings(config, db)
    secrets = SecretBox(config.master_key)
    accounts = AccountStore(db, secrets, default_concurrency=config.default_concurrency)
    api_keys = ApiKeyStore(db)
    admin = AdminStore(db)
    limiter = LoginLimiter()
    events = EventBus()
    checkin = checkin_service.CheckinService(
        config=config, db=db, accounts=accounts, transport=transport
    )
    catalog_cache = model_catalog.CatalogCache(ttl=config.model_catalog_ttl)
    refresher = TokenRefresher(
        accounts,
        refresh_window=config.refresh_window,
        interval=config.refresh_interval,
        transport=transport,
        client_id=config.trae_client_id,
        app_version=config.native_app_version,
        ide_version=config.ide_version,
        ide_version_code=config.ide_version_code,
        use_device_info=config.refresh_device_info,
    )
    maintenance = maintenance_service.AccountMaintenance(
        config=config,
        db=db,
        accounts=accounts,
        refresher=refresher,
        transport=transport,
    )

    http_client: Optional[httpx.AsyncClient] = None
    if transport is None:
        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=30.0, pool=30.0),
            limits=httpx.Limits(
                max_connections=100, max_keepalive_connections=20
            ),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        lock_fd = _acquire_instance_lock(config.data_dir / "trae_mouth.lock")
        events.bind_loop(asyncio.get_running_loop())
        update_service.reconcile(config, VERSION)
        if config.admin_password and not admin.has_admin():
            try:
                admin.create_admin(config.admin_username, config.admin_password)
                logger.info("created initial admin from environment")
            except Exception:
                logger.exception("failed to bootstrap admin")
        import_handoff.clear_expired(config)
        refresher.start()
        update_task = asyncio.create_task(_scheduled_update_check())
        auto_checkin_task = asyncio.create_task(_auto_checkin_loop(checkin))
        maintenance_task = asyncio.create_task(_maintenance_loop(maintenance))
        try:
            yield
        finally:
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass
            auto_checkin_task.cancel()
            try:
                await auto_checkin_task
            except asyncio.CancelledError:
                pass
            maintenance_task.cancel()
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass
            refresher.stop()
            if http_client is not None:
                await http_client.aclose()
            _release_instance_lock(lock_fd)

    app = FastAPI(
        title="Trae Mouth",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.db = db
    app.state.accounts = accounts
    app.state.admin = admin
    app.state.api_keys = api_keys
    app.state.limiter = limiter
    app.state.refresher = refresher
    app.state.events = events
    app.state.catalog_cache = catalog_cache
    app.state.transport = transport
    app.state.http_client = http_client
    app.state.checkin = checkin
    app.state.maintenance = maintenance
    app.state.started_at = now_ts()
    account_router = AccountRouter(
        accounts,
        affinity_ttl=config.conversation_affinity_ttl,
        cooldown=config.account_cooldown,
    )
    app.state.account_router = account_router

    # -- middleware -----------------------------------------------------
    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path
        limit = int(getattr(config, "max_request_bytes", 0) or 0)
        if limit > 0 and request.method in {"POST", "PUT", "PATCH"}:
            raw_length = request.headers.get("content-length", "")
            if raw_length:
                try:
                    if int(raw_length) > limit:
                        return JSONResponse(
                            {"detail": "request body too large"},
                            status_code=413,
                        )
                except ValueError:
                    return JSONResponse(
                        {"detail": "invalid content-length"}, status_code=400
                    )
        if (
            path.startswith("/admin/api/")
            and request.method in {"POST", "PATCH", "DELETE"}
            and path not in {"/admin/api/login", "/admin/api/setup"}
        ):
            cookie = request.cookies.get(CSRF_COOKIE, "")
            header = request.headers.get("x-csrf-token", "")
            if not cookie or cookie != header:
                return JSONResponse(
                    {"detail": "CSRF token mismatch"}, status_code=403
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path.startswith("/admin/assets/"):
            response.headers.setdefault(
                "Cache-Control", "public, max-age=31536000, immutable"
            )
        elif path.startswith("/admin"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # -- dependencies ---------------------------------------------------
    def current_admin(request: Request) -> str:
        token = request.cookies.get(SESSION_COOKIE, "")
        user_id = admin.get_session(token)
        if not user_id:
            raise HTTPException(status_code=401, detail="login required")
        return user_id

    # -- pages ----------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/admin", status_code=302)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, Any]:
        rows = accounts.list()
        return {
            "ok": True,
            "version": VERSION,
            "accounts": len(rows),
            "active": sum(1 for row in rows if row.status == STATUS_ACTIVE),
            "mode": "IDE" if config.global_ide else "R / W / IDE",
            "time": now_ts(),
        }

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        rows = accounts.list()
        summary = api_keys.usage_summary(days=1)
        requests = sum(int(row.get("requests") or 0) for row in summary["rows"])
        errors = sum(int(row.get("errors") or 0) for row in summary["rows"])
        total_tokens = sum(
            int(row.get("total_tokens") or 0) for row in summary["rows"]
        )
        lines = [
            "# HELP trae_mouth_accounts Total account count",
            "# TYPE trae_mouth_accounts gauge",
            f"trae_mouth_accounts {len(rows)}",
            "# HELP trae_mouth_active_accounts Active account count",
            "# TYPE trae_mouth_active_accounts gauge",
            f"trae_mouth_active_accounts {sum(1 for row in rows if row.status == STATUS_ACTIVE)}",
            "# HELP trae_mouth_requests_24h Requests in the last 24 hours",
            "# TYPE trae_mouth_requests_24h gauge",
            f"trae_mouth_requests_24h {requests}",
            "# HELP trae_mouth_errors_24h Error requests in the last 24 hours",
            "# TYPE trae_mouth_errors_24h gauge",
            f"trae_mouth_errors_24h {errors}",
            "# HELP trae_mouth_tokens_24h Total tokens in the last 24 hours",
            "# TYPE trae_mouth_tokens_24h gauge",
            f"trae_mouth_tokens_24h {total_tokens}",
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain")

    # -- admin auth -----------------------------------------------------
    @app.post("/admin/api/setup")
    def setup(body: SetupBody, response: Response) -> dict[str, Any]:
        if admin.has_admin():
            raise HTTPException(status_code=409, detail="admin already initialized")
        username = (body.username or "admin").strip() or "admin"
        try:
            user_id = admin.create_admin(username, body.password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session = admin.create_session(user_id, config.session_ttl)
        csrf = new_token(24)
        _set_session(response, config, session, csrf)
        return {"ok": True, "username": username, "csrf": csrf}

    @app.post("/admin/api/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        key = _client_key(request, body.username)
        if not limiter.allow(key):
            raise HTTPException(status_code=429, detail="too many login attempts")
        user_id = admin.verify(body.username, body.password)
        if not user_id:
            limiter.fail(key)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        limiter.reset(key)
        admin.audit(user_id, "login", _client_key(request, body.username))
        session = admin.create_session(user_id, config.session_ttl)
        csrf = new_token(24)
        _set_session(response, config, session, csrf)
        return {"ok": True, "username": body.username, "csrf": csrf}

    @app.post("/admin/api/logout")
    def logout(
        request: Request,
        response: Response,
        _: str = Depends(current_admin),
    ) -> dict[str, Any]:
        admin.delete_session(request.cookies.get(SESSION_COOKIE, ""))
        _clear_session(response)
        return {"ok": True}

    @app.get("/admin/api/me")
    def me(user_id: str = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "username": admin.username(user_id)}

    @app.post("/admin/api/password")
    def change_password(
        body: PasswordBody,
        user_id: str = Depends(current_admin),
    ) -> dict[str, Any]:
        if not admin.change_password(
            user_id, body.current_password, body.new_password
        ):
            raise HTTPException(status_code=400, detail="当前密码不正确")
        admin.audit(user_id, "password_change", "")
        return {"ok": True}

    def _model_catalog_for(
        account_id: str, force: bool = False, *, background: bool = True
    ) -> Optional[dict[str, Any]]:
        """取一个账号的模型目录。

        ``background`` 为 False 时只读缓存：有新鲜的用新鲜的，否则用过期的，都没有就
        返回空，既不现取也不另起线程。列模型这类只读场景用它，刷新交给错峰的后台任务。
        """
        record = accounts.get(account_id)
        if not record:
            raise HTTPException(status_code=404, detail="account not found")
        cached = catalog_cache.get(account_id, force=force)
        if cached:
            return cached
        stale = catalog_cache.get_stale(account_id)
        if not force and stale:
            if background and catalog_cache.begin_refresh(account_id):
                def _refresh() -> None:
                    try:
                        _model_catalog_for(account_id, True)
                    except Exception:
                        logger.warning(
                            "background catalog refresh failed for %s",
                            account_id,
                            exc_info=True,
                        )
                    finally:
                        catalog_cache.end_refresh(account_id)

                threading.Thread(
                    target=_refresh,
                    name=f"catalog-refresh-{account_id}",
                    daemon=True,
                ).start()
            return stale
        if not force and not background:
            return None
        secrets = accounts.get_secrets(account_id)
        if (
            secrets.token_expires_at
            and secrets.token_expires_at <= now_ts() + 60
            and secrets.refresh_token
        ):
            refresher.refresh(account_id)
            record = accounts.get(account_id) or record
            secrets = accounts.get_secrets(account_id)
        if not secrets.token:
            raise HTTPException(status_code=400, detail="account has no token")
        device = _ensure_device(accounts, account_id, config)
        # Web 目录只在通道需要它、或作为推理档位提示太久没更新时才拉
        need_web = not config.global_ide or not catalog_cache.web_fresh(
            account_id, WEB_CATALOG_TTL
        )
        result = model_catalog.fetch_catalog(
            config,
            secrets.token,
            record.user_id,
            device,
            transport=transport,
            include_web=need_web,
        )
        if not need_web:
            _carry_web_catalog(result, stale)
        result["ok"] = bool(result.get("groups"))
        result["account_id"] = account_id
        result["account"] = record.to_public()
        catalog_cache.put(account_id, result, full=need_web)
        return result

    # 后台任务用它刷新目录；只在需要时拉 Web 部分，与上面的逻辑保持一致
    maintenance.catalog_for = lambda account_id: _model_catalog_for(account_id, True)
    maintenance.catalog_cache = catalog_cache

    # -- account reads ---------------------------------------------------
    @app.get("/admin/api/accounts")
    def list_accounts(_: str = Depends(current_admin)) -> dict[str, Any]:
        rows = [row.to_public() for row in accounts.list()]
        return {"ok": True, "accounts": rows, "server_time": now_ts()}

    @app.get("/admin/api/events")
    async def account_event_stream(
        request: Request, _: str = Depends(current_admin)
    ) -> StreamingResponse:
        queue = events.subscribe()

        async def generator():
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                events.unsubscribe(queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/admin/api/accounts/{account_id}")
    def get_account(
        account_id: str, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        record = accounts.get(account_id)
        if not record:
            raise HTTPException(status_code=404, detail="account not found")
        device = _ensure_device(accounts, account_id, config)
        return {
            "ok": True,
            "account": record.to_public(),
            "device": device.to_dict() if device else None,
        }

    @app.get("/admin/api/accounts/{account_id}/models")
    def account_models(
        account_id: str,
        refresh: int = 0,
        _: str = Depends(current_admin),
    ) -> dict[str, Any]:
        catalog = _model_catalog_for(account_id, force=bool(refresh))
        public = json.loads(json.dumps(catalog, ensure_ascii=False))
        for group in (public.get("groups") or {}).values():
            if not isinstance(group, dict):
                continue
            for model in group.get("models") or []:
                if isinstance(model, dict):
                    model.pop("raw", None)
        return public

    @app.post("/admin/api/accounts/{account_id}/models/refresh")
    def refresh_account_models(
        account_id: str, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        catalog = _model_catalog_for(account_id, force=True)
        public = json.loads(json.dumps(catalog, ensure_ascii=False))
        for group in (public.get("groups") or {}).values():
            if not isinstance(group, dict):
                continue
            for model in group.get("models") or []:
                if isinstance(model, dict):
                    model.pop("raw", None)
        return public

    @app.get("/admin/api/accounts/{account_id}/events")
    def account_events(
        account_id: str, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        if not accounts.get(account_id):
            raise HTTPException(status_code=404, detail="account not found")
        return {"ok": True, "events": accounts.events(account_id)}

    # -- imports ---------------------------------------------------------
    def _publish_account(record, kind: str = "account_changed") -> None:
        events.publish(
            {
                "type": kind,
                "account_id": record.id,
                "user_id": record.user_id,
                "username": record.username,
                "status": record.status,
            }
        )

    def _store_imported(imported: ImportedAccount, label: str = "") -> tuple[dict[str, Any], bool]:
        record, created = accounts.add_imported(imported, label=label, actor="admin")
        _publish_account(record, "account_imported" if created else "account_updated")
        return record.to_public(), created

    @app.post("/admin/api/accounts/import/local")
    def import_local(
        body: ImportLocalBody, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        if not config.allow_local_import:
            raise HTTPException(status_code=403, detail="local import disabled")
        imported: list[tuple[str, ImportedAccount]] = []
        import_kwargs = _import_kwargs(config)
        local_device = load_device_override(config)
        if local_device:
            import_kwargs["device"] = local_device
        if body.path:
            expanded = os.path.expandvars(os.path.expanduser(body.path))
            try:
                imported.append(
                    (
                        "cn",
                        importer.import_from_path(expanded, **import_kwargs),
                    )
                )
            except importer.ImportError_ as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            discovered = importer.discover_local_accounts(**import_kwargs)
            if not discovered:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "没有找到可导入的 Trae 登录信息。请确认本机已安装并登录 Trae CN，"
                        "或使用“上传 storage.json”。"
                    ),
                )
            imported = [(edition, account) for edition, _path, account in discovered]

        added: list[dict[str, Any]] = []
        created_count = 0
        updated_count = 0
        for _edition, account in imported:
            public, created = _store_imported(account)
            added.append(public)
            created_count += 1 if created else 0
            updated_count += 0 if created else 1
        return {
            "ok": True,
            "created": created_count,
            "updated": updated_count,
            "accounts": added,
        }

    @app.post("/admin/api/accounts/import/manual")
    def import_manual(
        body: ManualImportBody, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        token = body.token.strip()
        refresh_token = body.refresh_token.strip()
        user_id = body.user_id.strip()
        client_id = body.client_id.strip() or config.trae_client_id
        host = body.host.strip() or config.trae_auth_host
        token_expires_at = None
        refresh_expires_at = None
        if not token and refresh_token:
            try:
                exchanged = oauth.exchange_refresh_token(
                    refresh_token,
                    client_id=client_id,
                    user_id=user_id,
                    host=host,
                    transport=transport,
                )
            except importer.ImportError_ as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            token = exchanged["token"]
            refresh_token = exchanged["refresh_token"]
            user_id = user_id or exchanged["user_id"]
            token_expires_at = exchanged.get("token_expires_at")
            refresh_expires_at = exchanged.get("refresh_expires_at")
        if not token:
            raise HTTPException(
                status_code=400, detail="token or refresh_token is required"
            )
        user_id = user_id or jwt_user_id(token)
        if not user_id:
            raise HTTPException(
                status_code=400, detail="cannot determine user id from token"
            )
        token_expires_at = token_expires_at or jwt_expiry(token)
        record, created = accounts.add_manual(
            token=token,
            user_id=user_id,
            refresh_token=refresh_token,
            host=host,
            client_id=client_id,
            label=body.label,
            username=body.username,
            token_expires_at=token_expires_at,
            refresh_expires_at=refresh_expires_at,
            device=build_device(
                app_version=config.login_app_version,
                ide_version=config.ide_version,
                ide_version_code=config.ide_version_code,
            ),
            actor="admin",
        )
        return {
            "ok": True,
            "created": 1 if created else 0,
            "account": record.to_public(),
        }

    @app.post("/admin/api/accounts/import/code")
    def create_code(
        body: CodeBody, request: Request, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        code, expires_at = accounts.create_import_code(
            created_by="admin", ttl=body.ttl or config.import_code_ttl
        )
        relay = _relay_base(request, config)
        if _is_loopback(request):
            import_handoff.write(config, code, expires_at, relay)
        return {
            "ok": True,
            "code": code,
            "expires_at": expires_at,
            "relay": relay,
            "bundle_url": f"{relay}/public/helper/bundle?code={code}",
            "oneclick_url": f"{relay}/public/helper/oneclick.bat?code={code}",
            "command": f'start_login.bat "{relay}" "{code}"',
        }

    # -- account actions -------------------------------------------------
    @app.post("/admin/api/accounts/{account_id}/refresh")
    def refresh_account(
        account_id: str, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        record = accounts.get(account_id)
        if not record:
            raise HTTPException(status_code=404, detail="account not found")
        result = refresher.refresh(account_id, force=True)
        record = accounts.get(account_id)
        if record:
            _publish_account(record)
        return {
            "ok": result.ok,
            "skipped": result.skipped,
            "message": result.message,
            "account": record.to_public() if record else None,
        }

    @app.post("/admin/api/accounts/{account_id}/test")
    def test_account(
        account_id: str, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        record = accounts.get(account_id)
        if not record:
            raise HTTPException(status_code=404, detail="account not found")
        secrets = accounts.get_secrets(account_id)
        if (
            secrets.token_expires_at
            and secrets.token_expires_at <= now_ts() + 60
            and secrets.refresh_token
        ):
            refresher.refresh(account_id)
            record = accounts.get(account_id) or record
            secrets = accounts.get_secrets(account_id)
        if not secrets.token:
            raise HTTPException(status_code=400, detail="account has no token")
        device = _ensure_device(accounts, account_id, config)
        result = catalog.test_account(
            config, secrets.token, record.user_id, device, transport=transport
        )
        if not result.get("ok"):
            accounts.mark_error(account_id, "account test failed")
        latest = accounts.get(account_id) or record
        _publish_account(latest)
        return {
            "ok": True,
            "result": result,
            "account": latest.to_public(),
        }

    @app.post("/admin/api/accounts/{account_id}/disable")
    def disable_account(
        account_id: str, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        if not accounts.get(account_id):
            raise HTTPException(status_code=404, detail="account not found")
        accounts.set_status(account_id, STATUS_DISABLED, "disabled by admin", actor="admin")
        record = accounts.get(account_id)
        _publish_account(record)
        return {"ok": True, "account": record.to_public()}

    @app.post("/admin/api/accounts/{account_id}/enable")
    def enable_account(
        account_id: str, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        record = accounts.get(account_id)
        if not record:
            raise HTTPException(status_code=404, detail="account not found")
        accounts.set_status(account_id, STATUS_ACTIVE, "", actor="admin")
        record = accounts.get(account_id)
        _publish_account(record)
        return {"ok": True, "account": record.to_public()}

    @app.patch("/admin/api/accounts/{account_id}")
    def patch_account(
        account_id: str,
        body: PatchAccountBody,
        _: str = Depends(current_admin),
    ) -> dict[str, Any]:
        if not accounts.get(account_id):
            raise HTTPException(status_code=404, detail="account not found")
        if body.label is not None:
            accounts.set_label(account_id, body.label.strip())
        if body.note is not None:
            accounts.set_note(account_id, body.note.strip())
        if body.disabled is not None:
            accounts.set_status(
                account_id,
                STATUS_DISABLED if body.disabled else STATUS_ACTIVE,
                "disabled by admin" if body.disabled else "",
                actor="admin",
            )
        if body.concurrency_limit is not None:
            accounts.set_concurrency(account_id, body.concurrency_limit)
        record = accounts.get(account_id)
        _publish_account(record)
        return {"ok": True, "account": record.to_public()}

    @app.delete("/admin/api/accounts/{account_id}")
    def delete_account(
        account_id: str, user_id: str = Depends(current_admin)
    ) -> dict[str, Any]:
        if not accounts.get(account_id):
            raise HTTPException(status_code=404, detail="account not found")
        accounts.delete(account_id, actor="admin")
        admin.audit(user_id, "account_delete", account_id)
        catalog_cache.invalidate(account_id)
        events.publish({"type": "account_deleted", "account_id": account_id})
        return {"ok": True}

    # -- settings --------------------------------------------------------
    @app.get("/admin/api/settings")
    def settings(request: Request, _: str = Depends(current_admin)) -> dict[str, Any]:
        payload = {
            "upstream_mode": "ide",
            "upstream_base_url": config.model_gateway,
            "model_gateway": config.model_gateway,
            "web_api_base": config.web_api_base,
            "auth_host": config.trae_auth_host,
            "refresh_window": config.refresh_window,
            "refresh_interval": config.refresh_interval,
            "default_concurrency": config.default_concurrency,
            "account_cooldown": config.account_cooldown,
            "account_failover_limit": config.account_failover_limit,
            "first_token_timeout": config.first_token_timeout,
            "model_catalog_ttl": config.model_catalog_ttl,
            "reasoning_as_content": config.reasoning_as_content,
            "strict_tool_args": config.strict_tool_args,
            "tool_duplicate_warning": config.tool_duplicate_warning,
            "global_ide": config.global_ide,
            "native_tools": config.native_tools,
            "prewarm_catalogs": config.prewarm_catalogs,
            "max_request_bytes": config.max_request_bytes,
            "tool_result_max_chars": config.tool_result_max_chars,
            "history_max_chars": config.history_max_chars,
            "history_max_messages": config.history_max_messages,
            "allow_local_import": config.allow_local_import,
            "client_id": config.trae_client_id,
            "checkin_auto": config.checkin_auto,
            "checkin_auto_time": config.checkin_auto_time,
            "account_stagger_seconds": config.account_stagger_seconds,
            "account_jitter_seconds": config.account_jitter_seconds,
            "token_refresh_time": config.token_refresh_time,
            "token_refresh_margin_hours": config.token_refresh_margin_hours,
            "keepalive_time": config.keepalive_time,
            "credit_refresh_times": config.credit_refresh_times,
            "version": VERSION,
        }
        return {
            "ok": True,
            "version": VERSION,
            "relay": _relay_base(request, config),
            "auth_host": config.trae_auth_host,
            "model_gateway": config.model_gateway,
            "web_api_base": config.web_api_base,
            "refresh_window": config.refresh_window,
            "refresh_interval": config.refresh_interval,
            "default_concurrency": config.default_concurrency,
            "allow_local_import": config.allow_local_import,
            "client_id": config.trae_client_id,
            "settings": payload,
            "raw": payload,
        }

    @app.get("/admin/api/update")
    def update_info(
        force: int = 0, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        return update_service.check_update(VERSION, force=bool(force))

    @app.post("/admin/api/update/apply")
    def update_apply(_: str = Depends(current_admin)) -> dict[str, Any]:
        try:
            return update_service.apply_update(config, VERSION)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc

    @app.get("/admin/api/update/status")
    def update_status(_: str = Depends(current_admin)) -> dict[str, Any]:
        return {"ok": True, "version": VERSION, "job": update_service.read_job(config)}

    @app.delete("/admin/api/update")
    def update_ack(_: str = Depends(current_admin)) -> dict[str, Any]:
        update_service.clear_job(config)
        return {"ok": True}

    @app.get("/admin/api/audit")
    def audit_events(
        limit: int = 100, _: str = Depends(current_admin)
    ) -> dict[str, Any]:
        return {"ok": True, "events": admin.audit_events(limit)}

    # -- api keys --------------------------------------------------------
    @app.get("/admin/api/keys")
    def list_api_keys(_: str = Depends(current_admin)) -> dict[str, Any]:
        return {
            "ok": True,
            "keys": [record.to_public() for record in api_keys.list()],
        }

    @app.post("/admin/api/keys")
    def create_api_key(
        body: ApiKeyCreateBody, user_id: str = Depends(current_admin)
    ) -> dict[str, Any]:
        record, token = api_keys.create(
            name=body.name,
            note=body.note,
            scopes=body.scopes,
            allowed_ips=body.allowed_ips,
            quota_tokens=body.quota_tokens,
            quota_requests=body.quota_requests,
            expires_at=body.expires_at,
        )
        admin.audit(user_id, "key_create", f"{record.id} {record.name}")
        return {"ok": True, "key": record.to_public(), "token": token}

    @app.patch("/admin/api/keys/{key_id}")
    def patch_api_key(
        key_id: str,
        body: ApiKeyPatchBody,
        _: str = Depends(current_admin),
    ) -> dict[str, Any]:
        fields = body.model_dump(exclude_unset=True)
        record = api_keys.update(key_id, fields)
        if record is None:
            raise HTTPException(status_code=404, detail="api key not found")
        return {"ok": True, "key": record.to_public()}

    @app.post("/admin/api/keys/{key_id}/reset")
    def reset_api_key(key_id: str, _: str = Depends(current_admin)) -> dict[str, Any]:
        record = api_keys.reset_usage(key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="api key not found")
        return {"ok": True, "key": record.to_public()}

    @app.delete("/admin/api/keys/{key_id}")
    def delete_api_key(
        key_id: str, user_id: str = Depends(current_admin)
    ) -> dict[str, Any]:
        if api_keys.get(key_id) is None:
            raise HTTPException(status_code=404, detail="api key not found")
        api_keys.delete(key_id)
        admin.audit(user_id, "key_delete", key_id)
        return {"ok": True}

    # -- usage -----------------------------------------------------------
    @app.get("/admin/api/usage")
    def usage_summary(
        days: int = 7,
        api_key_id: str = "",
        account_id: str = "",
        model: str = "",
        _: str = Depends(current_admin),
    ) -> dict[str, Any]:
        return {
            "ok": True,
            **api_keys.usage_summary(
                days=days,
                api_key_id=api_key_id,
                account_id=account_id,
                model=model,
            ),
            "keys": [record.to_public() for record in api_keys.list()],
        }

    @app.get("/admin/api/usage/events")
    def usage_events(
        limit: int = 100,
        api_key_id: str = "",
        account_id: str = "",
        model: str = "",
        _: str = Depends(current_admin),
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "events": api_keys.events(
                limit=limit,
                api_key_id=api_key_id,
                account_id=account_id,
                model=model,
            ),
        }

    # -- public helper endpoints ----------------------------------------
    @app.post("/public/accounts/import/check")
    def check_import_code(body: CodeCheckBody) -> dict[str, Any]:
        expires_at = accounts.check_import_code(body.import_code)
        if expires_at is None:
            raise HTTPException(
                status_code=403, detail="导入码无效、已使用或已过期"
            )
        return {"ok": True, "expires_at": expires_at}

    @app.post("/public/accounts/import/oauth")
    def public_import_oauth(body: PublicOAuthBody) -> dict[str, Any]:
        callback = dict(body.callback or {})
        if body.login_trace_id:
            callback.setdefault("login_trace_id", body.login_trace_id)
        trace = callback.get("login_trace_id") or body.login_trace_id
        if trace and body.login_trace_id and trace != body.login_trace_id:
            raise HTTPException(status_code=400, detail="login_trace_id mismatch")
        if not accounts.consume_import_code(body.import_code):
            raise HTTPException(
                status_code=403, detail="导入码无效、已使用或已过期"
            )
        import_handoff.clear(config, body.import_code)
        try:
            account = oauth.import_from_callback(
                callback,
                device_data=body.device,
                default_client_id=body.client_id or config.trae_client_id,
                default_host=config.trae_auth_host,
                app_version=config.login_app_version,
                ide_version=config.ide_version,
                ide_version_code=config.ide_version_code,
                transport=transport,
            )
        except importer.ImportError_ as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record, created = accounts.add_imported(
            account, actor="web_oauth"
        )
        _publish_account(record, "account_imported" if created else "account_updated")
        return {
            "ok": True,
            "created": created,
            "account": record.to_public(),
        }

    @app.post("/public/accounts/import/storage")
    def public_import_storage(body: PublicStorageBody) -> dict[str, Any]:
        if not accounts.consume_import_code(body.import_code):
            raise HTTPException(
                status_code=403, detail="导入码无效、已使用或已过期"
            )
        import_handoff.clear(config, body.import_code)
        import_kwargs = _import_kwargs(config)
        local_device = load_device_override(config)
        if local_device:
            import_kwargs["device"] = local_device
        try:
            storage = (
                {importer.SERVER_DATA_KEY: body.server_data}
                if body.server_data
                else None
            )
            account = importer.import_from_encrypted_value(
                body.encrypted_value,
                edition=body.edition or "cn",
                source=body.source or "local_storage",
                storage=storage,
                **import_kwargs,
            )
        except importer.ImportError_ as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record, created = accounts.add_imported(account, actor="helper")
        _publish_account(record, "account_imported" if created else "account_updated")
        return {
            "ok": True,
            "created": created,
            "account": record.to_public(),
        }

    @app.get("/public/helper/oneclick.bat", include_in_schema=False)
    def helper_oneclick(request: Request, code: str = "") -> Response:
        if not accounts.validate_import_code(code):
            raise HTTPException(status_code=403, detail="导入码无效或已过期")
        helper_path = config.root / "user tool" / "login_helper.py"
        if not helper_path.exists():
            raise HTTPException(status_code=404, detail="helper not packaged")
        relay = _relay_base(request, config)
        content = build_bat(relay, code, helper_path.read_bytes())
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="trae-mouth-login.bat"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/public/helper/bundle", include_in_schema=False)
    def helper_bundle(request: Request, code: str = "") -> Response:
        if not accounts.validate_import_code(code):
            raise HTTPException(status_code=403, detail="导入码无效或已过期")
        relay = _relay_base(request, config)
        helper_path = config.root / "user tool" / "login_helper.py"
        if not helper_path.exists():
            raise HTTPException(status_code=404, detail="helper not packaged")
        bat = f"""@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "RELAY={relay}"
set "CODE={code}"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [x] Python 3 not found. Install Python 3.10+ and enable "Add Python to PATH".
    pause
    exit /b 1
  )
  py -3 "%~dp0login_helper.py" --relay "%RELAY%" --code "%CODE%" %*
) else (
  python "%~dp0login_helper.py" --relay "%RELAY%" --code "%CODE%" %*
)
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo [x] Helper exited with code %RC%
  pause
)
exit /b %RC%
""".replace("\n", "\r\n")
        readme = """Trae Mouth 登录助手

1. 解压到任意目录。
2. 双击 start_login.bat。
3. 浏览器会自动打开 Trae 授权页，使用干净的 Chrome/Edge profile。
4. 登录完成后自动回传并关闭浏览器。

注意：
- 导入码 10 分钟内有效，只能使用一次。
- 助手只监听 127.0.0.1，不会暴露局域网端口。
- 如未安装 Python 3.10+，请先安装；安装时勾选 Add Python to PATH。
"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("start_login.bat", bat)
            zf.writestr("README.txt", readme)
            zf.write(helper_path, "login_helper.py")
        headers = {
            "Content-Disposition": 'attachment; filename="trae-mouth-login.zip"',
            "Cache-Control": "no-store",
        }
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers=headers,
        )

    app.include_router(
        create_openai_router(
            config=config,
            accounts=accounts,
            api_keys=api_keys,
            refresher=refresher,
            transport=transport,
            ensure_device=_ensure_device,
            model_catalog_for=_model_catalog_for,
            warm_catalogs=lambda: maintenance.start_manual("catalogs"),
            catalog_fresh=lambda account_id: bool(catalog_cache.get(account_id)),
            account_router=account_router,
            async_client=http_client,
        )
    )
    app.include_router(
        create_admin_console_router(
            config=config,
            db=db,
            accounts=accounts,
            api_keys=api_keys,
            admin=admin,
            refresher=refresher,
            catalog_cache=catalog_cache,
            account_router=account_router,
            model_catalog_for=_model_catalog_for,
            version=VERSION,
            started_at=app.state.started_at,
            transport=transport,
            checkin=checkin,
            maintenance=maintenance,
        )
    )

    admin_dist = config.root / "admin-web" / "dist"
    admin_index = admin_dist / "index.html"
    if admin_index.exists():
        app.mount(
            "/admin/assets",
            StaticFiles(directory=str(admin_dist / "assets")),
            name="admin-assets",
        )

        @app.get("/admin/logo.svg", include_in_schema=False)
        def admin_logo() -> FileResponse:
            return FileResponse(
                admin_dist / "logo.svg",
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        def _admin_index() -> FileResponse:
            return FileResponse(
                admin_index,
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/admin", include_in_schema=False)
        @app.get("/admin/", include_in_schema=False)
        def admin_root() -> FileResponse:
            return _admin_index()

        @app.get("/admin/{path:path}", include_in_schema=False)
        def admin_spa(path: str) -> Any:
            if path.startswith("api/") or path.startswith("assets/"):
                raise HTTPException(status_code=404, detail="not found")
            return _admin_index()

    if config.prewarm_catalogs:
        def _prewarm_catalogs() -> None:
            # 逐个账号预热并留出间隔，避免服务一启动就对全部账号同时拉目录
            previous = False
            for record in accounts.list():
                if record.status != STATUS_ACTIVE:
                    continue
                if previous:
                    time.sleep(max(0, int(config.account_stagger_seconds)))
                previous = True
                try:
                    _model_catalog_for(record.id, False)
                except Exception:
                    logger.warning(
                        "catalog prewarm failed for %s",
                        record.id,
                        exc_info=True,
                    )

        threading.Thread(
            target=_prewarm_catalogs,
            name="catalog-prewarm",
            daemon=True,
        ).start()

    return app
