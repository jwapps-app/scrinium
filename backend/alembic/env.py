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

# When the API runs migrations in-process (services/startup.py) the app owns
# logging already, and fileConfig would tear its handlers down mid-startup.
if config.config_file_name is not None and not config.attributes.get("in_app"):
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
    # on_version_apply lets the caller watch each step land. Nothing is set
    # when alembic is run from the command line; the API sets it so it can
    # report "3 of 5" instead of an unqualified "please wait".
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        on_version_apply=config.attributes.get("on_version_apply"),
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Fail fast rather than queue for a lock. A migration's ALTER TABLE needs
    # an exclusive lock, and Postgres grants locks in order — so a DDL waiting
    # behind the nightly pg_dump parks every later query on that table behind
    # it too, ordinary reads included. The API stays up answering nothing and
    # the whole app reads as "loading" until the dump finishes. Adding
    # document_pages widened that window by 2.4M rows.
    #
    # Applied as a connection setting, not a statement: executing anything
    # before Alembic's own begin_transaction() opens an implicit transaction,
    # and the migrations then roll back at close having appeared to succeed.
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={
            "server_settings": {
                "lock_timeout": os.environ.get("MIGRATION_LOCK_TIMEOUT", "4s")
            }
        },
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
