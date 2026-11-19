"""Whether a process that serves no HTTP is working: a file it touches while it works.

The worker has no port for Docker to probe. So while it works, it touches a file every
`BEAT_S` (`touch`), and its health check, `python -m actio.core.alive`, says that it is
healthy while the file was touched within `STALE_AFTER_S` (`fresh`). Where the file is,
both read from `ACTIO_ALIVE_FILE`: the worker through its settings, the check straight
from its own environment.

The worker inside the API's process (`actio --role all`) is asked over HTTP instead, by
the readiness probe. That process keeps its worker's last beat in memory, and holds it
to the same rule (`LastBeat`). It also forgets that beat when its worker ends with an
error, or finds that it can't work: there no process ends with it, and the worker is
started again sooner than `STALE_AFTER_S`, with a beat of its own each time.

Docker runs the check every few seconds, each time in a new process. So this module
imports nothing but the standard library: not the settings, and nothing that connects.
"""

import os
import sys
import time
from pathlib import Path

BEAT_S = 5.0
"""How often a process that works touches its file."""

STALE_AFTER_S = 4 * BEAT_S
"""A file that wasn't touched for this long says that its process isn't working. Four
beats, as a turn is given up on after four of its own: a process that is only slow
misses one or two."""

VARIABLE = "ACTIO_ALIVE_FILE"
"""Where the file is. The worker reads it as its `alive_file` setting."""


def touch(path: Path) -> None:
    """Say that the process is working: the file's time is now, and it is made if it
    wasn't there."""
    path.touch()


def age(path: Path, *, now: float) -> float | None:
    """How many seconds before `now`, a time as `time.time()` gives it, `path` was last
    touched. `None`: it doesn't exist."""
    try:
        return now - path.stat().st_mtime
    except FileNotFoundError:
        return None


def fresh(path: Path, *, now: float) -> bool:
    """Whether `path` was touched within `STALE_AFTER_S` before `now`.

    Not one touched after `now` by more than a beat: its time is from a clock that was
    set back since, and would say that the process works for as long as the clock takes
    to catch up."""
    ago = age(path, now=now)
    return ago is not None and -BEAT_S <= ago <= STALE_AFTER_S


class LastBeat:
    """When a process last said that it works, kept in memory: the file's rule, for a
    process that is asked over HTTP, and a way to say at once that it stopped.

    The times are the caller's, from a clock that never goes back, such as the event
    loop's: a beat is never later than now, as a file's time can be."""

    def __init__(self) -> None:
        self._at: float | None = None

    def beat(self, now: float) -> None:
        """Note that the process said, at `now`, that it works."""
        self._at = now

    def forget(self) -> None:
        """Note that the process stopped working: nothing is fresh until its next beat."""
        self._at = None

    def fresh(self, *, now: float) -> bool:
        """Whether the last beat was within `STALE_AFTER_S` before `now`. Not before
        the first one, nor after `forget` until the next."""
        return self._at is not None and now - self._at <= STALE_AFTER_S


def main() -> int:
    """The health check: 0 when the file named by `ACTIO_ALIVE_FILE` is fresh. Otherwise
    1, and one line on stderr that says why, which Docker keeps in the container's
    health log.

    This is a process of its own, started by Docker: it reads the clock itself."""
    named = os.environ.get(VARIABLE)
    if not named:
        return _unhealthy(f"{VARIABLE} isn't set, so there is no file to look at")
    path = Path(named)
    now = time.time()
    try:
        if fresh(path, now=now):
            return 0
        ago = age(path, now=now)
    except OSError as error:  # a path through something that is no folder, say
        return _unhealthy(f"{path} can't be looked at: {error.strerror}")
    if ago is None:
        return _unhealthy(f"{path} doesn't exist: nothing has said that it works")
    if ago < 0:
        return _unhealthy(
            f"{path} was touched {-ago:.0f} seconds from now: the clock was set back since"
        )
    return _unhealthy(
        f"{path} was last touched {ago:.0f} seconds ago: it is touched every {BEAT_S:g}"
        " seconds while its process works"
    )


def _unhealthy(why: str) -> int:
    sys.stderr.write(f"not working: {why}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
