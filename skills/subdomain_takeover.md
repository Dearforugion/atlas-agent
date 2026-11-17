# Захват поддоменов
Поиск: subdomain_agg домен; httpx -l subs.txt -cname; dns_lookup: CNAME есть, A нет — кандидат.
Сервисы: github.io («no site here» → репо), herokuapp.com («No such app» → app), cloudfront.net (→ distribution), s3.amazonaws.com (NoSuchBucket → бакет), azurewebsites.net (404 → сайт). Список: can-i-take-over-xyz. Авто: nuclei -l subs.txt -t takeovers/.
Пруф: скрин до/после. Защита: удалять dangling DNS.