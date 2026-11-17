# OSINT: люди, email, учётки
Когда: нужен логин/пароль — сначала собери профили.
Источники: google_dork (site:цель inurl:login, intitle:admin, "@цель.ru"); wayback_urls (старые страницы, забытые токены); virustotal_domain/subdomain_agg (dev, test, vpn, mail); ip_info (хостинг, организация); соцсети/LinkedIn → вероятные логины (i.ivanov, ivanov); формы логина: имена полей, сообщения об ошибках («пользователь не найден» → перебор логинов валиден).
Логины: паттерн email компании + users.txt → credential_stuffing. Пруф: источник, логин, что доступно.