from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import update, update_apply
from app.main import VERSION, create_app
from tests.test_api import setup_admin


def test_version_ordering() -> None:
    assert update._version("v0.1.2") > update._version("0.1.1")
    assert update._version("0.1.10") > update._version("0.1.2")
    assert update._version("0.2") == update._version("0.2.0")
    assert update._version("bad") == ()


def test_extract_skips_private_and_unsafe_files(tmp_path) -> None:
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("app/main.py", "print('ok')")
        handle.writestr("../escape.txt", "bad")
        handle.writestr("data/secret.db", "bad")
        handle.writestr(".env", "bad")
        handle.writestr(".zcode.md", "bad")
        handle.writestr("tools/workspace/backup.py", "bad")

    destination = tmp_path / "out"
    update._extract(archive, destination)

    assert (destination / "app" / "main.py").read_text("utf-8") == "print('ok')"
    assert not (destination / "escape.txt").exists()
    assert not (destination / "data").exists()
    assert not (destination / ".env").exists()
    assert not (destination / ".zcode.md").exists()
    assert not (destination / "tools").exists()


def test_download_reports_progress(tmp_path) -> None:
    body = b"x" * (65536 * 3)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    seen: list[int] = []

    digest = update._download(client, "http://release/a.zip", tmp_path / "a.zip", seen.append)

    assert digest == hashlib.sha256(body).hexdigest()
    # 校验通过前不报满，所以最多到 99
    assert seen[-1] == 99
    assert seen == sorted(seen)


class _ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def test_download_without_length_has_no_percentage(tmp_path) -> None:
    stream = _ChunkStream([b"x" * 65536, b"y"])
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)))
    seen: list[int] = []

    update._download(client, "http://release/a.zip", tmp_path / "a.zip", seen.append)

    assert seen == []


def test_extract_reports_progress(tmp_path) -> None:
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        for index in range(5):
            handle.writestr(f"app/file{index}.py", "print('ok')")

    seen: list[int] = []
    update._extract(archive, tmp_path / "out", seen.append)

    assert seen == sorted(seen)
    assert seen[-1] == update.EXTRACT_END


def test_reconcile_marks_done_when_version_matches(tmp_path) -> None:
    config = SimpleNamespace(data_dir=tmp_path)
    update._write_job(update._state_path(config), {"target": "v0.2.6", "status": "restarting"})

    result = update.reconcile(config, "0.2.6")

    assert result is not None and result["status"] == "done"
    assert update.read_job(config)["status"] == "done"


def test_reconcile_marks_failed_when_version_unchanged(tmp_path) -> None:
    config = SimpleNamespace(data_dir=tmp_path)
    update._write_job(update._state_path(config), {"target": "v0.2.6", "status": "restarting"})

    result = update.reconcile(config, "0.2.4")

    assert result is not None and result["status"] == "failed"
    assert "版本未变化" in result["error"]


def test_reconcile_marks_interrupted_download_failed(tmp_path) -> None:
    config = SimpleNamespace(data_dir=tmp_path)
    update._write_job(update._state_path(config), {"target": "v0.2.6", "status": "downloading"})

    result = update.reconcile(config, "0.2.4")

    assert result is not None and result["status"] == "failed"
    assert "被中断" in result["error"]


def test_clear_job_keeps_running_update(tmp_path) -> None:
    config = SimpleNamespace(data_dir=tmp_path)
    path = update._state_path(config)
    update._write_job(path, {"target": "v0.2.6", "status": "installing"})
    update.clear_job(config)
    assert path.exists()

    update._write_job(path, {"target": "v0.2.6", "status": "done"})
    update.clear_job(config)
    assert not path.exists()


def test_finished_job_expires(tmp_path) -> None:
    config = SimpleNamespace(data_dir=tmp_path)
    path = update._state_path(config)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"target": "v0.2.6", "status": "failed", "updated_at": 1}), "utf-8")

    assert update.read_job(config) is None


def test_apply_update_starts_in_background(tmp_path, monkeypatch) -> None:
    config = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(
        update,
        "check_update",
        lambda current, force=False: {"available": True, "latest": "v9.9.9"},
    )
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(update, "_run", lambda *args: (entered.set(), release.wait(5)))
    try:
        result = update.apply_update(config, "0.2.4")

        assert entered.wait(5)
        assert result["ok"] and result["started"] and result["latest"] == "v9.9.9"
        assert result["job"]["status"] == "downloading"
        assert update.read_job(config)["target"] == "v9.9.9"

        again = update.apply_update(config, "0.2.4")
        assert again == {"ok": False, "error": "update already running"}
    finally:
        release.set()
        update._applying = False


def test_apply_script_extends_shared_state(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"target": "v0.2.6", "status": "installing"}), "utf-8")

    update_apply.write_state(path, percent=55, detail="正在替换 app/")

    data = json.loads(path.read_text("utf-8"))
    assert data["status"] == "installing"
    assert data["percent"] == 55
    assert isinstance(data["updated_at"], int)


def test_prepare_reports_download_failure_in_plain_words(tmp_path, monkeypatch) -> None:
    config = SimpleNamespace(data_dir=tmp_path)
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="missing"))
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=transport))
    info = {
        "latest": "v9.9.9",
        "zip_url": "http://release/a.zip",
        "sha_url": "http://release/a.zip.sha256",
    }

    with pytest.raises(ValueError, match="HTTP 404"):
        update._prepare(config, info, {}, update._state_path(config))

    assert not (tmp_path / "update" / "release-v9.9.9.zip").exists()


def test_run_marks_failed_when_installer_exits_early(tmp_path, monkeypatch) -> None:
    config = SimpleNamespace(data_dir=tmp_path, root=tmp_path)
    monkeypatch.setattr(
        update, "_prepare", lambda *args: (tmp_path / "a.zip", tmp_path / "staging")
    )

    class FakeProcess:
        def wait(self, timeout=None) -> int:
            return 3

    monkeypatch.setattr(update, "_launch", lambda *args: FakeProcess())
    job = {"target": "v9.9.9", "status": "downloading"}
    update._applying = True
    try:
        update._run(config, {}, job)

        assert job["status"] == "failed"
        assert "3" in job["error"]
        assert update.read_job(config)["status"] == "failed"
        assert update._applying is False
    finally:
        update._applying = False


def test_apply_script_installs_tree_and_reports_progress(tmp_path, monkeypatch) -> None:
    staging = tmp_path / "staging"
    (staging / "app").mkdir(parents=True)
    (staging / "app" / "main.py").write_text("new", "utf-8")
    (staging / "admin-web" / "dist").mkdir(parents=True)
    (staging / "admin-web" / "dist" / "index.html").write_text("new page", "utf-8")
    (staging / "release-notes").mkdir()
    (staging / "release-notes" / "v9.md").write_text("notes", "utf-8")
    (staging / "data").mkdir()
    (staging / "data" / "secret.db").write_text("x", "utf-8")
    (staging / ".env").write_text("stale", "utf-8")

    root = tmp_path / "root"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("old", "utf-8")
    (root / ".env").write_text("keep", "utf-8")
    (root / "run.py").write_text(
        "from pathlib import Path; Path('started').write_text('ok', encoding='utf-8')",
        "utf-8",
    )
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"target": "v9.9.9", "status": "installing", "percent": 40}),
        "utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply.py",
            "--root",
            str(root),
            "--staging",
            str(staging),
            "--archive",
            str(tmp_path / "missing.zip"),
            "--pid",
            "0",
            "--log",
            str(tmp_path / "update.log"),
            "--state",
            str(state),
        ],
    )

    assert update_apply.main() == 0

    assert (root / "app" / "main.py").read_text("utf-8") == "new"
    assert (root / "admin-web" / "dist" / "index.html").read_text("utf-8") == "new page"
    assert (root / "release-notes" / "v9.md").exists()
    assert (root / ".env").read_text("utf-8") == "keep"
    assert not (root / "data").exists()
    assert not staging.exists()
    data = json.loads(state.read_text("utf-8"))
    assert data["status"] == "restarting"
    assert data["percent"] == 100
    # 新进程由脚本拉起
    for _ in range(100):
        if (root / "started").exists():
            break
        time.sleep(0.1)
    assert (root / "started").read_text("utf-8") == "ok"


def test_update_endpoints_track_background_job(config, monkeypatch) -> None:
    release = threading.Event()
    monkeypatch.setattr(
        update,
        "check_update",
        lambda current, force=False: {"available": True, "latest": "v9.9.9"},
    )
    monkeypatch.setattr(update, "_run", lambda *args: release.wait(5))
    client = TestClient(create_app(config))
    try:
        with client:
            headers = setup_admin(client)
            status_url = "/admin/api/update/status"
            assert client.get(status_url).json()["job"] is None

            started = client.post("/admin/api/update/apply", headers=headers)

            assert started.status_code == 200, started.text
            assert started.json()["job"]["status"] == "downloading"
            status = client.get(status_url).json()
            assert status["version"] == VERSION
            assert status["job"]["target"] == "v9.9.9"

            # 进行中的更新不能被确认清除
            assert client.delete("/admin/api/update", headers=headers).status_code == 200
            assert client.get(status_url).json()["job"] is not None

            update._write_job(
                update._state_path(config), {**status["job"], "status": "done"}
            )
            assert client.delete("/admin/api/update", headers=headers).status_code == 200
            assert client.get(status_url).json()["job"] is None
    finally:
        release.set()
        update._applying = False


def test_restart_finishes_update_when_version_matches(config) -> None:
    # 模拟安装脚本结束旧进程前留下的状态：新进程启动时应把它记为完成
    update._write_job(
        update._state_path(config), {"target": f"v{VERSION}", "status": "restarting"}
    )

    with TestClient(create_app(config)) as client:
        setup_admin(client)
        job = client.get("/admin/api/update/status").json()["job"]

    assert job is not None and job["status"] == "done"


def test_restart_reports_update_that_did_not_take_effect(config) -> None:
    update._write_job(
        update._state_path(config), {"target": "v9.9.9", "status": "restarting"}
    )

    with TestClient(create_app(config)) as client:
        setup_admin(client)
        job = client.get("/admin/api/update/status").json()["job"]

    assert job is not None and job["status"] == "failed"
    assert "版本未变化" in job["error"]
