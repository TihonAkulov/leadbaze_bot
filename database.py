"""
database.py — вся работа с базой данных (SQLite + SQLAlchemy 2.x async).

Одна таблица: leads.
Все функции — тонкие обёртки над простыми SQL-запросами через ORM.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import String, Integer, DateTime, delete, select, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
    sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    follow_up_1_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    follow_up_2_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
# Выдача новых лидов и отметка "отправлено"
# ---------------------------------------------------------------------------

async def clear_all_leads() -> int:
    """Полностью удаляет всех лидов из базы. Возвращает, сколько было удалено."""
    async with async_session() as session:
        count = await session.scalar(select(func.count(Lead.id))) or 0
        await session.execute(delete(Lead))
        await session.commit()
        return count


async def get_all_leads() -> list[Lead]:
    async with async_session() as session:
        result = await session.scalars(select(Lead).order_by(Lead.created_at.asc()))
        return list(result.all())


async def get_new_leads(limit: int) -> list[Lead]:
    async with async_session() as session:
        result = await session.scalars(
            select(Lead)
            .where(Lead.status == STATUS_NEW)
            .order_by(Lead.created_at.asc())
            .limit(limit)
        )
        return list(result.all())


async def mark_sent(usernames: list[str]) -> int:
    """Переводит список username в статус 'Отправлено', считает follow-up даты."""
    if not usernames:
        return 0
    now = dt.datetime.utcnow()
    fu1 = now + dt.timedelta(days=4)
    fu2 = now + dt.timedelta(days=10)
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.status = STATUS_SENT
            lead.sent_at = now
            lead.follow_up_1_at = fu1
            lead.follow_up_2_at = fu2
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
# Follow-up
# ---------------------------------------------------------------------------

async def get_followups_today() -> tuple[list[str], list[str]]:
    """Возвращает (fu1_usernames, fu2_usernames) с датой follow-up сегодня или раньше."""
    today = dt.datetime.utcnow().date()
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.status == STATUS_SENT))
        leads = list(result.all())
    fu1 = [l.username for l in leads if l.follow_up_1_at and l.follow_up_1_at.date() <= today]
    fu2 = [l.username for l in leads if l.follow_up_2_at and l.follow_up_2_at.date() <= today]
    return fu1, fu2


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

        now = dt.datetime.utcnow()
        today_start = dt.datetime(now.year, now.month, now.day)
        week_start = now - dt.timedelta(days=7)
        month_start = now - dt.timedelta(days=30)

        today_count = await session.scalar(
            select(func.count(Lead.id)).where(Lead.created_at >= today_start)
        ) or 0
        week_count = await session.scalar(
            select(func.count(Lead.id)).where(Lead.created_at >= week_start)
        ) or 0
        month_count = await session.scalar(
            select(func.count(Lead.id)).where(Lead.created_at >= month_start)
        ) or 0

    return {
        "total": total,
        "by_status": by_status,
        "new_today": today_count,
        "new_7d": week_count,
        "new_30d": month_count,
    }
