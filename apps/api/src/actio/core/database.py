"""The Postgres connection, through SQLAlchemy on the psycopg 3 driver.

One async engine per process, created at startup and disposed at shutdown. The same
driver also runs the migrations, synchronously (see `actio.core.schema`).
"""

from sqlalchemy import URL, MetaData, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
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
