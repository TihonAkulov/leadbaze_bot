"""
bot.py — Telegram-бот для ведения базы лидов (ручные рассылки).

Вся логика бота — здесь. Работа с БД — в database.py.
Запуск: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from dotenv import load_dotenv

import database as db

# ---------------------------------------------------------------------------
# Настройка
# ---------------------------------------------------------------------------

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lead_bot")

router = Router()

# username в Telegram: 5-32 символов, буквы/цифры/подчёркивание
USERNAME_BODY_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")

# один токен: @username / username / t.me/username / telegram.me/username (с http(s):// или без)
USERNAME_TOKEN_RE = re.compile(
    r"^(?:https?://)?(?:(?:t\.me|telegram\.me)/)?@?([A-Za-z0-9_]{5,32})/?$",
    re.IGNORECASE,
)

RANGE_RE = re.compile(r"^(\d+)\s*-\s*(\d+)$")
SINGLE_NUM_RE = re.compile(r"^(\d+)$")

# "последняя выданная пачка" — храним в памяти процесса, как договорились (V1)
last_batch: list[str] = []

# сегодняшние FU1/FU2 из последнего показа /followup — для общей кнопки "✅ Отправлено"
pending_fu1_batch: list[str] = []
pending_fu2_batch: list[str] = []

# размер "страницы" при постраничном просмотре всей базы (пагинация не меняется)
LEADS_PER_PAGE = 10


# ---------------------------------------------------------------------------
# FSM состояния
# ---------------------------------------------------------------------------

class Form(StatesGroup):
    new_leads_count = State()      # ждём число "сколько лидов показать"
    searching = State()            # ждём username для поиска
    adding_note = State()          # ждём текст заметки
    choosing_message = State()     # ждём выбор/ввод текста отправленного сообщения
    msg_leads_range = State()      # /msg_leads: ждём диапазон номеров
    msg_leads_confirm = State()    # /msg_leads: ждём подтверждения
    stats_message_choice = State()  # /stats: ждём номер сообщения для детальной статистики


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Добавить лидов"), KeyboardButton(text="📋 Новые лиды")],
            [KeyboardButton(text="🔎 Найти лида"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📅 Follow-up"), KeyboardButton(text="📋 Показать всю базу")],
        ],
        resize_keyboard=True,
    )


def batch_action_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✉️ Отметить отправленными", callback_data="mark_sent"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_batch"),
            ]
        ]
    )


def status_kb(lead: db.Lead) -> InlineKeyboardMarkup:
    username = lead.username
    rows = [
        [InlineKeyboardButton(text="🟡 Отправлено", callback_data=f"st:{username}:sent")],
        [InlineKeyboardButton(text="💬 Ответил", callback_data=f"st:{username}:replied")],
        [InlineKeyboardButton(text="🔥 Интерес", callback_data=f"st:{username}:interest")],
        [InlineKeyboardButton(text="🤝 Клиент", callback_data=f"st:{username}:client")],
        [InlineKeyboardButton(text="❌ Отказ", callback_data=f"st:{username}:reject")],
        [InlineKeyboardButton(text="🚫 Бан", callback_data=f"st:{username}:ban")],
        [InlineKeyboardButton(text="🗑 Удалено", callback_data=f"st:{username}:deleted")],
        [InlineKeyboardButton(text="📦 Архив", callback_data=f"st:{username}:archive")],
    ]
    # кнопка следующего фактического FU — только пока он ещё не отправлен, и только одна за раз
    if lead.status == db.STATUS_SENT:
        if lead.fu1_sent_at is None:
            rows.append([InlineKeyboardButton(text="✅ FU1 отправлен", callback_data=f"markfu1:{username}")])
        elif lead.fu2_sent_at is None:
            rows.append([InlineKeyboardButton(text="✅ FU2 отправлен", callback_data=f"markfu2:{username}")])
    rows.append([InlineKeyboardButton(text="📝 Заметка", callback_data=f"note:{username}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_pagination_kb(page: int, total_pages: int, window: int = 1) -> Optional[InlineKeyboardMarkup]:
    """Компактная пагинация: ⏪ 1 2 … 10 ⏩ (макс. 5 кнопок-цифр). НЕ МЕНЯТЬ — уже согласовано."""
    if total_pages <= 1:
        return None

    keep = {1, total_pages}
    for p in range(page - window, page + window + 1):
        if 1 <= p <= total_pages:
            keep.add(p)
    ordered = sorted(keep)

    buttons: list[InlineKeyboardButton] = []
    if page > 1:
        buttons.append(InlineKeyboardButton(text="⏪", callback_data=f"leads_page:{page - 1}"))

    prev_shown: Optional[int] = None
    for p in ordered:
        if prev_shown is not None and p - prev_shown > 1:
            buttons.append(InlineKeyboardButton(text="…", callback_data="noop"))
        if p == page:
            buttons.append(InlineKeyboardButton(text=f"·{p}·", callback_data="noop"))
        else:
            buttons.append(InlineKeyboardButton(text=str(p), callback_data=f"leads_page:{p}"))
        prev_shown = p

    if page < total_pages:
        buttons.append(InlineKeyboardButton(text="⏩", callback_data=f"leads_page:{page + 1}"))

    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def confirm_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=f"confirm:{action}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_confirm"),
            ]
        ]
    )


def delete_confirm_kb(username: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"confirm:delreal:{username}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_confirm"),
            ]
        ]
    )


def choose_message_kb(recents: list[str]) -> Optional[InlineKeyboardMarkup]:
    if not recents:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{i + 1}. {m[:30]}", callback_data=f"pickmsg:{i}")]
            for i, m in enumerate(recents)
        ]
    )


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def normalize_username(token: str) -> Optional[str]:
    """@username / username / t.me/username / telegram.me/username (с http(s) или без) -> username."""
    m = USERNAME_TOKEN_RE.match(token.strip())
    if not m:
        return None
    return m.group(1)


def extract_usernames(text_value: str) -> list[str]:
    """Достаёт из текста все username (в т.ч. из ссылок t.me/telegram.me), без дублей, порядок сохранён."""
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


def fmt_date(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def fmt_datetime(value) -> str:
    return value.strftime("%d.%m.%Y %H:%M") if value else "—"


def fu_field_text(due_at, sent_at) -> str:
    """До фактической отправки — только дата плана; после — дата и время факта."""
    if sent_at:
        return fmt_datetime(sent_at)
    if due_at:
        return fmt_date(due_at)
    return "—"


def status_display(lead: db.Lead) -> str:
    """Базовый статус + суффикс о последнем фактически отправленном FU / этапе ответа."""
    base = lead.status
    if base == db.STATUS_SENT:
        if lead.fu2_sent_at:
            return f"{base} (FU2 | {fmt_datetime(lead.fu2_sent_at)})"
        if lead.fu1_sent_at:
            return f"{base} (FU1 | {fmt_datetime(lead.fu1_sent_at)})"
        return base
    if base == db.STATUS_REPLIED and lead.response_stage in ("fu1", "fu2"):
        label = "после FU1" if lead.response_stage == "fu1" else "после FU2"
        return f"{base} ({label})"
    return base


def lead_card_text(lead: db.Lead) -> str:
    return (
        f"👤 @{lead.username}\n"
        f"Статус: {status_display(lead)}\n"
        f"Сообщение: {lead.message or '—'}\n"
        f"Отправлен: {fmt_datetime(lead.message_sent_at)}\n"
        f"FU1: {fu_field_text(lead.fu1_due_at, lead.fu1_sent_at)}\n"
        f"FU2: {fu_field_text(lead.fu2_due_at, lead.fu2_sent_at)}\n"
        f"Заметка:\n{lead.note or '—'}"
    )


def lead_list_block(i: int, lead: db.Lead) -> str:
    """Компактный блок одного лида в постраничном списке (пагинация не меняется, только поля)."""
    return (
        f"{i}. @{lead.username} — {status_display(lead)}\n"
        f"  Сообщение: {lead.message or '—'}\n"
        f"  Отправлен: {fmt_datetime(lead.message_sent_at)}\n"
        f"  FU1: {fu_field_text(lead.fu1_due_at, lead.fu1_sent_at)} | "
        f"FU2: {fu_field_text(lead.fu2_due_at, lead.fu2_sent_at)}\n"
        f"  Заметка: {lead.note or '—'}"
    )


# ---------------------------------------------------------------------------
# Общий флоу выбора сообщения (используется и в "Новые лиды", и в /msg_leads)
# ---------------------------------------------------------------------------

async def prompt_choose_message(target: Message, state: FSMContext, purpose: str) -> None:
    recents = await db.get_recent_messages(4)
    await state.set_state(Form.choosing_message)
    await state.update_data(purpose=purpose, recent_messages=recents)

    lines = ["Какое сообщение отправлено?", ""]
    for i, m in enumerate(recents, start=1):
        lines.append(f"{i}. {m}")
    lines.append("")
    lines.append("Или напишите сообщение вручную.")
    await target.answer("\n".join(lines), reply_markup=choose_message_kb(recents))


async def apply_chosen_message(target: Message, state: FSMContext, message_label: str) -> None:
    data = await state.get_data()
    purpose = data.get("purpose")

    if purpose == "batch_mark_sent":
        await state.clear()
        global last_batch
        if not last_batch:
            await target.answer("Пачка уже пуста.", reply_markup=main_menu_kb())
            return
        count = await db.assign_message_and_send(last_batch, message_label)
        last_batch = []
        await target.answer(
            f"✅ Отмечено отправленными: {count}\nСообщение: «{message_label}»",
            reply_markup=main_menu_kb(),
        )
        return

    if purpose == "msg_leads":
        usernames = data.get("usernames") or []
        await state.update_data(usernames=usernames, message_label=message_label)
        await state.set_state(Form.msg_leads_confirm)
        text_lines = [
            "⚠️ Проверьте данные",
            f"Лиды: {data.get('range_start')}–{data.get('range_end')}",
            f"Количество: {len(usernames)}",
            "Сообщение:",
            f"«{message_label}»",
            "Статус:",
            "🟡 Отправлено",
            "Время:",
            fmt_datetime(db.moscow_now()),
            "Продолжить?",
        ]
        await target.answer("\n".join(text_lines), reply_markup=confirm_kb("msg_leads_apply"))
        return

    await state.clear()


@router.callback_query(F.data.startswith("pickmsg:"), Form.choosing_message)
async def cb_pick_message(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    recents = data.get("recent_messages") or []
    if idx < 0 or idx >= len(recents):
        await callback.answer("Такого варианта уже нет.", show_alert=True)
        return
    message_label = recents[idx]
    await callback.message.edit_reply_markup(reply_markup=None)
    await apply_chosen_message(callback.message, state, message_label)
    await callback.answer()


@router.message(Form.choosing_message)
async def msg_choose_manual(message: Message, state: FSMContext) -> None:
    text_value = (message.text or "").strip()
    if not text_value:
        await message.answer("Пришли текст сообщения.")
        return
    await apply_chosen_message(message, state, text_value)


# ---------------------------------------------------------------------------
# /start, /help
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я бот для ведения базы лидов.\n\n"
        "Пришли список username (можно @user1 @user2, ссылками t.me/username, "
        "или каждый с новой строки) — я добавлю новых в базу.\n\n"
        "Или используй меню ниже.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📥 Добавить лидов — пришли список @username / ссылок t.me/username\n"
        "📋 Новые лиды — выдать пачку ещё не отправленных\n"
        "🔎 Найти лида — карточка, смена статуса, отметка FU, заметка\n"
        "📊 Статистика (/stats) — общая сводка + по конкретному сообщению\n"
        "📅 Follow-up (/followup) — кому сегодня FU1/FU2\n"
        "📋 Показать всю базу — постранично; /5 откроет лида №5\n"
        "/msg_leads — массово отметить диапазон лидов отправленными\n"
        "/del@username — удалить лида навсегда (с подтверждением)\n"
        "/clear_db — полностью очистить базу (с подтверждением)\n\n"
        "Быстрая смена статуса: @username интерес / ответил / клиент / отказ / бан / удалено / архив",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("clear_db"))
async def cmd_clear_db(message: Message) -> None:
    await message.answer(
        "⚠️ Это удалит ВСЮ базу лидов без возможности восстановления. Продолжить?",
        reply_markup=confirm_kb("clear_db"),
    )


@router.callback_query(F.data == "confirm:clear_db")
async def cb_confirm_clear_db(callback: CallbackQuery) -> None:
    global last_batch
    count = await db.clear_all_leads()
    last_batch = []
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"🗑 База очищена. Удалено лидов: {count}")
    await callback.answer()


@router.callback_query(F.data == "cancel_confirm")
async def cb_cancel_confirm(callback: CallbackQuery) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


# ---------------------------------------------------------------------------
# /del@username — удаление навсегда
# ---------------------------------------------------------------------------

@router.message(F.text.regexp(r"^/del@([A-Za-z0-9_]{5,32})$"))
async def cmd_del_lead(message: Message) -> None:
    username = message.text[len("/del@"):]
    lead = await db.find_lead(username)
    if not lead:
        await message.answer(f"⚠️ @{username} не найден в базе.")
        return
    await message.answer(
        f"⚠️ Удалить лида @{username}?\nЛид будет удалён НАВСЕГДА из базы.",
        reply_markup=delete_confirm_kb(username),
    )


@router.callback_query(F.data.startswith("confirm:delreal:"))
async def cb_confirm_delete_real(callback: CallbackQuery) -> None:
    username = callback.data.split(":", 2)[2]
    ok = await db.delete_lead(username)
    await callback.message.edit_reply_markup(reply_markup=None)
    if ok:
        await callback.message.answer(f"🗑 @{username} удалён навсегда.")
    else:
        await callback.message.answer(f"⚠️ @{username} не найден в базе.")
    await callback.answer()


# ---------------------------------------------------------------------------
# 📋 Новые лиды
# ---------------------------------------------------------------------------

@router.message(F.text == "📋 Новые лиды")
async def new_leads_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.new_leads_count)
    await message.answer("Сколько лидов показать?", reply_markup=ReplyKeyboardRemove())


@router.message(Form.new_leads_count)
async def new_leads_count(message: Message, state: FSMContext) -> None:
    text_value = (message.text or "").strip()
    if not text_value.isdigit():
        await message.answer("Пришли число, например: 20")
        return
    limit = int(text_value)
    await state.clear()

    leads = await db.get_new_leads(limit)
    if not leads:
        await message.answer("Новых лидов со статусом ⚪ Не отправлено нет.", reply_markup=main_menu_kb())
        return

    global last_batch
    last_batch = [l.username for l in leads]

    lines = "\n".join(f"@{u}" for u in last_batch)
    await message.answer(
        f"📋 Новые лиды — {len(last_batch)}\n{lines}",
        reply_markup=batch_action_kb(),
    )
    await message.answer("Меню:", reply_markup=main_menu_kb())


@router.callback_query(F.data == "mark_sent")
async def cb_mark_sent(callback: CallbackQuery, state: FSMContext) -> None:
    if not last_batch:
        await callback.answer("Пачка уже пуста.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await prompt_choose_message(callback.message, state, purpose="batch_mark_sent")
    await callback.answer()


@router.callback_query(F.data == "cancel_batch")
async def cb_cancel_batch(callback: CallbackQuery) -> None:
    global last_batch
    last_batch = []
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


# ---------------------------------------------------------------------------
# 📥 Добавить лидов (кнопка) + свободная отправка списка username
# ---------------------------------------------------------------------------

@router.message(F.text == "📥 Добавить лидов")
async def add_leads_prompt(message: Message) -> None:
    await message.answer("Пришли список username, например:\n@user1\nhttps://t.me/user2\nuser3")


async def process_add_leads(message: Message, usernames: list[str]) -> None:
    added, duplicates = await db.add_leads(usernames)
    lines = ["📥 Результат", f"✅ Добавлено: {len(added)}", f"⚠️ Уже были в базе: {len(duplicates)}"]
    if added:
        lines.append("Новые:")
        lines.extend(f"@{u}" for u in added)
    await message.answer("\n".join(lines), reply_markup=main_menu_kb())


# ---------------------------------------------------------------------------
# 🔎 Найти лида
# ---------------------------------------------------------------------------

@router.message(F.text == "🔎 Найти лида")
async def search_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.searching)
    await message.answer("Пришли username лида (можно с @ или без, или ссылку t.me/...).", reply_markup=ReplyKeyboardRemove())


@router.message(Form.searching)
async def search_result(message: Message, state: FSMContext) -> None:
    usernames = extract_usernames(message.text or "")
    await state.clear()
    if not usernames:
        await message.answer("Не нашёл username в сообщении.", reply_markup=main_menu_kb())
        return
    username = usernames[0]
    lead = await db.find_lead(username)
    if not lead:
        await message.answer(f"⚠️ @{username} не найден в базе.", reply_markup=main_menu_kb())
        return
    await message.answer(lead_card_text(lead), reply_markup=status_kb(lead))
    await message.answer("Меню:", reply_markup=main_menu_kb())


# ---------------------------------------------------------------------------
# Изменение статуса из карточки (инлайн-кнопки)
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("st:"))
async def cb_set_status(callback: CallbackQuery) -> None:
    _, username, code = callback.data.split(":", 2)
    status = db.STATUS_BY_CODE.get(code)
    if not status:
        await callback.answer("Неизвестный статус", show_alert=True)
        return

    ok = await db.set_status(username, status)
    if ok:
        await callback.answer(f"Статус изменён: {status}")
        lead = await db.find_lead(username)
        if lead:
            await callback.message.edit_text(lead_card_text(lead), reply_markup=status_kb(lead))
    else:
        await callback.answer("Лид не найден", show_alert=True)


# ---------------------------------------------------------------------------
# Фактическая отправка FU1 / FU2 с карточки лида
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("markfu1:"))
async def cb_mark_fu1(callback: CallbackQuery) -> None:
    username = callback.data.split(":", 1)[1]
    await db.mark_fu1_batch([username])
    lead = await db.find_lead(username)
    if lead:
        await callback.message.edit_text(lead_card_text(lead), reply_markup=status_kb(lead))
    await callback.answer("FU1 отмечен отправленным")


@router.callback_query(F.data.startswith("markfu2:"))
async def cb_mark_fu2(callback: CallbackQuery) -> None:
    username = callback.data.split(":", 1)[1]
    await db.mark_fu2_batch([username])
    lead = await db.find_lead(username)
    if lead:
        await callback.message.edit_text(lead_card_text(lead), reply_markup=status_kb(lead))
    await callback.answer("FU2 отмечен отправленным")


# ---------------------------------------------------------------------------
# Заметка
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("note:"))
async def cb_note_start(callback: CallbackQuery, state: FSMContext) -> None:
    username = callback.data.split(":", 1)[1]
    await state.set_state(Form.adding_note)
    await state.update_data(username=username)
    await callback.message.answer(f"Пришли текст заметки для @{username}:")
    await callback.answer()


@router.message(Form.adding_note)
async def note_save(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    username = data.get("username")
    await state.clear()
    if not username:
        await message.answer("Что-то пошло не так, попробуй заново.", reply_markup=main_menu_kb())
        return
    await db.set_note(username, message.text or "")
    await message.answer(f"📝 Заметка для @{username} сохранена.", reply_markup=main_menu_kb())


# ---------------------------------------------------------------------------
# 📅 Follow-up
# ---------------------------------------------------------------------------

@router.message(F.text == "📅 Follow-up")
@router.message(Command("followup"))
async def followups(message: Message) -> None:
    fu1_leads, fu2_leads = await db.get_followups_due()

    global pending_fu1_batch, pending_fu2_batch
    pending_fu1_batch = [l.username for l in fu1_leads]
    pending_fu2_batch = [l.username for l in fu2_leads]

    if not fu1_leads and not fu2_leads:
        await message.answer("🔔 На сегодня follow-up нет.", reply_markup=main_menu_kb())
        return

    lines = ["🔔 FOLLOW-UP НА СЕГОДНЯ"]
    if fu1_leads:
        lines.append("FU1:")
        for l in fu1_leads:
            lines.append(f"@{l.username}")
            lines.append(l.message or "—")
            lines.append("FU1: сегодня")
    if fu2_leads:
        lines.append("FU2:")
        for l in fu2_leads:
            lines.append(f"@{l.username}")
            lines.append(l.message or "—")
            lines.append("FU2: сегодня")

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправлено", callback_data="followup_mark"),
    ]])
    await message.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data == "followup_mark")
async def cb_followup_mark(callback: CallbackQuery) -> None:
    if not pending_fu1_batch and not pending_fu2_batch:
        await callback.answer("Нечего отмечать.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "Будут отмечены отправленными все сегодняшние FU.\nПродолжить?",
        reply_markup=confirm_kb("followup_apply"),
    )
    await callback.answer()


@router.callback_query(F.data == "confirm:followup_apply")
async def cb_confirm_followup(callback: CallbackQuery) -> None:
    global pending_fu1_batch, pending_fu2_batch
    n1 = await db.mark_fu1_batch(pending_fu1_batch) if pending_fu1_batch else 0
    n2 = await db.mark_fu2_batch(pending_fu2_batch) if pending_fu2_batch else 0
    pending_fu1_batch, pending_fu2_batch = [], []
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Отмечено: FU1 — {n1}, FU2 — {n2}")
    await callback.answer()
    await followups(callback.message)


# ---------------------------------------------------------------------------
# /msg_leads — массовая отметка диапазона отправленными
# ---------------------------------------------------------------------------

@router.message(Command("msg_leads"))
async def cmd_msg_leads(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.msg_leads_range)
    await message.answer("Каких лидов отметить?\nНапример:\n1-30")


@router.message(Form.msg_leads_range)
async def msg_leads_range_input(message: Message, state: FSMContext) -> None:
    text_value = (message.text or "").strip()
    range_match = RANGE_RE.match(text_value)
    single_match = SINGLE_NUM_RE.match(text_value)

    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
    elif single_match:
        start = end = int(single_match.group(1))
    else:
        await message.answer("Не понял диапазон. Пример: 1-30")
        return

    leads = await db.get_leads_by_position_range(start, end)
    if not leads:
        await message.answer("Лидов с такими номерами не нашлось.", reply_markup=main_menu_kb())
        await state.clear()
        return

    usernames = [l.username for l in leads]
    await state.update_data(usernames=usernames, range_start=start, range_end=start + len(usernames) - 1)
    await prompt_choose_message(message, state, purpose="msg_leads")


@router.callback_query(F.data == "confirm:msg_leads_apply")
async def cb_confirm_msg_leads(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    usernames = data.get("usernames") or []
    message_label = data.get("message_label")
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    if not usernames or not message_label:
        await callback.answer("Данные устарели, начни заново.", show_alert=True)
        return
    count = await db.assign_message_and_send(usernames, message_label)
    await callback.message.answer(f"✅ Отмечено отправленными: {count}\nСообщение: «{message_label}»")
    await callback.answer()


# ---------------------------------------------------------------------------
# 📊 Статистика (/stats)
# ---------------------------------------------------------------------------

@router.message(F.text == "📊 Статистика")
@router.message(Command("stats"))
async def stats(message: Message, state: FSMContext) -> None:
    s = await db.get_stats()
    lines = ["📊 ОБЩАЯ СТАТИСТИКА", f"Всего лидов: {s['total']}"]
    for status in db.ALL_STATUSES:
        lines.append(f"{status}: {s['by_status'][status]}")
    await message.answer("\n".join(lines), reply_markup=main_menu_kb())

    messages = await db.get_all_messages()
    if not messages:
        return

    await state.set_state(Form.stats_message_choice)
    await state.update_data(messages=messages)
    lines2 = ["По какому сообщению показать статистику?", ""]
    for i, m in enumerate(messages, start=1):
        lines2.append(f"{i}. {m}")
    await message.answer("\n".join(lines2))


@router.message(Form.stats_message_choice)
async def stats_message_pick(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    messages = data.get("messages") or []
    await state.clear()
    text_value = (message.text or "").strip()
    if not text_value.isdigit() or not (1 <= int(text_value) <= len(messages)):
        await message.answer("Не понял номер. Открой «📊 Статистика» ещё раз, если нужно.", reply_markup=main_menu_kb())
        return
    chosen = messages[int(text_value) - 1]
    s = await db.get_message_stats(chosen)
    lines = [
        "📊 СТАТИСТИКА",
        "Сообщение:",
        f"«{chosen}»",
        f"Отправлено: {s['total']}",
    ]
    for status in (db.STATUS_REPLIED, db.STATUS_INTEREST, db.STATUS_CLIENT, db.STATUS_REJECT, db.STATUS_ARCHIVE):
        lines.append(f"{status}: {s['by_status'][status]}")
    await message.answer("\n".join(lines), reply_markup=main_menu_kb())


# ---------------------------------------------------------------------------
# 📋 Показать всю базу (постранично, с редактированием сообщения) — НЕ МЕНЯЕТСЯ
# ---------------------------------------------------------------------------

async def render_leads_page(page: int) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Собирает текст и клавиатуру для страницы `page` (1-based). Ничего не отправляет."""
    total = await db.count_leads()
    if total == 0:
        return "📋 <b>ВСЯ БАЗА</b>\nБаза пока пуста.\nДобавь первых лидов, чтобы они появились здесь.", None

    total_pages = (total + LEADS_PER_PAGE - 1) // LEADS_PER_PAGE
    page = max(1, min(page, total_pages))
    offset = (page - 1) * LEADS_PER_PAGE

    leads = await db.get_leads_page(offset, LEADS_PER_PAGE)
    blocks = [lead_list_block(offset + i, lead) for i, lead in enumerate(leads, start=1)]

    text_value = (
        f"📋 <b>ВСЯ БАЗА</b>\n"
        f"<b>Страница {page} из {total_pages}</b>\n"
        f"Показано: {offset + 1}–{offset + len(leads)} из {total}\n\n"
        + "\n\n".join(blocks)
    )
    return text_value, build_pagination_kb(page, total_pages)


@router.message(F.text == "📋 Показать всю базу")
async def all_leads_show(message: Message) -> None:
    text_value, kb = await render_leads_page(1)
    await message.answer(text_value, reply_markup=kb or main_menu_kb())


@router.callback_query(F.data.startswith("leads_page:"))
async def cb_leads_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":", 1)[1])
    text_value, kb = await render_leads_page(page)
    await callback.message.edit_text(text_value, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(F.text.regexp(r"^/(\d+)$"))
async def open_lead_by_number(message: Message) -> None:
    num = int(message.text[1:])
    lead = await db.get_lead_by_position(num)
    if not lead:
        await message.answer("⚠️ Такого номера нет. Сначала открой «📋 Показать всю базу».")
        return
    await message.answer(lead_card_text(lead), reply_markup=status_kb(lead))


# ---------------------------------------------------------------------------
# Свободный текст: быстрая смена статуса ИЛИ добавление лидов
# ---------------------------------------------------------------------------

QUICK_STATUS_RE = re.compile(
    r"^@?([A-Za-z0-9_]{5,32})\s+(" + "|".join(db.QUICK_STATUS_WORDS.keys()) + r")$",
    re.IGNORECASE,
)


@router.message(F.text)
async def free_text(message: Message) -> None:
    text_value = (message.text or "").strip()

    # 1) быстрая смена статуса: "@username слово"
    match = QUICK_STATUS_RE.match(text_value)
    if match:
        username, word = match.group(1), match.group(2).lower()
        status = db.QUICK_STATUS_WORDS[word]
        lead = await db.find_lead(username)
        if not lead:
            await message.answer(f"⚠️ @{username} не найден в базе.")
            return
        await db.set_status(username, status)
        lead = await db.find_lead(username)
        await message.answer(f"Статус @{username} изменён: {status_display(lead)}")
        return

    # 2) список username (в т.ч. ссылки) -> добавление лидов
    usernames = extract_usernames(text_value)
    if usernames:
        await process_add_leads(message, usernames)
        return

    await message.answer(
        "Не понял сообщение. Пришли список @username, или используй меню.",
        reply_markup=main_menu_kb(),
    )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def periodic_auto_archive() -> None:
    """Регулярная проверка 14/7-дневной архивации — переживает перезапуск (запускается и при старте)."""
    while True:
        try:
            archived = await db.auto_archive_check()
            if archived:
                logger.info(f"Автоархив: перенесено в 📦 Архив — {archived}")
        except Exception:
            logger.exception("Ошибка авто-архивации")
        await asyncio.sleep(3600)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID не задан в .env")

    await db.init_db()
    await db.auto_archive_check()  # проверка сразу при старте — не зависит только от фонового таймера
    asyncio.create_task(periodic_auto_archive())

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    # Все реальные хендлеры бота живут в `router` и доступны только ADMIN_ID.
    router.message.filter(F.from_user.id == ADMIN_ID)
    router.callback_query.filter(F.from_user.id == ADMIN_ID)
    dp.include_router(router)

    # Для всех остальных пользователей — простой отказ, без доступа к логике бота.
    fallback_router = Router()

    @fallback_router.message()
    async def deny_message(message: Message) -> None:
        await message.answer("⛔ Доступ запрещён.")

    @fallback_router.callback_query()
    async def deny_callback(callback: CallbackQuery) -> None:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)

    dp.include_router(fallback_router)

    logger.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
