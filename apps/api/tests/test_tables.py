"""The list of table modules is complete, and the migrations create what they declare."""

import pkgutil
import subprocess
import sys

import psycopg
import pytest

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


@pytest.mark.parametrize("module", tables.MODULES)
def test_a_table_module_brings_along_the_tables_it_points_at(module: str) -> None:
    """A process that loads one table module, such as a background job that only handles
    conversations, must be able to write its rows. SQLAlchemy refuses when a table points
    at one that was never loaded, so each module imports the modules it points into.

    The API loads every module, which hides a missing import; a new interpreter that
    loads only this one doesn't.
    """
    alone = (
        f"import {module}\n"
        "from actio.core.database import Base\n"
        "print(len(Base.metadata.sorted_tables))  # fails if a pointed-at table is unknown\n"
    )

    done = subprocess.run(  # noqa: S603 - this interpreter, running the three lines above
        [sys.executable, "-c", alone], capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stderr[-400:]


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
