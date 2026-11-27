"""The database schema: applying migrations, and telling whether they were applied.

The migrations live in `actio/migrations`, inside the package, so the container image
carries them and needs no configuration file to find them. `actio --role migrate` runs
`upgrade`; the readiness probe compares `current` with `head`.
"""

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import AsyncConnection


def _config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", "actio:migrations")
    if database_url is not None:
        # Passed as an attribute, not an option: options go through a parser that would
        # misread a `%` in a password.
        config.attributes["database_url"] = database_url
    return config


def head() -> str:
    """The revision this code expects the database to be at."""
    revision = ScriptDirectory.from_config(_config()).get_current_head()
    if revision is None:
        raise RuntimeError("actio/migrations holds no migration")
    return revision


def knows(revision: str) -> bool:
    """Whether this code has the migration `revision`. One it doesn't have was applied
    by newer code: the database is ahead of this code, not behind it."""
    try:
        return ScriptDirectory.from_config(_config()).get_revision(revision) is not None
    except CommandError:
        return False


def mismatch(revision: str | None, needed: str) -> str | None:
    """What keeps code that needs the migration `needed` from working on a database at
    `revision`, in one line that says what to do about it, or `None` when nothing does.
    `revision` is `None` for a database that was never migrated."""
    if revision == needed:
        return None
    if revision is None:
        at, to_do = "was never migrated", 'run "actio --role migrate" first'
    elif knows(revision):
        at, to_do = f"is at migration {revision}", 'run "actio --role migrate" first'
    else:
        # Applied by newer code than this. Migrating with this code would not help.
        at, to_do = f"is at migration {revision}", "the database is ahead of this code"
    return f"the database {at}, and this code needs {needed}: {to_do}"


def upgrade(database_url: str) -> str:
    """Apply every migration that is still missing. Returns the revision reached.

    Safe to run from several processes at once: they take turns (see `migrations/env.py`).
    """
    command.upgrade(_config(database_url), "head")
    return head()


def downgrade(database_url: str, revision: str) -> None:
    """Undo migrations back to `revision`; `"base"` undoes them all."""
    command.downgrade(_config(database_url), revision)


def check(database_url: str) -> None:
    """Raise if the table models describe something no migration creates."""
    command.check(_config(database_url))


async def current(connection: AsyncConnection) -> str | None:
    """The revision the database is at. `None`: no migration was ever applied."""

    def read(sync_connection: Connection) -> str | None:
        return MigrationContext.configure(sync_connection).get_current_revision()

    return await connection.run_sync(read)
