# Credential Stuffing / брутфорс логинов
Определи форму: URL, поля, CSRF, капча/лимит (5 неудач → пауза).
Дефолт: admin:admin, admin:password, admin:123456, root:root, test:test. CMS: wpscan --enumerate u.
Словари: workspace/wordlists/users.txt, passwords.txt; hydra -L users.txt -P passwords.txt хост http-post-form "/login:user=^USER^&pass=^PASS^:неверный"; --wait 3. Найденные пароли проверь через password_breach.
Эксплуатация: вошёл → зафиксируй; админка → web shell; пароль → SSH/FTP/API. Пруф: URL, логин, пароль, уровень. Защита: 2FA, лимит, капча.