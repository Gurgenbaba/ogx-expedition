# app/db.py
from __future__ import annotations
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from .settings import settings

_raw = settings.database_url or "sqlite+aiosqlite:///./expedition_dev.db"

# Normalise postgres:// → postgresql+asyncpg://
if _raw.startswith("postgres://"):
    _raw = "postgresql+asyncpg://" + _raw[len("postgres://"):]
elif _raw.startswith("postgresql://") and "+asyncpg" not in _raw:
    _raw = _raw.replace("postgresql://", "postgresql+asyncpg://", 1)

IS_SQLITE = _raw.startswith("sqlite")
IS_POSTGRES = "postgresql" in _raw

_kwargs: dict = {}
if IS_SQLITE:
    _kwargs["connect_args"] = {"check_same_thread": False}
    _kwargs["pool_pre_ping"] = True
else:
    _kwargs["pool_size"] = 5
    _kwargs["max_overflow"] = 10
    _kwargs["pool_pre_ping"] = True

engine = create_async_engine(_raw, echo=False, **_kwargs)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
