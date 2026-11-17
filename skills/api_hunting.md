# Охота на API
Найти: crawl_site + wayback_urls: /api/, /v1/, /v2/, /graphql, /swagger, /openapi.json, /api-docs, JS (fetch/axios); directory_brute с api-словарём.
Тест: /swagger, /openapi.json — карта эндпоинтов; IDOR: соседние ID без/с чужим токеном; Auth: без токена, старый токен, пустой Bearer, обычный юзер → админ-эндпоинт; инъекции в JSON-body; GraphQL: introspection, мутации без прав.
Пруф: эндпоинт, запрос, что доступно. Защита: авторизация на каждом эндпоинте, отключить introspection.