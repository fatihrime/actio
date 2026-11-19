import functools
from pathlib import Path
from typing import Any

import psycopg
import psycopg_pool
import pytest
import uvicorn
from pydantic import ValidationError

from actio import cli, worker
from actio.core import schema
from actio.core.settings import Settings

DATABASE = "postgresql://actio:pw@127.0.0.1:1/actio"
VALKEY = "valkey://127.0.0.1:1/0"
MODELS = Path(__file__).resolve().parents[3] / "config" / "models.toml"
PLANS = MODELS.with_name("plans.toml")


@pytest.fixture(autouse=True)
def stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two required settings, and the repository's models and plans files wherever
    the tests are run from. Nothing in these tests connects unless it says so."""
    monkeypatch.setenv("ACTIO_DATABASE_URL", DATABASE)
    monkeypatch.setenv("ACTIO_VALKEY_URL", VALKEY)
    monkeypatch.setenv("ACTIO_MODELS_FILE", str(MODELS))
    monkeypatch.setenv("ACTIO_PLANS_FILE", str(PLANS))


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


def test_the_server_is_told_how_long_to_wait_for_its_requests_when_it_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a limit it would wait for ever for a request that never ends, and be
    killed in the end. How the limit fits the time answers get, and the time Docker
    gives, is checked where all three are in view: deploy/tests."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))

    cli.main(["--role", "api"])

    [kwargs] = calls
    assert kwargs["timeout_graceful_shutdown"] == cli.WAITS_FOR_REQUESTS_S == 25


def test_reload_watches_only_the_api_package(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))

    cli.main(["--role", "api", "--reload"])

    [kwargs] = calls
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == [str(Path(cli.__file__).parent)]


def test_the_worker_role_runs_the_worker_without_the_models_and_plans_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No job talks to a model yet."""
    ran: list[Settings] = []

    async def run(settings: Settings) -> None:
        ran.append(settings)

    monkeypatch.setattr(worker, "run", run)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it served HTTP"))
    monkeypatch.setenv("ACTIO_MODELS_FILE", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ACTIO_PLANS_FILE", str(tmp_path / "missing-too.toml"))

    cli.main(["--role", "worker"])

    [settings] = ran
    assert settings.database_url.get_secret_value() == DATABASE


@pytest.mark.parametrize("role", ["migrate", "worker"])
def test_reload_is_refused_for_a_role_that_serves_no_http(
    role: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(worker, "run", lambda _settings: pytest.fail("the worker started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", role, "--reload"])

    assert stopped.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"actio: error: --reload restarts the API's server; the {role} role has none"
    )


def test_a_worker_that_cant_connect_to_its_database_says_so_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pool waits 30 seconds for its first connection; here, half of one. Nothing
    listens where the settings point."""
    monkeypatch.setattr(
        psycopg_pool.AsyncConnectionPool,
        "open",
        functools.partialmethod(psycopg_pool.AsyncConnectionPool.open, timeout=0.5),
    )

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "worker"])

    assert stopped.value.code == 1
    assert capsys.readouterr().err == (
        "actio: couldn't connect to the database: pool initialization incomplete after 0.5 sec\n"
    )


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


def test_the_api_does_not_start_without_its_models_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "models.toml"
    monkeypatch.setenv("ACTIO_MODELS_FILE", str(missing))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "api"])

    assert stopped.value.code == 2
    said = capsys.readouterr().err
    assert said.startswith(f"actio: the models file '{missing}' doesn't exist.")
    assert "ACTIO_MODELS_FILE" in said
    assert said.count("\n") == 1  # one line to read, not a stack of them


def test_a_mistake_in_the_models_file_is_said_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broken = tmp_path / "models.toml"
    broken.write_text(MODELS.read_text().replace('default = "standard"', 'default = "best"'))
    monkeypatch.setenv("ACTIO_MODELS_FILE", str(broken))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "api"])

    assert stopped.value.code == 2
    assert capsys.readouterr().err == (
        f'actio: {broken}: default = "best" is not one of the models\n'
    )


def test_the_api_does_not_start_without_its_plans_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "plans.toml"
    monkeypatch.setenv("ACTIO_PLANS_FILE", str(missing))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "api"])

    assert stopped.value.code == 2
    said = capsys.readouterr().err
    assert said.startswith(f"actio: the plans file '{missing}' doesn't exist.")
    assert "ACTIO_PLANS_FILE" in said
    assert said.count("\n") == 1


def test_a_mistake_in_the_plans_file_is_said_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broken = tmp_path / "plans.toml"
    broken.write_text(PLANS.read_text().replace('guests = "guest"', 'guests = "visitor"'))
    monkeypatch.setenv("ACTIO_PLANS_FILE", str(broken))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "api"])

    assert stopped.value.code == 2
    assert capsys.readouterr().err == (
        f'actio: {broken}: guests = "visitor" is not one of the plans\n'
    )


def test_the_api_does_not_start_while_a_model_on_offer_has_no_price(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The two files are edited apart. Offering a model and forgetting what it costs
    must not make it free, or fail at the first question: it stops the start."""
    unpriced = tmp_path / "plans.toml"
    unpriced.write_text(PLANS.read_text().replace("[prices.standard]", "[prices.standrd]"))
    monkeypatch.setenv("ACTIO_PLANS_FILE", str(unpriced))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "api"])

    assert stopped.value.code == 2
    assert capsys.readouterr().err == (
        f'actio: {unpriced}: prices: the model "standard" is on offer and has no price. '
        "Add [prices.standard]; input = 0 and output = 0 make it free\n"
    )


def test_migrating_needs_no_models_file_and_no_plans_file(
    monkeypatch: pytest.MonkeyPatch, database_url: str, tmp_path: Path
) -> None:
    """The migration job changes the database and exits: it never talks to a model."""
    monkeypatch.setenv("ACTIO_DATABASE_URL", database_url)
    monkeypatch.setenv("ACTIO_MODELS_FILE", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("ACTIO_PLANS_FILE", str(tmp_path / "missing-too.toml"))

    cli.main(["--role", "migrate"])

    with psycopg.connect(database_url) as connection:
        assert connection.execute("SELECT count(*) FROM alembic_version").fetchone() == (1,)


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
