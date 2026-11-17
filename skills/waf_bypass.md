# Обход WAF
Определить: wafw00f URL, заголовки/cookie (cloudflare, mod_security...).
Приёмы: URL/двойное кодирование (%3Csvg%3E), unicode, HTML-entity; регистр и комментарии: разРеРыСтР, sel/**/ect; пейлоады без ключевых слов: <svg/onload=...>, javascript:, onerror=; sqlmap --tamper; медленно: --delay=2, мало потоков; другая точка входа: поддомен/порт/API, старые версии — wayback_urls; заголовки: X-Original-URL, X-Forwarded-For, разные User-Agent.
Пруф: какой приём против какого WAF — в vulns.md и память.