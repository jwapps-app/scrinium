import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.database import Base
from app import models  # noqa: F401  (register models on Base.metadata)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Fail fast rather than queue for a lock. A migration's ALTER TABLE needs
    # an exclusive lock, and Postgres grants locks in order — so a DDL waiting
    # behind the nightly pg_dump puts every later query on that table behind
    # it too, ordinary reads included. The API stays up but answers nothing,
    # and the whole app reads as "loading" until the dump finishes. That got
    # worse once document_pages joined the dump: 2.4M more rows to copy.
    #
    # With a short timeout the migration gives up instead, releasing the queue
    # immediately, and the entrypoint retries until the coast is clear.
    connection.exec_driver_sql(
        f"SET lock_timeout = '{os.environ.get('MIGRATION_LOCK_TIMEOUT', '4s')}'"
    )
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
