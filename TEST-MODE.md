# ATLAS — тестовый режим

Дата: 13.11.2026

Резервные копии:

- до тестового режима: `.atlas-backup\20261113-205621`
- перед лабораторным режимом: `.atlas-backup\20261113-205924`
- перед новыми инструментами и `scope allow`: `.atlas-backup\20261113-210713`
- после `start.bat`, 32 инструментов и новых навыков: `.atlas-backup\20261113-211533`

## Что отключено

- Подтверждения: `config.json` → `"confirm": "never"`.
- Изоляция рабочей папки: `"allow_outside_workspace": true`.
- Блокировка локальной сети в `web_fetch`: `"allow_private_web": true`.
- Пентест без скоупа для локальной лаборатории: `"allow_private_targets": true`.
- Лимиты подняты: `max_steps` 100, `shell_timeout` 1800, `max_tool_output` 20000, `memory_context` 30.
- В системный промпт добавлен `system_extra` с указанием тестового режима.
- `MAX_READ_BYTES` поднят до 20 МБ, `MAX_DOWNLOAD` — до 20 МБ.

## Что добавлено

- `start.bat` — запускает Ollama, проверяет модель, прогоняет диагностику и сразу открывает чат.
- `site_audit` — аудит сайта: HTTP, TLS, DNS, технологии, заголовки безопасности, типовые пути, отчёт.
- `crawl_site` — обход страниц сайта, ссылки, формы, email, внешние домены, отчёт.
- `dns_lookup` — DNS-разведка через DNS-over-HTTPS.
- `whois_lookup` — WHOIS/RDAP: регистратор, даты, статусы, NS, DNSSEC.
- `port_scan` — TCP-скан портов без nmap.
- `tls_info` — TLS-сертификат и параметры соединения.
- `hash_file` — MD5, SHA-1, SHA-256 файла.
- Навык `site-audit` — методика полного аудита сайта.
- Всего: 32 инструмента, 4 навыка.

## Лабораторный режим

Без предварительного скоупа разрешены цели только из этих диапазонов:

- `10.0.0.0/8`
- `172.16.0.0/12`
- `192.168.0.0/16`
- `127.0.0.0/8`
- `fc00::/7`, `::1`
- `localhost`

Если домен резолвится хотя бы в один публичный адрес, он не считается локальным и требует скоупа.

Проверить цель можно командой:

```powershell
.\run.cmd scope show
```

Или через инструмент `scope_check` в чате.

## Публичные цели

Публичные IP и домены по-прежнему требуют явной авторизации:

```powershell
.\run.cmd scope add-host example.com
.\run.cmd scope authorize "Разрешённая тренировочная цель"
```

## Что осталось

- Пентест-скоуп для публичных целей. Это защита третьих лиц, а не запрет на тестирование модели.
- Защита от удаления корня `workspace/`.
- Анти-цикл: одинаковое действие блокируется после 3 повторов.
- Проверка бинарных файлов при чтении.

## Как вернуть ограничения

Полный откат к состоянию до тестового режима:

```powershell
Copy-Item ".atlas-backup\20261113-205621\*" . -Recurse -Force
.\run.cmd selftest
```

Откат только лабораторного режима, с сохранением остальных послаблений:

```powershell
Copy-Item ".atlas-backup\20261113-205924\config.json" . -Force
Copy-Item ".atlas-backup\20261113-205924\agent\settings.py" .\agent\ -Force
Copy-Item ".atlas-backup\20261113-205924\agent\core.py" .\agent\ -Force
Copy-Item ".atlas-backup\20261113-205924\agent\scope.py" .\agent\ -Force
Copy-Item ".atlas-backup\20261113-205924\agent\tools\shell.py" .\agent\tools\ -Force
Copy-Item ".atlas-backup\20261113-205924\agent\tools\pentest.py" .\agent\tools\ -Force
Copy-Item ".atlas-backup\20261113-205924\skills\pentest-methodology.md" .\skills\ -Force
.\run.cmd selftest
```

Вручную в `config.json`: `confirm: smart`, `allow_outside_workspace: false`, `allow_private_web: false`, `allow_private_targets: false`, вернуть лимиты.

## Важно

В тестовом режиме агент может без предупреждения удалять файлы, выполнять опасные команды, обращаться к локальным сервисам и сканировать локальную сеть. Работай в виртуалке или сделай снимок системы.