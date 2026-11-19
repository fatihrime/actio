"""The file a process touches while it works, and the health check that looks at it.
And the same rule kept in memory, for the worker inside the API's process.

A file is made old with `os.utime`, and `fresh` is told the time: no test waits for a
file to go stale."""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from actio.core import alive

TOUCHED = 1_700_000_000  # in 2023, in seconds, as `time.time()` gives a time


def test_a_file_just_touched_is_fresh(tmp_path: Path) -> None:
    path = tmp_path / "alive"

    alive.touch(path)

    assert alive.fresh(path, now=time.time())


def test_touching_an_old_file_makes_it_fresh_again(tmp_path: Path) -> None:
    path = tmp_path / "alive"
    path.touch()
    os.utime(path, (TOUCHED, TOUCHED))
    assert not alive.fresh(path, now=time.time())

    alive.touch(path)

    assert alive.fresh(path, now=time.time())


def test_a_file_is_fresh_for_four_beats_and_no_longer(tmp_path: Path) -> None:
    path = tmp_path / "alive"
    path.touch()
    os.utime(path, (TOUCHED, TOUCHED))

    assert alive.STALE_AFTER_S == 4 * alive.BEAT_S == 20
    assert alive.fresh(path, now=TOUCHED + alive.STALE_AFTER_S)
    assert not alive.fresh(path, now=TOUCHED + alive.STALE_AFTER_S + 0.5)


def test_a_file_that_doesnt_exist_is_not_fresh(tmp_path: Path) -> None:
    assert not alive.fresh(tmp_path / "alive", now=time.time())


def test_a_file_touched_later_than_now_is_not_fresh_beyond_a_beat(tmp_path: Path) -> None:
    """The clock was set back after the file was touched. Were such a file fresh, a
    process that hangs just then would be healthy until the clock had caught up."""
    path = tmp_path / "alive"
    path.touch()
    os.utime(path, (TOUCHED, TOUCHED))

    assert alive.fresh(path, now=TOUCHED - alive.BEAT_S)
    assert not alive.fresh(path, now=TOUCHED - alive.BEAT_S - 0.5)
    assert not alive.fresh(path, now=TOUCHED - 600)


def test_a_beat_kept_in_memory_is_fresh_for_four_beats_and_no_longer() -> None:
    last = alive.LastBeat()
    assert not last.fresh(now=TOUCHED)  # nothing has said that it works

    last.beat(TOUCHED)

    assert last.fresh(now=TOUCHED)
    assert last.fresh(now=TOUCHED + alive.STALE_AFTER_S)
    assert not last.fresh(now=TOUCHED + alive.STALE_AFTER_S + 0.5)


def test_a_new_beat_kept_in_memory_makes_it_fresh_again() -> None:
    last = alive.LastBeat()
    last.beat(TOUCHED)
    later = TOUCHED + 600
    assert not last.fresh(now=later)

    last.beat(later)

    assert last.fresh(now=later + alive.STALE_AFTER_S)


def test_a_beat_kept_in_memory_and_forgotten_is_not_fresh_until_the_next() -> None:
    """As when the worker inside the API ends a second after it said that it works."""
    last = alive.LastBeat()
    last.beat(TOUCHED)

    last.forget()

    assert not last.fresh(now=TOUCHED + 1)
    last.beat(TOUCHED + 2)
    assert last.fresh(now=TOUCHED + 2)


def test_the_module_loads_nothing_but_the_standard_library() -> None:
    """Docker starts the check every few seconds, each time in a new process: 0.04
    seconds as it is. The settings alone would bring pydantic along."""
    loaded = subprocess.run(
        [sys.executable, "-c", "import actio.core.alive, sys; print(*sorted(sys.modules))"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.split()
    packages = {name.partition(".")[0] for name in loaded}
    heavy = {
        "alembic",
        "anyio",
        "fastapi",
        "procrastinate",
        "psycopg",
        "pydantic",
        "pydantic_settings",
        "sqlalchemy",
        "uvicorn",
        "valkey",
    }

    assert "actio.core.alive" in loaded
    assert packages & heavy == set()
    assert [name for name in loaded if name.startswith("actio.")] == [
        "actio.core",
        "actio.core.alive",
    ]


def test_the_check_passes_while_the_file_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "alive"
    alive.touch(path)
    monkeypatch.setenv("ACTIO_ALIVE_FILE", str(path))

    assert alive.main() == 0
    assert capsys.readouterr() == ("", "")


def test_the_check_fails_on_a_file_last_touched_too_long_ago_and_says_when(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "alive"
    path.touch()
    a_minute_ago = time.time() - 60
    os.utime(path, (a_minute_ago, a_minute_ago))
    monkeypatch.setenv("ACTIO_ALIVE_FILE", str(path))

    assert alive.main() == 1
    said = capsys.readouterr().err
    # 60, or 61 should the test have taken half a second since.
    assert re.fullmatch(
        rf"not working: {re.escape(str(path))} was last touched 6[01] seconds ago: it is"
        r" touched every 5 seconds while its process works\n",
        said,
    ), said


def test_the_check_fails_on_a_file_that_doesnt_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "alive"
    monkeypatch.setenv("ACTIO_ALIVE_FILE", str(path))

    assert alive.main() == 1
    assert capsys.readouterr().err == (
        f"not working: {path} doesn't exist: nothing has said that it works\n"
    )


def test_the_check_fails_on_a_file_touched_later_than_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "alive"
    path.touch()
    in_ten_minutes = time.time() + 600
    os.utime(path, (in_ten_minutes, in_ten_minutes))
    monkeypatch.setenv("ACTIO_ALIVE_FILE", str(path))

    assert alive.main() == 1
    said = capsys.readouterr().err
    # 600, or 599 should the test have taken half a second since.
    assert re.fullmatch(
        rf"not working: {re.escape(str(path))} was touched (599|600) seconds from now: the"
        r" clock was set back since\n",
        said,
    ), said


def test_the_check_says_in_one_line_that_it_cant_look_at_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Told of a path that leads through a file: a mistake in whatever set the variable.
    One line still, for the health log, not a trace."""
    (tmp_path / "a-file").write_text("not a folder")
    path = tmp_path / "a-file" / "alive"
    monkeypatch.setenv("ACTIO_ALIVE_FILE", str(path))

    assert alive.main() == 1
    assert capsys.readouterr().err == f"not working: {path} can't be looked at: Not a directory\n"


def test_the_check_fails_when_it_isnt_told_where_the_file_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert "ACTIO_ALIVE_FILE" not in os.environ

    assert alive.main() == 1
    assert capsys.readouterr().err == (
        "not working: ACTIO_ALIVE_FILE isn't set, so there is no file to look at\n"
    )
