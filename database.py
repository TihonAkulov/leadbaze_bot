"""
database.py — модели и низкоуровневые функции работы с БД (SQLite + SQLAlchemy 2.x async).

Таблицы: leads, messages, lead_events, improvement_notes, backups, users (задел на будущее).
UI-логика сюда не кладём — только данные.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean, String, Integer, DateTime, Float, ForeignKey, delete, select, func, text,
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from utils import moscow_now, short

# ---------------------------------------------------------------------------
# Статусы лида
# ---------------------------------------------------------------------------

STATUS_NEW = "⚪ Не отправлено"
STATUS_SENT = "🟡 Отправлено"
STATUS_REPLIED = "💬 Ответил"
STATUS_INTEREST = "🔥 Интерес"
STATUS_CLIENT = "🤝 Клиент"
STATUS_CLIENT_CLOSED = "💰 Клиент закрыт"
STATUS_REJECT = "❌ Отказ"
STATUS_BAN = "🚫 Бан"
STATUS_DELETED = "🗑 Удалено"
STATUS_ARCHIVE = "📦 Архив"

ALL_STATUSES = [
    STATUS_NEW, STATUS_SENT, STATUS_REPLIED, STATUS_INTEREST, STATUS_CLIENT,
    STATUS_CLIENT_CLOSED, STATUS_REJECT, STATUS_BAN, STATUS_DELETED, STATUS_ARCHIVE,
]

STATUS_BY_CODE: dict[str, str] = {
    "sent": STATUS_SENT, "replied": STATUS_REPLIED, "interest": STATUS_INTEREST,
    "client": STATUS_CLIENT, "client_closed": STATUS_CLIENT_CLOSED,
    "reject": STATUS_REJECT, "ban": STATUS_BAN,
    "deleted": STATUS_DELETED, "archive": STATUS_ARCHIVE,
}

# «Тёплые» — те, с кем есть живой прогресс (без отказников)
WARM_STATUSES = (STATUS_REPLIED, STATUS_INTEREST, STATUS_CLIENT, STATUS_CLIENT_CLOSED)

QUICK_STATUS_WORDS: dict[str, str] = {
    "ответил": STATUS_REPLIED, "интерес": STATUS_INTEREST, "клиент": STATUS_CLIENT,
    "отказ": STATUS_REJECT, "бан": STATUS_BAN, "удалено": STATUS_DELETED, "архив": STATUS_ARCHIVE,
}

# статусы, которые засчитываются как "дошёл живым" при подсчёте конверсии
ENGAGED_STATUSES = (STATUS_SENT, STATUS_REPLIED, STATUS_INTEREST, STATUS_CLIENT)

ARCHIVE_ELIGIBLE_STATUS = STATUS_SENT

CATEGORY_LABELS = {"initial": "Первое сообщение", "fu1": "FU1", "fu2": "FU2"}
MESSAGE_TITLE_PREFIX = {"initial": "fst", "fu1": "fu1", "fu2": "fu2"}
SEND_EVENT_BY_CATEGORY = {"initial": "message_sent", "fu1": "fu1_sent", "fu2": "fu2_sent"}

# статусы, для которых статистика считается исторически (по событиям, а не по текущему status)
HISTORICAL_FUNNEL_STATUSES = (STATUS_REPLIED, STATUS_INTEREST, STATUS_CLIENT, STATUS_CLIENT_CLOSED)

# ---------------------------------------------------------------------------
# Подключение
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

    message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)
    message_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    fu1_due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    fu1_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    fu1_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)

    fu2_due_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    fu2_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    fu2_message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)

    response_stage: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class Message(Base):
    """Библиотека текстов сообщений — отдельно от лидов, с историей использования."""
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String)  # initial / fu1 / fu2
    title: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    last_used_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


class LeadEvent(Base):
    """История действий по лиду — для будущей аналитики (кто/что/когда)."""
    __tablename__ = "lead_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    event_type: Mapped[str] = mapped_column(String)
    message_id: Mapped[Optional[int]] = mapped_column(ForeignKey("messages.id"), nullable=True)
    timestamp: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    details: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # сумма сделки — только для client_closed


class ImprovementNote(Base):
    __tablename__ = "improvement_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(String)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    file_path: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, default="manual")  # manual / auto


class User(Base):
    """Задел на будущее (доп. админы/роли) — сейчас не используется в логике доступа."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Инициализация и миграция схемы
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    import migrations
    await migrations.migrate_old_schema()
    await backfill_missing_status_events()


# ---------------------------------------------------------------------------
# Лиды: добавление / удаление
# ---------------------------------------------------------------------------

async def add_leads(usernames: list[str]) -> tuple[list[str], list[str]]:
    added, duplicates = [], []
    async with async_session() as session:
        for uname in usernames:
            existing = await session.scalar(select(Lead).where(Lead.username == uname))
            if existing:
                duplicates.append(uname)
                continue
            lead = Lead(username=uname, status=STATUS_NEW)
            session.add(lead)
            await session.flush()
            session.add(LeadEvent(lead_id=lead.id, event_type="added"))
            added.append(uname)
        await session.commit()
    return added, duplicates


async def clear_all_leads() -> int:
    async with async_session() as session:
        count = await session.scalar(select(func.count(Lead.id))) or 0
        await session.execute(delete(LeadEvent))
        await session.execute(delete(Lead))
        await session.commit()
        return count


async def delete_lead(username: str) -> bool:
    async with async_session() as session:
        lead = await session.scalar(select(Lead).where(Lead.username == username))
        if not lead:
            return False
        await session.execute(delete(LeadEvent).where(LeadEvent.lead_id == lead.id))
        await session.delete(lead)
        await session.commit()
        return True


# ---------------------------------------------------------------------------
# Лиды: постраничный просмотр / выбор по позициям
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
    if position < 1:
        return None
    async with async_session() as session:
        return await session.scalar(
            select(Lead).order_by(Lead.id.asc()).offset(position - 1).limit(1)
        )


async def get_leads_by_positions(positions: list[int]) -> list[Lead]:
    leads: list[Lead] = []
    for p in positions:
        lead = await get_lead_by_position(p)
        if lead:
            leads.append(lead)
    return leads


async def count_warm_leads() -> int:
    async with async_session() as session:
        return await session.scalar(
            select(func.count(Lead.id)).where(Lead.status.in_(WARM_STATUSES))
        ) or 0


async def get_warm_leads_page(offset: int, limit: int) -> list[Lead]:
    async with async_session() as session:
        result = await session.scalars(
            select(Lead).where(Lead.status.in_(WARM_STATUSES))
            .order_by(Lead.id.asc()).offset(offset).limit(limit)
        )
        return list(result.all())


async def get_position_map() -> dict[int, int]:
    """lead.id -> сквозной номер (тот же порядок, что и в «Вся база» / /номер) — для нумерации «Тёплых»."""
    async with async_session() as session:
        result = await session.scalars(select(Lead.id).order_by(Lead.id.asc()))
        return {lead_id: i + 1 for i, lead_id in enumerate(result.all())}


async def get_new_leads(limit: int) -> list[Lead]:
    async with async_session() as session:
        result = await session.scalars(
            select(Lead).where(Lead.status == STATUS_NEW)
            .order_by(Lead.created_at.asc()).limit(limit)
        )
        return list(result.all())


async def find_lead(username: str) -> Optional[Lead]:
    async with async_session() as session:
        return await session.scalar(select(Lead).where(Lead.username == username))


# ---------------------------------------------------------------------------
# Лиды: отправка первого сообщения / статус / FU
# ---------------------------------------------------------------------------

async def _log(session, lead_id: int, event_type: str, message_id: Optional[int] = None, details: Optional[str] = None) -> None:
    session.add(LeadEvent(lead_id=lead_id, event_type=event_type, message_id=message_id, details=details))


async def assign_message_and_send(usernames: list[str], message_id: int) -> int:
    """Первое касание: статус -> Отправлено, назначает сообщение, ставит дату, обнуляет FU-цикл."""
    if not usernames:
        return 0
    now = moscow_now()
    fu1_due = now + dt.timedelta(days=4)
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.status = STATUS_SENT
            lead.message_id = message_id
            lead.message_sent_at = now
            lead.fu1_due_at = fu1_due
            lead.fu1_sent_at = None
            lead.fu1_message_id = None
            lead.fu2_due_at = None
            lead.fu2_sent_at = None
            lead.fu2_message_id = None
            lead.response_stage = None
            await _log(session, lead.id, "message_sent", message_id)
        await touch_message(session, message_id)
        await session.commit()
        return len(leads)


async def assign_message_only(usernames: list[str], message_id: int) -> int:
    """/msg_leads: только присваивает сообщение. Статус и дата отправки НЕ меняются."""
    if not usernames:
        return 0
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            lead.message_id = message_id
            await _log(session, lead.id, "message_assigned", message_id)
        await touch_message(session, message_id)
        await session.commit()
        return len(leads)


async def assign_fu_message(usernames: list[str], stage: str, message_id: int) -> int:
    """/fu_leads: присваивает FU1/FU2-сообщение. Статус, даты и отметки об отправке НЕ меняются."""
    if not usernames:
        return 0
    field = "fu1_message_id" if stage == "fu1" else "fu2_message_id"
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.username.in_(usernames)))
        leads = list(result.all())
        for lead in leads:
            setattr(lead, field, message_id)
            await _log(session, lead.id, f"{stage}_message_assigned", message_id)
        await touch_message(session, message_id)
        await session.commit()
        return len(leads)


async def set_status_bulk(usernames: list[str], status: str) -> int:
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
                lead.response_stage = _response_stage_for(lead)
            await _log(session, lead.id, "status_changed", details=status)
        await session.commit()
        return len(leads)


async def set_status(username: str, status: str) -> bool:
    async with async_session() as session:
        lead = await session.scalar(select(Lead).where(Lead.username == username))
        if not lead:
            return False
        lead.status = status
        if status == STATUS_REPLIED:
            lead.response_stage = _response_stage_for(lead)
        await _log(session, lead.id, "status_changed", details=status)
        await session.commit()
        return True


def _response_stage_for(lead: Lead) -> str:
    if lead.fu2_sent_at:
        return "fu2"
    if lead.fu1_sent_at:
        return "fu1"
    return "initial"


async def backfill_missing_status_events() -> int:
    """Одноразовая (идемпотентная) докрутка истории: если у лида текущий статус — один из
    воронки (Ответил/Интерес/Клиент/Клиент закрыт), но подходящего события в lead_events нет
    (лид старше, чем появилось логирование) — подставляем одно событие с датой создания лида.
    Ничего не выдумываем сверх текущего статуса — промежуточные этапы не достраиваем."""
    filled = 0
    async with async_session() as session:
        # «Отправлено»: у лида есть message_sent_at, но ни одного события отправки нет
        result = await session.scalars(select(Lead).where(Lead.message_sent_at.is_not(None)))
        for lead in result.all():
            exists = await session.scalar(
                select(LeadEvent.id).where(
                    LeadEvent.lead_id == lead.id,
                    LeadEvent.event_type.in_(("message_sent",)),
                )
            )
            has_sent_status_event = await session.scalar(
                select(LeadEvent.id).where(
                    LeadEvent.lead_id == lead.id,
                    LeadEvent.event_type == "status_changed", LeadEvent.details == STATUS_SENT,
                )
            )
            if not exists and not has_sent_status_event:
                session.add(LeadEvent(
                    lead_id=lead.id, event_type="message_sent",
                    message_id=lead.message_id, timestamp=lead.message_sent_at,
                ))
                filled += 1

        for status in HISTORICAL_FUNNEL_STATUSES:
            result = await session.scalars(select(Lead).where(Lead.status == status))
            for lead in result.all():
                if status == STATUS_CLIENT_CLOSED:
                    exists = await session.scalar(
                        select(LeadEvent.id).where(
                            LeadEvent.lead_id == lead.id, LeadEvent.event_type == "client_closed"
                        )
                    )
                    if not exists:
                        session.add(LeadEvent(
                            lead_id=lead.id, event_type="client_closed",
                            message_id=lead.message_id, timestamp=lead.created_at,
                        ))
                        filled += 1
                else:
                    exists = await session.scalar(
                        select(LeadEvent.id).where(
                            LeadEvent.lead_id == lead.id,
                            LeadEvent.event_type == "status_changed",
                            LeadEvent.details == status,
                        )
                    )
                    if not exists:
                        session.add(LeadEvent(
                            lead_id=lead.id, event_type="status_changed",
                            details=status, timestamp=lead.created_at,
                        ))
                        filled += 1
        if filled:
            await session.commit()
    return filled


async def close_client(username: str, amount: float) -> bool:
    """Переводит лида в 💰 Клиент закрыт и фиксирует сумму сделки отдельным событием."""
    async with async_session() as session:
        lead = await session.scalar(select(Lead).where(Lead.username == username))
        if not lead:
            return False
        lead.status = STATUS_CLIENT_CLOSED
        session.add(LeadEvent(
            lead_id=lead.id, event_type="client_closed",
            message_id=lead.message_id, amount=amount,
        ))
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


async def change_message_date_bulk(usernames: list[str], new_date: dt.datetime) -> int:
    """Меняет message_sent_at и пересчитывает fu1_due_at / fu2_due_at. Статус НЕ трогает."""
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
            await _log(session, lead.id, "fu1_sent", lead.fu1_message_id)
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
            await _log(session, lead.id, "fu2_sent", lead.fu2_message_id)
        await session.commit()
        return len(leads)


async def get_followups_due() -> tuple[list[Lead], list[Lead]]:
    today = moscow_now().date()
    async with async_session() as session:
        result = await session.scalars(select(Lead).where(Lead.status == STATUS_SENT))
        leads = list(result.all())
    fu1_due = [l for l in leads if l.fu1_sent_at is None and l.fu1_due_at and l.fu1_due_at.date() <= today]
    fu2_due = [
        l for l in leads
        if l.fu1_sent_at is not None and l.fu2_sent_at is None
        and l.fu2_due_at and l.fu2_due_at.date() <= today
    ]
    return fu1_due, fu2_due


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
                    await _log(session, lead.id, "status_changed", details=STATUS_ARCHIVE + " (авто, 7д после FU2)")
                    archived += 1
                continue
            last_touch = lead.fu1_sent_at or lead.message_sent_at
            if last_touch and now - last_touch >= dt.timedelta(days=14):
                lead.status = STATUS_ARCHIVE
                await _log(session, lead.id, "status_changed", details=STATUS_ARCHIVE + " (авто, 14д без движения)")
                archived += 1
        if archived:
            await session.commit()
    return archived


# ---------------------------------------------------------------------------
# Библиотека сообщений
# ---------------------------------------------------------------------------

async def touch_message(session, message_id: Optional[int]) -> None:
    if message_id is None:
        return
    msg = await session.get(Message, message_id)
    if msg:
        msg.last_used_at = moscow_now()


async def get_or_create_message(category: str, content: str) -> Message:
    """Дедуп по (category, точный текст). Если нашли — обновляем last_used_at и возвращаем."""
    content = content.strip()
    async with async_session() as session:
        existing = await session.scalar(
            select(Message).where(Message.category == category, Message.content == content)
        )
        if existing:
            existing.last_used_at = moscow_now()
            await session.commit()
            return existing
        count = await session.scalar(
            select(func.count(Message.id)).where(Message.category == category)
        ) or 0
        prefix = MESSAGE_TITLE_PREFIX.get(category, category)
        msg = Message(
            category=category,
            title=f"#{prefix}_{count + 1}",
            content=content,
            active=True,
            last_used_at=moscow_now(),
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg


async def get_message(message_id: int) -> Optional[Message]:
    async with async_session() as session:
        return await session.get(Message, message_id)


async def get_messages(category: str, active_only: bool = False) -> list[Message]:
    async with async_session() as session:
        query = select(Message).where(Message.category == category)
        if active_only:
            query = query.where(Message.active.is_(True))
        result = await session.scalars(query.order_by(Message.id.asc()))
        return list(result.all())


async def count_messages_active_archived(category: str) -> tuple[int, int]:
    async with async_session() as session:
        active = await session.scalar(
            select(func.count(Message.id)).where(Message.category == category, Message.active.is_(True))
        ) or 0
        archived = await session.scalar(
            select(func.count(Message.id)).where(Message.category == category, Message.active.is_(False))
        ) or 0
    return active, archived


async def get_recent_messages(category: str, limit: int = 4) -> list[Message]:
    async with async_session() as session:
        result = await session.scalars(
            select(Message)
            .where(Message.category == category, Message.active.is_(True))
            .order_by(Message.last_used_at.desc().nullslast(), Message.id.desc())
            .limit(limit)
        )
        return list(result.all())


async def get_message_by_tag(tag: str) -> Optional[Message]:
    """tag — это title, например fst_1 (без решётки, её пользователь может не писать)."""
    tag = tag.strip().lstrip("#")
    async with async_session() as session:
        return await session.scalar(select(Message).where(Message.title == f"#{tag}"))


async def rename_message_tag(message_id: int, new_tag: str) -> bool:
    """Меняет ТОЛЬКО тег (title), например у старых сообщений формата N1 -> #fst_1. Статистика не трогается."""
    async with async_session() as session:
        msg = await session.get(Message, message_id)
        if not msg:
            return False
        msg.title = new_tag
        await session.commit()
        return True


async def change_message_content(message_id: int, new_content: str) -> bool:
    """Меняет ТОЛЬКО текст сообщения. Тег (title), id и вся история/статистика не трогаются."""
    async with async_session() as session:
        msg = await session.get(Message, message_id)
        if not msg:
            return False
        msg.content = new_content.strip()
        await session.commit()
        return True


async def archive_message(message_id: int) -> bool:
    async with async_session() as session:
        msg = await session.get(Message, message_id)
        if not msg:
            return False
        msg.active = False
        await session.commit()
        return True


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def _period_filter(column, period: Optional[tuple[dt.datetime, dt.datetime]]):
    if period is None:
        return None
    start, end = period
    return column.between(start, end)


async def _distinct_lead_ids_with_event(session, event_type: str, message_id: Optional[int] = None,
                                         period_leads: Optional[list[int]] = None) -> set[int]:
    q = select(LeadEvent.lead_id).where(LeadEvent.event_type == event_type).distinct()
    if message_id is not None:
        q = q.where(LeadEvent.message_id == message_id)
    result = await session.scalars(q)
    ids = set(result.all())
    if period_leads is not None:
        ids &= set(period_leads)
    return ids


async def _ever_reached_status(session, status: str, lead_ids: Optional[set[int]] = None) -> set[int]:
    """Множество lead_id, у которых хотя бы раз был status_changed с этим статусом.
    Для STATUS_CLIENT дополнительно учитывает client_closed-события — «Клиент закрыт» можно
    поставить и напрямую, минуя промежуточный шаг «🤝 Клиент»."""
    if status == STATUS_CLIENT_CLOSED:
        result = await session.scalars(
            select(LeadEvent.lead_id).where(LeadEvent.event_type == "client_closed").distinct()
        )
        ids = set(result.all())
        if lead_ids is not None:
            ids &= lead_ids
        return ids

    q = select(LeadEvent.lead_id).where(
        LeadEvent.event_type == "status_changed", LeadEvent.details == status
    ).distinct()
    result = await session.scalars(q)
    ids = set(result.all())
    if status == STATUS_CLIENT:
        closed = await session.scalars(
            select(LeadEvent.lead_id).where(LeadEvent.event_type == "client_closed").distinct()
        )
        ids |= set(closed.all())
    if lead_ids is not None:
        ids &= lead_ids
    return ids


async def get_stats(period: Optional[tuple[dt.datetime, dt.datetime]] = None) -> dict:
    """Историческая статистика: Отправлено/Ответили/Интерес/Клиенты/Клиент закрыт считаются
    по lead_events (кто хоть раз дошёл до этапа), а не по текущему статусу — переход дальше
    по воронке не уменьшает показатели предыдущих этапов."""
    async with async_session() as session:
        total = await session.scalar(select(func.count(Lead.id))) or 0

        cond = _period_filter(Lead.message_sent_at, period)
        period_lead_ids: Optional[list[int]] = None
        if cond is not None:
            result = await session.scalars(select(Lead.id).where(cond))
            period_lead_ids = list(result.all())

        sent_ids = await _distinct_lead_ids_with_event(session, "message_sent")
        sent_ids |= await _ever_reached_status(session, STATUS_SENT)
        if period_lead_ids is not None:
            sent_ids &= set(period_lead_ids)
        sent_total = len(sent_ids)

        by_status: dict[str, int] = {}
        for status in HISTORICAL_FUNNEL_STATUSES:
            ids = await _ever_reached_status(session, status, set(period_lead_ids) if period_lead_ids is not None else None)
            by_status[status] = len(ids)

        # остальные статусы — обычный текущий срез (это не funnel-этапы, а "парковочные" состояния)
        for status in ALL_STATUSES:
            if status in HISTORICAL_FUNNEL_STATUSES:
                continue
            q = select(func.count(Lead.id)).where(Lead.status == status)
            if cond is not None:
                q = q.where(cond)
            by_status[status] = await session.scalar(q) or 0

    return {"total": total, "sent_total": sent_total, "by_status": by_status}


MESSAGE_STATS_STATUSES = (STATUS_REPLIED, STATUS_INTEREST, STATUS_CLIENT, STATUS_CLIENT_CLOSED, STATUS_REJECT, STATUS_ARCHIVE)


async def get_message_stats(message_id: int) -> dict:
    """Исторически честная статистика по конкретному сообщению (по lead_events, категория-специфична)."""
    async with async_session() as session:
        msg = await session.get(Message, message_id)
        if not msg:
            return {"total": 0, "by_status": {s: 0 for s in MESSAGE_STATS_STATUSES}}

        send_event = SEND_EVENT_BY_CATEGORY[msg.category]
        sent_ids = await _distinct_lead_ids_with_event(session, send_event, message_id)
        total = len(sent_ids)

        by_status = {}
        for status in MESSAGE_STATS_STATUSES:
            if status in HISTORICAL_FUNNEL_STATUSES:
                ids = await _ever_reached_status(session, status, sent_ids)
                by_status[status] = len(ids)
            elif not sent_ids:
                by_status[status] = 0
            else:
                # Отказ/Архив — текущий срез среди тех, кому отправляли именно это сообщение
                by_status[status] = await session.scalar(
                    select(func.count(Lead.id)).where(Lead.id.in_(list(sent_ids)), Lead.status == status)
                ) or 0
    return {"total": total, "by_status": by_status}


async def best_initial_message(period: Optional[tuple[dt.datetime, dt.datetime]] = None) -> Optional[tuple[Message, int, int]]:
    """Сообщение категории initial с лучшей исторической конверсией в клиента (клиент+закрыт). Возвращает (message, sent, clients) или None."""
    messages = await get_messages("initial")
    best = None
    async with async_session() as session:
        cond = _period_filter(Lead.message_sent_at, period)
        period_lead_ids: Optional[set[int]] = None
        if cond is not None:
            result = await session.scalars(select(Lead.id).where(cond))
            period_lead_ids = set(result.all())

        for msg in messages:
            sent_ids = await _distinct_lead_ids_with_event(session, "message_sent", msg.id)
            if period_lead_ids is not None:
                sent_ids &= period_lead_ids
            sent = len(sent_ids)
            if sent == 0:
                continue
            client_ids = await _ever_reached_status(session, STATUS_CLIENT, sent_ids)
            closed_ids = await _ever_reached_status(session, STATUS_CLIENT_CLOSED, sent_ids)
            clients = len(client_ids | closed_ids)
            ratio = clients / sent
            if best is None or ratio > best[3]:
                best = (msg, sent, clients, ratio)
    return (best[0], best[1], best[2]) if best else None


# ---------------------------------------------------------------------------
# Заметки по улучшению
# ---------------------------------------------------------------------------

async def add_improvement_note(text_value: str) -> None:
    async with async_session() as session:
        session.add(ImprovementNote(text=text_value))
        await session.commit()


async def get_improvement_notes() -> list[ImprovementNote]:
    async with async_session() as session:
        result = await session.scalars(select(ImprovementNote).order_by(ImprovementNote.created_at.asc()))
        return list(result.all())


async def clear_improvement_notes() -> int:
    async with async_session() as session:
        count = await session.scalar(select(func.count(ImprovementNote.id))) or 0
        await session.execute(delete(ImprovementNote))
        await session.commit()
        return count


# ---------------------------------------------------------------------------
# Бэкапы (сама запись в БД; файловые операции — в backup.py)
# ---------------------------------------------------------------------------

async def add_backup_record(file_path: str, kind: str = "manual") -> None:
    async with async_session() as session:
        session.add(Backup(file_path=file_path, kind=kind))
        await session.commit()


async def get_backup_history(limit: int = 10) -> list[Backup]:
    async with async_session() as session:
        result = await session.scalars(
            select(Backup).order_by(Backup.created_at.desc()).limit(limit)
        )
        return list(result.all())


async def prune_old_backups(keep: int = 5) -> list[str]:
    """Возвращает пути файлов старых бэкапов сверх `keep` (для удаления с диска backup.py)."""
    async with async_session() as session:
        result = await session.scalars(select(Backup).order_by(Backup.created_at.desc()))
        rows = list(result.all())
        to_delete = rows[keep:]
        paths = [r.file_path for r in to_delete]
        for r in to_delete:
            await session.delete(r)
        await session.commit()
        return paths
