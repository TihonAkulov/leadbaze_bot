"""stats.py — сбор и форматирование статистики (общая, по периодам, по сообщениям)."""

from __future__ import annotations

import datetime as dt
from typing import Optional

import database as db
from utils import moscow_now, short


PERIOD_LABELS = {"1": "1 день", "7": "7 дней", "30": "Месяц", "all": "Всё время"}


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
        f"✉️ Отправлено: {s['by_status'][db.STATUS_SENT] + s['by_status'][db.STATUS_REPLIED] + s['by_status'][db.STATUS_INTEREST] + s['by_status'][db.STATUS_CLIENT] + s['by_status'][db.STATUS_REJECT] + s['by_status'][db.STATUS_ARCHIVE]}",
        f"💬 Ответили: {s['by_status'][db.STATUS_REPLIED]}",
        f"🔥 Интерес: {s['by_status'][db.STATUS_INTEREST]}",
        f"🤝 Клиенты: {s['by_status'][db.STATUS_CLIENT]}",
        f"❌ Отказ: {s['by_status'][db.STATUS_REJECT]}",
    ]

    best = await db.best_initial_message(period)
    if best:
        msg, sent, clients = best
        lines.append("")
        lines.append("🏆 Лучшее сообщение:")
        lines.append(f"«{short(msg.content, 50)}»")

    sent_total = s['by_status'][db.STATUS_SENT] + s['by_status'][db.STATUS_REPLIED] + s['by_status'][db.STATUS_INTEREST] + s['by_status'][db.STATUS_CLIENT] + s['by_status'][db.STATUS_REJECT] + s['by_status'][db.STATUS_ARCHIVE]
    clients_total = s['by_status'][db.STATUS_CLIENT]
    conv = f"{clients_total / sent_total * 100:.1f}".replace(".", ",") + "%" if sent_total else "—"
    lines.append("")
    lines.append(f"Конверсия в клиента: {conv}")
    return "\n".join(lines)


async def render_leads_summary() -> str:
    """Краткая сводка по статусам для раздела «Лиды» (без периода — общий срез сейчас)."""
    s = await db.get_stats(period=None)
    lines = [f"Всего: {s['total']}"]
    for status in db.ALL_STATUSES:
        lines.append(f"{status}: {s['by_status'][status]}")
    return "\n".join(lines)
