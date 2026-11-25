@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [x] Python 3 not found. Install Python 3.10+ and enable "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
)

python -c "import fastapi, uvicorn, httpx, cryptography" 2>nul
if errorlevel 1 (
    echo [i] Installing dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [x] Dependency installation failed.
        pause
        exit /b 1
    )
)

echo ============================================================
echo  Trae Mouth
echo  Admin: http://127.0.0.1:18792/admin
echo ============================================================
python run.py
pause
