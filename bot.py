"""
bot.py — Telegram-бот для ведения базы лидов, V2.

Логика поделена по модулям:
  database.py   — модели и данные (leads, messages, lead_events, notes, backups)
  migrations.py — перенос со старой схемы (вызывается из database.init_db())
  messages.py   — рендер раздела «Сообщения»
  stats.py      — сбор и форматирование статистики
  backup.py     — резервные копии (.db + .xlsx), восстановление
  keyboards.py  — все клавиатуры
  utils.py      — время (МСК), username, парсинг ввода, форматирование

Запуск: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, ReplyKeyboardRemove,
)
from dotenv import load_dotenv

import backup as backup_mod
import database as db
import evening
import keyboards as kb
import stats as stats_mod
from messages import render_category_list, render_message_card
from utils import (
    ADMIN_NAME, extract_usernames, fmt_date, fmt_datetime, moscow_now,
    parse_date_input, parse_position_selector, seconds_until, short,
)

# ---------------------------------------------------------------------------
# Настройка
# ---------------------------------------------------------------------------

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lead_bot")

router = Router()

last_batch: list[str] = []
pending_fu1_batch: list[str] = []
pending_fu2_batch: list[str] = []

LEADS_PER_PAGE = 10


class Form(StatesGroup):
    new_leads_count = State()
    searching = State()
    adding_note = State()
    choosing_message = State()
    msg_leads_range = State()
    msg_leads_confirm = State()
    change_status_selector = State()
    change_date_selector = State()
    change_date_value = State()
    fu_leads_selector = State()
    adding_improvement_note = State()
    change_msg_text_tag = State()
    change_msg_text_value = State()
    change_msg_tag_pick = State()
    change_msg_tag_value = State()
    close_client_amount = State()
    awaiting_restore_file = State()


# ---------------------------------------------------------------------------
# Общие вспомогательные тексты
# ---------------------------------------------------------------------------

def status_display(lead: db.Lead) -> str:
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


def fu_field_text(due_at, sent_at) -> str:
    if sent_at:
        return fmt_datetime(sent_at)
    if due_at:
        return fmt_date(due_at)
    return "—"


async def _msg_title(message_id) -> str:
    if not message_id:
        return "—"
    msg = await db.get_message(message_id)
    return msg.title if msg else "—"


async def lead_card_text(lead: db.Lead) -> str:
    first_title = await _msg_title(lead.message_id)
    fu1_title = await _msg_title(lead.fu1_message_id)
    fu2_title = await _msg_title(lead.fu2_message_id)
    return (
        f"👤 @{lead.username}\n"
        f"Статус: {status_display(lead)}\n"
        f"Сообщение: {first_title}\n"
        f"FU: {fu1_title} | {fu2_title}\n"
        f"Отправлен: {fmt_datetime(lead.message_sent_at)}\n"
        f"FU1: {fu_field_text(lead.fu1_due_at, lead.fu1_sent_at)}\n"
        f"FU2: {fu_field_text(lead.fu2_due_at, lead.fu2_sent_at)}\n"
        f"Заметка:\n{lead.note or '—'}"
    )


async def lead_list_block(i: int, lead: db.Lead) -> str:
    first_title = await _msg_title(lead.message_id)
    fu1_title = await _msg_title(lead.fu1_message_id)
    fu2_title = await _msg_title(lead.fu2_message_id)
    return (
        f"{i}. @{lead.username} — {status_display(lead)}\n"
        f"  Сообщение: {first_title}\n"
        f"  FU: {fu1_title} | {fu2_title}\n"
        f"  Отправлен: {fmt_datetime(lead.message_sent_at)}\n"
        f"  FU1: {fu_field_text(lead.fu1_due_at, lead.fu1_sent_at)} | "
        f"FU2: {fu_field_text(lead.fu2_due_at, lead.fu2_sent_at)}\n"
        f"  Заметка: {lead.note or '—'}"
    )


async def send_lead_card(target: Message, username: str) -> None:
    lead = await db.find_lead(username)
    if lead:
        await target.answer(await lead_card_text(lead), reply_markup=kb.status_kb(lead))


async def resolve_leads_selector(message: Message, state: FSMContext, text_value: str) -> list[db.Lead] | None:
    positions = parse_position_selector(text_value)
    if positions is None:
        await message.answer("Не понял формат. Примеры: 5   5-20   5, 7, 10")
        return None
    leads = await db.get_leads_by_positions(positions)
    if not leads:
        await message.answer("Лидов с такими номерами не нашлось.", reply_markup=kb.main_menu_kb())
        await state.clear()
        return None
    return leads


# ---------------------------------------------------------------------------
# Общий флоу выбора сообщения
# ---------------------------------------------------------------------------

async def prompt_choose_message(target: Message, state: FSMContext, purpose: str, category: str, title: str | None = None) -> None:
    recents = await db.get_recent_messages(category, 4)
    await state.set_state(Form.choosing_message)
    await state.update_data(purpose=purpose, category=category, recent_ids=[m.id for m in recents], recent_texts=[m.content for m in recents])
    lines = [title or "Какое сообщение отправлено?", ""]
    for i, m in enumerate(recents, start=1):
        lines.append(f"{i}. {short(m.content, 60)}")
    lines.append("")
    lines.append("Или напишите текст сообщения вручную.")
    await target.answer("\n".join(lines), reply_markup=kb.choose_message_kb(recents))


async def apply_chosen_message(target: Message, state: FSMContext, content: str) -> None:
    data = await state.get_data()
    purpose = data.get("purpose")
    category = data.get("category")
    message_obj = await db.get_or_create_message(category, content)

    if purpose == "batch_mark_sent":
        await state.clear()
        global last_batch
        if not last_batch:
            await target.answer("Пачка уже пуста.", reply_markup=kb.main_menu_kb())
            return
        count = await db.assign_message_and_send(last_batch, message_obj.id)
        last_batch = []
        await target.answer(f"✅ Отмечено отправленными: {count}\nСообщение: {message_obj.title}", reply_markup=kb.main_menu_kb())
        return

    if purpose == "msg_leads":
        usernames = data.get("usernames") or []
        selector_text = data.get("selector_text", "")
        await state.update_data(usernames=usernames, message_id=message_obj.id, message_title=message_obj.title)
        await state.set_state(Form.msg_leads_confirm)
        text_lines = [
            "⚠️ Проверьте данные",
            f"Лиды: {selector_text}",
            f"Количество: {len(usernames)}",
            "Сообщение:",
            message_obj.title,
            "",
            "Статус и дата отправки НЕ меняются — только сообщение.",
            "Продолжить?",
        ]
        await target.answer("\n".join(text_lines), reply_markup=kb.confirm_kb("msg_leads_apply"))
        return

    if purpose == "card_first":
        username = data.get("username")
        await state.clear()
        if not username:
            await target.answer("Данные устарели, начни заново.", reply_markup=kb.main_menu_kb())
            return
        await db.assign_message_and_send([username], message_obj.id)
        await send_lead_card(target, username)
        return

    if purpose == "card_fu":
        username = data.get("username")
        fu_stage = "fu1" if category == "fu1" else "fu2"
        await state.clear()
        if not username:
            await target.answer("Данные устарели, начни заново.", reply_markup=kb.main_menu_kb())
            return
        await db.assign_fu_message([username], fu_stage, message_obj.id)
        if fu_stage == "fu1":
            await db.mark_fu1_batch([username])
        else:
            await db.mark_fu2_batch([username])
        await send_lead_card(target, username)
        return

    if purpose == "followup_fu1":
        fu2_usernames = data.get("fu2_usernames") or []
        await state.update_data(fu1_message_id=message_obj.id)
        if fu2_usernames:
            await prompt_choose_message(target, state, purpose="followup_fu2", category="fu2",
                                         title=f"Какой текст FU2 отправлен? (лидов: {len(fu2_usernames)})")
            return
        await finalize_followup(target, state)
        return

    if purpose == "followup_fu2":
        await state.update_data(fu2_message_id=message_obj.id)
        await finalize_followup(target, state)
        return

    if purpose == "fu_leads":
        usernames = data.get("usernames") or []
        fu_stage = data.get("fu_stage")
        await state.clear()
        if not usernames or fu_stage not in ("fu1", "fu2"):
            await target.answer("Данные устарели, начни заново.", reply_markup=kb.main_menu_kb())
            return
        count = await db.assign_fu_message(usernames, fu_stage, message_obj.id)
        label = "FU1" if fu_stage == "fu1" else "FU2"
        await target.answer(f"✅ Сообщение {label} присвоено {count} лидам: {message_obj.title}", reply_markup=kb.main_menu_kb())
        return

    if purpose == "msguse":
        usernames = data.get("usernames") or []
        await state.clear()
        if not usernames:
            await target.answer("Данные устарели, начни заново.", reply_markup=kb.main_menu_kb())
            return
        count = await db.assign_message_only(usernames, message_obj.id)
        await target.answer(f"✅ Сообщение присвоено {count} лидам: {message_obj.title}", reply_markup=kb.main_menu_kb())
        return

    # неизвестный/утерянный purpose — не молчим, сбрасываем состояние и сообщаем явно
    await state.clear()
    await target.answer(
        "Не понял, что делать с этим текстом (похоже, предыдущий сценарий прервался). "
        "Открой нужный раздел заново.",
        reply_markup=kb.main_menu_kb(),
    )


async def finalize_followup(target: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fu1_usernames = data.get("fu1_usernames") or []
    fu2_usernames = data.get("fu2_usernames") or []
    fu1_message_id = data.get("fu1_message_id")
    fu2_message_id = data.get("fu2_message_id")
    await state.clear()

    n1 = n2 = 0
    if fu1_usernames and fu1_message_id:
        await db.assign_fu_message(fu1_usernames, "fu1", fu1_message_id)
        n1 = await db.mark_fu1_batch(fu1_usernames)
    if fu2_usernames and fu2_message_id:
        await db.assign_fu_message(fu2_usernames, "fu2", fu2_message_id)
        n2 = await db.mark_fu2_batch(fu2_usernames)

    await target.answer(f"✅ Отмечено: FU1 — {n1}, FU2 — {n2}", reply_markup=kb.main_menu_kb())


@router.callback_query(F.data == "cancel_choose")
async def cb_cancel_choose(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("pickmsg:"), Form.choosing_message)
async def cb_pick_message(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    texts = data.get("recent_texts") or []
    if idx < 0 or idx >= len(texts):
        await callback.answer("Такого варианта уже нет.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await apply_chosen_message(callback.message, state, texts[idx])
    await callback.answer()


@router.message(Form.choosing_message)
async def msg_choose_manual(message: Message, state: FSMContext) -> None:
    text_value = (message.text or "").strip()
    if not text_value:
        await message.answer("Пришли текст сообщения.")
        return
    await apply_chosen_message(message, state, text_value)


# ---------------------------------------------------------------------------
# Навигация: /start, /help, главное меню
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Привет! Бот для ведения базы лидов.\n\nВыбери раздел в меню ниже.",
        reply_markup=kb.main_menu_kb(),
    )


@router.message(Command("help"))
@router.callback_query(F.data == "other:help")
async def cmd_help(event) -> None:
    text = (
        "👤 <b>Лиды</b> — добавить, новые, поиск, вся база, 🔥 тёплые (ответил/интерес/клиент)\n"
        "📅 <b>Follow-up</b> — кому сегодня писать (и утром в 09:00 МСК — автоматически)\n"
        "📈 <b>Статистика</b> → 💬 По сообщениям — тексты (первое/FU1/FU2) и статистика по каждому живут здесь\n"
        "💾 <b>Бэкап</b> — .db + .xlsx, история, восстановление (можно прислать свой .db файлом)\n"
        "⚙️ <b>Другое</b> — заметки, помощь, очистка базы\n\n"
        "💰 Клиент закрыт — отдельная кнопка на карточке лида, спросит сумму сделки\n"
        "📈 Статистика → 📅 По дням — Сегодня/Вчера/Позавчера, окно 07:00–23:00 МСК\n"
        "/change_msg_text — изменить текст сообщения по тегу, не создавая новое\n"
        "/change_msg_tag — переименовать тег у старых сообщений (N1 → #fst_1 и т.п.)\n\n"
        "Быстрая смена статуса текстом: @username интерес / ответил / клиент / отказ / бан / удалено / архив\n"
        "/msg_leads, /fu_leads, /change_status, /change_date, /del@username — как раньше, текстовыми командами"
    )
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb.main_menu_kb())
    else:
        await event.message.answer(text, reply_markup=kb.main_menu_kb())
        await event.answer()


@router.callback_query(F.data == "nav:main")
async def cb_nav_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


# ---------------------------------------------------------------------------
# Раздел «Лиды»
# ---------------------------------------------------------------------------

@router.message(F.text == "👤 Лиды")
@router.callback_query(F.data == "leads:menu")
async def leads_menu(event) -> None:
    summary = await stats_mod.render_leads_summary()
    text = f"👤 <b>ЛИДЫ</b>\n{summary}"
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb.leads_menu_kb())
    else:
        await event.message.edit_text(text, reply_markup=kb.leads_menu_kb())
        await event.answer()


@router.callback_query(F.data == "leads:add")
async def cb_leads_add(callback: CallbackQuery) -> None:
    await callback.message.answer("Пришли список username, например:\n@user1\nhttps://t.me/user2\nuser3")
    await callback.answer()


async def process_add_leads(message: Message, usernames: list[str]) -> None:
    added, duplicates = await db.add_leads(usernames)
    lines = ["✅ <b>Лиды добавлены</b>", "", f"Добавлено: {len(added)}", f"Уже были: {len(duplicates)}"]
    await message.answer("\n".join(lines), reply_markup=kb.after_add_leads_kb())


@router.callback_query(F.data == "leads:new")
async def cb_leads_new(callback: CallbackQuery) -> None:
    total_new = len(await db.get_new_leads(10**9))
    await callback.message.answer(f"Доступно новых (⚪): {total_new}\nСколько показать?", reply_markup=kb.new_leads_count_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("newcount:"))
async def cb_new_count(callback: CallbackQuery, state: FSMContext) -> None:
    value = callback.data.split(":", 1)[1]
    await callback.message.edit_reply_markup(reply_markup=None)
    if value == "custom":
        await state.set_state(Form.new_leads_count)
        await callback.message.answer("Сколько лидов показать? Пришли число.")
        await callback.answer()
        return
    await callback.answer()
    await _show_new_leads(callback.message, int(value))


@router.message(Form.new_leads_count)
async def new_leads_count_input(message: Message, state: FSMContext) -> None:
    text_value = (message.text or "").strip()
    if not text_value.isdigit():
        await message.answer("Пришли число, например: 20")
        return
    await state.clear()
    await _show_new_leads(message, int(text_value))


async def _show_new_leads(message: Message, limit: int) -> None:
    leads = await db.get_new_leads(limit)
    if not leads:
        await message.answer("Новых лидов со статусом ⚪ нет.", reply_markup=kb.main_menu_kb())
        return
    global last_batch
    last_batch = [l.username for l in leads]
    lines = "\n".join(f"{i}. @{u}" for i, u in enumerate(last_batch, start=1))
    await message.answer(f"📋 Новые лиды — {len(last_batch)}\n{lines}", reply_markup=kb.batch_action_kb())


@router.callback_query(F.data == "mark_sent")
async def cb_mark_sent(callback: CallbackQuery, state: FSMContext) -> None:
    if not last_batch:
        await callback.answer("Пачка уже пуста.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await prompt_choose_message(callback.message, state, purpose="batch_mark_sent", category="initial",
                                 title="Какое сообщение отправлено?")
    await callback.answer()


@router.callback_query(F.data == "cancel_batch")
async def cb_cancel_batch(callback: CallbackQuery) -> None:
    global last_batch
    last_batch = []
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


@router.callback_query(F.data == "leads:find")
async def cb_leads_find(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.searching)
    await callback.message.answer("Пришли username лида (можно с @, без, или ссылку t.me/...).")
    await callback.answer()


@router.message(Form.searching)
async def search_result(message: Message, state: FSMContext) -> None:
    usernames = extract_usernames(message.text or "")
    await state.clear()
    if not usernames:
        await message.answer("Не нашёл username в сообщении.", reply_markup=kb.main_menu_kb())
        return
    for username in usernames:
        lead = await db.find_lead(username)
        if not lead:
            await message.answer(f"❓ @{username} — не найден в базе.")
        else:
            await message.answer(await lead_card_text(lead), reply_markup=kb.status_kb(lead))


# ---------------------------------------------------------------------------
# Статус / FU с карточки лида
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("st:"))
async def cb_set_status(callback: CallbackQuery, state: FSMContext) -> None:
    _, username, code = callback.data.split(":", 2)
    status = db.STATUS_BY_CODE.get(code)
    if not status:
        await callback.answer("Неизвестный статус", show_alert=True)
        return

    if code == "sent":
        if not await db.find_lead(username):
            await callback.answer("Лид не найден", show_alert=True)
            return
        await state.clear()
        await state.update_data(username=username)
        await prompt_choose_message(callback.message, state, purpose="card_first", category="initial",
                                     title=f"Какое сообщение отправлено @{username}?")
        await callback.answer()
        return

    ok = await db.set_status(username, status)
    if ok:
        await callback.answer(f"Статус изменён: {status}")
        lead = await db.find_lead(username)
        if lead:
            await callback.message.edit_text(await lead_card_text(lead), reply_markup=kb.status_kb(lead))
    else:
        await callback.answer("Лид не найден", show_alert=True)


@router.callback_query(F.data.startswith("markfu1:"))
async def cb_mark_fu1(callback: CallbackQuery, state: FSMContext) -> None:
    username = callback.data.split(":", 1)[1]
    await state.clear()
    await state.update_data(username=username)
    await prompt_choose_message(callback.message, state, purpose="card_fu", category="fu1",
                                 title=f"Какой текст FU1 отправлен @{username}?")
    await callback.answer()


@router.callback_query(F.data.startswith("markfu2:"))
async def cb_mark_fu2(callback: CallbackQuery, state: FSMContext) -> None:
    username = callback.data.split(":", 1)[1]
    await state.clear()
    await state.update_data(username=username)
    await prompt_choose_message(callback.message, state, purpose="card_fu", category="fu2",
                                 title=f"Какой текст FU2 отправлен @{username}?")
    await callback.answer()


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
        await message.answer("Что-то пошло не так, попробуй заново.", reply_markup=kb.main_menu_kb())
        return
    await db.set_note(username, message.text or "")
    await message.answer(f"📝 Заметка для @{username} сохранена.", reply_markup=kb.main_menu_kb())


# ---------------------------------------------------------------------------
# /del@username, /clear_db
# ---------------------------------------------------------------------------

@router.message(F.text.regexp(r"^/del@([A-Za-z0-9_]{5,32})$"))
async def cmd_del_lead(message: Message) -> None:
    username = message.text[len("/del@"):]
    if not await db.find_lead(username):
        await message.answer(f"⚠️ @{username} не найден в базе.")
        return
    await message.answer(
        f"⚠️ Удалить лида @{username}?\nЛид будет удалён НАВСЕГДА из базы.",
        reply_markup=kb.delete_confirm_kb(username),
    )


@router.callback_query(F.data.startswith("confirm:delreal:"))
async def cb_confirm_delete_real(callback: CallbackQuery) -> None:
    username = callback.data.split(":", 2)[2]
    ok = await db.delete_lead(username)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"🗑 @{username} удалён навсегда." if ok else f"⚠️ @{username} не найден в базе.")
    await callback.answer()


@router.message(Command("clear_db"))
@router.callback_query(F.data == "other:clear_db")
async def cmd_clear_db(event) -> None:
    text = "⚠️ Это удалит ВСЮ базу лидов без возможности восстановления (кроме бэкапов). Продолжить?"
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb.confirm_kb("clear_db"))
    else:
        await event.message.answer(text, reply_markup=kb.confirm_kb("clear_db"))
        await event.answer()


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
# /msg_leads, /change_status, /change_date, /fu_leads — массовые операции текстом
# ---------------------------------------------------------------------------

@router.message(Command("msg_leads"))
async def cmd_msg_leads(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.msg_leads_range)
    await message.answer("Каких лидов отметить?\nПримеры: 5   5-20   5, 7, 10")


@router.message(Form.msg_leads_range)
async def msg_leads_range_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    msguse_message_id = data.get("msguse_message_id")
    text_value = (message.text or "").strip()
    leads = await resolve_leads_selector(message, state, text_value)
    if leads is None:
        return
    usernames = [l.username for l in leads]

    if msguse_message_id:
        # пришли сюда с кнопки «✅ Использовать» на карточке сообщения — сообщение уже выбрано
        await state.clear()
        count = await db.assign_message_only(usernames, msguse_message_id)
        msg = await db.get_message(msguse_message_id)
        await message.answer(
            f"✅ Сообщение присвоено {count} лидам: {msg.title if msg else ''}",
            reply_markup=kb.main_menu_kb(),
        )
        return

    # обычный /msg_leads — дальше спрашиваем, какое сообщение отправлено
    await state.update_data(usernames=usernames, selector_text=text_value)
    await prompt_choose_message(message, state, purpose="msg_leads", category="initial",
                                 title="Какое сообщение отправлено?")


@router.callback_query(F.data == "confirm:msg_leads_apply")
async def cb_confirm_msg_leads(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    usernames = data.get("usernames") or []
    message_id = data.get("message_id")
    title = data.get("message_title", "")
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    if not usernames or not message_id:
        await callback.answer("Данные устарели, начни заново.", show_alert=True)
        return
    count = await db.assign_message_only(usernames, message_id)
    await callback.message.answer(f"✅ Сообщение присвоено {count} лидам: {title}")
    await callback.answer()


@router.message(Command("change_status"))
async def cmd_change_status(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.change_status_selector)
    await message.answer("Каких лидов изменить?\nПримеры: 5   5-20   5, 7, 10")


@router.message(Form.change_status_selector)
async def change_status_selector_input(message: Message, state: FSMContext) -> None:
    text_value = (message.text or "").strip()
    leads = await resolve_leads_selector(message, state, text_value)
    if leads is None:
        return
    usernames = [l.username for l in leads]
    await state.clear()
    await state.update_data(usernames=usernames)
    await message.answer(f"Выбрано лидов: {len(usernames)}\nНовый статус:", reply_markup=kb.bulk_status_kb())


@router.callback_query(F.data.startswith("bulkstatus:"))
async def cb_bulk_status(callback: CallbackQuery, state: FSMContext) -> None:
    code = callback.data.split(":", 1)[1]
    status = db.STATUS_BY_CODE.get(code)
    if not status:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    data = await state.get_data()
    usernames = data.get("usernames") or []
    await state.clear()
    if not usernames:
        await callback.answer("Данные устарели, начни заново.", show_alert=True)
        return
    count = await db.set_status_bulk(usernames, status)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"✅ Статус изменён у {count} лидов: {status}")
    await callback.answer()


@router.message(Command("change_date"))
async def cmd_change_date(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.change_date_selector)
    await message.answer("Каких лидов изменить?\nПримеры: 5   5-20   5, 7, 10")


@router.message(Form.change_date_selector)
async def change_date_selector_input(message: Message, state: FSMContext) -> None:
    text_value = (message.text or "").strip()
    leads = await resolve_leads_selector(message, state, text_value)
    if leads is None:
        return
    usernames = [l.username for l in leads]
    await state.update_data(usernames=usernames)
    await state.set_state(Form.change_date_value)
    await message.answer(
        f"Выбрано лидов: {len(usernames)}\nНовая дата первой отправки?\n"
        "Формат: 28.09, 28.09.2026 или 28.09 14:00\nFU1 и FU2 пересчитаются, статус не меняется."
    )


@router.message(Form.change_date_value)
async def change_date_value_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    usernames = data.get("usernames") or []
    await state.clear()
    if not usernames:
        await message.answer("Данные устарели, начни заново.", reply_markup=kb.main_menu_kb())
        return
    new_date = parse_date_input((message.text or "").strip())
    if new_date is None:
        await message.answer("Не понял дату. Формат: 28.09, 28.09.2026 или 28.09 14:00.")
        return
    count = await db.change_message_date_bulk(usernames, new_date)
    import datetime as _dt
    fu1_due = new_date + _dt.timedelta(days=4)
    fu2_due = fu1_due + _dt.timedelta(days=7)
    await message.answer(
        f"✅ Дата отправки обновлена у {count} лидов: {fmt_datetime(new_date)}\n"
        f"FU1: {fmt_date(fu1_due)} | FU2: {fmt_date(fu2_due)}",
        reply_markup=kb.main_menu_kb(),
    )


@router.message(Command("fu_leads"))
async def cmd_fu_leads(message: Message, state: FSMContext) -> None:
    await state.clear()
    ikb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="FU1", callback_data="fufor:fu1"),
        InlineKeyboardButton(text="FU2", callback_data="fufor:fu2"),
    ]])
    await message.answer("Для какого follow-up назначить сообщение?", reply_markup=ikb)


@router.callback_query(F.data.startswith("fufor:"))
async def cb_fu_leads_stage(callback: CallbackQuery, state: FSMContext) -> None:
    fu_stage = callback.data.split(":", 1)[1]
    await state.set_state(Form.fu_leads_selector)
    await state.update_data(fu_stage=fu_stage)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Каких лидов отметить?\nПримеры: 5   5-20   5, 7, 10")
    await callback.answer()


@router.message(Form.fu_leads_selector)
async def fu_leads_selector_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fu_stage = data.get("fu_stage")
    leads = await resolve_leads_selector(message, state, (message.text or "").strip())
    if leads is None:
        return
    usernames = [l.username for l in leads]
    await prompt_choose_message(message, state, purpose="fu_leads", category=fu_stage,
                                 title=f"Какой текст {fu_stage.upper()} отправлен?")
    await state.update_data(usernames=usernames, fu_stage=fu_stage)


# ---------------------------------------------------------------------------
# Сообщения — больше не отдельный раздел главного меню (V2.1): живёт только
# внутри «📈 Статистика → 💬 По сообщениям» (см. cb_stats_bymessage / cb_stats_msgcat ниже).
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("msgcard:"))
async def cb_message_card(callback: CallbackQuery) -> None:
    message_id = int(callback.data.split(":", 1)[1])
    msg = await db.get_message(message_id)
    if not msg:
        await callback.answer("Сообщение не найдено", show_alert=True)
        return
    text = await render_message_card(msg)
    await callback.message.edit_text(text, reply_markup=kb.message_card_kb(msg, msg.category))
    await callback.answer()


@router.callback_query(F.data.startswith("msgfulltext:"))
async def cb_message_fulltext(callback: CallbackQuery) -> None:
    message_id = int(callback.data.split(":", 1)[1])
    msg = await db.get_message(message_id)
    if not msg:
        await callback.answer("Сообщение не найдено", show_alert=True)
        return
    await callback.message.answer(f"🏷 {msg.title}\n\n{msg.content}")
    await callback.answer()


@router.callback_query(F.data.startswith("msgarchive:"))
async def cb_message_archive(callback: CallbackQuery) -> None:
    message_id = int(callback.data.split(":", 1)[1])
    await db.archive_message(message_id)
    msg = await db.get_message(message_id)
    await callback.message.edit_text(await render_message_card(msg), reply_markup=kb.message_card_kb(msg, msg.category))
    await callback.answer("Сообщение архивировано")


@router.callback_query(F.data.startswith("msguse:"))
async def cb_message_use(callback: CallbackQuery, state: FSMContext) -> None:
    message_id = int(callback.data.split(":", 1)[1])
    await state.set_state(Form.msg_leads_range)
    await state.update_data(msguse_message_id=message_id)
    await callback.message.answer("Каким лидам присвоить это сообщение?\nПримеры: 5   5-20   5, 7, 10")
    await callback.answer()


# --- /change_msg_text: правка текста БЕЗ создания нового сообщения/тега/статистики ---

@router.callback_query(F.data.startswith("msgedit:"))
async def cb_message_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    message_id = int(callback.data.split(":", 1)[1])
    msg = await db.get_message(message_id)
    if not msg:
        await callback.answer("Сообщение не найдено", show_alert=True)
        return
    await state.set_state(Form.change_msg_text_value)
    await state.update_data(edit_message_id=message_id)
    await callback.message.answer(
        f"🏷 {msg.title}\nТекущий текст:\n«{msg.content}»\n\nОтправьте новый текст сообщения."
    )
    await callback.answer()


@router.message(Command("change_msg_text"))
async def cmd_change_msg_text(message: Message, state: FSMContext) -> None:
    await state.set_state(Form.change_msg_text_tag)
    await message.answer("Введите тег сообщения:\nПример: fst_1")


@router.message(Form.change_msg_text_tag)
async def change_msg_text_tag_input(message: Message, state: FSMContext) -> None:
    tag = (message.text or "").strip()
    msg = await db.get_message_by_tag(tag)
    if not msg:
        await message.answer("Не нашёл сообщение с таким тегом. Проверь и пришли ещё раз (например: fst_1).")
        return
    await state.set_state(Form.change_msg_text_value)
    await state.update_data(edit_message_id=msg.id)
    await message.answer(
        f"🏷 {msg.title}\nТекущий текст:\n«{msg.content}»\n\nОтправьте новый текст сообщения."
    )


@router.message(Form.change_msg_text_value)
async def change_msg_text_value_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    message_id = data.get("edit_message_id")
    await state.clear()
    new_text = (message.text or "").strip()
    if not message_id or not new_text:
        await message.answer("Не получилось — попробуй /change_msg_text заново.", reply_markup=kb.main_menu_kb())
        return
    ok = await db.change_message_content(message_id, new_text)
    msg = await db.get_message(message_id)
    if ok and msg:
        await message.answer(f"✅ Текст {msg.title} обновлён. Тег и статистика сохранены.", reply_markup=kb.main_menu_kb())
    else:
        await message.answer("Сообщение не найдено.", reply_markup=kb.main_menu_kb())


# --- /change_msg_tag: перевод старых тегов (N1, N2...) в новый формат (#fst_1 и т.д.) ---

@router.message(Command("change_msg_tag"))
async def cmd_change_msg_tag(message: Message, state: FSMContext) -> None:
    try:
        await state.clear()
        ikb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📨 Первые", callback_data="tagcat:initial"),
            InlineKeyboardButton(text="↩️ FU1", callback_data="tagcat:fu1"),
            InlineKeyboardButton(text="↪️ FU2", callback_data="tagcat:fu2"),
        ]])
        await message.answer("Для какой категории менять теги?", reply_markup=ikb)
    except Exception as e:
        logger.exception("Ошибка в /change_msg_tag")
        await message.answer(f"⚠️ Ошибка: {e}")


@router.callback_query(F.data.startswith("tagcat:"))
async def cb_change_msg_tag_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.split(":", 1)[1]
    messages_list = await db.get_messages(category, active_only=False)
    if not messages_list:
        await callback.answer("В этой категории пока нет сообщений.", show_alert=True)
        return
    lines = [f"Сообщения категории «{db.CATEGORY_LABELS.get(category, category)}»:", ""]
    for i, m in enumerate(messages_list, start=1):
        lines.append(f"{i}. {m.title}")
    lines.append("")
    lines.append("Введите номер сообщения, тег которого нужно поменять.")
    await state.set_state(Form.change_msg_tag_pick)
    await state.update_data(tag_message_ids=[m.id for m in messages_list])
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.message(Form.change_msg_tag_pick)
async def change_msg_tag_pick_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ids = data.get("tag_message_ids") or []
    text_value = (message.text or "").strip()
    if not text_value.isdigit() or not (1 <= int(text_value) <= len(ids)):
        await message.answer("Не понял номер. Пришли число из списка выше.")
        return
    message_id = ids[int(text_value) - 1]
    await state.set_state(Form.change_msg_tag_value)
    await state.update_data(tag_message_id=message_id)
    await message.answer("Пришли новый тег (например: fst_1).")


@router.message(Form.change_msg_tag_value)
async def change_msg_tag_value_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    message_id = data.get("tag_message_id")
    await state.clear()
    raw = (message.text or "").strip().lstrip("#")
    if not raw:
        await message.answer("Пустой тег не подходит.", reply_markup=kb.main_menu_kb())
        return
    new_tag = f"#{raw}"
    existing = await db.get_message_by_tag(raw)
    if existing and existing.id != message_id:
        await message.answer(
            f"Тег {new_tag} уже занят сообщением {existing.title}. Пришли другой тег.",
            reply_markup=kb.main_menu_kb(),
        )
        return
    ok = await db.rename_message_tag(message_id, new_tag)
    if ok:
        await message.answer(f"✅ Тег изменён на {new_tag}.", reply_markup=kb.main_menu_kb())
    else:
        await message.answer("Сообщение не найдено.", reply_markup=kb.main_menu_kb())


# --- 💰 Клиент закрыт: статус + сумма сделки ---

@router.callback_query(F.data.startswith("closeclient:"))
async def cb_close_client_start(callback: CallbackQuery, state: FSMContext) -> None:
    username = callback.data.split(":", 1)[1]
    await state.set_state(Form.close_client_amount)
    await state.update_data(username=username)
    await callback.message.answer(f"💰 Клиент закрыт\nУкажите сумму заработка с этого клиента (@{username}):")
    await callback.answer()


@router.message(Form.close_client_amount)
async def close_client_amount_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    username = data.get("username")
    await state.clear()
    text_value = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(text_value)
    except ValueError:
        await message.answer("Не понял сумму. Пришли число, например: 7450")
        return
    if not username or not await db.close_client(username, amount):
        await message.answer("⚠️ Лид не найден.", reply_markup=kb.main_menu_kb())
        return
    await message.answer(f"✅ Клиент закрыт\n💰 Сумма: {amount:,.0f} ₽".replace(",", " "))
    await send_lead_card(message, username)


# ---------------------------------------------------------------------------
# 📅 Follow-up
# ---------------------------------------------------------------------------

@router.message(F.text == "📅 Follow-up")
@router.message(Command("followup"))
async def followups_summary(event) -> None:
    fu1_leads, fu2_leads = await db.get_followups_due()
    global pending_fu1_batch, pending_fu2_batch
    pending_fu1_batch = [l.username for l in fu1_leads]
    pending_fu2_batch = [l.username for l in fu2_leads]
    text = f"📅 <b>Follow-up</b>\nНа сегодня:\n🔁 FU1 — {len(fu1_leads)}\n🔁 FU2 — {len(fu2_leads)}"
    target = event if isinstance(event, Message) else event.message
    await target.answer(text, reply_markup=kb.followup_summary_kb())


@router.callback_query(F.data == "followup:open")
async def cb_followup_open(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        f"🔁 FU1 — {len(pending_fu1_batch)} лидов\n🔁 FU2 — {len(pending_fu2_batch)} лидов",
        reply_markup=kb.followup_detail_kb(bool(pending_fu1_batch), bool(pending_fu2_batch)),
    )
    await callback.answer()


@router.callback_query(F.data == "followup:list")
async def cb_followup_list(callback: CallbackQuery) -> None:
    lines = ["📄 Список на сегодня:"]
    if pending_fu1_batch:
        lines.append("FU1: " + ", ".join(f"@{u}" for u in pending_fu1_batch))
    if pending_fu2_batch:
        lines.append("FU2: " + ", ".join(f"@{u}" for u in pending_fu2_batch))
    if not pending_fu1_batch and not pending_fu2_batch:
        lines.append("Пусто.")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data.startswith("followup:send:"))
async def cb_followup_send(callback: CallbackQuery, state: FSMContext) -> None:
    stage = callback.data.split(":", 2)[2]
    usernames = pending_fu1_batch if stage == "fu1" else pending_fu2_batch
    if not usernames:
        await callback.answer("Список пуст.", show_alert=True)
        return
    await state.clear()
    if stage == "fu1":
        await state.update_data(fu1_usernames=usernames, fu2_usernames=[])
        purpose = "followup_fu1"
    else:
        await state.update_data(fu1_usernames=[], fu2_usernames=usernames)
        purpose = "followup_fu2"
    await callback.message.edit_reply_markup(reply_markup=None)
    await prompt_choose_message(callback.message, state, purpose=purpose, category=stage,
                                 title=f"Какой текст {stage.upper()} отправлен? (лидов: {len(usernames)})")
    await callback.answer()


# ---------------------------------------------------------------------------
# 📄 Вся база (постранично, редактирование сообщения — как в V1)
# ---------------------------------------------------------------------------

async def render_leads_page(page: int) -> tuple[str, kb.InlineKeyboardMarkup | None]:
    total = await db.count_leads()
    if total == 0:
        return "📄 <b>ВСЯ БАЗА</b>\nБаза пока пуста.", None
    total_pages = (total + LEADS_PER_PAGE - 1) // LEADS_PER_PAGE
    page = max(1, min(page, total_pages))
    offset = (page - 1) * LEADS_PER_PAGE
    leads = await db.get_leads_page(offset, LEADS_PER_PAGE)
    blocks = [await lead_list_block(offset + i, lead) for i, lead in enumerate(leads, start=1)]
    text = (
        f"📄 <b>ВСЯ БАЗА</b>\n<b>Страница {page} из {total_pages}</b>\n"
        f"Показано: {offset + 1}–{offset + len(leads)} из {total}\n\n" + "\n\n".join(blocks)
    )
    return text, kb.build_pagination_kb(page, total_pages)


@router.callback_query(F.data == "leads:all")
async def cb_leads_all(callback: CallbackQuery) -> None:
    text, markup = await render_leads_page(1)
    await callback.message.edit_text(text, reply_markup=markup or kb.leads_menu_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("leads_page:"))
async def cb_leads_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":", 1)[1])
    text, markup = await render_leads_page(page)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


async def render_warm_leads_page(page: int) -> tuple[str, kb.InlineKeyboardMarkup | None]:
    """Те же ⚡«живые» лиды (Ответил/Интерес/Клиент/Клиент закрыт), без Отказа. Пагинация та же,
    номера — сквозные, как в «Вся база» (а не 1..N по отфильтрованному списку)."""
    total = await db.count_warm_leads()
    if total == 0:
        return "🔥 <b>ТЁПЛЫЕ</b>\nПока никто не ответил/не заинтересовался.", None
    total_pages = (total + LEADS_PER_PAGE - 1) // LEADS_PER_PAGE
    page = max(1, min(page, total_pages))
    offset = (page - 1) * LEADS_PER_PAGE
    leads = await db.get_warm_leads_page(offset, LEADS_PER_PAGE)
    position_map = await db.get_position_map()
    blocks = [await lead_list_block(position_map.get(lead.id, 0), lead) for lead in leads]
    text = (
        f"🔥 <b>ТЁПЛЫЕ</b>\n<b>Страница {page} из {total_pages}</b>\n"
        f"Показано: {offset + 1}–{offset + len(leads)} из {total}\n\n" + "\n\n".join(blocks)
    )
    return text, kb.build_pagination_kb(page, total_pages, prefix="warm_page")


@router.callback_query(F.data == "leads:warm")
async def cb_leads_warm(callback: CallbackQuery) -> None:
    text, markup = await render_warm_leads_page(1)
    await callback.message.edit_text(text, reply_markup=markup or kb.leads_menu_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("warm_page:"))
async def cb_warm_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":", 1)[1])
    text, markup = await render_warm_leads_page(page)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(F.text.regexp(r"^/(\d+)$"))
async def open_lead_by_number(message: Message) -> None:
    num = int(message.text[1:])
    lead = await db.get_lead_by_position(num)
    if not lead:
        await message.answer("⚠️ Такого номера нет.")
        return
    await message.answer(await lead_card_text(lead), reply_markup=kb.status_kb(lead))


# ---------------------------------------------------------------------------
# 📊 Статистика
# ---------------------------------------------------------------------------

@router.message(F.text == "📈 Статистика")
@router.message(Command("stats"))
async def stats_menu(event) -> None:
    text = await stats_mod.render_overall_stats("all")
    target = event if isinstance(event, Message) else event.message
    if isinstance(event, Message):
        await target.answer(text, reply_markup=kb.stats_period_kb())
    else:
        await target.edit_text(text, reply_markup=kb.stats_period_kb())
        await event.answer()


@router.callback_query(F.data == "stats:menu")
async def cb_stats_menu(callback: CallbackQuery) -> None:
    text = await stats_mod.render_overall_stats("all")
    await callback.message.edit_text(text, reply_markup=kb.stats_period_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("statsperiod:"))
async def cb_stats_period(callback: CallbackQuery) -> None:
    code = callback.data.split(":", 1)[1]
    text = await stats_mod.render_overall_stats(code)
    await callback.message.edit_text(text, reply_markup=kb.stats_period_kb())
    await callback.answer()


@router.callback_query(F.data == "stats:daily")
async def cb_stats_daily(callback: CallbackQuery) -> None:
    text = await stats_mod.render_daily_breakdown()
    await callback.message.edit_text(text, reply_markup=kb.stats_period_kb())
    await callback.answer()


@router.callback_query(F.data == "stats:bymessage")
async def cb_stats_bymessage(callback: CallbackQuery) -> None:
    await callback.message.edit_text("💬 По какой категории сообщений?", reply_markup=kb.stats_by_message_categories_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("statsmsgcat:"))
async def cb_stats_msgcat(callback: CallbackQuery) -> None:
    category = callback.data.split(":", 1)[1]
    _, messages = await render_category_list(category)
    if not messages:
        await callback.answer("В этой категории пока нет сообщений.", show_alert=True)
        return
    await callback.message.edit_text(
        f"Выбери сообщение категории «{db.CATEGORY_LABELS.get(category)}»:",
        reply_markup=kb.messages_list_kb(messages, category),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 💾 Бэкап
# ---------------------------------------------------------------------------

@router.message(F.text == "💾 Бэкап")
@router.callback_query(F.data == "backup:menu")
async def backup_menu(event) -> None:
    history = await backup_mod.list_backups(1)
    last = history[0] if history else None
    text = "💾 <b>Бэкап</b>\n"
    text += f"Последний: {fmt_datetime(last.created_at)} ({last.kind})" if last else "Бэкапов пока не было."
    target_answer = event.answer if isinstance(event, Message) else event.message.edit_text
    await target_answer(text, reply_markup=kb.backup_menu_kb())
    if not isinstance(event, Message):
        await event.answer()


@router.callback_query(F.data == "backup:create")
async def cb_backup_create(callback: CallbackQuery) -> None:
    await callback.answer("Создаю бэкап...")
    db_path, xlsx_path, count = await backup_mod.create_backup(kind="manual")
    await callback.message.answer(f"✅ Бэкап создан\nЛидов: {count}\nДата: {fmt_datetime(moscow_now())}")
    await callback.message.answer_document(FSInputFile(db_path))
    await callback.message.answer_document(FSInputFile(xlsx_path))


@router.callback_query(F.data == "backup:history")
async def cb_backup_history(callback: CallbackQuery) -> None:
    history = await backup_mod.list_backups(10)
    if not history:
        await callback.answer("Бэкапов пока нет.", show_alert=True)
        return
    await callback.message.edit_text("🕘 История бэкапов:", reply_markup=kb.backup_history_kb(history))
    await callback.answer()


async def _restore_warning_text(new_count: int, current_count: int) -> str:
    if new_count < current_count:
        return (
            f"⚠️ Внимание\nВ выбранном бэкапе {new_count} лид(ов),\n"
            f"а в текущей базе {current_count}.\nПри восстановлении часть более новых данных "
            f"может быть потеряна.\nТочно восстановить эту базу?"
        )
    return f"В бэкапе {new_count} лид(ов) (сейчас: {current_count}).\nВосстановить базу из этого файла?"


@router.callback_query(F.data.startswith("backup:restore_ask:"))
async def cb_backup_restore_ask(callback: CallbackQuery) -> None:
    backup_id = int(callback.data.split(":", 2)[2])
    history = await backup_mod.list_backups(50)
    match = next((b for b in history if b.id == backup_id), None)
    if not match:
        await callback.answer("Бэкап не найден.", show_alert=True)
        return
    info = await backup_mod.validate_db_file(match.file_path)
    if not info.get("ok"):
        await callback.answer(f"Файл повреждён или недоступен: {info.get('error')}", show_alert=True)
        return
    current_count = await db.count_leads()
    text = await _restore_warning_text(info["leads_count"], current_count)
    await callback.message.answer(text, reply_markup=kb.confirm_kb(f"restore:{backup_id}", yes_text="♻️ Да, восстановить"))
    await callback.answer()


@router.callback_query(F.data.startswith("confirm:restore:"))
async def cb_confirm_restore(callback: CallbackQuery) -> None:
    backup_id = int(callback.data.split(":", 2)[2])
    history = await backup_mod.list_backups(50)
    match = next((b for b in history if b.id == backup_id), None)
    await callback.message.edit_reply_markup(reply_markup=None)
    if not match:
        await callback.message.answer("Бэкап не найден.")
        await callback.answer()
        return
    ok, info = await backup_mod.restore_backup(match.file_path)
    if ok:
        new_count = await db.count_leads()
        await callback.message.answer(f"✅ База успешно восстановлена.\nЛидов: {new_count}")
    else:
        await callback.message.answer(f"⚠️ {info}")
    await callback.answer()


# --- восстановление из загруженного файла ---

@router.callback_query(F.data == "backup:restore")
async def cb_backup_restore_upload_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.awaiting_restore_file)
    await callback.message.answer("Пришли файл базы (.db) для восстановления.\nExcel для восстановления не подходит.")
    await callback.answer()


@router.message(Form.awaiting_restore_file, F.document)
async def restore_file_received(message: Message, state: FSMContext, bot: Bot) -> None:
    doc = message.document
    await state.clear()
    if not doc.file_name or not doc.file_name.lower().endswith(".db"):
        await message.answer("Нужен файл с расширением .db (не Excel).", reply_markup=kb.main_menu_kb())
        return

    os.makedirs("/tmp/leadbot_restore", exist_ok=True)
    tmp_path = f"/tmp/leadbot_restore/{doc.file_unique_id}.db"
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, tmp_path)

    info = await backup_mod.validate_db_file(tmp_path)
    if not info.get("ok"):
        await message.answer(f"⚠️ Файл не похож на корректную базу ЛидБазы: {info.get('error')}", reply_markup=kb.main_menu_kb())
        return

    current_count = await db.count_leads()
    await state.update_data(restore_path=tmp_path)
    text = await _restore_warning_text(info["leads_count"], current_count)
    await message.answer(text, reply_markup=kb.confirm_kb("restore_upload", yes_text="♻️ Да, восстановить"))


@router.message(Form.awaiting_restore_file)
async def restore_file_wrong_type(message: Message) -> None:
    await message.answer("Пришли именно файл (.db), не текст.")


@router.callback_query(F.data == "confirm:restore_upload")
async def cb_confirm_restore_upload(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    path = data.get("restore_path")
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    if not path or not os.path.exists(path):
        await callback.message.answer("Файл уже недоступен, пришли его заново.")
        await callback.answer()
        return
    ok, info = await backup_mod.restore_backup(path)
    if ok:
        new_count = await db.count_leads()
        await callback.message.answer(f"✅ База успешно восстановлена.\nЛидов: {new_count}")
    else:
        await callback.message.answer(f"⚠️ {info}")
    await callback.answer()
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# ⚙️ Другое + заметки по улучшению
# ---------------------------------------------------------------------------

@router.message(F.text == "⚙️ Другое")
@router.callback_query(F.data == "other:menu")
async def other_menu(event) -> None:
    text = "⚙️ Другое"
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb.other_menu_kb())
    else:
        await event.message.edit_text(text, reply_markup=kb.other_menu_kb())
        await event.answer()


@router.callback_query(F.data == "notes:menu")
async def cb_notes_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text("📝 Заметки по улучшению", reply_markup=kb.notes_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "notes_add")
async def cb_notes_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Form.adding_improvement_note)
    await callback.message.answer("Пришли текст заметки:")
    await callback.answer()


@router.message(Form.adding_improvement_note)
async def improvement_note_save(message: Message, state: FSMContext) -> None:
    await state.clear()
    text_value = (message.text or "").strip()
    if not text_value:
        await message.answer("Пустая заметка не сохранена.", reply_markup=kb.main_menu_kb())
        return
    await db.add_improvement_note(text_value)
    await message.answer("📝 Заметка сохранена.", reply_markup=kb.main_menu_kb())


@router.callback_query(F.data == "notes_view")
async def cb_notes_view(callback: CallbackQuery) -> None:
    notes = await db.get_improvement_notes()
    if not notes:
        await callback.message.answer("Заметок пока нет.")
    else:
        lines = ["📝 ЗАМЕТКИ ПО УЛУЧШЕНИЮ"] + [f"{i}. {n.text}" for i, n in enumerate(notes, start=1)]
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@router.callback_query(F.data == "notes_delete_all")
async def cb_notes_delete_all(callback: CallbackQuery) -> None:
    await callback.message.answer("⚠️ Удалить ВСЕ заметки? Необратимо.", reply_markup=kb.confirm_kb("del_notes"))
    await callback.answer()


@router.callback_query(F.data == "confirm:del_notes")
async def cb_confirm_del_notes(callback: CallbackQuery) -> None:
    count = await db.clear_improvement_notes()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"🗑 Удалено заметок: {count}")
    await callback.answer()


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject) -> None:
    text_value = (command.args or "").strip()
    if not text_value:
        await message.answer("Использование: /note текст заметки")
        return
    await db.add_improvement_note(text_value)
    await message.answer("📝 Заметка сохранена.")


@router.message(Command("get_notes"))
async def cmd_get_notes(message: Message) -> None:
    notes = await db.get_improvement_notes()
    if not notes:
        await message.answer("Заметок пока нет.")
        return
    lines = ["📝 ЗАМЕТКИ ПО УЛУЧШЕНИЮ"] + [f"{i}. {n.text}" for i, n in enumerate(notes, start=1)]
    await message.answer("\n".join(lines))


@router.message(Command("del_notes"))
async def cmd_del_notes(message: Message) -> None:
    await message.answer("⚠️ Удалить ВСЕ заметки? Необратимо.", reply_markup=kb.confirm_kb("del_notes"))


# ---------------------------------------------------------------------------
# Свободный текст: быстрая смена статуса ИЛИ добавление лидов (в самом конце!)
# ---------------------------------------------------------------------------

import re as _re

QUICK_STATUS_RE = _re.compile(
    r"^@?([A-Za-z0-9_]{5,32})\s+(" + "|".join(db.QUICK_STATUS_WORDS.keys()) + r")$",
    _re.IGNORECASE,
)


@router.message(F.text)
async def free_text(message: Message) -> None:
    text_value = (message.text or "").strip()

    match = QUICK_STATUS_RE.match(text_value)
    if match:
        username, word = match.group(1), match.group(2).lower()
        status = db.QUICK_STATUS_WORDS[word]
        if not await db.find_lead(username):
            await message.answer(f"⚠️ @{username} не найден в базе.")
            return
        await db.set_status(username, status)
        lead = await db.find_lead(username)
        await message.answer(f"Статус @{username} изменён: {status_display(lead)}")
        return

    usernames = extract_usernames(text_value)
    if usernames:
        await process_add_leads(message, usernames)
        return

    await message.answer("Не понял сообщение. Используй меню ниже.", reply_markup=kb.main_menu_kb())


# ---------------------------------------------------------------------------
# Фоновые задачи: авто-архив, ежедневный бэкап 03:00, ежедневный Follow-up 09:00
# ---------------------------------------------------------------------------

async def periodic_auto_archive() -> None:
    while True:
        try:
            archived = await db.auto_archive_check()
            if archived:
                logger.info(f"Автоархив: {archived}")
        except Exception:
            logger.exception("Ошибка авто-архивации")
        await asyncio.sleep(3600)


async def daily_backup_task() -> None:
    while True:
        await asyncio.sleep(seconds_until(3, 0))
        try:
            await backup_mod.create_backup(kind="auto")
            logger.info("Автоматический бэкап создан")
        except Exception:
            logger.exception("Ошибка автоматического бэкапа")


async def daily_followup_task(bot: Bot) -> None:
    while True:
        await asyncio.sleep(seconds_until(9, 0))
        try:
            fu1_leads, fu2_leads = await db.get_followups_due()
            global pending_fu1_batch, pending_fu2_batch
            pending_fu1_batch = [l.username for l in fu1_leads]
            pending_fu2_batch = [l.username for l in fu2_leads]
            if fu1_leads or fu2_leads:
                text = (
                    f"Доброе утро, {ADMIN_NAME}! ☀️\n\n"
                    f"📅 <b>Follow-up на сегодня</b>\n🔁 FU1 — {len(fu1_leads)}\n🔁 FU2 — {len(fu2_leads)}"
                )
                await bot.send_message(ADMIN_ID, text, reply_markup=kb.followup_summary_kb())
        except Exception:
            logger.exception("Ошибка ежедневного Follow-up")


async def daily_evening_report_task(bot: Bot) -> None:
    """Вся логика — в evening.py. Здесь только планировщик (тот же паттерн, что и у остальных
    фоновых задач выше)."""
    while True:
        await asyncio.sleep(seconds_until(23, 0))
        try:
            await evening.run_evening_report(bot, ADMIN_ID)
        except Exception:
            logger.exception("Ошибка вечернего отчёта")


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------

async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")
    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID не задан в .env")

    await db.init_db()
    await db.auto_archive_check()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    router.message.filter(F.from_user.id == ADMIN_ID)
    router.callback_query.filter(F.from_user.id == ADMIN_ID)
    dp.include_router(router)

    fallback_router = Router()

    @fallback_router.message()
    async def deny_message(message: Message) -> None:
        await message.answer("⛔ Доступ запрещён.")

    @fallback_router.callback_query()
    async def deny_callback(callback: CallbackQuery) -> None:
        await callback.answer("⛔ Доступ запрещён.", show_alert=True)

    dp.include_router(fallback_router)

    asyncio.create_task(periodic_auto_archive())
    asyncio.create_task(daily_backup_task())
    asyncio.create_task(daily_followup_task(bot))
    asyncio.create_task(daily_evening_report_task(bot))

    logger.info("Бот запущен (V2)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
