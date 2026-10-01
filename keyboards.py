"""keyboards.py — все клавиатуры (Reply и Inline) в одном месте."""

from __future__ import annotations

from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

import database as db
from utils import short

# ---------------------------------------------------------------------------
# Главное меню
# ---------------------------------------------------------------------------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Лиды"), KeyboardButton(text="📈 Статистика")],
            [KeyboardButton(text="📅 Follow-up"), KeyboardButton(text="💾 Бэкап")],
            [KeyboardButton(text="⚙️ Другое")],
        ],
        resize_keyboard=True,
    )


def back_row(callback_data: str = "nav:main") -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)]


# ---------------------------------------------------------------------------
# Раздел «Лиды»
# ---------------------------------------------------------------------------

def leads_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить лидов", callback_data="leads:add")],
        [InlineKeyboardButton(text="📋 Новые лиды", callback_data="leads:new")],
        [InlineKeyboardButton(text="🔎 Найти лида", callback_data="leads:find")],
        [
            InlineKeyboardButton(text="📄 Вся база", callback_data="leads:all"),
            InlineKeyboardButton(text="🔥 Тёплые", callback_data="leads:warm"),
        ],
    ])


def new_leads_count_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="10", callback_data="newcount:10"),
            InlineKeyboardButton(text="20", callback_data="newcount:20"),
            InlineKeyboardButton(text="30", callback_data="newcount:30"),
        ],
        [
            InlineKeyboardButton(text="50", callback_data="newcount:50"),
            InlineKeyboardButton(text="100", callback_data="newcount:100"),
            InlineKeyboardButton(text="✏️ Другое", callback_data="newcount:custom"),
        ],
    ])


def after_add_leads_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Открыть новые", callback_data="leads:new"),
            InlineKeyboardButton(text="➕ Добавить ещё", callback_data="leads:add"),
        ],
    ])


def batch_action_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✉️ Отметить отправленными", callback_data="mark_sent"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_batch"),
        ]
    ])


def status_kb(lead: db.Lead) -> InlineKeyboardMarkup:
    username = lead.username
    rows = [
        [InlineKeyboardButton(text="🟡 Отправлено", callback_data=f"st:{username}:sent")],
        [InlineKeyboardButton(text="💬 Ответил", callback_data=f"st:{username}:replied")],
        [InlineKeyboardButton(text="🔥 Интерес", callback_data=f"st:{username}:interest")],
        [InlineKeyboardButton(text="🤝 Клиент", callback_data=f"st:{username}:client")],
        [InlineKeyboardButton(text="💰 Клиент закрыт", callback_data=f"closeclient:{username}")],
        [InlineKeyboardButton(text="❌ Отказ", callback_data=f"st:{username}:reject")],
        [InlineKeyboardButton(text="🚫 Бан", callback_data=f"st:{username}:ban")],
        [InlineKeyboardButton(text="🗑 Удалено", callback_data=f"st:{username}:deleted")],
        [InlineKeyboardButton(text="📦 Архив", callback_data=f"st:{username}:archive")],
    ]
    if lead.status == db.STATUS_SENT:
        if lead.fu1_sent_at is None:
            rows.append([InlineKeyboardButton(text="✅ FU1 отправлен", callback_data=f"markfu1:{username}")])
        elif lead.fu2_sent_at is None:
            rows.append([InlineKeyboardButton(text="✅ FU2 отправлен", callback_data=f"markfu2:{username}")])
    rows.append([InlineKeyboardButton(text="📝 Заметка", callback_data=f"note:{username}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bulk_status_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟡 Отправлено", callback_data="bulkstatus:sent")],
        [InlineKeyboardButton(text="💬 Ответил", callback_data="bulkstatus:replied")],
        [InlineKeyboardButton(text="🔥 Интерес", callback_data="bulkstatus:interest")],
        [InlineKeyboardButton(text="🤝 Клиент", callback_data="bulkstatus:client")],
        [InlineKeyboardButton(text="❌ Отказ", callback_data="bulkstatus:reject")],
        [InlineKeyboardButton(text="🚫 Бан", callback_data="bulkstatus:ban")],
        [InlineKeyboardButton(text="🗑 Удалено", callback_data="bulkstatus:deleted")],
        [InlineKeyboardButton(text="📦 Архив", callback_data="bulkstatus:archive")],
    ])


def build_pagination_kb(
    page: int, total_pages: int, window: int = 1,
    prefix: str = "leads_page", back_target: str = "leads:menu",
) -> Optional[InlineKeyboardMarkup]:
    """Компактная пагинация: ⏪ 1 2 … 10 ⏩ (макс. 5 цифровых кнопок). Логика/раскладка не менялись —
    только callback-префикс вынесен параметром, чтобы переиспользовать для «Тёплых»."""
    if total_pages <= 1:
        return None
    keep = {1, total_pages}
    for p in range(page - window, page + window + 1):
        if 1 <= p <= total_pages:
            keep.add(p)
    ordered = sorted(keep)

    buttons: list[InlineKeyboardButton] = []
    if page > 1:
        buttons.append(InlineKeyboardButton(text="⏪", callback_data=f"{prefix}:{page - 1}"))
    prev_shown = None
    for p in ordered:
        if prev_shown is not None and p - prev_shown > 1:
            buttons.append(InlineKeyboardButton(text="…", callback_data="noop"))
        if p == page:
            buttons.append(InlineKeyboardButton(text=f"·{p}·", callback_data="noop"))
        else:
            buttons.append(InlineKeyboardButton(text=str(p), callback_data=f"{prefix}:{p}"))
        prev_shown = p
    if page < total_pages:
        buttons.append(InlineKeyboardButton(text="⏩", callback_data=f"{prefix}:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons, back_row(back_target)])


def confirm_kb(action: str, yes_text: str = "✅ Да", no_text: str = "❌ Отмена") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=yes_text, callback_data=f"confirm:{action}"),
            InlineKeyboardButton(text=no_text, callback_data="cancel_confirm"),
        ]
    ])


def delete_confirm_kb(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"confirm:delreal:{username}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_confirm"),
        ]
    ])


# ---------------------------------------------------------------------------
# Выбор сообщения (кнопками из недавних + свободный ввод)
# ---------------------------------------------------------------------------

def choose_message_kb(recents: list[db.Message]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{m.title}  {short(m.content, 20)}", callback_data=f"pickmsg:{i}")]
        for i, m in enumerate(recents)
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_choose")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Раздел «Сообщения»
# ---------------------------------------------------------------------------

def messages_list_kb(messages: list[db.Message], category: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{m.title}  {short(m.content, 20)}", callback_data=f"msgcard:{m.id}")]
        for m in messages
    ]
    rows.append(back_row("stats:bymessage"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def message_card_kb(message: db.Message, category: str) -> InlineKeyboardMarkup:
    """Без «Статистика» — полная статистика теперь показывается прямо в карточке (см. messages.py)."""
    rows = [
        [InlineKeyboardButton(text="📤 Использовать", callback_data=f"msguse:{message.id}")],
        [InlineKeyboardButton(text="📄 Полный текст", callback_data=f"msgfulltext:{message.id}")],
        [InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"msgedit:{message.id}")],
    ]
    if message.active:
        rows.append([InlineKeyboardButton(text="📦 Архивировать", callback_data=f"msgarchive:{message.id}")])
    rows.append(back_row(f"statsmsgcat:{category}"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Follow-up
# ---------------------------------------------------------------------------

def followup_summary_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Открыть Follow-up", callback_data="followup:open")],
    ])


def followup_detail_kb(has_fu1: bool, has_fu2: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_fu1:
        rows.append([InlineKeyboardButton(text="📨 Отправить FU1", callback_data="followup:send:fu1")])
    if has_fu2:
        rows.append([InlineKeyboardButton(text="📨 Отправить FU2", callback_data="followup:send:fu2")])
    rows.append([InlineKeyboardButton(text="📄 Посмотреть список", callback_data="followup:list")])
    rows.append(back_row("nav:main"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def stats_period_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 день", callback_data="statsperiod:1"),
            InlineKeyboardButton(text="7 дней", callback_data="statsperiod:7"),
            InlineKeyboardButton(text="Месяц", callback_data="statsperiod:30"),
            InlineKeyboardButton(text="Всё время", callback_data="statsperiod:all"),
        ],
        [InlineKeyboardButton(text="💬 По сообщениям", callback_data="stats:bymessage")],
        back_row("nav:main"),
    ])


def stats_by_message_categories_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📨 Первые", callback_data="statsmsgcat:initial"),
            InlineKeyboardButton(text="↩️ FU1", callback_data="statsmsgcat:fu1"),
            InlineKeyboardButton(text="↪️ FU2", callback_data="statsmsgcat:fu2"),
        ],
        back_row("stats:menu"),
    ])


# ---------------------------------------------------------------------------
# Бэкап
# ---------------------------------------------------------------------------

def backup_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💾 Создать бэкап", callback_data="backup:create"),
        ],
        [
            InlineKeyboardButton(text="♻️ Восстановить", callback_data="backup:restore"),
            InlineKeyboardButton(text="📋 Последние бэкапы", callback_data="backup:history"),
        ],
    ])


def backup_history_kb(backups: list[db.Backup]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{b.created_at.strftime('%d.%m.%Y %H:%M')} ({b.kind})",
            callback_data=f"backup:restore_ask:{b.id}",
        )]
        for b in backups
    ]
    rows.append(back_row("backup:menu"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# «Другое»
# ---------------------------------------------------------------------------

def other_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Заметки", callback_data="notes:menu")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="other:help")],
        [InlineKeyboardButton(text="🗑 Очистить базу", callback_data="other:clear_db")],
    ])


def notes_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить заметку", callback_data="notes_add")],
        [InlineKeyboardButton(text="📋 Посмотреть заметки", callback_data="notes_view")],
        [InlineKeyboardButton(text="🗑 Удалить все заметки", callback_data="notes_delete_all")],
        back_row("other:menu"),
    ])
