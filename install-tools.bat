@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-tools.ps1" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
