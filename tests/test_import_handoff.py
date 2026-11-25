from __future__ import annotations

import time

from app import import_handoff


def test_handoff_roundtrip(config, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    code = "tmimp_test_code_1234567890"
    expires = int(time.time()) + 600
    import_handoff.write(config, code, expires, "http://127.0.0.1:18792")
    data = import_handoff.read(config)
    assert data and data["code"] == code
    assert data["relay"] == "http://127.0.0.1:18792"
    import_handoff.clear(config, code)
    assert import_handoff.read(config) is None


def test_expired_handoff_is_ignored(config, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    import_handoff.write(
        config, "tmimp_expired_code_123456", int(time.time()) - 10, "http://x"
    )
    assert import_handoff.read(config) is None
    import_handoff.clear_expired(config)


def test_clear_only_matching_code(config, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    code = "tmimp_keep_code_1234567890"
    import_handoff.write(config, code, int(time.time()) + 600, "http://x")
    import_handoff.clear(config, "tmimp_other_code")
    assert import_handoff.read(config)["code"] == code
    import_handoff.clear(config, code)
    assert import_handoff.read(config) is None
