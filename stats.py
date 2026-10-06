"""stats.py — сбор и форматирование статистики (общая, по периодам, по сообщениям)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

import database as db
from utils import moscow_now, short


PERIOD_LABELS = {"1": "1 день", "7": "7 дней", "30": "Месяц", "all": "Всё время"}


def sent_breakdown_lines(s: dict) -> list[str]:
    """Единое место форматирования разбивки отправок — используется везде, где показывается
    «Отправлено», чтобы не дублировать разметку по экранам."""
    return [
        f"📨 Отправлено: {s['sent_total']}",
        f"├ Первое: {s['sent_initial']}",
        f"├ FU1: {s['sent_fu1']}",
        f"└ FU2: {s['sent_fu2']}",
    ]


def period_range(code: str) -> Optional[tuple[dt.datetime, dt.datetime]]:
    if code == "all":
        return None
    days = int(code)
    now = moscow_now()
    return (now - dt.timedelta(days=days), now)


async def render_overall_stats(period_code: str = "all") -> str:
    period = period_range(period_code)
    s = await db.get_stats(period)
    lines = [
        "📊 <b>СТАТИСТИКА</b>",
        f"Период: {PERIOD_LABELS.get(period_code, 'Всё время')}",
        "",
        *sent_breakdown_lines(s),
        "",
        f"💬 Ответили: {s['by_status'][db.STATUS_REPLIED]}",
        f"🔥 Интерес: {s['by_status'][db.STATUS_INTEREST]}",
        f"🤝 Клиенты: {s['by_status'][db.STATUS_CLIENT]}",
        f"💰 Клиент закрыт: {s['by_status'][db.STATUS_CLIENT_CLOSED]}",
        f"❌ Отказ: {s['by_status'][db.STATUS_REJECT]}",
    ]

    best = await db.best_initial_message(period)
    if best:
        msg, sent, clients = best
        lines.append("")
        lines.append("🏆 Лучшее сообщение:")
        lines.append(f"🏷 {msg.title}")

    lines.append("")
    lines.append(f"📊 Конверсия в ответ: {reply_conversion_pct(s)}")
    lines.append(f"💰 Конверсия в клиента: {client_conversion_pct(s)}")
    return "\n".join(lines)


def conversion_pct(count: int, sent: int) -> str:
    return f"{count / sent * 100:.2f}".replace(".", ",") + "%" if sent else "—"


def reply_conversion_pct(s: dict) -> str:
    return conversion_pct(s["by_status"][db.STATUS_REPLIED], s["sent_total"])


def client_conversion_pct(s: dict) -> str:
    return conversion_pct(s["by_status"][db.STATUS_CLIENT], s["sent_total"])


def day_window(days_back: int) -> tuple[dt.datetime, dt.datetime]:
    """Окно 07:00–23:00 по Москве для дня `days_back` назад (0 — сегодня)."""
    day = (moscow_now() - dt.timedelta(days=days_back)).date()
    start = dt.datetime.combine(day, dt.time(7, 0))
    end = dt.datetime.combine(day, dt.time(23, 0))
    return start, end


async def render_daily_breakdown() -> str:
    labels = ["Сегодня", "Вчера", "Позавчера"]
    lines = ["📅 <b>СТАТИСТИКА ПО ДНЯМ</b>", "(окно 07:00–23:00 по Москве, по дате отправки)"]
    for i, label in enumerate(labels):
        start, end = day_window(i)
        s = await db.get_stats((start, end))
        lines.append("")
        lines.append(f"<b>{label}</b> ({start.strftime('%d.%m')})")
        lines.extend(sent_breakdown_lines(s))
        lines.append(f"💬 Ответили: {s['by_status'][db.STATUS_REPLIED]}")
        lines.append(f"🔥 Интерес: {s['by_status'][db.STATUS_INTEREST]}")
        lines.append(f"🤝 Клиенты: {s['by_status'][db.STATUS_CLIENT]}")
        lines.append(f"💰 Клиент закрыт: {s['by_status'][db.STATUS_CLIENT_CLOSED]}")
    return "\n".join(lines)


async def render_leads_summary() -> str:
    """Краткая сводка по статусам для раздела «Лиды» (без периода — общий срез сейчас)."""
    s = await db.get_stats(period=None)
    lines = [f"Всего: {s['total']}"]
    for status in db.ALL_STATUSES:
        lines.append(f"{status}: {s['by_status'][status]}")
    return "\n".join(lines)

