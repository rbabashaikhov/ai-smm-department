"""Alembic environment.

The database URL is read from AI_SMM_DATABASE_URL only, so alembic.ini can
stay in Git without carrying a credential.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from ai_smm.db.models import Base
from alembic import context


config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silence every
    # logger already configured in this process -- including the
    # application's own, when migrations are run in-process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.getenv("AI_SMM_DATABASE_URL") or config.get_main_option(
        "sqlalchemy.url"
    )

    if not url:
        raise RuntimeError(
            "AI_SMM_DATABASE_URL is not set; cannot run migrations."
        )

    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
