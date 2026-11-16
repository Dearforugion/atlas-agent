@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title ATLAS - локальный ИИ-агент
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=%~dp0.venv\Scripts\python.exe"

echo.
echo ============================================================
echo   ATLAS - локальный ИИ-агент
echo ============================================================
echo.

if not exist "%PY%" (
    echo [setup] Создаю локальное окружение .venv...
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3.12 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if not exist "%PY%" (
        echo [error] Не удалось создать .venv. Установи Python 3.12+.
        pause
        exit /b 1
    )
    "%PY%" -m pip install --upgrade pip >nul 2>nul
    if exist requirements.txt "%PY%" -m pip install -r requirements.txt
)

rem Flash-attention + q8_0 KV-кэш: модель 8B с контекстом 16k целиком влезает в 8 ГБ
rem VRAM и не выгружается в ОЗУ. Переменные читает сервер Ollama при старте ниже.
set "OLLAMA_FLASH_ATTENTION=1"
set "OLLAMA_KV_CACHE_TYPE=q8_0"

set "OLLAMA=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA%" set "OLLAMA=ollama"
if /I "%OLLAMA%"=="ollama" (
    where ollama >nul 2>nul
    if errorlevel 1 (
        echo [error] Ollama не найдена. Установи: https://ollama.com/download
        pause
        exit /b 1
    )
)

powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:11434/api/tags | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 goto ollama_ready

echo [atlas] Запускаю Ollama...
start "Ollama" /min "%OLLAMA%" serve
set /a OLLAMA_WAIT=0

:wait_ollama
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:11434/api/tags | Out-Null; exit 0 } catch { exit 1 }" >nul 2>nul
if not errorlevel 1 goto ollama_ready
set /a OLLAMA_WAIT+=1
if %OLLAMA_WAIT% GEQ 30 goto ollama_failed
timeout /t 1 /nobreak >nul
goto wait_ollama

:ollama_failed
echo [error] Ollama не запустилась за 30 секунд.
echo         Проверь: "%OLLAMA%" serve
pause
exit /b 1

:ollama_ready
"%OLLAMA%" list 2>nul | findstr /I /C:"atlas:latest" >nul
if errorlevel 1 (
    echo [atlas] Модель atlas не найдена. Создаю...
    "%OLLAMA%" pull qwen3:8b
    "%OLLAMA%" create atlas -f Modelfile
    if errorlevel 1 (
        echo [error] Не удалось создать модель atlas.
        pause
        exit /b 1
    )
)

"%PY%" -m agent doctor
if errorlevel 1 (
    echo.
    echo [error] Диагностика не прошла. Исправь ошибки выше.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ATLAS готов к работе.
echo.
echo   Главное меню: пентест/аудит, роли, навыки, инструменты, память.
echo   Управление: стрелки / цифры, Enter — выбрать, q — выход.
echo.
echo   Примеры запросов:
echo     сделай аудит https://example.com
echo     просканируй 192.168.56.10 и собери отчёт
echo     проверь TLS у https://example.com
echo.
echo   Публичный сайт сначала разреши:
echo     .\run.cmd scope allow example.com "мой сайт"
echo.
echo   Без меню: run.cmd ask --agent scout "проверь example.com"
echo ============================================================
echo.

if "%~1"=="" (
    "%PY%" -m agent menu
) else (
    "%PY%" -m agent ask %*
)
set "ATLAS_EXIT=%errorlevel%"
echo.
echo [atlas] Сессия завершена.
if "%~1"=="" pause
exit /b %ATLAS_EXIT%