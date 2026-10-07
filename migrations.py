"""
migrations.py — перенос данных со старой схемы (V1.x, текстовые message/fu1_message/fu2_message
прямо в leads) на новую (V2, отдельная таблица messages + message_id-ссылки).

Ничего не удаляет: старые текстовые колонки остаются в файле БД (просто больше не читаются
кодом), новые данные заполняются рядом. Идемпотентно — повторный запуск ничего не ломает.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

import database as db

logger = logging.getLogger("lead_bot.migrations")


async def _old_columns_present(conn) -> set[str]:
    result = await conn.execute(text("PRAGMA table_info(leads)"))
    return {row[1] for row in result.fetchall()}


async def _ensure_lead_events_amount_column() -> None:
    async with db.engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(lead_events)"))
        cols = {row[1] for row in result.fetchall()}
        if "amount" not in cols:
            await conn.execute(text("ALTER TABLE lead_events ADD COLUMN amount FLOAT"))


async def migrate_old_schema() -> None:
    await _ensure_lead_events_amount_column()

    async with db.engine.begin() as conn:
        cols = await _old_columns_present(conn)

        # 1) добавляем новые колонки, если их ещё нет (на случай ручного/частичного апгрейда)
        new_columns = {
            "message_id": "INTEGER",
            "fu1_message_id": "INTEGER",
            "fu2_message_id": "INTEGER",
        }
        for name, coltype in new_columns.items():
            if name not in cols:
                await conn.execute(text(f"ALTER TABLE leads ADD COLUMN {name} {coltype}"))

        # старые текстовые поля V1.2/V1.3, из которых переносим содержимое
        has_old_text = "message" in cols or "fu1_message" in cols or "fu2_message" in cols
        if not has_old_text:
            return  # свежая база V2 — переносить нечего

    # 2) сами тексты переносим через ORM (нужен доступ к get_or_create_message)
    async with db.async_session() as session:
        from sqlalchemy import select

        rows = await session.execute(text(
            "SELECT id, "
            + ("message" if "message" in cols else "NULL") + " AS message, "
            + ("fu1_message" if "fu1_message" in cols else "NULL") + " AS fu1_message, "
            + ("fu2_message" if "fu2_message" in cols else "NULL") + " AS fu2_message "
            "FROM leads"
        ))
        leads_raw = rows.fetchall()

    migrated_initial = migrated_fu1 = migrated_fu2 = 0
    for row in leads_raw:
        lead_id, old_message, old_fu1, old_fu2 = row[0], row[1], row[2], row[3]

        async with db.async_session() as session:
            lead = await session.get(db.Lead, lead_id)
            if not lead:
                continue
            changed = False

            if old_message and not lead.message_id:
                msg = await db.get_or_create_message("initial", old_message)
                lead.message_id = msg.id
                changed = True
                migrated_initial += 1

            if old_fu1 and not lead.fu1_message_id:
                msg = await db.get_or_create_message("fu1", old_fu1)
                lead.fu1_message_id = msg.id
                changed = True
                migrated_fu1 += 1

            if old_fu2 and not lead.fu2_message_id:
                msg = await db.get_or_create_message("fu2", old_fu2)
                lead.fu2_message_id = msg.id
                changed = True
                migrated_fu2 += 1

            if changed:
                await session.commit()

    if migrated_initial or migrated_fu1 or migrated_fu2:
        logger.info(
            f"Миграция V1->V2: перенесено сообщений — "
            f"первое: {migrated_initial}, FU1: {migrated_fu1}, FU2: {migrated_fu2}"
        )


FLAG_LEAD_EVENTS_TZ = "lead_events_timestamp_utc_to_msk"


async def fix_lead_events_timezone() -> None:
    """Одноразово: lead_events.timestamp писался по умолчанию в UTC (dt.datetime.utcnow),
    а «По дням»/вечерний отчёт считают окна в МСК — события, случившиеся 07:00-10:00 МСК
    (время утреннего Follow-up), проваливались между днями. Колонка в database.py уже
    переведена на московское время для новых событий; здесь — разовый сдвиг старых записей
    (+3 часа), чтобы и прошлые дни считались верно. Флаг в system_flags — защита от повторного
    сдвига при каждом рестарте.

    ВАЖНО: должна вызываться из database.init_db() ДО backfill_missing_status_events().
    Часть событий, которые создаёт backfill, уже берёт МСК-время (message_sent_at) — если
    сдвинуть их ПОСЛЕ, они станут неверными на +3 часа."""
    if await db.get_flag(FLAG_LEAD_EVENTS_TZ):
        return

    async with db.engine.begin() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM lead_events"))
        count = result.scalar() or 0
        if count:
            await conn.execute(text(
                "UPDATE lead_events SET timestamp = datetime(timestamp, '+3 hours')"
            ))

    await db.set_flag(FLAG_LEAD_EVENTS_TZ)
    if count:
        logger.info(f"Миграция часового пояса: сдвинуто событий lead_events — {count}")
