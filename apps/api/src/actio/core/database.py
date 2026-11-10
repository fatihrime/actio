"""The Postgres connection, through SQLAlchemy on the psycopg 3 driver.

One async engine per process, created at startup and disposed at shutdown. The same
driver also runs the migrations, synchronously (see `actio.core.schema`).
"""

from datetime import datetime

from sqlalchemy import URL, DateTime, MetaData, make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# Explicit names for every constraint and index, so a migration can drop or rename one
# by a name that is the same on every database.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The base class of every table model. Alembic compares its metadata with the
    database to write migrations, and to check that none is missing."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Every time is stored with its time zone (`timestamptz`), so it means one instant
    # wherever it is read.
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012 - SQLAlchemy reads it


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Makes one database session per unit of work, such as a request.

    Objects stay readable after a commit: loading them again would need another round
    trip, and after the session is closed it would fail.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


def sqlalchemy_url(database_url: str) -> URL:
    """A standard ``postgresql://`` URL, pointed at the psycopg 3 driver.

    Settings take the URL in its standard form, as hosting providers hand it out;
    SQLAlchemy wants the driver named in it.
    """
    url = make_url(database_url.replace("postgres://", "postgresql://", 1))
    if url.get_backend_name() != "postgresql":
        raise ValueError("the database URL must start with postgresql://")
    return url.set(drivername="postgresql+psycopg")


def create_engine(database_url: str) -> AsyncEngine:
    """The process's connection pool. It connects on first use, not here."""
    return create_async_engine(
        sqlalchemy_url(database_url),
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,  # replace a connection the server closed while it sat idle
    )
