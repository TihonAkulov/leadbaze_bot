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
USERNAME_RE = re.compile(r"@?([A-Za-z0-9_]{5,32})")

# "последняя выданная пачка" — храним в памяти процесса, как договорились (V1)
last_batch: list[str] = []

# номер (при показе "вся база") -> username, для команд вида /5
all_leads_index: list[str] = []


# ---------------------------------------------------------------------------
# FSM состояния
# ---------------------------------------------------------------------------

class Form(StatesGroup):
    new_leads_count = State()   # ждём число "сколько лидов показать"
    searching = State()         # ждём username для поиска
    adding_note = State()       # ждём текст заметки


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

MAIN_MENU_BUTTONS = [
    "📥 Добавить лидов",
    "📋 Новые лиды",
    "🔎 Найти лида",
    "📊 Статистика",
    "📅 Follow-up",
]


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


def status_kb(username: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🟡 Отправлено", callback_data=f"st:{username}:sent")],
        [InlineKeyboardButton(text="💬 Ответил", callback_data=f"st:{username}:replied")],
        [InlineKeyboardButton(text="🔥 Интерес", callback_data=f"st:{username}:interest")],
        [InlineKeyboardButton(text="🤝 Клиент", callback_data=f"st:{username}:client")],
        [InlineKeyboardButton(text="❌ Отказ", callback_data=f"st:{username}:reject")],
        [InlineKeyboardButton(text="🚫 Бан", callback_data=f"st:{username}:ban")],
        [InlineKeyboardButton(text="🗑 Удалено", callback_data=f"st:{username}:deleted")],
        [InlineKeyboardButton(text="📦 Архив", callback_data=f"st:{username}:archive")],
        [InlineKeyboardButton(text="📝 Заметка", callback_data=f"note:{username}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да", callback_data=f"confirm:{action}"),
                InlineKeyboardButton(text="Отмена", callback_data="cancel_confirm"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def extract_usernames(text: str) -> list[str]:
    """Достаёт из текста все username (без @, без дублей, порядок сохранён)."""
    found = USERNAME_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for uname in found:
        low = uname.lower()
        if low not in seen:
            seen.add(low)
            result.append(uname)
    return result


def fmt_date(value) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def lead_card_text(lead: db.Lead) -> str:
    return (
        f"👤 @{lead.username}\n"
        f"Статус: {lead.status}\n"
        f"Добавлен: {fmt_date(lead.created_at)}\n"
        f"Отправлен: {fmt_date(lead.sent_at)}\n"
        f"Заметка:\n{lead.note or '—'}"
    )


# ---------------------------------------------------------------------------
# /start, /help
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Я бот для ведения базы лидов.\n\n"
        "Пришли список username (можно просто @user1 @user2 или каждый с новой строки) — "
        "я добавлю новых в базу.\n\n"
        "Или используй меню ниже.",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "📥 Добавить лидов — пришли список @username\n"
        "📋 Новые лиды — выдать пачку ещё не отправленных\n"
        "🔎 Найти лида — посмотреть карточку и сменить статус\n"
        "📊 Статистика — сводка по базе\n"
        "📅 Follow-up — кому сегодня писать повторно\n"
        "📋 Показать всю базу — вся база целиком, пронумерованная (/5 откроет лида №5)\n\n"
        "Быстрая смена статуса: @username интерес / ответил / клиент / отказ / бан / удалено / архив",
        reply_markup=main_menu_kb(),
    )


# ---------------------------------------------------------------------------
# 📋 Новые лиды
# ---------------------------------------------------------------------------

@router.message(F.text == "📋 Новые лиды")
async def new_leads_start(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.new_leads_count)
    await message.answer("Сколько лидов показать?", reply_markup=ReplyKeyboardRemove())


@router.message(Form.new_leads_count)
async def new_leads_count(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer("Пришли число, например: 20")
        return
    limit = int(text)
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
async def cb_mark_sent(callback: CallbackQuery) -> None:
    global last_batch
    if not last_batch:
        await callback.answer("Пачка уже пуста.", show_alert=True)
        return
    count = await db.mark_sent(last_batch)
    last_batch = []
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Отмечено отправленными: {count}")
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
    await message.answer("Пришли список username, например:\n@user1\n@user2 @user3")


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
    await message.answer("Пришли username лида (можно с @ или без).", reply_markup=ReplyKeyboardRemove())


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
    await message.answer(lead_card_text(lead), reply_markup=status_kb(lead.username))
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

    # массовое/безвозвратное действие — просим подтверждение
    if code == "deleted":
        await callback.message.answer(
            f"⚠️ Пометить @{username} как удалённый?",
            reply_markup=confirm_kb(f"del:{username}"),
        )
        await callback.answer()
        return

    ok = await db.set_status(username, status)
    if ok:
        await callback.answer(f"Статус изменён: {status}")
        lead = await db.find_lead(username)
        if lead:
            await callback.message.edit_text(lead_card_text(lead), reply_markup=status_kb(username))
    else:
        await callback.answer("Лид не найден", show_alert=True)


@router.callback_query(F.data.startswith("confirm:del:"))
async def cb_confirm_delete(callback: CallbackQuery) -> None:
    username = callback.data.split(":", 2)[2]
    await db.set_status(username, db.STATUS_DELETED)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"🗑 @{username} помечен как удалённый.")
    await callback.answer()


@router.callback_query(F.data == "cancel_confirm")
async def cb_cancel_confirm(callback: CallbackQuery) -> None:
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


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
async def followups(message: Message) -> None:
    fu1, fu2 = await db.get_followups_today()
    lines = ["📅 Сегодня"]
    lines.append("FU1:")
    lines.extend(f"@{u}" for u in fu1) if fu1 else lines.append("—")
    lines.append("FU2:")
    lines.extend(f"@{u}" for u in fu2) if fu2 else lines.append("—")
    lines.append(f"Всего: {len(fu1) + len(fu2)}")
    await message.answer("\n".join(lines), reply_markup=main_menu_kb())


# ---------------------------------------------------------------------------
# 📊 Статистика
# ---------------------------------------------------------------------------

@router.message(F.text == "📊 Статистика")
async def stats(message: Message) -> None:
    s = await db.get_stats()
    lines = ["📊 БАЗА ЛИДОВ", f"Всего: {s['total']}"]
    for status in db.ALL_STATUSES:
        lines.append(f"{status}: {s['by_status'][status]}")
    lines.append("")
    lines.append("Новых лидов за:")
    lines.append(f"Сегодня: {s['new_today']}")
    lines.append(f"7 дней: {s['new_7d']}")
    lines.append(f"30 дней: {s['new_30d']}")
    lines.append(f"Всё время: {s['total']}")
    await message.answer("\n".join(lines), reply_markup=main_menu_kb())


# ---------------------------------------------------------------------------
# 📋 Показать всю базу
# ---------------------------------------------------------------------------

@router.message(F.text == "📋 Показать всю базу")
async def show_all_leads(message: Message) -> None:
    leads = await db.get_all_leads()
    if not leads:
        await message.answer("База пуста.", reply_markup=main_menu_kb())
        return

    global all_leads_index
    all_leads_index = [lead.username for lead in leads]

    lines = []
    for i, lead in enumerate(leads, start=1):
        lines.append(
            f"{i}. @{lead.username} — {lead.status}\n"
            f"  Добавлен: {fmt_date(lead.created_at)} | Отправлен: {fmt_date(lead.sent_at)}\n"
            f"  FU1: {fmt_date(lead.follow_up_1_at)} | FU2: {fmt_date(lead.follow_up_2_at)}\n"
            f"  Заметка: {lead.note or '—'}"
        )

    chunk = f"📋 Вся база — {len(leads)}\n(введи /номер, чтобы открыть лида, например /5)\n\n"
    for block in lines:
        if len(chunk) + len(block) > 3800:
            await message.answer(chunk)
            chunk = ""
        chunk += block + "\n\n"
    if chunk:
        await message.answer(chunk, reply_markup=main_menu_kb())


@router.message(F.text.regexp(r"^/(\d+)$"))
async def open_lead_by_number(message: Message) -> None:
    num = int(message.text[1:])
    if num < 1 or num > len(all_leads_index):
        await message.answer("⚠️ Такого номера нет. Сначала открой «📋 Показать всю базу».")
        return
    username = all_leads_index[num - 1]
    lead = await db.find_lead(username)
    if not lead:
        await message.answer(f"⚠️ @{username} не найден в базе.")
        return
    await message.answer(lead_card_text(lead), reply_markup=status_kb(lead.username))


# ---------------------------------------------------------------------------
# Свободный текст: быстрая смена статуса ИЛИ добавление лидов
# ---------------------------------------------------------------------------

QUICK_STATUS_RE = re.compile(
    r"^@?([A-Za-z0-9_]{5,32})\s+(" + "|".join(db.QUICK_STATUS_WORDS.keys()) + r")$",
    re.IGNORECASE,
)


@router.message(F.text)
async def free_text(message: Message) -> None:
    text = (message.text or "").strip()

    # 1) быстрая смена статуса: "@username слово"
    match = QUICK_STATUS_RE.match(text)
    if match:
        username, word = match.group(1), match.group(2).lower()
        status = db.QUICK_STATUS_WORDS[word]
        lead = await db.find_lead(username)
        if not lead:
            await message.answer(f"⚠️ @{username} не найден в базе.")
            return
        if status == db.STATUS_DELETED:
            await message.answer(
                f"⚠️ Пометить @{username} как удалённый?",
                reply_markup=confirm_kb(f"del:{username}"),
            )
            return
        await db.set_status(username, status)
        await message.answer(f"Статус @{username} изменён: {status}")
        return

    # 2) список username -> добавление лидов
    usernames = extract_usernames(text)
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

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID не задан в .env")

    await db.init_db()

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
