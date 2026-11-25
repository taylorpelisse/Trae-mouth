from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "user tool" / "login_helper.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("trae_mouth_login_helper", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = load_helper()


def test_normalize_relay_url() -> None:
    assert helper.normalize_relay_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert helper.normalize_relay_url("https://x.example/base/") == "https://x.example/base"
    with pytest.raises(ValueError):
        helper.normalize_relay_url("ftp://x")
    with pytest.raises(ValueError):
        helper.normalize_relay_url("http://user:pass@x")


def test_mask_code() -> None:
    assert helper.mask_code("") == "(未提供)"
    assert "..." in helper.mask_code("tmimp_abcdefghijklmnop")
    assert helper.mask_code("short") == "shor***"


def test_build_auth_url() -> None:
    device = helper.build_device()
    url = helper.build_auth_url(
        auth_url="https://www.trae.cn/authorization",
        client_id="client-1",
        callback_url="http://127.0.0.1:8765/authorize",
        device=device,
        trace_id="trace-1",
        plugin_version="2.3.24254",
        app_version="0.1.7",
    )
    assert url.startswith("https://www.trae.cn/authorization?")
    assert "login_trace_id=trace-1" in url
    assert "auth_callback_url=http%3A%2F%2F127.0.0.1%3A8765%2Fauthorize" in url
    assert f"machine_id={device['machine_id']}" in url
    assert "x_device_type=windows" in url


def test_login_state_flow() -> None:
    state = helper.LoginState()
    assert state.snapshot()["state"] == "waiting"
    state.set_callback({"userJwt": "{}"})
    assert state.wait(1)
    assert state.snapshot()["state"] == "submitting"
    state.set_result(True, "ok", {"user_id": "u1"})
    snapshot = state.snapshot()
    assert snapshot["state"] == "ok"
    assert snapshot["account"]["user_id"] == "u1"


def test_load_handoff_code(tmp_path) -> None:
    path = tmp_path / "code.json"
    path.write_text(
        json.dumps({"code": "tmimp_test", "expires_at": time.time() + 60}),
        encoding="utf-8",
    )
    loaded = helper.load_handoff_code(str(path))
    assert loaded and loaded[0] == "tmimp_test"
    path.write_text(
        json.dumps({"code": "tmimp_test", "expires_at": time.time() - 1}),
        encoding="utf-8",
    )
    assert helper.load_handoff_code(str(path)) is None
