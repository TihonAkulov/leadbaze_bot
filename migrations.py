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


async def migrate_old_schema() -> None:
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
