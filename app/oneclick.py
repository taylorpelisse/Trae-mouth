from __future__ import annotations

import base64


def build_bat(relay: str, code: str, helper_source: bytes) -> bytes:
    """Build a self-contained Windows launcher with the helper embedded."""
    if not helper_source:
        raise ValueError("helper source is empty")
    encoded = base64.b64encode(helper_source).decode("ascii")
    lines = [encoded[index : index + 76] for index in range(0, len(encoded), 76)]
    payload = "\n".join("echo " + line for line in lines)
    bat = f"""@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "RELAY={relay}"
set "CODE={code}"
set "TMPDIR=%TEMP%\\TraeMouthLogin-%RANDOM%%RANDOM%"
mkdir "%TMPDIR%" >nul 2>nul
> "%TMPDIR%\\helper.b64" (
{payload}
)
certutil -decode "%TMPDIR%\\helper.b64" "%TMPDIR%\\login_helper.py" >nul 2>nul
if not exist "%TMPDIR%\\login_helper.py" (
    echo [x] Helper extraction failed.
    pause
    exit /b 1
)
where python >nul 2>nul
if not errorlevel 1 (
    set "PY=python"
) else (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [x] Python 3 not found. Install Python 3.10+ and enable "Add Python to PATH".
        pause
        exit /b 1
    )
    set "PY=py -3"
)
%PY% "%TMPDIR%\\login_helper.py" --relay "%RELAY%" --code "%CODE%"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo [x] Helper exited with code %RC%
    pause
)
rmdir /s /q "%TMPDIR%" >nul 2>nul
exit /b %RC%
"""
    return bat.replace("\n", "\r\n").encode("utf-8")
