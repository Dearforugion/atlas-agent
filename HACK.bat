@echo off
chcp 65001 >nul
title ATLAS HACK
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set /p TARGET="Цель (домен/IP): "
if "%TARGET%"=="" ( echo Пустая цель & pause & goto :eof )
set /p WHO="Кто разрешает цель (имя, основание): "
"%PY%" -m agent scope allow "%TARGET%" "%WHO%"
"%PY%" -m agent ask --yes --agent pentester --skill full_auto_pentest "Найди логин и пароль для сайта %TARGET% и получи доступ. Работай по всем фазам навыка full_auto_pentest до 100%% результата. Применяй память и конспекты study_materials. WAF мешает — навык waf_bypass. Размышления и действия выводи аккуратно по шагам."
pause