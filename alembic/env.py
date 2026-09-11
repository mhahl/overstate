"""Alembic env — reads DATABASE_URL so autogenerate can target any backend."""

import os

from alembic import context
from sqlalchemy import create_engine

from overstate_ui.models import Base

config = context.config
url = os.environ.get("DATABASE_URL", config.get_main_option("sqlalchemy.url"))

target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
