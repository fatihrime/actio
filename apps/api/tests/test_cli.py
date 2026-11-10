from pathlib import Path
from typing import Any

import psycopg
import pytest
import uvicorn
from pydantic import ValidationError

from actio import cli
from actio.core import schema

DATABASE = "postgresql://actio:pw@127.0.0.1:1/actio"
VALKEY = "valkey://127.0.0.1:1/0"


@pytest.fixture(autouse=True)
def stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two required settings; nothing in these tests connects unless it says so."""
    monkeypatch.setenv("ACTIO_DATABASE_URL", DATABASE)
    monkeypatch.setenv("ACTIO_VALKEY_URL", VALKEY)


def test_api_role_serves_the_app_factory_with_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("ACTIO_PORT", "9000")
    monkeypatch.setenv("ACTIO_FORWARDED_ALLOW_IPS", "*")

    cli.main(["--role", "api"])

    [(args, kwargs)] = calls
    assert args == ("actio.app:create_app",)
    assert kwargs["factory"] is True
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 9000
    assert kwargs["server_header"] is False
    assert kwargs["forwarded_allow_ips"] == "*"
    assert kwargs["reload"] is False


def test_reload_watches_only_the_api_package(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))

    cli.main(["--role", "api", "--reload"])

    [kwargs] = calls
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == [str(Path(cli.__file__).parent)]


def test_unknown_role_exits_with_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--role", "nope"])

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_a_process_without_its_database_setting_does_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACTIO_DATABASE_URL")
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it started"))

    with pytest.raises(ValidationError, match="database_url"):
        cli.main(["--role", "api"])


def test_migrate_role_upgrades_the_database_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], database_url: str
) -> None:
    monkeypatch.setenv("ACTIO_DATABASE_URL", database_url)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it served HTTP"))

    cli.main(["--role", "migrate"])

    assert capsys.readouterr().out == f"The database schema is at migration {schema.head()}.\n"
    with psycopg.connect(database_url) as connection:
        applied = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    assert applied == [(schema.head(),)]
