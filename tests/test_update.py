from __future__ import annotations

import zipfile

from app import update


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
