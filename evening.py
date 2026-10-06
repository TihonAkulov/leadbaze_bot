"""
evening.py — «вечерний ассистент»: результат дня, пороги/уровни, очки, личные рекорды,
варианты сообщений, сборка итогового текста и логика ежедневного отчёта в 23:00.

Вся статистика берётся через stats.day_window() + database.get_stats() — те же функции,
которыми пользуется «Статистика → По дням». Здесь только интерпретация и подача.
"""

from __future__ import annotations

import datetime as dt
import random

import database as db
import stats as stats_mod
from utils import ADMIN_NAME, moscow_now

# ---------------------------------------------------------------------------
# Очки и уровни
# ---------------------------------------------------------------------------

SCORE_WEIGHTS = {"sent": 1, "replies": 3, "interest": 5, "clients": 10, "closed": 15}

# (нижняя граница очков, эмодзи, название) — по возрастанию
LEVELS_WEEKDAY = [
    (0, "🔴", "Слабый"), (10, "🟡", "Минимальный"), (20, "🟢", "Нормальный"),
    (35, "🔥", "Хороший"), (50, "🚀", "Отличный"),
]
LEVELS_WEEKEND = [
    (0, "🔴", "Слабый"), (7, "🟡", "Минимальный"), (14, "🟢", "Нормальный"),
    (25, "🔥", "Хороший"), (35, "🚀", "Отличный"),
]


def is_weekend_msk(moment: dt.date | dt.datetime) -> bool:
    return moment.weekday() >= 5  # 5=суббота, 6=воскресенье


def compute_score(sent: int, replies: int, interest: int, clients: int, closed: int) -> int:
    return (
        sent * SCORE_WEIGHTS["sent"] + replies * SCORE_WEIGHTS["replies"]
        + interest * SCORE_WEIGHTS["interest"] + clients * SCORE_WEIGHTS["clients"]
        + closed * SCORE_WEIGHTS["closed"]
    )


def score_level(score: int, weekend: bool) -> tuple[str, str]:
    levels = LEVELS_WEEKEND if weekend else LEVELS_WEEKDAY
    emoji, label = levels[0][1], levels[0][2]
    for threshold, e, l in levels:
        if score >= threshold:
            emoji, label = e, l
    return emoji, label


# ---------------------------------------------------------------------------
# Варианты сообщений (минимум 5 на уровень, {name} — обращение к администратору)
# ---------------------------------------------------------------------------

LEVEL_MESSAGES: dict[str, list[str]] = {
    "Слабый": [
        "{name}, сегодня темп просел. Не страшно — завтра возвращаемся в рабочий ритм.",
        "{name}, сегодня получилось сделать немного меньше запланированного. Завтра есть возможность наверстать.",
        "{name}, день вышел спокойным. Сегодня — без рывка, завтра снова в работу.",
        "{name}, сегодня активности было маловато. Ничего критичного — главное, не превращать один тихий день в систему.",
        "{name}, сегодня не самый продуктивный день. Завтра снова набираем темп.",
    ],
    "Минимальный": [
        "{name}, работу сегодня сделал, но темп можно было держать выше. Завтра добавляем.",
        "{name}, небольшой, но рабочий день. Главное — движение есть.",
        "{name}, сегодня без большого объёма, но задача не заброшена. Продолжаем.",
        "{name}, минимальный план закрыт. Завтра можно попробовать взять темп выше.",
        "{name}, сегодня скорее разминка, чем полноценный разгон. Завтра работаем плотнее.",
    ],
    "Нормальный": [
        "{name}, хороший рабочий день. Основную задачу закрыл, движение по лидам продолжается. 👌",
        "{name}, сегодня нормально поработали. Есть объём, есть новые касания — продолжаем в том же направлении.",
        "{name}, день прошёл продуктивно. Без рекордов, но стабильный результат есть.",
        "{name}, хороший темп на сегодня. Главное — сохранять такую регулярность.",
        "{name}, план на день выполнен достойно. Завтра можно попробовать немного поднять планку.",
    ],
    "Хороший": [
        "{name}, отличная работа сегодня. 🔥 Хороший объём и достойный отклик от лидов. Так держать.",
        "{name}, сегодня хорошо разогнались. Есть и объём, и результат — именно такой темп нам и нужен.",
        "{name}, сильный рабочий день. 🔥 Проделана хорошая работа, теперь главное — не сбавлять.",
        "{name}, сегодня получилось заметно выше обычного темпа. Хороший результат, продолжаем.",
        "{name}, сегодня прям хорошо. Много касаний, хороший отклик и движение по воронке. 🚀",
    ],
    "Отличный": [
        "{name}, вот это день. 🚀 Отличный объём и сильный результат по лидам. Сегодня реально хорошо поработали.",
        "{name}, мощный результат за день. 🔥 Такой темп уже напрямую двигает нас к цели.",
        "{name}, сегодня очень сильный день. Много касаний, хорошие ответы и движение по воронке. Есть чем довольствоваться.",
        "{name}, отличный результат. Сегодня работа была не просто активной — она дала конкретный отклик. 🚀",
        "{name}, день закрываем очень достойно. Такой объём и такой отклик — именно ради этого и работаем. 🔥",
    ],
}

RECORD_LABELS = {
    "sent": "Отправленные", "replies": "Ответы", "warm": "Тёплые",
    "clients": "Клиенты", "closed": "Закрытые сделки", "revenue": "Выручка",
}


def fmt_money(amount: float) -> str:
    return f"{amount:,.0f}".replace(",", " ") + " ₽"


# ---------------------------------------------------------------------------
# Сборка отчёта
# ---------------------------------------------------------------------------

async def build_daily_report(report_date: dt.date) -> tuple[str, dict]:
    """Текст вечернего отчёта + сырые цифры (для сохранения в daily_reports).
    Источник данных — stats.day_window()/database.get_stats(), как и в «Статистика → По дням»."""
    days_back = (moscow_now().date() - report_date).days
    period = stats_mod.day_window(days_back)

    s = await db.get_stats(period)
    revenue = await db.get_revenue(period)
    warm = await db.count_warm_in_period(period)

    sent = s["sent_total"]
    replies = s["by_status"][db.STATUS_REPLIED]
    interest = s["by_status"][db.STATUS_INTEREST]
    clients = s["by_status"][db.STATUS_CLIENT]
    closed = s["by_status"][db.STATUS_CLIENT_CLOSED]
    score = compute_score(sent, replies, interest, clients, closed)

    current_by_key = {
        "sent": sent, "replies": replies, "warm": warm,
        "clients": clients, "closed": closed, "revenue": revenue,
    }

    prev = await db.get_previous_daily_maxes()
    records: list[str] = []
    if prev is not None:
        for key, value in current_by_key.items():
            if value > prev[key]:
                records.append(key)

    lines: list[str] = []
    if records:
        if len(records) == 1:
            key = records[0]
            value_str = fmt_money(current_by_key[key]) if key == "revenue" else str(current_by_key[key])
            lines.append(f"{ADMIN_NAME}, сегодня новый личный рекорд! 🚀")
            lines.append("")
            lines.append(f"{RECORD_LABELS[key]} — {value_str}, лучший результат за всё время.")
        else:
            lines.append(f"{ADMIN_NAME}, сегодня сразу {len(records)} личных рекорда! 🏆")
            lines.append("")
            for key in records:
                value_str = fmt_money(current_by_key[key]) if key == "revenue" else str(current_by_key[key])
                lines.append(f"• {RECORD_LABELS[key]} — {value_str}")
    else:
        emoji, label = score_level(score, is_weekend_msk(report_date))
        template = random.choice(LEVEL_MESSAGES[label])
        lines.append(f"{emoji} {template.format(name=ADMIN_NAME)}")

    lines.append("")
    lines.append("📊 <b>Результат дня</b>")
    lines.append("")
    lines.extend(stats_mod.sent_breakdown_lines(s))
    lines.append("")
    lines.append(f"💬 Ответов: {replies}")
    lines.append(f"🔥 Тёплых: {warm}")
    lines.append(f"🤝 Клиентов: {clients}")
    lines.append(f"💰 Закрытых: {closed}")
    lines.append(f"💵 Выручка: {fmt_money(revenue)}")

    raw = {**current_by_key, "score": score}
    return "\n".join(lines), raw


async def run_evening_report(bot, admin_id: int) -> None:
    """Вызывается планировщиком в 23:00 МСК. Идемпотентно — проверяет daily_reports по дате
    (уникальность), так что рестарт бота рядом с 23:00 не даёт дубль."""
    today = moscow_now().date()
    report_date = today.isoformat()

    if await db.daily_report_exists(report_date):
        return

    text, raw = await build_daily_report(today)

    saved = await db.save_daily_report(
        report_date=report_date, sent=raw["sent"], replies=raw["replies"], warm=raw["warm"],
        clients=raw["clients"], closed=raw["closed"], revenue=raw["revenue"], score=raw["score"],
    )
    if not saved:
        # кто-то другой (параллельный вызов) уже успел сохранить — не шлём второй раз
        return

    await bot.send_message(admin_id, text)
