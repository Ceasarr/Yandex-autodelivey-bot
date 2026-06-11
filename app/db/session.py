"""Асинхронная сессия БД и инициализация схемы."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.db.models import Base

# Гарантируем, что директория для файла SQLite существует
if settings.database_url.startswith("sqlite"):
    # sqlite+aiosqlite:///./data/service.db  -> ./data/service.db
    # sqlite+aiosqlite:////app/data/service.db -> /app/data/service.db
    # Четыре слэша = абсолютный путь: ////app/data -> /app/data
    # Три слэша = относительный:       ///./data   -> ./data
    raw = settings.database_url.split("sqlite+aiosqlite:///", 1)[-1]
    db_path = "/" + raw if settings.database_url.startswith("sqlite+aiosqlite:////") else raw
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    # NullPool: каждая сессия открывает/закрывает соединение сама.
    # Устраняет "unable to open database file" при долгой работе —
    # пул не хранит соединения между запросами, поэтому протухших
    # соединений и зависших потоков aiosqlite не возникает.
    poolclass=NullPool,
)


# SQLite по умолчанию работает в режиме rollback-журнала: пишущая транзакция
# берёт эксклюзивную блокировку на весь файл, а busy_timeout = 0. В нашем
# процессе одновременно пишут поллер (выдача ключей) и бот (загрузка ключей,
# добавление SKU), поэтому без настройки запись из бота «зависала»/падала на
# блокировке, пока поллер держал транзакцию.
#
# Включаем WAL (читатели не блокируют писателя, пишет один) и busy_timeout
# (второй писатель ждёт освобождения, а не падает сразу). PRAGMA применяется
# к каждому новому соединению пула.
if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")  # ждать блокировку до 10с
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


async def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Контекстный менеджер сессии с авто-rollback при ошибке."""
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
