from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        for raw in path.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                values[key] = value
    except OSError:
        pass
    return values


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


def _decode_key(raw: str) -> bytes:
    raw = raw.strip()
    if not raw:
        raise ValueError("empty master key")
    try:
        if raw.startswith("base64:"):
            return base64.urlsafe_b64decode(raw[7:] + "=" * (-len(raw[7:]) % 4))
        if raw.startswith("hex:"):
            return bytes.fromhex(raw[4:])
        if len(raw) == 64:
            try:
                return bytes.fromhex(raw)
            except ValueError:
                pass
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as exc:
        raise ValueError("master key must be base64/hex encoded") from exc


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def _load_master_key(data_dir: Path) -> bytes:
    raw = _env("TRAE_MOUTH_MASTER_KEY")
    if raw:
        key = _decode_key(raw)
    else:
        key_file = _resolve_path(
            _env("TRAE_MOUTH_MASTER_KEY_FILE", str(data_dir / "master.key")),
            data_dir,
        )
        if key_file.exists():
            raw_file = key_file.read_text("ascii").strip()
            key = _decode_key(raw_file)
        else:
            key = secrets.token_bytes(32)
            key_file.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(base64.urlsafe_b64encode(key).decode("ascii"))
    if len(key) != 32:
        raise ValueError(f"master key must be 32 bytes, got {len(key)}")
    return key


@dataclass
class Config:
    root: Path
    data_dir: Path
    db_path: Path
    master_key: bytes = field(repr=False)

    admin_username: str = "admin"
    admin_password: str = ""
    host: str = "127.0.0.1"
    port: int = 18792
    session_ttl: int = 7 * 86400
    import_code_ttl: int = 600
    refresh_window: int = 86400
    # 刷新令牌时先走带设备签名的原生接口，与真实客户端一致
    refresh_device_info: bool = True
    refresh_interval: int = 300
    default_concurrency: int = 3
    model_catalog_ttl: int = 3600
    api_key: str = ""
    reasoning_as_content: bool = True
    strict_tool_args: bool = True
    tool_result_max_chars: int = 120000
    history_max_chars: int = 1200000
    history_max_messages: int = 0
    tool_duplicate_warning: bool = True
    global_ide: bool = True
    native_tools: bool = False
    first_token_timeout: int = 60
    stream_heartbeat: int = 15
    conversation_affinity_ttl: int = 7 * 86400
    account_failover_limit: int = 2
    account_cooldown: int = 120
    prewarm_catalogs: bool = True
    max_request_bytes: int = 128 * 1024 * 1024
    # 每日自动签到：开关与当天的发起时刻（HH:MM）
    checkin_auto: bool = False
    checkin_auto_time: str = "09:00"
    # 账号维护：账号之间的间隔与抖动，以及令牌刷新、保活、积分刷新的时刻
    account_stagger_seconds: int = 10
    account_jitter_seconds: int = 60
    token_refresh_time: str = "04:30"
    token_refresh_margin_hours: int = 24
    keepalive_time: str = "10:30"
    credit_refresh_times: str = "08:30,14:30,20:30"

    trae_auth_host: str = "https://api.trae.cn"
    trae_client_id: str = "ono9krqynydwx5"
    trae_auth_url: str = "https://www.trae.cn/authorization"
    trae_web_origin: str = "https://solo.trae.cn"
    model_gateway: str = "https://trae-api-cn.mchost.guru"
    web_api_base: str = "https://trae-api-cn.mchost.guru/api/remote/v1"
    login_plugin_version: str = "2.3.24254"
    login_app_version: str = "0.1.7"
    native_app_version: str = "2.3.105403"
    ide_version: str = "3.4.22"
    ide_version_code: str = "20261118"

    public_base_url: str = ""
    cookie_secure: bool = False
    allow_local_import: bool = True
    trust_proxy: bool = False
    log_level: str = "INFO"

    @classmethod
    def load(cls, root: str | Path | None = None) -> "Config":
        base = Path(root or Path(__file__).resolve().parent.parent).resolve()
        env_values = _load_env_file(base / ".env")
        # .env only fills variables that are not already exported.
        for key, value in env_values.items():
            os.environ.setdefault(key, value)

        data_dir = _resolve_path(
            _env("TRAE_MOUTH_DATA_DIR", str(base / "data")), base
        )
        data_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            root=base,
            data_dir=data_dir,
            db_path=_resolve_path(
                _env("TRAE_MOUTH_DB_PATH", str(data_dir / "trae_mouth.db")),
                data_dir,
            ),
            master_key=_load_master_key(data_dir),
            admin_username=_env("TRAE_MOUTH_ADMIN_USERNAME", "admin"),
            admin_password=_env("TRAE_MOUTH_ADMIN_PASSWORD"),
            host=_env("TRAE_MOUTH_HOST", "127.0.0.1"),
            port=_env_int("TRAE_MOUTH_PORT", 18792),
            session_ttl=_env_int("TRAE_MOUTH_SESSION_TTL", 7 * 86400),
            import_code_ttl=_env_int("TRAE_MOUTH_IMPORT_CODE_TTL", 600),
            refresh_window=_env_int("TRAE_MOUTH_REFRESH_WINDOW", 86400),
            refresh_device_info=_env_bool("TRAE_REFRESH_DEVICE_INFO", True),
            refresh_interval=_env_int("TRAE_MOUTH_REFRESH_INTERVAL", 300),
            default_concurrency=_env_int("TRAE_MOUTH_DEFAULT_CONCURRENCY", 3),
            model_catalog_ttl=_env_int("TRAE_MOUTH_MODEL_CATALOG_TTL", 3600),
            api_key=_env("TRAE_MOUTH_API_KEY"),
            reasoning_as_content=_env_bool("TRAE_MOUTH_REASONING_AS_CONTENT", True),
            strict_tool_args=_env_bool("TRAE_MOUTH_STRICT_TOOL_ARGS", True),
            tool_result_max_chars=_env_int(
                "TRAE_MOUTH_TOOL_RESULT_MAX_CHARS", 120000
            ),
            history_max_chars=_env_int("TRAE_MOUTH_HISTORY_MAX_CHARS", 1200000),
            history_max_messages=_env_int("TRAE_MOUTH_HISTORY_MAX_MESSAGES", 0),
            tool_duplicate_warning=_env_bool(
                "TRAE_MOUTH_TOOL_DUPLICATE_WARNING", True
            ),
            global_ide=_env_bool("TRAE_MOUTH_GLOBAL_IDE", True),
            native_tools=_env_bool("TRAE_MOUTH_NATIVE_TOOLS", False),
            first_token_timeout=_env_int("TRAE_MOUTH_FIRST_TOKEN_TIMEOUT", 60),
            stream_heartbeat=_env_int("TRAE_MOUTH_STREAM_HEARTBEAT", 15),
            conversation_affinity_ttl=_env_int(
                "TRAE_MOUTH_CONVERSATION_AFFINITY_TTL", 7 * 86400
            ),
            account_failover_limit=_env_int(
                "TRAE_MOUTH_ACCOUNT_FAILOVER_LIMIT", 2
            ),
            account_cooldown=_env_int("TRAE_MOUTH_ACCOUNT_COOLDOWN", 120),
            prewarm_catalogs=_env_bool("TRAE_MOUTH_PREWARM_CATALOGS", True),
            max_request_bytes=_env_int(
                "TRAE_MOUTH_MAX_REQUEST_BYTES", 128 * 1024 * 1024
            ),
            trae_auth_host=_env("TRAE_AUTH_HOST", "https://api.trae.cn"),
            trae_client_id=_env("TRAE_CLIENT_ID", "ono9krqynydwx5"),
            trae_auth_url=_env(
                "TRAE_AUTH_URL", "https://www.trae.cn/authorization"
            ),
            trae_web_origin=_env("TRAE_WEB_ORIGIN", "https://solo.trae.cn"),
            model_gateway=_env(
                "TRAE_MODEL_GATEWAY", "https://trae-api-cn.mchost.guru"
            ),
            web_api_base=_env(
                "TRAE_WEB_API_BASE",
                "https://trae-api-cn.mchost.guru/api/remote/v1",
            ),
            login_plugin_version=_env("TRAE_LOGIN_PLUGIN_VERSION", "2.3.24254"),
            login_app_version=_env("TRAE_LOGIN_APP_VERSION", "0.1.7"),
            native_app_version=_env("TRAE_NATIVE_APP_VERSION", "2.3.105403"),
            ide_version=_env("TRAE_IDE_VERSION", "3.4.22"),
            ide_version_code=_env("TRAE_IDE_VERSION_CODE", "20261118"),
            public_base_url=_env("TRAE_MOUTH_PUBLIC_BASE_URL").rstrip("/"),
            cookie_secure=_env_bool("TRAE_MOUTH_COOKIE_SECURE", False),
            allow_local_import=_env_bool("TRAE_MOUTH_ALLOW_LOCAL_IMPORT", True),
            trust_proxy=_env_bool("TRAE_MOUTH_TRUST_PROXY", False),
            log_level=_env("TRAE_MOUTH_LOG_LEVEL", "INFO").upper(),
        )
