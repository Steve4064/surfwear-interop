"""Accès base de données (SQLAlchemy 2, asynchrone)."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

log = logging.getLogger("surfwear.db")


def make_engine(url: str) -> AsyncEngine:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs = {"connect_args": {"timeout": 30}}
    return create_async_engine(url, **kwargs)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine, base: type[DeclarativeBase], retries: int = 30) -> None:
    """Crée les tables, en attendant que la base soit joignable."""
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(base.metadata.create_all)
            return
        except Exception as exc:
            if attempt == retries:
                raise
            log.warning("Base indisponible (%s/%s) : %s", attempt, retries, exc)
            await asyncio.sleep(2)
