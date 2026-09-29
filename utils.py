"""utils.py — общие функции: время (МСК), username, разбор ввода, форматирование."""

from __future__ import annotations

import datetime as dt
import html
import os
import re
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")

esc = html.escape  # экранирование пользовательского текста для HTML-режима Telegram


def moscow_now() -> dt.datetime:
    """Текущее время в Europe/Moscow, наивное (без tzinfo) — для хранения и сравнения."""
    return dt.datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def seconds_until(hour: int, minute: int = 0) -> float:
    """Сколько секунд до ближайшего hour:minute по Москве."""
    now = moscow_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def short(text: Optional[str], n: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------------
# username
# ---------------------------------------------------------------------------

USERNAME_TOKEN_RE = re.compile(
    r"^(?:https?://)?(?:(?:t\.me|telegram\.me)/)?@?([A-Za-z0-9_]{5,32})/?$",
    re.IGNORECASE,
)


def normalize_username(token: str) -> Optional[str]:
    """@username / username / t.me/username / telegram.me/username (с http(s) или без) -> username."""
    m = USERNAME_TOKEN_RE.match(token.strip())
    return m.group(1) if m else None


def extract_usernames(text_value: str) -> list[str]:
    """Все username из текста (в т.ч. из ссылок), без дублей, порядок сохранён."""
    tokens = re.split(r"[\s,]+", text_value.strip())
    seen: set[str] = set()
    result: list[str] = []
    for token in tokens:
        if not token:
            continue
        uname = normalize_username(token)
        if uname is None:
            continue
        low = uname.lower()
        if low not in seen:
            seen.add(low)
            result.append(uname)
    return result


# ---------------------------------------------------------------------------
# разбор ввода
# ---------------------------------------------------------------------------

RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
SINGLE_NUM_RE = re.compile(r"^(\d+)$")
DATE_INPUT_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?:\s+(\d{1,2}):(\d{2}))?$")


def parse_position_selector(text_value: str) -> Optional[list[int]]:
    """Выбор лидов по номерам: "5", "5-20" или "5, 7, 10". None при мусоре."""
    text_value = text_value.strip()
    if not text_value:
        return None
    m = RANGE_RE.match(text_value)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if start > end:
            start, end = end, start
        return list(range(start, end + 1))
    if SINGLE_NUM_RE.match(text_value):
        return [int(text_value)]
    parts = [p.strip() for p in text_value.split(",") if p.strip()]
    if parts and all(p.isdigit() for p in parts):
        return sorted({int(p) for p in parts})
    return None


def parse_date_input(text_value: str) -> Optional[dt.datetime]:
    """Дата для /change_date: 28.09 / 28.09.2026 / 28.09 14:00."""
    m = DATE_INPUT_RE.match(text_value.strip())
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    try:
        return dt.datetime(
            int(year) if year else moscow_now().year,
            int(month), int(day),
            int(hour) if hour else 9,
            int(minute) if minute else 0,
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# форматирование
# ---------------------------------------------------------------------------

def fmt_date(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def fmt_datetime(value) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


def pct(part: int, total: int) -> str:
    return f"{part / total * 100:.1f}".replace(".", ",") + "%" if total else "—"
