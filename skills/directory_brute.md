# Брутфорс директорий
Когда: перечисление, каждый живой хост.
ffuf -u https://хост/FUZZ -w workspace/wordlists/common.txt -mc 200,301,302,401,403 -t 50; dirsearch -u URL -e php,asp,jsp,html,bak,zip,sql; gobuster dir; feroxbuster --depth 2.
Интересно: .git .env backup* dump*.sql *.zip — сразу эксплуатация; /admin /wp-login.php /login api/ — в credential_stuffing; обход 403: X-Original-URL: /admin, /admin..;/, //admin//, /%2e%2e/.
Процесс: common.txt быстро → рекурсивно. Находки в enum.md. Защита: убрать бэкапы/.git, листинги off.