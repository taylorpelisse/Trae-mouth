from __future__ import annotations

import base64
import re

from app.oneclick import build_bat


def test_oneclick_bat_contains_helper_and_arguments() -> None:
    source = b"print('hello')\n"
    bat = build_bat(
        "http://127.0.0.1:18792",
        "tmimp_test_code",
        source,
    )
    text = bat.decode("utf-8")
    assert "\r\n" in text
    assert 'set "RELAY=http://127.0.0.1:18792"' in text
    assert 'set "CODE=tmimp_test_code"' in text
    assert "certutil -decode" in text
    assert "login_helper.py" in text
    payload = "".join(
        line[5:]
        for line in text.splitlines()
        if re.fullmatch(r"echo [A-Za-z0-9+/=]+", line)
    )
    assert base64.b64decode(payload) == source
