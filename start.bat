@echo off
chcp 65001 >nul
title ATLAS
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

:menu
cls
echo ==================================================
echo                ATLAS - ГЛАВНОЕ МЕНЮ
echo ==================================================
echo   1. Открыть агента (чат)
echo   2. Быстрый взлом цели
echo   3. Загрузить материал для обучения
echo   4. История диалогов и логи
echo   5. Дашборд ATLAS (agent menu)
echo   6. Первичная настройка (venv + Ollama)
echo   7. Установить инструменты пентеста
echo   8. Показать скоуп (разрешённые цели)
echo   9. Диагностика (doctor)
echo   0. Выход
echo ==================================================
set /p CH="Выбор: "
if "%CH%"=="1" ( "%PY%" -m agent repl --agent pentester & pause & goto menu )
if "%CH%"=="2" ( call HACK.bat & goto menu )
if "%CH%"=="3" goto learn
if "%CH%"=="4" goto history
if "%CH%"=="5" ( "%PY%" -m agent menu & goto menu )
if "%CH%"=="6" ( call start-original.bat & goto menu )
if "%CH%"=="7" ( call install-tools.bat & goto menu )
if "%CH%"=="8" ( "%PY%" -m agent scope show & pause & goto menu )
if "%CH%"=="9" ( "%PY%" -m agent doctor & pause & goto menu )
if "%CH%"=="0" goto :eof
goto menu

:learn
set /p FILE="Путь к txt/md файлу с материалом: "
if not exist "%FILE%" ( echo Файл не найден & pause & goto menu )
if not exist "workspace\training" mkdir "workspace\training"
copy /y "%FILE%" "workspace\training\" >nul
for %%I in ("%FILE%") do set "NAME=%%~nxI"
echo Изучаю материал агентом...
"%PY%" -m agent ask --yes --skill study_materials "Изучи файл workspace/training/%NAME% (конспект/разбор взлома). По навыку study_materials: извлеки цель, вектор атаки, инструменты и команды, обход защит, результат. Сохрани ключевые техники в долговременную память. Дай краткий конспект."
pause
goto menu

:history
if exist "data\sessions" start "" explorer "data\sessions"
if exist "data\audit.log" start notepad "data\audit.log"
if not exist "data" echo Данных пока нет
pause
goto menu