@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating local environment .venv ...
    py -3.12 -m venv .venv >nul 2>&1
    if not exist ".venv\Scripts\python.exe" python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [error] Could not create .venv. Install Python 3.12+ and retry.
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m agent %*
exit /b %errorlevel%