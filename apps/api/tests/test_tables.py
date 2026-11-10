"""The list of table modules is complete, and the migrations create what they declare."""

import pkgutil

import psycopg

import actio
from actio import tables


def test_every_models_module_is_listed() -> None:
    """A module left out of `tables.MODULES` would be invisible to the migrations:
    its tables would never be created, and no check would notice."""
    found = {
        module.name
        for module in pkgutil.walk_packages(actio.__path__, prefix="actio.")
        if module.name.rsplit(".", 1)[-1] == "models"
    }

    assert found == set(tables.MODULES)


def test_the_migrations_create_exactly_the_declared_tables(migrated: str) -> None:
    with psycopg.connect(migrated) as connection:
        rows = connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()

    assert {name for (name,) in rows} == set(tables.metadata.tables) | {"alembic_version"}


def test_every_time_column_keeps_its_time_zone(migrated: str) -> None:
    """`timestamp` without a zone means a different instant on every server."""
    with psycopg.connect(migrated) as connection:
        rows = connection.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type = 'timestamp without time zone'"
        ).fetchall()

    assert rows == []
