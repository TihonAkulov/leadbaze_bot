"""messages.py — логика раздела «Сообщения»: выбор, отображение, статистика по категориям."""

from __future__ import annotations

import database as db
from utils import short


async def render_category_list(category: str) -> tuple[str, list[db.Message]]:
    """Текст + список сообщений категории (для messages_list_kb)."""
    active, archived = await db.count_messages_active_archived(category)
    messages = await db.get_messages(category, active_only=False)
    label = db.CATEGORY_LABELS.get(category, category)
    text = f"✉️ <b>{label}</b>\nАктивных: {active}   Архивных: {archived}"
    if not messages:
        text += "\n\nПока нет ни одного сообщения этой категории."
    return text, messages


async def render_message_card(message: db.Message) -> str:
    s = await db.get_message_stats(message.id)
    sent = s["total"]
    replied = s["by_status"][db.STATUS_REPLIED]
    interest = s["by_status"][db.STATUS_INTEREST]
    clients = s["by_status"][db.STATUS_CLIENT]
    conv = f"{clients / sent * 100:.1f}".replace(".", ",") + "%" if sent else "—"
    archived_note = "\n📦 В архиве" if not message.active else ""
    return (
        f"✉️ <b>{message.title}</b>{archived_note}\n"
        f"«{message.content}»\n\n"
        f"📊 Отправлено: {sent}\n"
        f"💬 Ответили: {replied}\n"
        f"🔥 Интерес: {interest}\n"
        f"🤝 Клиентов: {clients}\n\n"
        f"Конверсия в клиента: {conv}"
    )


async def render_message_full_stats(message: db.Message) -> str:
    s = await db.get_message_stats(message.id)
    lines = [f"📊 <b>{message.title}</b>", f"«{short(message.content, 60)}»", ""]
    lines.append(f"Отправлено: {s['total']}")
    for status in (db.STATUS_REPLIED, db.STATUS_INTEREST, db.STATUS_CLIENT, db.STATUS_REJECT, db.STATUS_ARCHIVE):
        lines.append(f"{status}: {s['by_status'][status]}")
    return "\n".join(lines)
