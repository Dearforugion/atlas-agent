@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title ATLAS pentest agent

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [setup] Creating virtual environment...
    where py >nul 2>nul && ( py -3 -m venv ".venv" ) || ( python -m venv ".venv" )
    if not exist "%PY%" (
        echo [ERROR] Could not create .venv. Install Python 3 from python.org and retry.
        pause
        exit /b 1
    )
    echo [setup] Installing dependencies...
    "%PY%" -m pip install --upgrade pip >nul 2>nul
    "%PY%" -m pip install -r requirements.txt
)

"%PY%" -m agent menu %*

endlocal
