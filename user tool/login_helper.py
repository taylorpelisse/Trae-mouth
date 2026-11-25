#!/usr/bin/env python3
"""Trae Mouth login helper.

Runs on the user's machine, opens a clean Chrome/Edge profile for the official
Trae web authorization page, receives the loopback callback and forwards the
result to a Trae Mouth relay.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

VERSION = "0.1.0"
DEFAULT_RELAY = "http://127.0.0.1:18792"
DEFAULT_AUTH_URL = "https://www.trae.cn/authorization"
DEFAULT_CLIENT_ID = "ono9krqynydwx5"
STORAGE_KEY = "iCubeAuthInfo://icube.cloudide"
SERVER_DATA_KEY = "iCubeServerData://icube.cloudide"

DEVICE_SPECS = [
    {
        "device_model": "83DG",
        "device_manufacturer": "LENOVO",
        "cpu_manufacturer": "AMD",
        "cpu_brand": "AMD Ryzen 7 5800H with Radeon Graphics",
        "cpu_family": "Ryzen 7",
        "cpu_speed": 3.2,
        "cpu_count": 16,
        "memory_gb": 16,
        "os_version": "Windows 11 Pro",
        "os_release": "10.0.22631",
        "screen_width": 1920,
        "screen_height": 1080,
    },
    {
        "device_model": "82WM",
        "device_manufacturer": "LENOVO",
        "cpu_manufacturer": "AMD",
        "cpu_brand": "AMD Ryzen 7 6800H with Radeon Graphics",
        "cpu_family": "Ryzen 7",
        "cpu_speed": 3.2,
        "cpu_count": 16,
        "memory_gb": 16,
        "os_version": "Windows 11 Home",
        "os_release": "10.0.22631",
        "screen_width": 2560,
        "screen_height": 1440,
    },
    {
        "device_model": "20XW",
        "device_manufacturer": "LENOVO",
        "cpu_manufacturer": "Intel",
        "cpu_brand": "12th Gen Intel(R) Core(TM) i7-1260P",
        "cpu_family": "Core i7",
        "cpu_speed": 2.1,
        "cpu_count": 16,
        "memory_gb": 16,
        "os_version": "Windows 11 Pro",
        "os_release": "10.0.22631",
        "screen_width": 1920,
        "screen_height": 1200,
    },
    {
        "device_model": "FA507RM",
        "device_manufacturer": "ASUSTeK COMPUTER INC.",
        "cpu_manufacturer": "AMD",
        "cpu_brand": "AMD Ryzen 7 6800H with Radeon Graphics",
        "cpu_family": "Ryzen 7",
        "cpu_speed": 3.2,
        "cpu_count": 16,
        "memory_gb": 16,
        "os_version": "Windows 11 Home",
        "os_release": "10.0.22631",
        "screen_width": 2560,
        "screen_height": 1440,
    },
    {
        "device_model": "15-dk2xxx",
        "device_manufacturer": "HP",
        "cpu_manufacturer": "Intel",
        "cpu_brand": "11th Gen Intel(R) Core(TM) i7-11370H",
        "cpu_family": "Core i7",
        "cpu_speed": 3.3,
        "cpu_count": 8,
        "memory_gb": 16,
        "os_version": "Windows 11 Home",
        "os_release": "10.0.22000",
        "screen_width": 1920,
        "screen_height": 1080,
    },
    {
        "device_model": "XPS 15 9520",
        "device_manufacturer": "Dell Inc.",
        "cpu_manufacturer": "Intel",
        "cpu_brand": "12th Gen Intel(R) Core(TM) i7-12700H",
        "cpu_family": "Core i7",
        "cpu_speed": 2.3,
        "cpu_count": 20,
        "memory_gb": 32,
        "os_version": "Windows 11 Pro",
        "os_release": "10.0.22631",
        "screen_width": 3456,
        "screen_height": 2160,
    },
    {
        "device_model": "MateBook 14",
        "device_manufacturer": "HUAWEI",
        "cpu_manufacturer": "Intel",
        "cpu_brand": "13th Gen Intel(R) Core(TM) i7-1360P",
        "cpu_family": "Core i7",
        "cpu_speed": 2.2,
        "cpu_count": 16,
        "memory_gb": 16,
        "os_version": "Windows 11 Home",
        "os_release": "10.0.22631",
        "screen_width": 2160,
        "screen_height": 1440,
    },
]


def normalize_relay_url(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("中转站地址必须是 http:// 或 https:// URL")
    if parsed.username or parsed.password:
        raise ValueError("中转站地址不能包含用户名或密码")
    path = parsed.path.rstrip("/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def is_loopback_host(host: str) -> bool:
    host = (host or "").lower().strip("[]")
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


def warn_insecure(relay: str) -> None:
    parsed = urllib.parse.urlsplit(relay)
    if parsed.scheme == "http" and not is_loopback_host(parsed.hostname or ""):
        print("[!] 警告：中转站使用 HTTP，凭据会以明文经过网络。建议使用 HTTPS 或本机回环。")


def build_device() -> dict[str, Any]:
    spec = random.choice(DEVICE_SPECS)
    return {
        "machine_id": secrets.token_hex(16),
        "device_id": str(random.randint(10**18, 10**19 - 1)),
        "brand": spec["device_model"],
        "cpu": spec["cpu_manufacturer"],
        "device_type": "windows",
        "os_version": spec["os_version"],
        "app_version": "0.1.7",
        "ide_version": "3.4.22",
        "ide_version_code": "20261118",
        "web_id": str(random.randint(10**17, 10**18 - 1)),
        "device_name": "DESKTOP-"
        + "".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(7)),
        "device_model": spec["device_model"],
        "device_manufacturer": spec["device_manufacturer"],
        "cpu_manufacturer": spec["cpu_manufacturer"],
        "cpu_brand": spec["cpu_brand"],
        "cpu_model": spec["cpu_brand"],
        "cpu_family": spec["cpu_family"],
        "cpu_stepping": "0",
        "cpu_speed": spec["cpu_speed"],
        "cpu_count": spec["cpu_count"],
        "memory": spec["memory_gb"] * 1024**3,
        "os_name": "windows",
        "os_release": spec["os_release"],
        "os_kernel": spec["os_release"],
        "platform": "electron",
        "platform_code": "IDE_PC",
        "device_class": "PC",
        "screen_width": spec["screen_width"],
        "screen_height": spec["screen_height"],
        "language": "zh-CN",
    }


def load_device(path: str | None) -> dict[str, Any]:
    device = build_device()
    if path:
        try:
            data = json.loads(Path(path).read_text("utf-8"))
            if isinstance(data, dict):
                device.update({k: v for k, v in data.items() if v not in (None, "")})
        except Exception as exc:
            raise SystemExit(f"无法读取设备配置 {path}: {exc}") from exc
    return device


def build_auth_url(
    *,
    auth_url: str,
    client_id: str,
    callback_url: str,
    device: dict[str, Any],
    trace_id: str,
    plugin_version: str,
    app_version: str,
) -> str:
    params = {
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        "plugin_version": plugin_version,
        "auth_type": "local",
        "client_id": client_id,
        "redirect": "0",
        "login_trace_id": trace_id,
        "auth_callback_url": callback_url,
        "machine_id": device["machine_id"],
        "device_id": device["device_id"],
        "x_device_id": device["device_id"],
        "x_machine_id": device["machine_id"],
        "x_device_brand": device.get("device_model") or device.get("brand") or "",
        "x_device_type": device.get("os_name") or device.get("device_type") or "",
        "x_os_version": device.get("os_version") or "",
        "x_env": "",
        "x_app_version": app_version,
        "x_app_type": "stable",
        "hide_saas_login": "true",
    }
    separator = "&" if "?" in auth_url else "?"
    return auth_url + separator + urllib.parse.urlencode(params)


def find_browser(preference: str) -> Optional[str]:
    candidates: list[str] = []
    env = os.environ
    local = Path(env.get("LOCALAPPDATA", ""))
    pf = Path(env.get("PROGRAMFILES", ""))
    pf86 = Path(env.get("PROGRAMFILES(X86)", ""))

    chrome = [
        pf / "Google" / "Chrome" / "Application" / "chrome.exe",
        pf86 / "Google" / "Chrome" / "Application" / "chrome.exe",
        local / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    edge = [
        pf / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        pf86 / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        local / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    if preference == "chrome":
        candidates = [str(p) for p in chrome]
    elif preference == "edge":
        candidates = [str(p) for p in edge]
    else:
        candidates = [str(p) for p in chrome + edge]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def new_profile_dir(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "TraeMouth" / "auth-profiles"
    base.mkdir(parents=True, exist_ok=True)
    # Remove stale profiles from earlier runs (keeps the newest one).
    try:
        for old in sorted(base.glob("login-*"), key=lambda p: p.stat().st_mtime)[:-3]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    path = Path(__import__("tempfile").mkdtemp(prefix="login-", dir=str(base)))
    return path


def launch_browser(
    url: str,
    *,
    preference: str,
    profile_dir: Path,
    keep_profile: bool,
) -> Optional[subprocess.Popen]:
    executable = find_browser(preference)
    if executable:
        args = [
            executable,
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-extensions",
            "--disable-background-networking",
            "--new-window",
            url,
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            print(f"[i] 已用干净浏览器实例打开授权页：{Path(executable).name}")
            print(f"[i] 临时 profile：{profile_dir}")
            return proc
        except OSError as exc:
            print(f"[!] 启动浏览器失败：{exc}")
    print("[!] 未找到 Chrome/Edge，改用系统默认浏览器（无法隔离 profile）。")
    webbrowser.open(url)
    return None


def close_browser(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                timeout=10,
            )
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def mask_code(code: str) -> str:
    if not code:
        return "(未提供)"
    if len(code) <= 12:
        return code[:4] + "***"
    return code[:10] + "..." + code[-4:]


def post_json(url: str, payload: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("detail") or body
        except Exception:
            detail = body
        raise RuntimeError(f"中转站返回 HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接中转站：{exc.reason}") from exc
    try:
        result = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"中转站返回了非 JSON 响应：{body[:300]}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("中转站响应格式不正确")
    return result


def validate_code(relay: str, code: str) -> bool:
    if not code.startswith("tmimp_") or len(code) < 20:
        print("[x] 导入码格式不对。请在面板点“生成网页登录导入码”，完整复制 tmimp_... 开头的码。")
        return False
    try:
        result = post_json(
            f"{relay}/public/accounts/import/check",
            {"import_code": code},
            timeout=10,
        )
    except Exception as exc:
        print(f"[x] 导入码校验失败：{exc}")
        return False
    expires_at = result.get("expires_at")
    if expires_at:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(expires_at)))
        print(f"[i] 导入码有效，截止 {when}")
    return True


def _handoff_paths(code_file: str = "") -> list[Path]:
    paths: list[Path] = []
    if code_file:
        paths.append(Path(code_file).expanduser())
    base = Path(__file__).resolve().parent.parent
    paths.append(base / "data" / "last_import_code.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        paths.append(Path(local) / "TraeMouth" / "last_import_code.json")
    return paths


def load_handoff_code(code_file: str = "") -> Optional[tuple[str, int]]:
    now = time.time()
    for path in _handoff_paths(code_file):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        code = str(data.get("code") or "").strip()
        expires_at = int(data.get("expires_at") or 0)
        if code and expires_at > now:
            return code, expires_at
    return None


def clear_handoff_code(code: str) -> None:
    for path in _handoff_paths():
        try:
            data = json.loads(path.read_text("utf-8"))
            if data.get("code") == code:
                path.unlink()
        except (OSError, ValueError):
            continue


def resolve_code(args: argparse.Namespace) -> str:
    code = (args.code or os.environ.get("TRAE_MOUTH_IMPORT_CODE") or "").strip()
    if not code:
        handoff = load_handoff_code(getattr(args, "code_file", "") or "")
        if handoff:
            code, expires_at = handoff
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at))
            print(f"[i] 已自动读取面板生成的导入码（截止 {when}）")
    if not code:
        print("[i] 未发现面板写入的导入码。请先在面板点“生成网页登录导入码”，或手动粘贴。")
        try:
            code = input("请输入面板生成的一次性导入码: ").strip()
        except EOFError:
            code = ""
    return code


class LoginState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self.callback: Optional[dict[str, str]] = None
        self.state = "waiting"
        self.message = "等待浏览器回调"
        self.account: dict[str, Any] = {}

    def set_callback(self, query: dict[str, str]) -> None:
        with self._lock:
            self.callback = query
            self.state = "submitting"
            self.message = "已收到授权回调，正在写入中转站"
        self._event.set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)

    def set_result(self, ok: bool, message: str, account: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.state = "ok" if ok else "error"
            self.message = message
            self.account = account or {}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state,
                "message": self.message,
                "account": self.account,
            }


STATE = LoginState()


class Handler(BaseHTTPRequestHandler):
    server_version = f"TraeMouthLogin/{VERSION}"
    relay = DEFAULT_RELAY
    auth_url = ""
    trace_id = ""
    code = ""
    device: dict[str, Any] = {}
    callback_url = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, status: int, html: str) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/healthz":
            self._json(200, {"ok": True, "helper": "trae-mouth", "version": VERSION})
        elif parsed.path == "/":
            self._html(200, self._index_page())
        elif parsed.path == "/start":
            self.send_response(302)
            self.send_header("Location", self.auth_url)
            self.end_headers()
        elif parsed.path == "/authorize":
            self._handle_callback(parsed.query)
        elif parsed.path == "/status":
            self._json(200, STATE.snapshot())
        elif parsed.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def _handle_callback(self, query: str) -> None:
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        callback = {key: values[-1] for key, values in params.items()}
        trace = callback.get("login_trace_id") or callback.get("loginTraceId") or ""
        if trace and self.trace_id and trace != self.trace_id:
            self._html(400, self._result_page(False, "回调 login_trace_id 不匹配，已拒绝"))
            return
        if not callback.get("userJwt") and not callback.get("refreshToken"):
            self._html(400, self._result_page(False, "回调里没有 userJwt 或 refreshToken"))
            return
        STATE.set_callback(callback)
        self._html(200, self._result_page(True, "授权信息已接收，正在写入中转站…", poll=True))

    def _result_page(self, ok: bool, message: str, poll: bool = False) -> str:
        color = "#1a8c5c" if ok else "#b42323"
        poll_js = """
<script>
const timer = setInterval(async () => {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    const el = document.getElementById('msg');
    el.textContent = d.message || '';
    if (d.state === 'ok') {
      clearInterval(timer);
      document.getElementById('box').style.borderColor = '#1a8c5c';
      document.getElementById('title').textContent = '登录成功';
      el.textContent = d.message;
      if (window.opener) window.opener.postMessage({type:'trae-mouth-login', ok:true, message:d.message}, '*');
      setTimeout(() => window.close(), 1800);
    } else if (d.state === 'error') {
      clearInterval(timer);
      document.getElementById('box').style.borderColor = '#b42323';
      document.getElementById('title').textContent = '登录失败';
      el.textContent = d.message;
    }
  } catch (e) {}
}, 600);
</script>
""" if poll else ""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Trae Mouth 授权</title>
<style>
body{{margin:0;background:#0f1117;color:#e8eaed;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
#box{{width:420px;padding:28px;background:#1a1d28;border:1px solid {color};border-radius:10px}}
h1{{font-size:18px;margin:0 0 12px}}
p{{color:#a8afc0;line-height:1.6;margin:0}}
code{{background:#252836;padding:2px 6px;border-radius:4px}}
</style></head><body>
<div id="box">
  <h1 id="title">{'授权已接收' if ok else '授权失败'}</h1>
  <p id="msg">{message}</p>
</div>
{poll_js}
</body></html>"""

    def _index_page(self) -> str:
        device = self.device
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Trae Mouth 登录助手</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#0f1117;color:#e8eaed;font:15px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
.panel{{width:520px;background:#1a1d28;border:1px solid #2d3140;border-radius:10px;padding:26px}}
h1{{font-size:20px;margin:0 0 6px}}
.sub{{color:#8f97a8;font-size:13px;margin-bottom:18px}}
.row{{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid #262a38;font-size:13px}}
.row span:first-child{{color:#8f97a8}}
.mono{{font-family:Consolas,monospace;color:#cfd5e3;word-break:break-all}}
.btn{{display:inline-flex;align-items:center;justify-content:center;width:100%;margin-top:18px;padding:12px 16px;border:0;border-radius:7px;background:#1a8c5c;color:#fff;font-size:15px;font-weight:600;cursor:pointer}}
.btn:hover{{background:#21a06b}}
.btn:disabled{{opacity:.5;cursor:not-allowed}}
#msg{{margin-top:14px;padding:10px 12px;border-radius:7px;background:#252836;color:#cfd5e3;font-size:13px;line-height:1.5}}
.ok{{color:#8fe0af}}
.err{{color:#f3a6a6}}
</style></head><body>
<div class="panel">
  <h1>Trae Mouth 登录助手</h1>
  <div class="sub">使用干净的浏览器 profile 完成 official Trae 授权，不会影响你正在使用的浏览器。</div>
  <div class="row"><span>中转站</span><span class="mono">{self.relay}</span></div>
  <div class="row"><span>导入码</span><span class="mono">{mask_code(self.code)}</span></div>
  <div class="row"><span>设备</span><span class="mono">{device.get('brand','')} / {device.get('device_id','')}</span></div>
  <div class="row"><span>回环回调</span><span class="mono">{self.callback_url}</span></div>
  <button class="btn" id="go" onclick="startAuth()">使用 Trae 网页授权登录</button>
  <div id="msg">点击按钮后，会弹出一个全新的浏览器窗口。登录完成后本页会自动更新。</div>
</div>
<script>
var popup = null;
function startAuth() {{
  document.getElementById('go').disabled = true;
  popup = window.open('/start', 'trae-mouth-login', 'width=560,height=760');
  if (!popup) {{
    document.getElementById('msg').innerHTML = '<span class="err">弹窗被拦截，请允许弹窗后重试。</span>';
    document.getElementById('go').disabled = false;
    return;
  }}
  poll();
}}
async function poll() {{
  try {{
    const r = await fetch('/status');
    const d = await r.json();
    const msg = document.getElementById('msg');
    msg.textContent = d.message || '';
    if (d.state === 'ok') {{
      msg.className = 'ok';
      const a = d.account || {{}};
      msg.textContent = '导入成功：' + (a.username || a.label || a.user_id || '') + (a.plan ? '（' + a.plan + '）' : '');
      document.getElementById('go').disabled = false;
      if (popup && !popup.closed) popup.close();
      return;
    }}
    if (d.state === 'error') {{
      msg.className = 'err';
      document.getElementById('go').disabled = false;
      return;
    }}
  }} catch (e) {{}}
  setTimeout(poll, 700);
}}
</script>
</body></html>"""


def run_oauth(args: argparse.Namespace) -> int:
    relay = normalize_relay_url(args.relay)
    warn_insecure(relay)
    code = resolve_code(args)
    if not code:
        print("[x] 缺少导入码。请在面板里点“生成导入码”后重试。")
        return 2
    if not validate_code(relay, code):
        return 2

    device = load_device(args.device_json)
    trace_id = str(uuid.uuid4())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    callback_url = f"http://127.0.0.1:{port}/authorize"
    auth_url = build_auth_url(
        auth_url=args.auth_url,
        client_id=args.client_id,
        callback_url=callback_url,
        device=device,
        trace_id=trace_id,
        plugin_version=args.plugin_version,
        app_version=args.app_version,
    )

    Handler.relay = relay
    Handler.auth_url = auth_url
    Handler.trace_id = trace_id
    Handler.code = code
    Handler.device = device
    Handler.callback_url = callback_url

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("=" * 62)
    print("Trae Mouth 登录助手")
    print(f"中转站   : {relay}")
    print(f"导入码   : {mask_code(code)}")
    print(f"本地回调 : {callback_url}")
    print(f"设备     : {device['brand']} / {device['device_id']} / {device['machine_id'][:12]}…")
    print("=" * 62)

    profile_dir = new_profile_dir(args.profile_dir)
    proc = None
    if args.no_browser:
        print(f"[i] --no-browser 已启用，请手动打开：http://127.0.0.1:{port}/")
    else:
        print("[i] 正在打开 Trae 授权页…")
        proc = launch_browser(
            auth_url,
            preference=args.browser,
            profile_dir=profile_dir,
            keep_profile=args.keep_profile,
        )

    ok = STATE.wait(max(30, args.timeout))
    if not ok:
        print("[x] 等待授权回调超时。")
        close_browser(proc)
        server.shutdown()
        return 1

    callback = STATE.callback or {}
    payload = {
        "import_code": code,
        "source": "web_oauth",
        "callback": callback,
        "device": device,
        "client_id": args.client_id,
        "auth_url": args.auth_url,
        "login_trace_id": trace_id,
        "helper_version": VERSION,
    }
    print("[i] 正在把授权结果写入中转站…")
    try:
        result = post_json(f"{relay}/public/accounts/import/oauth", payload)
    except Exception as exc:
        STATE.set_result(False, str(exc))
        print(f"[x] 导入失败：{exc}")
        close_browser(proc)
        server.shutdown()
        return 1

    account = result.get("account") if isinstance(result, dict) else {}
    if not isinstance(account, dict):
        account = {}
    name = account.get("username") or account.get("label") or account.get("user_id") or ""
    plan = f"（{account.get('plan')}）" if account.get("plan") else ""
    STATE.set_result(True, f"导入成功：{name}{plan}", account)
    clear_handoff_code(code)
    print(f"[√] 导入成功：{name}{plan}")
    if account.get("token_expires_at"):
        print(f"    token 到期时间戳：{account['token_expires_at']}")
    print("[i] 登录完成，正在关闭浏览器…")
    time.sleep(1.5)
    close_browser(proc)
    server.shutdown()

    if not args.keep_profile:
        # The browser may still hold files; cleanup is best effort.
        try:
            if proc is None or proc.poll() is not None:
                shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass
    return 0


def find_storage_file(explicit: str | None) -> Optional[Path]:
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_dir():
            path = path / "globalStorage" / "storage.json"
        return path if path.exists() else None
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    candidates = [
        appdata / "Trae CN" / "User" / "globalStorage" / "storage.json",
        appdata / "TRAE SOLO CN" / "User" / "globalStorage" / "storage.json",
        Path.home() / "Library" / "Application Support" / "Trae CN" / "User" / "globalStorage" / "storage.json",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Trae CN" / "User" / "globalStorage" / "storage.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def run_local_import(args: argparse.Namespace) -> int:
    relay = normalize_relay_url(args.relay)
    warn_insecure(relay)
    code = resolve_code(args)
    if not code:
        print("[x] 缺少导入码。")
        return 2
    if not validate_code(relay, code):
        return 2
    path = find_storage_file(args.storage)
    if not path:
        print("[x] 没找到 Trae 的 storage.json，请用 --storage 指定路径。")
        return 1
    try:
        storage = json.loads(path.read_text("utf-8"))
    except Exception as exc:
        print(f"[x] 无法读取 {path}: {exc}")
        return 1
    value = storage.get(STORAGE_KEY) if isinstance(storage, dict) else None
    if not value:
        print(f"[x] {path} 里没有 {STORAGE_KEY}，请先在官方客户端登录。")
        return 1
    server_data = storage.get(SERVER_DATA_KEY) if isinstance(storage, dict) else ""
    print(f"[i] 读取到凭据：{path}")
    payload = {
        "import_code": code,
        "source": "local_storage",
        "encrypted_value": value,
        "storage_path": str(path),
        "edition": "cn",
    }
    if isinstance(server_data, str) and server_data:
        payload["server_data"] = server_data
    try:
        result = post_json(f"{relay}/public/accounts/import/storage", payload)
    except Exception as exc:
        print(f"[x] 导入失败：{exc}")
        return 1
    account = result.get("account") or {}
    name = account.get("username") or account.get("user_id") or ""
    clear_handoff_code(code)
    print(f"[√] 导入成功：{name}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trae Mouth 登录助手：干净浏览器 profile + 回环授权"
    )
    parser.add_argument("--relay", default=os.environ.get("TRAE_MOUTH_RELAY", DEFAULT_RELAY))
    parser.add_argument("--code", default="", help="面板生成的一次性导入码")
    parser.add_argument(
        "--code-file",
        default="",
        help="从指定文件读取导入码；默认自动读取面板写入的 last_import_code.json",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 表示随机端口")
    parser.add_argument("--auth-url", default=DEFAULT_AUTH_URL)
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    parser.add_argument("--plugin-version", default="2.3.24254")
    parser.add_argument("--app-version", default="0.1.7")
    parser.add_argument("--browser", choices=["auto", "chrome", "edge"], default="auto")
    parser.add_argument("--profile-dir", default="")
    parser.add_argument("--keep-profile", action="store_true")
    parser.add_argument("--device-json", default="")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--import-local",
        action="store_true",
        help="不打开浏览器，直接读取本机 storage.json 并导入",
    )
    parser.add_argument("--storage", default="", help="storage.json 或 User 目录")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.import_local:
            return run_local_import(args)
        return run_oauth(args)
    except KeyboardInterrupt:
        print("\n[i] 已取消。")
        return 130
    except Exception as exc:
        print(f"[x] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
