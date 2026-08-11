from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_recycle=1800,
    # Without this a connection that dies mid-statement leaves the awaiting
    # coroutine suspended for good: pre-ping validates on checkout, not during
    # a query. A hung await is worse than an error — it holds a worker slot and
    # never retries, where a raised timeout is caught and the job requeued.
    connect_args={"command_timeout": settings.db_command_timeout},
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
