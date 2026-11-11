"""Migrations, run against a real Postgres: each test gets its own empty database."""

import asyncio
import os
import subprocess
import sys

import psycopg
import pytest
from sqlalchemy.exc import OperationalError

from actio.core import schema
from actio.core.database import create_engine

VALKEY = "valkey://127.0.0.1:1/0"  # required by the settings; a migrator never connects to it


def revisions_in(database_url: str) -> list[str]:
    """What the database itself records, read without Alembic or SQLAlchemy."""
    with psycopg.connect(database_url) as connection:
        if connection.execute("SELECT to_regclass('alembic_version')").fetchone() == (None,):
            return []
        return [row[0] for row in connection.execute("SELECT version_num FROM alembic_version")]


def test_upgrade_brings_an_empty_database_to_the_head(database_url: str) -> None:
    assert revisions_in(database_url) == []

    assert schema.upgrade(database_url) == schema.head()

    assert revisions_in(database_url) == [schema.head()]


def test_upgrading_again_changes_nothing(database_url: str) -> None:
    schema.upgrade(database_url)
    schema.upgrade(database_url)

    assert revisions_in(database_url) == [schema.head()]


def test_migrators_started_together_take_turns(database_url: str) -> None:
    """Several replicas deploying at once: all succeed, and each migration runs once.

    They are separate processes, as in a real deployment. Without the lock in
    `migrations/env.py`, they race to create Alembic's version table and some fail.
    """
    environment = os.environ | {"ACTIO_DATABASE_URL": database_url, "ACTIO_VALKEY_URL": VALKEY}
    migrators = [
        subprocess.Popen(
            [sys.executable, "-m", "actio", "--role", "migrate"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    outputs = [migrator.communicate(timeout=60) for migrator in migrators]

    assert [migrator.returncode for migrator in migrators] == [0] * 6, [e for _, e in outputs]
    assert revisions_in(database_url) == [schema.head()]


def test_every_migration_can_be_undone(database_url: str) -> None:
    schema.upgrade(database_url)
    schema.downgrade(database_url, "base")

    assert revisions_in(database_url) == []


def test_going_back_before_accounts_keeps_the_guests(database_url: str) -> None:
    """Migration 0003 added what an account needs. Undoing it can't keep accounts, and
    says so; guests, which need none of it, survive, and going forward again works."""
    schema.upgrade(database_url)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO users (id, is_guest, created_at, expires_at)"
            " VALUES (gen_random_uuid(), true, now(), now())"
        )
        connection.execute(
            "INSERT INTO users (id, is_guest, created_at, email, password_hash)"
            " VALUES (gen_random_uuid(), false, now(), 'deniz@example.com', 'a hash')"
        )

    schema.downgrade(database_url, "0002")

    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT is_guest FROM users").fetchall() == [(True,)]
    assert schema.upgrade(database_url) == schema.head()


def test_the_models_and_the_migrations_agree(database_url: str) -> None:
    """Fails when a table model changes and no migration was written for it."""
    schema.upgrade(database_url)

    schema.check(database_url)


@pytest.mark.anyio
async def test_current_reads_the_revision_through_the_apps_own_engine(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as connection:
            assert await schema.current(connection) is None
        # Alembic's commands block, so this one runs beside the event loop.
        reached = await asyncio.to_thread(schema.upgrade, database_url)
        async with engine.connect() as connection:
            assert await schema.current(connection) == reached
    finally:
        await engine.dispose()


def test_an_unreachable_database_is_an_error_not_a_hang() -> None:
    with pytest.raises(OperationalError):
        schema.upgrade("postgresql://actio:pw@127.0.0.1:1/actio")
