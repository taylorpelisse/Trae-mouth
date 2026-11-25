@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
if not exist "%~dp0..\cache" goto plain
set "PYTHONPYCACHEPREFIX=%~dp0..\cache\pycache"
for %%I in ("%~dp0..\cache\pytest") do set "PYTEST_CACHE=%%~fI"
python -m pytest -q -o "cache_dir=%PYTEST_CACHE%" %*
goto end
:plain
python -m pytest -q %*
:end
pause
