"""
database.py — вся работа с базой данных (SQLite + SQLAlchemy 2.x async).

Таблицы: leads, improvement_notes.
Все функции — тонкие обёртки над простыми SQL-запросами через ORM.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import String, Integer, DateTime, delete, select, func, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------------------------------------------------------------------------
# Часовой пояс
# ---------------------------------------------------------------------------

MOSCOW_TZ = ZoneInfo("Europe/Moscow")


def moscow_now() -> dt.datetime:
    """Текущее время в Europe/Moscow, наивное (без tzinfo) — для хранения и сравнения."""
    return dt.datetime.now(MOSCOW_TZ).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Статусы
# ---------------------------------------------------------------------------

STATUS_NEW = "⚪ Не отправлено"
STATUS_SENT = "🟡 Отправлено"
STATUS_REPLIED = "💬 Ответил"
STATUS_INTEREST = "🔥 Интерес"
STATUS_CLIENT = "🤝 Клиент"
STATUS_REJECT = "❌ Отказ"
STATUS_BAN = "🚫 Бан"
STATUS_DELETED = "🗑 Удалено"
STATUS_ARCHIVE = "📦 Архив"

ALL_STATUSES = [
    STATUS_NEW, STATUS_SENT, STATUS_REPLIED, STATUS_INTEREST,
    STATUS_CLIENT, STATUS_REJECT, STATUS_BAN, STATUS_DELETED, STATUS_ARCHIVE,
]

# короткие коды статусов — используются в callback_data инлайн-кнопок
STATUS_BY_CODE: dict[str, str] = {
    "sent": STATUS_SENT,
    "replied": STATUS_REPLIED,
    "interest": STATUS_INTEREST,
    "client": STATUS_CLIENT,
    "reject": STATUS_REJECT,
    "ban": STATUS_BAN,
    "deleted": STATUS_DELETED,
    "archive": STATUS_ARCHIVE,
}

# слова для быстрой смены статуса текстом: "@username слово"
QUICK_STATUS_WORDS: dict[str, str] = {
    "ответил": STATUS_REPLIED,
    "интерес": STATUS_INTEREST,
    "клиент": STATUS_CLIENT,
    "отказ": STATUS_REJECT,
    "бан": STATUS_BAN,
    "удалено": STATUS_DELETED,
    "архив": STATUS_ARCHIVE,
}

# статусы, которые нельзя автоматически перевести в архив по правилу 14/7 дней
ARCHIVE_ELIGIBLE_STATUS = STATUS_SENT

# ---------------------------------------------------------------------------
# Модель и подключение
# ---------------------------------------------------------------------------

DB_PATH = "data/leads.db" 
engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, default=STATUS_NEW)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    message: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    message_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    fu1_due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    fu1_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    fu1_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    fu2_due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    fu2_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    fu2_message: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    response_stage: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ImprovementNote(Base):
    """Заметки по улучшению бота/процесса — не связаны с лидами."""
    __tablename__ = "improvement_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


async def _ensure_schema_migrations() -> None:
    """Лёгкая миграция для уже существующей базы. Ничего не удаляет и не выдумывает."""
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(leads)"))
        cols = {row[1] for row in result.fetchall()}

        new_columns = {
            "message": "VARCHAR",
            "message_sent_at": "DATETIME",
            "fu1_due_at": "DATETIME",
            "fu1_sent_at": "DATETIME",
            "fu1_message": "VARCHAR",
            "fu2_due_at": "DATETIME",
            "fu2_sent_at": "DATETIME",
            "fu2_message": "VARCHAR",
            "response_stage": "VARCHAR",
        }
        for name, coltype in new_columns.items():
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE leads ADD COLUMN {name} {coltype}"))

        if "sent_at" in cols:
            await conn.execute(text(
                "UPDATE leads SET message_sent_at = sent_at "
                "WHERE message_sent_at IS NULL AND sent_at IS NOT NULL"
            ))
        if "follow_up_1_at" in cols:
            await conn.execute(text(
                "UPDATE leads SET fu1_due_at = follow_up_1_at "
                "WHERE fu1_due_at IS NULL AND follow_up_1_at IS NOT NULL"
            ))
        if "follow_up_2_at" in cols:
            await conn.execute(text(
                "UPDATE leads SET fu2_due_at = follow_up_2_at "
                "WHERE fu2_due_at IS NULL AND follow_up_2_at IS NOT NULL"
            ))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await _ensure_schema_migrations()


# ---------------------------------------------------------------------------
# Добавление лидов
# ---------------------------------------------------------------------------

async def add_leads(usernames: list[str]) -> tuple[list[str], list[str]]:
    """Добавляет новых лидов. Возвращает (добавленные, уже_были_в_базе)."""
    added: list[str] = []
    duplicates: list[str] = []
    async with async_session() as session:
        for uname in usernames:
            existing = await session.scalar(select(Lead).where(Lead.username == uname))
            if existing:
                duplicates.append(uname)
                continue
            session.add(Lead(username=uname, status=STATUS_NEW))
            added.append(uname)
        await session.commit()
    return added, duplicates


# ---------------------------------------------------------------------------
# Полная очистка / удаление
# ---------------------------------------------------------------------------

async def clear_all_leads() -> int:
    async with async_session() as session:
        count = await session.scalar(select(func.count(Lead.id))) or 0
        await session.execute(delete(Lead))
        await session.commit()
        return count


async def delete_lead(username: str) -> bool:
    async with async_session() as session:
        lead = await session.scalar(select(Lead).where(Lead.username == username))
        if not lead:
            return False
        await session.delete(lead)
        await session.commit()
        return True


# ---------------------------------------------------------------------------
# Постраничный просмотр / выбор по позициям (пагинация не меняется)
# ---------------------------------------------------------------------------

async def count_leads() -> int:
    async with async_session() as session:
        return await session.scalar(select(func.count(Lead.id))) or 0


async def get_leads_page(offset: int, limit: int) -> list[Lead]:
    async with async_session() as session:
        result = await session.scalars(
            select(Lead).order_by(Lead.id.asc()).offset(offset).limit(limit)
        )
        return list(result.all())


async def get_lead_by_position(position: int) -> Optional[Lead]:
    """Лид по сквозному номеру (1-based, порядок id ASC — тот же, что и в пагинации)."""
    if position < 1:
        return None
    async with async_session() as session:
        return await session.scalar(
            select(Lead).order_by(Lead.id.asc()).offset(position - 1).limit(1)
        )


async def get_leads_by_positions(positions: list[int]) -> list[Lead]:
    """Лиды по произвольному списку сквозных номеров (используется /msg_leads, /change_status, /change_date)."""
    leads: list[Lead] = []
    for p in positions:
        lead = await get_lead_by_position(p)
        if lead:
            leads.append(lead)
    return leads


async def get_new_leads(limit: int) -> list[Lead]:
    async with async_session() as session:
        result = await session.scalars(
            select(Lead)
            .where(Lead.status == STATUS_NEW)
            .order_by(Lead.created_at.asc())
            .limit(limit)
        )
        return list(result.all())


# ---------------------------------------------------------------------------
# Отправка сообщения (первое касание — "Новые лиды" -> "Отметить отправленными")
# ---------------------------------------------------------------------------

async def assign_message_and_send(usernames: list[str], message: str) -> int:
    """Переводит лидов в 🟡 Отправлено, назначает сообщение, ставит дату и обнуляет FU-цикл."""
    if not usernames:
        return 0
    now = moscow_now()
    fu1_due = now + dt.timedelta(days=4)
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.status = STATUS_SENT
            lead.message = message
            lead.message_sent_at = now
            lead.fu1_due_at = fu1_due
            lead.fu1_sent_at = None
            lead.fu2_due_at = None
            lead.fu2_sent_at = None
            lead.response_stage = None
        await session.commit()
        return len(leads)


async def assign_message_only(usernames: list[str], message: str) -> int:
    """/msg_leads: только присваивает текст сообщения. Статус и дата отправки НЕ меняются."""
    if not usernames:
        return 0
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.message = message
        await session.commit()
        return len(leads)


# ---------------------------------------------------------------------------
# Массовое изменение статуса (/change_status)
# ---------------------------------------------------------------------------

async def set_status_bulk(usernames: list[str], status: str) -> int:
    """Меняет статус группе лидов. Если новый статус — 🟡 Отправлено, дата отправки = сейчас."""
    if not usernames:
        return 0
    now = moscow_now()
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.status = status
            if status == STATUS_SENT:
                lead.message_sent_at = now
            if status == STATUS_REPLIED:
                if lead.fu2_sent_at:
                    lead.response_stage = "fu2"
                elif lead.fu1_sent_at:
                    lead.response_stage = "fu1"
                else:
                    lead.response_stage = "initial"
        await session.commit()
        return len(leads)


# ---------------------------------------------------------------------------
# Массовое изменение даты первой отправки (/change_date) — статус НЕ меняется
# ---------------------------------------------------------------------------

async def change_message_date_bulk(usernames: list[str], new_date: dt.datetime) -> int:
    """Меняет message_sent_at и пересчитывает fu1_due_at / fu2_due_at. Статус не трогает."""
    if not usernames:
        return 0
    fu1_due = new_date + dt.timedelta(days=4)
    fu2_due = fu1_due + dt.timedelta(days=7)
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.message_sent_at = new_date
            lead.fu1_due_at = fu1_due
            lead.fu2_due_at = fu2_due
        await session.commit()
        return len(leads)


# ---------------------------------------------------------------------------
# Библиотека сообщений (отдельной таблицы нет — берём из уже использованных)
# ---------------------------------------------------------------------------

async def get_recent_messages(limit: int = 4) -> list[str]:
    async with async_session() as session:
        result = await session.scalars(
            select(Lead.message)
            .where(Lead.message.is_not(None))
            .order_by(Lead.message_sent_at.desc())
            .limit(200)
        )
        seen: list[str] = []
        for msg in result.all():
            if msg and msg not in seen:
                seen.append(msg)
            if len(seen) >= limit:
                break
        return seen


async def get_all_messages() -> list[str]:
    return await get_recent_messages(limit=10**6)


async def get_recent_fu_messages(stage: str, limit: int = 4) -> list[str]:
    """Последние уникальные варианты FU1- или FU2-сообщения (отдельная библиотека от первого сообщения)."""
    column = Lead.fu1_message if stage == "fu1" else Lead.fu2_message
    async with async_session() as session:
        result = await session.scalars(
            select(column).where(column.is_not(None)).order_by(Lead.id.desc()).limit(200)
        )
        seen: list[str] = []
        for msg in result.all():
            if msg and msg not in seen:
                seen.append(msg)
            if len(seen) >= limit:
                break
        return seen


async def assign_fu_message(usernames: list[str], stage: str, message: str) -> int:
    """Присваивает текст FU1- или FU2-сообщения группе лидов. Статус, даты и FU-отметки НЕ меняются."""
    if not usernames:
        return 0
    field = "fu1_message" if stage == "fu1" else "fu2_message"
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            setattr(lead, field, message)
        await session.commit()
        return len(leads)


# ---------------------------------------------------------------------------
# Поиск / изменение одного лида
# ---------------------------------------------------------------------------

async def find_lead(username: str) -> Optional[Lead]:
    async with async_session() as session:
        return await session.scalar(select(Lead).where(Lead.username == username))


async def set_status(username: str, status: str) -> bool:
    async with async_session() as session:
        lead = await session.scalar(select(Lead).where(Lead.username == username))
        if not lead:
            return False
        lead.status = status
        if status == STATUS_REPLIED:
            if lead.fu2_sent_at:
                lead.response_stage = "fu2"
            elif lead.fu1_sent_at:
                lead.response_stage = "fu1"
            else:
                lead.response_stage = "initial"
        await session.commit()
        return True


async def set_note(username: str, note: str) -> bool:
    async with async_session() as session:
        lead = await session.scalar(select(Lead).where(Lead.username == username))
        if not lead:
            return False
        lead.note = note
        await session.commit()
        return True


# ---------------------------------------------------------------------------
# Follow-up: фактическая отправка FU1 / FU2
# ---------------------------------------------------------------------------

async def mark_fu1_batch(usernames: list[str]) -> int:
    if not usernames:
        return 0
    now = moscow_now()
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.fu1_sent_at = now
            lead.fu2_due_at = now + dt.timedelta(days=7)
        await session.commit()
        return len(leads)


async def mark_fu2_batch(usernames: list[str]) -> int:
    if not usernames:
        return 0
    now = moscow_now()
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.fu2_sent_at = now
        await session.commit()
        return len(leads)


async def get_followups_due() -> tuple[list[Lead], list[Lead]]:
    today = moscow_now().date()
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.status == STATUS_SENT))
        leads = list(result.all())
    fu1_due = [
        l for l in leads
        if l.fu1_sent_at is None and l.fu1_due_at and l.fu1_due_at.date() <= today
    ]
    fu2_due = [
        l for l in leads
        if l.fu1_sent_at is not None and l.fu2_sent_at is None
        and l.fu2_due_at and l.fu2_due_at.date() <= today
    ]
    return fu1_due, fu2_due


# ---------------------------------------------------------------------------
# Автоматическая архивация (14 дней без движения / 7 дней после FU2)
# ---------------------------------------------------------------------------

async def auto_archive_check() -> int:
    now = moscow_now()
    archived = 0
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.status == ARCHIVE_ELIGIBLE_STATUS))
        leads = list(result.all())
        for lead in leads:
            if lead.fu2_sent_at:
                if now - lead.fu2_sent_at >= dt.timedelta(days=7):
                    lead.status = STATUS_ARCHIVE
                    archived += 1
                continue
            last_touch = lead.fu1_sent_at or lead.message_sent_at
            if last_touch and now - last_touch >= dt.timedelta(days=14):
                lead.status = STATUS_ARCHIVE
                archived += 1
        if archived:
            await session.commit()
    return archived


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

async def get_stats() -> dict:
    async with async_session() as session:
        total = await session.scalar(select(func.count(Lead.id))) or 0
        by_status = {}
        for status in ALL_STATUSES:
            count = await session.scalar(
                select(func.count(Lead.id)).where(Lead.status == status)
            )
            by_status[status] = count or 0
    return {"total": total, "by_status": by_status}


async def get_message_stats(message: str) -> dict:
    async with async_session() as session:
        total = await session.scalar(
            select(func.count(Lead.id)).where(Lead.message == message)
        ) or 0
        by_status = {}
        for status in (STATUS_REPLIED, STATUS_INTEREST, STATUS_CLIENT, STATUS_REJECT, STATUS_ARCHIVE):
            count = await session.scalar(
                select(func.count(Lead.id)).where(Lead.message == message, Lead.status == status)
            )
            by_status[status] = count or 0
    return {"total": total, "by_status": by_status}


# ---------------------------------------------------------------------------
# Заметки по улучшению (/note, /get_notes, /del_notes) — не про лидов
# ---------------------------------------------------------------------------

async def add_improvement_note(text_value: str) -> None:
    async with async_session() as session:
        session.add(ImprovementNote(text=text_value))
        await session.commit()


async def get_improvement_notes() -> list[ImprovementNote]:
    async with async_session() as session:
        result = await session.scalars(
            select(ImprovementNote).order_by(ImprovementNote.created_at.asc())
        )
        return list(result.all())


async def clear_improvement_notes() -> int:
    async with async_session() as session:
        count = await session.scalar(select(func.count(ImprovementNote.id))) or 0
        await session.execute(delete(ImprovementNote))
        await session.commit()
        return count



#sosite