"""The database module, without a database: URLs and the engine's configuration."""

import pytest

from actio.core.database import Base, create_engine, sqlalchemy_url


@pytest.mark.parametrize(
    "url",
    ["postgresql://actio:pw@db:5432/actio", "postgres://actio:pw@db:5432/actio"],
)
def test_a_standard_url_is_pointed_at_the_psycopg_driver(url: str) -> None:
    converted = sqlalchemy_url(url)

    assert converted.drivername == "postgresql+psycopg"
    assert (converted.username, converted.host, converted.port, converted.database) == (
        "actio",
        "db",
        5432,
        "actio",
    )


def test_a_percent_encoded_password_survives() -> None:
    converted = sqlalchemy_url("postgresql://actio:p%40ss%2Fword@db/actio")

    assert converted.password == "p@ss/word"


def test_other_databases_are_refused() -> None:
    with pytest.raises(ValueError, match="postgresql://"):
        sqlalchemy_url("sqlite:///actio.db")


@pytest.mark.anyio
async def test_the_engine_is_async_and_does_not_connect_until_used() -> None:
    engine = create_engine("postgresql://actio:pw@127.0.0.1:1/actio")  # nothing listens there

    assert engine.dialect.is_async
    assert engine.dialect.driver == "psycopg"
    await engine.dispose()


def test_constraints_get_predictable_names() -> None:
    """So a migration can drop one by a name that is the same on every database."""
    assert Base.metadata.naming_convention["pk"] == "pk_%(table_name)s"
