"""Migrations, run against a real Postgres: each test gets its own empty database."""

import asyncio
import importlib.resources
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


def test_the_code_knows_its_own_migrations_and_no_others() -> None:
    """A database at a migration this code doesn't have was migrated by newer code."""
    assert schema.knows(schema.head())
    assert schema.knows("0001")
    assert not schema.knows("9999")


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


def test_the_conversations_there_were_are_taken_as_named_by_a_person(database_url: str) -> None:
    """Migration 0012 says who named a conversation. Nothing had recorded whether one
    was renamed, so the conversations that were there are taken as named by a person,
    and never asked for a title: no model writes over a title someone may have chosen.
    The default that filled them in is gone again, and no one else can name one. Undone
    and done again, it says the same."""
    schema.upgrade(database_url)
    schema.downgrade(database_url, "0011")
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO users (id, is_guest, created_at, expires_at)"
            " VALUES ('01900000-0000-7000-8000-000000000001', true, now(), now())"
        )
        connection.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, last_active_at)"
            " VALUES ('01900000-0000-7000-8000-000000000002',"
            " '01900000-0000-7000-8000-000000000001', 'Bike repair', now(), now())"
        )

    schema.upgrade(database_url)

    with psycopg.connect(database_url) as connection:
        named = connection.execute("SELECT title, title_by, title_asked_at FROM conversations")
        assert named.fetchall() == [("Bike repair", "person", None)]
        default = connection.execute(
            "SELECT column_default FROM information_schema.columns"
            " WHERE table_name = 'conversations' AND column_name = 'title_by'"
        )
        assert default.fetchall() == [(None,)]
        with pytest.raises(psycopg.errors.CheckViolation, match="title_by_is_known"):
            connection.execute("UPDATE conversations SET title_by = 'robot'")
        connection.rollback()
    schema.downgrade(database_url, "0011")
    assert schema.upgrade(database_url) == schema.head()
    with psycopg.connect(database_url) as connection:
        named = connection.execute("SELECT title, title_by FROM conversations")
        assert named.fetchall() == [("Bike repair", "person")]


def test_going_back_before_a_plan_of_its_own_keeps_the_account_without_it(
    database_url: str,
) -> None:
    """Migration 0015 gave an account a plan of its own. Undoing it keeps the account,
    which then has the accounts' plan, as every account had before. Going forward again
    gives it none of its own, and the database refuses one for a guest again."""
    schema.upgrade(database_url)
    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO users (id, is_guest, created_at, email, password_hash, plan)"
            " VALUES (gen_random_uuid(), false, now(), 'deniz@example.com', 'a hash', 'dev')"
        )

    schema.downgrade(database_url, "0014")

    with psycopg.connect(database_url) as connection:
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'users' AND column_name = 'plan'"
        )
        assert columns.fetchall() == []
        assert connection.execute("SELECT email FROM users").fetchall() == [("deniz@example.com",)]
    assert schema.upgrade(database_url) == schema.head()
    with psycopg.connect(database_url) as connection:
        kept = connection.execute("SELECT email, plan FROM users")
        assert kept.fetchall() == [("deniz@example.com", None)]
        with pytest.raises(psycopg.errors.CheckViolation, match="only_an_account_has_a_plan"):
            connection.execute(
                "INSERT INTO users (id, is_guest, created_at, expires_at, plan)"
                " VALUES (gen_random_uuid(), true, now(), now(), 'dev')"
            )
        connection.rollback()


def test_the_models_and_the_migrations_agree(database_url: str) -> None:
    """Fails when a table model changes and no migration was written for it. The job
    queue's tables are in the database too, and no model describes them: the
    comparison leaves them alone."""
    schema.upgrade(database_url)

    schema.check(database_url)


QUEUE_OBJECTS = r"""
    SELECT 'relation', relname, relkind::text FROM pg_class
    WHERE relnamespace = current_schema()::regnamespace AND relname LIKE '%procrastinate%'
    UNION ALL
    SELECT 'routine', proname, prokind::text FROM pg_proc
    WHERE pronamespace = current_schema()::regnamespace AND proname LIKE '%procrastinate%'
    UNION ALL
    SELECT 'type', typname, typtype::text FROM pg_type
    WHERE typnamespace = current_schema()::regnamespace AND typname LIKE '%procrastinate%'
"""
"""Everything of the job queue's: tables, indexes and sequences, functions and
procedures, and types. Procrastinate names them all after itself."""


def queue_objects(database_url: str) -> set[tuple[str, str, str]]:
    with psycopg.connect(database_url) as connection:
        return {(kind, name, which) for kind, name, which in connection.execute(QUEUE_OBJECTS)}


def test_the_job_queue_comes_with_its_migration_and_goes_with_it(database_url: str) -> None:
    schema.upgrade(database_url)
    built = queue_objects(database_url)

    schema.downgrade(database_url, "0008")

    assert {
        ("relation", "procrastinate_jobs", "r"),
        ("type", "procrastinate_job_status", "e"),
    } < built
    assert queue_objects(database_url) == set()
    assert revisions_in(database_url) == ["0008"]
    assert schema.upgrade(database_url) == schema.head()
    assert queue_objects(database_url) == built


QUEUE_CATALOG = {
    "columns": r"""
        SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name LIKE 'procrastinate\_%'
    """,
    "functions and procedures": r"""
        SELECT proname, pg_get_function_identity_arguments(oid), pg_get_functiondef(oid)
        FROM pg_proc
        WHERE pronamespace = current_schema()::regnamespace AND proname LIKE 'procrastinate\_%'
    """,
    "enum labels": r"""
        SELECT t.typname, array_agg(e.enumlabel ORDER BY e.enumsortorder)
        FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
        WHERE t.typnamespace = current_schema()::regnamespace
          AND t.typname LIKE 'procrastinate\_%'
        GROUP BY t.typname
    """,
    "composite types": r"""
        SELECT t.typname, a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_type t
        JOIN pg_class c ON c.oid = t.typrelid AND c.relkind = 'c'
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE t.typnamespace = current_schema()::regnamespace
          AND t.typname LIKE 'procrastinate\_%'
    """,
    "indexes": r"""
        SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname = current_schema() AND tablename LIKE 'procrastinate\_%'
    """,
    "constraints": r"""
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE connamespace = current_schema()::regnamespace
          AND conrelid::regclass::text LIKE 'procrastinate\_%'
    """,
    "triggers": r"""
        SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid)
        FROM pg_trigger
        WHERE NOT tgisinternal AND tgrelid::regclass::text LIKE 'procrastinate\_%'
    """,
}
"""What the job queue is made of, as the database describes it."""


def queue_catalog(database_url: str) -> dict[str, list[tuple[object, ...]]]:
    with psycopg.connect(database_url) as connection:
        return {
            part: sorted(tuple(row) for row in connection.execute(query))
            for part, query in QUEUE_CATALOG.items()
        }


def test_the_job_queue_is_the_one_the_installed_procrastinate_describes(
    database_url: str, another_database_url: str
) -> None:
    """Our migrations apply Procrastinate's files up to a version they name. After an
    upgrade of the package, its `schema.sql` describes a newer queue, until a migration
    applies the files in between."""
    schema.upgrade(database_url)
    package = importlib.resources.files("procrastinate.sql")
    whole = package.joinpath("schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(another_database_url) as connection:
        connection.execute(whole)

    ours, the_packages = queue_catalog(database_url), queue_catalog(another_database_url)

    assert all(ours.values()), ours
    assert ours == the_packages, (
        "The job queue our migrations build isn't the one the installed Procrastinate "
        "describes. Was Procrastinate upgraded? Then write the next migration: one that "
        "applies its files after the version the last one names (ADR 0024)."
    )


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
