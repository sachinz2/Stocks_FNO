import asyncio
import os
import sys
from logging.config import fileConfig
from urllib.parse import quote_plus

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.database.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Build URL from env vars — bypasses configparser so % in passwords is safe
_db_url = os.environ.get("DATABASE_URL") or (
    f"mysql+aiomysql://{os.environ.get('DB_USER', 'root')}:"
    f"{quote_plus(os.environ.get('DB_PASSWORD', 'password123'))}@"
    f"{os.environ.get('DB_HOST', 'mysql')}:{os.environ.get('DB_PORT', '3306')}/"
    f"{os.environ.get('DB_NAME', 'falcon_db')}"
)


def run_migrations_offline() -> None:
    context.configure(
        url=_db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_db_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
