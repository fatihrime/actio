"""Alembic's entry point: runs the migrations in `versions/` against the database.

Alembic imports this file for every command. The database URL comes from the caller
(`actio.core.schema`), or from ``ACTIO_DATABASE_URL`` when Alembic's own command line is
used to write a new migration (`make migration`).
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool, text

from actio.core.database import Base, sqlalchemy_url

MIGRATION_LOCK = 4_203_771_106
"""The key of the Postgres advisory lock that migrators take. Any constant will do, as
long as nothing else in the database uses the same one."""


def run_migrations() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Actio's migrations run against a database, not into a SQL script")
    url = context.config.attributes.get("database_url") or os.environ.get("ACTIO_DATABASE_URL")
    if not url:
        raise RuntimeError("no database URL: set ACTIO_DATABASE_URL")

    # Migrations are plain synchronous work: the same driver, without the event loop.
    engine = create_engine(sqlalchemy_url(url), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                # One migrator at a time. A second one waits here until the first
                # commits, then finds the schema up to date and does nothing.
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK}
                )
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations()
