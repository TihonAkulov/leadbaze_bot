"""
backup.py — резервное копирование БД (.db через sqlite3 .backup()) + сводка в .xlsx,
хранение истории, восстановление.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from typing import Optional

from openpyxl import Workbook
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

import database as db
from utils import BACKUP_DIR, moscow_now, fmt_datetime

KEEP_BACKUPS = 5


def _sync_sqlite_backup(source_path: str, dest_path: str) -> None:
    """Точная копия БД через встроенный sqlite3.backup() — безопасно при активной async-сессии,
    т.к. SQLite сам управляет блокировками на уровне файла."""
    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(dest_path)
    try:
        with dst:
            src.backup(dst)
    finally:
        src.close()
        dst.close()


def _build_xlsx(dest_path: str, leads: list[db.Lead], stats: dict) -> None:
    wb = Workbook()

    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Показатель", "Значение"])
    ws.append(["Всего лидов", stats["total"]])
    for status in db.ALL_STATUSES:
        ws.append([status, stats["by_status"][status]])

    ws2 = wb.create_sheet("Лиды")
    ws2.append(["#", "username", "статус", "отправлен", "FU1 план", "FU1 факт", "FU2 план", "FU2 факт", "заметка"])
    for i, lead in enumerate(leads, start=1):
        ws2.append([
            i, lead.username, lead.status,
            fmt_datetime(lead.message_sent_at),
            fmt_datetime(lead.fu1_due_at), fmt_datetime(lead.fu1_sent_at),
            fmt_datetime(lead.fu2_due_at), fmt_datetime(lead.fu2_sent_at),
            lead.note or "",
        ])

    wb.save(dest_path)


async def create_backup(kind: str = "manual") -> tuple[str, str, int]:
    """Создаёт <db_file> и <xlsx_file> с меткой времени. Возвращает (db_path, xlsx_path, кол-во лидов)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = moscow_now().strftime("%Y%m%d_%H%M%S")
    db_dest = os.path.join(BACKUP_DIR, f"leads_{stamp}.db")
    xlsx_dest = os.path.join(BACKUP_DIR, f"leads_{stamp}.xlsx")

    await asyncio.to_thread(_sync_sqlite_backup, db.DB_PATH, db_dest)

    total = await db.count_leads()
    leads = await db.get_leads_page(0, total) if total else []
    stats = await db.get_stats()
    await asyncio.to_thread(_build_xlsx, xlsx_dest, leads, stats)

    await db.add_backup_record(db_dest, kind=kind)

    old_paths = await db.prune_old_backups(keep=KEEP_BACKUPS)
    for p in old_paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    return db_dest, xlsx_dest, total


async def list_backups(limit: int = 10) -> list[db.Backup]:
    return await db.get_backup_history(limit)


async def restore_backup(file_path: str) -> tuple[bool, str]:
    """Заменяет текущую БД файлом бэкапа. Предварительно бэкапит текущее состояние."""
    if not os.path.exists(file_path):
        return False, "Файл бэкапа не найден на диске."

    # бэкап текущего состояния перед восстановлением — на случай ошибки
    await create_backup(kind="pre-restore")

    old_count = await db.count_leads()

    await db.engine.dispose()
    await asyncio.to_thread(_sync_sqlite_backup, file_path, db.DB_PATH)

    db.engine = create_async_engine(f"sqlite+aiosqlite:///{db.DB_PATH}")
    db.async_session = async_sessionmaker(db.engine, expire_on_commit=False)

    new_count = await db.count_leads()
    return True, f"Было лидов: {old_count} -> стало: {new_count}"
