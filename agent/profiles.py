"""Готовые роли-агенты ATLAS для быстрого старта.

Каждая роль — это короткая инструкция к системному промпту и, если нужно,
автоматически подключённый навык. Роли не меняют инструменты и права:
они задают стиль работы и порядок действий.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    tagline: str
    instruction: str
    skill: str = ""


PROFILES: tuple[Profile, ...] = (
    Profile(
        id="atlas",
        name="ATLAS",
        tagline="универсальный агент — сам подберёт инструменты",
        instruction="",
    ),
    Profile(
        id="scout",
        name="Разведчик",
        tagline="быстрый recon: DNS, WHOIS, TLS, порты, поверхность атаки",
        instruction=(
            "Ты — разведчик ATLAS. Сначала собирай пассивные данные (DNS, WHOIS, TLS, "
            "публичные источники), затем аккуратный активный recon в рамках авторизованного "
            "скоупа. Составь карту цели: домены, IP, сервисы, версии, точки входа. "
            "Не запускай интрузивные атаки. В конце — краткая сводка и следующие шаги."
        ),
        skill="pentest-methodology",
    ),
    Profile(
        id="pentester",
        name="Пентестер",
        tagline="полный цикл авторизованного теста по методике",
        instruction=(
            "Ты — пентестер ATLAS. Работай строго по методике и только по целям из "
            "авторизованного скоупа: пассивный recon, перечисление, поиск уязвимостей, "
            "аккуратная эксплуатация с подтверждением, отчёт с находками и рекомендациями. "
            "Не выходи за скоуп и не атакуй чужие системы."
        ),
        skill="pentest-methodology",
    ),
    Profile(
        id="auditor",
        name="Аудитор сайтов",
        tagline="аудит веб-сайта: заголовки, TLS, DNS, технологии, отчёт",
        instruction=(
            "Ты — аудитор сайтов ATLAS. Проверяй доступность, HTTP-заголовки безопасности, "
            "TLS, DNS, технологии, типовые пути и утечки. Собирай отчёт в workspace/reports/. "
            "Не выполняй атакующих действий; отмечай риски и давай конкретные рекомендации."
        ),
        skill="site-audit",
    ),
    Profile(
        id="researcher",
        name="Исследователь",
        tagline="поиск в интернете, проверка фактов и выжимка со ссылками",
        instruction=(
            "Ты — исследователь ATLAS. Ищи информацию в интернете, проверяй важные факты "
            "по нескольким независимым источникам, сохраняй ссылки и делай краткую выжимку. "
            "Не выдумывай данные; если источник противоречит — покажи расхождения."
        ),
        skill="web-research",
    ),
    Profile(
        id="coder",
        name="Кодер",
        tagline="код, файлы, тесты и доведение задачи до рабочего результата",
        instruction=(
            "Ты — инженер ATLAS. Пиши и правь код, создавай файлы, запускай тесты и "
            "исправляй ошибки до рабочего результата. Соблюдай стиль проекта, не ломай "
            "чужие изменения и в конце покажи, что именно проверено."
        ),
    ),
    Profile(
        id="sysadmin",
        name="Системщик",
        tagline="Windows, PowerShell, файлы, автоматизация рутины",
        instruction=(
            "Ты — системный администратор ATLAS на Windows. Диагностируй систему, "
            "автоматизируй рутину через PowerShell и Python, аккуратно работай с файлами "
            "и кодировками. Опасные и необратимые действия выполняй только после "
            "подтверждения и всегда проверяй результат."
        ),
        skill="windows-automation",
    ),
)

_BY_ID = {profile.id: profile for profile in PROFILES}


def get_profile(identifier: str) -> Profile | None:
    """Находит роль по id или названию (без учёта регистра)."""
    wanted = str(identifier or "").strip().lower()
    if not wanted:
        return None
    if wanted in _BY_ID:
        return _BY_ID[wanted]
    for profile in PROFILES:
        if profile.name.lower() == wanted:
            return profile
    return None
