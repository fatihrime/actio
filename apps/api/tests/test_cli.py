import asyncio
import copy
import functools
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import psycopg_pool
import pytest
import uvicorn
import uvicorn.config
from pydantic import ValidationError

from actio import cli, worker
from actio.core import jobs, schema
from actio.core.database import create_engine, create_session_factory
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
    assert "log_config" not in kwargs  # uvicorn's own


def test_the_all_role_serves_the_app_with_the_worker_as_the_api_role_serves_the_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("ACTIO_PORT", "9000")
    monkeypatch.setenv("ACTIO_FORWARDED_ALLOW_IPS", "*")

    cli.main(["--role", "api"])
    cli.main(["--role", "all"])

    [(api, served_as_api), (both, served_as_all)] = calls
    assert api == ("actio.app:create_app",)
    assert both == ("actio.app:create_app_with_worker",)
    assert "log_config" in served_as_all
    del served_as_all["log_config"]  # see the tests of the log below
    assert served_as_all == served_as_api  # host, port, the wait at a stop, and the rest


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


def test_a_process_that_runs_the_worker_too_is_given_no_longer_to_stop_than_the_api() -> None:
    """In the all role the worker is told to stop at the signal, at the same moment as
    the answers, so the job in hand has its grace while the web server waits for its
    requests. Shorter than that wait, the grace is over before the server shuts the
    application down, and the time Docker gives the API is enough for both (checked in
    deploy/tests). Told only then, the worker's grace would come on top of the server's
    wait."""
    assert jobs.SHUTDOWN_GRACE_S < cli.WAITS_FOR_REQUESTS_S


@pytest.mark.parametrize("role", ["api", "all"])
def test_reload_watches_only_the_api_package(role: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **kwargs: calls.append(kwargs))

    cli.main(["--role", role, "--reload"])

    [kwargs] = calls
    assert kwargs["reload"] is True
    assert kwargs["reload_dirs"] == [str(Path(cli.__file__).parent)]


@contextmanager
def logging_put_back() -> Iterator[None]:
    """Whatever the test sets up in the log is undone afterwards, pass or fail: each
    logger's level, handlers and propagation, the root's included. A logger made
    meanwhile is left as a new one is."""
    manager = logging.root.manager

    def loggers() -> list[logging.Logger]:
        named = list(manager.loggerDict.values())
        return [logging.root, *(found for found in named if isinstance(found, logging.Logger))]

    before = {
        logger: (logger.level, list(logger.handlers), logger.propagate, logger.disabled)
        for logger in loggers()
    }
    try:
        yield
    finally:
        for logger in loggers():
            level, handlers, propagate, disabled = before.get(
                logger, (logging.NOTSET, [], True, False)
            )
            logger.setLevel(level)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.disabled = disabled


WRITTEN_AS_THE_WORKER_DOES = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ")
"""The start of a line of the worker's log: the time."""


@pytest.mark.parametrize(
    ("level", "written", "left_out"),
    [
        (
            "debug",
            [
                ("actio.worker", logging.DEBUG),
                ("procrastinate.worker", logging.DEBUG),
                ("httpx2", logging.WARNING),
            ],
            [("httpx2", logging.INFO), ("alembic.runtime.migration", logging.INFO)],
        ),
        (
            "error",
            [
                ("actio.worker", logging.ERROR),
                ("procrastinate.worker", logging.ERROR),
                ("httpx2", logging.ERROR),
            ],
            [
                ("actio.worker", logging.WARNING),
                ("procrastinate.worker", logging.WARNING),
                ("httpx2", logging.WARNING),
            ],
        ),
    ],
)
def test_the_all_role_logs_actio_and_the_queue_from_the_settings_level_and_the_rest_from_warnings(
    level: str,
    written: list[tuple[str, int]],
    left_out: list[tuple[str, int]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The worker's lines, such as what the sweep deleted, are Actio's and the queue's.
    Other libraries say a lot below a warning: a line for each request to a model's
    server, for each change the reloader sees, two each time Alembic reads the
    database's migration.

    The log is set up as uvicorn sets it up, from what it was given, and records are
    sent through it. uvicorn's own lines are written as uvicorn writes them, once."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("ACTIO_LOG_LEVEL", level)
    uvicorns_own = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)

    cli.main(["--role", "all"])

    [(args, kwargs)] = calls
    given = kwargs["log_config"]
    with logging_put_back():
        uvicorn.Config(*args, **kwargs)  # sets the log up, as `uvicorn.run` does
        for name, at in [*written, *left_out]:
            logging.getLogger(name).log(at, "a record of %s", name)
        logging.getLogger("uvicorn.error").error("a record of uvicorn's")
    said = [
        WRITTEN_AS_THE_WORKER_DOES.sub("<time> ", line)
        for line in capsys.readouterr().err.splitlines()
    ]

    assert said == [
        *(f"<time> {logging.getLevelName(at)} {name}: a record of {name}" for name, at in written),
        "ERROR:    a record of uvicorn's",
    ]
    for part in ("formatters", "handlers", "loggers"):
        assert {name: given[part][name] for name in uvicorns_own[part]} == uvicorns_own[part]
    assert uvicorns_own == uvicorn.config.LOGGING_CONFIG  # given a copy, not its own


def test_the_worker_role_runs_the_worker_without_the_plans_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A title asks a model, so the worker reads the models file. No job costs anything,
    so it doesn't read the plans file."""
    ran: list[Settings] = []

    async def run(settings: Settings) -> None:
        ran.append(settings)

    monkeypatch.setattr(worker, "run", run)
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it served HTTP"))
    monkeypatch.setenv("ACTIO_PLANS_FILE", str(tmp_path / "missing.toml"))

    cli.main(["--role", "worker"])

    [settings] = ran
    assert settings.database_url.get_secret_value() == DATABASE
    assert settings.models_file == MODELS


def test_the_worker_role_leaves_out_what_others_write_below_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alembic's lines about migrations the worker doesn't run, and the HTTP client's
    line for each request to a model's server, two for every title: as the `all` role
    leaves them out. Also when the settings ask for everything, and the worker's own
    lines are written from the debug level."""
    heard: dict[str, tuple[bool, bool]] = {}

    async def run(settings: Settings) -> None:  # noqa: ARG001
        for name in ("actio.worker", "httpx2", "alembic.runtime.migration"):
            logger = logging.getLogger(name)
            heard[name] = (logger.isEnabledFor(logging.INFO), logger.isEnabledFor(logging.WARNING))

    monkeypatch.setattr(worker, "run", run)
    monkeypatch.setenv("ACTIO_LOG_LEVEL", "debug")

    with logging_put_back():
        # As in a process of its own: with a handler there already, as the test runner
        # has put one, the role's set-up of the log would change nothing.
        logging.root.handlers = []
        cli.main(["--role", "worker"])

    assert heard == {
        "actio.worker": (True, True),
        "httpx2": (False, True),
        "alembic.runtime.migration": (False, True),
    }


class SnagError(Exception):
    """An error that a test's job lists as worth another try."""


async def a_job_that_snags_once(database_url: str) -> None:
    """Defer a job, and run it with a worker that returns once nothing is left: the job
    raises an error that its listing names, and goes back into the queue for a minute."""

    async def snags() -> None:
        raise SnagError("the chain came off")

    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as db:
            await jobs.defer(db, "snags")
            await db.commit()
    finally:
        await engine.dispose()
    again = jobs.Again(errors=(SnagError,), waits=(timedelta(minutes=1),))
    async with asyncio.timeout(10):  # a job that went round and round would hang the run
        await jobs.work(
            [jobs.Job("snags", snags, again=again)],
            database_url=database_url,
            until_idle=True,
            on_signals=False,
        )


def put_back_without_a_trace(said: str) -> bool:
    """Whether the log says that a job is run again after an error, without its trace,
    nor a word of the error."""
    return (
        "ended with status: Error, to retry" in said
        and "Traceback" not in said
        and "the chain came off" not in said
    )


def test_the_worker_roles_log_says_a_job_is_run_again_after_an_error_without_the_trace(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The log is set up by the role, and the worker runs a job whose listing names its
    error. The line is Procrastinate's, and what takes the trace off it is on
    Procrastinate's logger, whatever handlers the log has."""

    async def run(settings: Settings) -> None:  # noqa: ARG001
        await a_job_that_snags_once(migrated)

    monkeypatch.setattr(worker, "run", run)

    with logging_put_back():
        logging.root.handlers = []  # as in a process of its own: see the test above
        cli.main(["--role", "worker"])

    assert put_back_without_a_trace(capsys.readouterr().err)


def test_the_all_roles_log_says_a_job_is_run_again_after_an_error_without_the_trace(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The log is set up as uvicorn sets it up, from what the role gives it, and the
    worker runs a job whose listing names its error, as the worker inside the API does."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    cli.main(["--role", "all"])

    [(args, kwargs)] = calls
    with logging_put_back():
        uvicorn.Config(*args, **kwargs)  # sets the log up, as `uvicorn.run` does
        asyncio.run(a_job_that_snags_once(migrated))

    assert put_back_without_a_trace(capsys.readouterr().err)


def test_the_worker_does_not_start_without_its_models_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    missing = tmp_path / "models.toml"
    monkeypatch.setenv("ACTIO_MODELS_FILE", str(missing))
    monkeypatch.setattr(worker, "run", lambda _settings: pytest.fail("the worker started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "worker"])

    assert stopped.value.code == 2
    said = capsys.readouterr().err
    assert said.startswith(f"actio: the models file '{missing}' doesn't exist.")
    assert said.count("\n") == 1


def test_a_worker_whose_models_cant_be_built_says_so_in_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The file reads well, and names the variable that holds a provider's key, which
    isn't set: that shows only when the models are built, as the worker starts. Its
    database is nowhere, and it never gets as far as trying it."""
    keyed = tmp_path / "models.toml"
    keyed.write_text(
        MODELS.read_text()
        + '\n[providers.lab]\nkind = "openai-compatible"\nurl = "http://lab.example.com/v1"\n'
        'api_key_env = "ACTIO_TEST_UNSET_LAB_KEY"\n'
    )
    monkeypatch.setenv("ACTIO_MODELS_FILE", str(keyed))
    monkeypatch.delenv("ACTIO_TEST_UNSET_LAB_KEY", raising=False)
    no_longer_than_ten_seconds(monkeypatch)

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "worker"])

    assert stopped.value.code == 2
    assert capsys.readouterr().err == (
        "actio: providers.lab: the environment variable ACTIO_TEST_UNSET_LAB_KEY, which"
        " should hold its key, is not set\n"
    )


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


def no_longer_than_ten_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the worker ten seconds. One that starts where it should have refused to
    would work for ever, and the test with it: so the test fails instead."""
    run = worker.run

    async def within_ten_seconds(settings: Settings) -> None:
        async with asyncio.timeout(10):
            await run(settings)

    monkeypatch.setattr(worker, "run", within_ten_seconds)


@pytest.mark.parametrize("at", [None, "0008"], ids=["never migrated", "an older migration"])
def test_a_worker_on_a_database_that_isnt_migrated_says_so_in_one_line(
    at: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    database_url: str,
) -> None:
    """It connects, and finds that its jobs' tables aren't the ones it was written for:
    the queue's own, the first time, are missing. It takes no job then."""
    if at is not None:
        schema.upgrade(database_url)
        schema.downgrade(database_url, at)
    monkeypatch.setenv("ACTIO_DATABASE_URL", database_url)
    no_longer_than_ten_seconds(monkeypatch)

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "worker"])

    assert stopped.value.code == 1
    where = "was never migrated" if at is None else f"is at migration {at}"
    assert capsys.readouterr().err == (
        f"actio: the database {where}, and this code needs {schema.head()}:"
        ' run "actio --role migrate" first\n'
    )


def test_a_worker_on_a_database_that_newer_code_migrated_doesnt_say_to_migrate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], database_url: str
) -> None:
    """The database is at a migration this code doesn't have, as after going back to an
    older commit. Migrating with this code couldn't help, so it isn't what the line says."""
    schema.upgrade(database_url)
    with psycopg.connect(database_url) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999'")
    monkeypatch.setenv("ACTIO_DATABASE_URL", database_url)
    no_longer_than_ten_seconds(monkeypatch)

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", "worker"])

    assert stopped.value.code == 1
    assert capsys.readouterr().err == (
        f"actio: the database is at migration 9999, and this code needs {schema.head()}:"
        " the database is ahead of this code\n"
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


@pytest.mark.parametrize("role", ["api", "all", "worker"])
def test_a_mistake_in_the_models_file_is_said_in_one_line(
    role: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broken = tmp_path / "models.toml"
    broken.write_text(MODELS.read_text().replace('default = "standard"', 'default = "best"'))
    monkeypatch.setenv("ACTIO_MODELS_FILE", str(broken))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it started"))
    monkeypatch.setattr(worker, "run", lambda _settings: pytest.fail("the worker started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", role])

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


@pytest.mark.parametrize("role", ["api", "all"])
def test_a_mistake_in_the_plans_file_is_said_in_one_line(
    role: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broken = tmp_path / "plans.toml"
    broken.write_text(PLANS.read_text().replace('guests = "guest"', 'guests = "visitor"'))
    monkeypatch.setenv("ACTIO_PLANS_FILE", str(broken))
    monkeypatch.setattr(uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("it started"))

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--role", role])

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
