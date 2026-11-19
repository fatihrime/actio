"""The queue of background jobs: Procrastinate's tables, functions and types.

No table model describes them: they are built by Procrastinate's own SQL, the files it
ships for each of its versions, up to 3.11.0, applied in order (`jobs.schema_files`).
Reading the files up to a named version, not its `schema.sql`, keeps what this
migration builds the same after Procrastinate is upgraded. The upgrade's own changes
are the next migration's: the files after 3.11.0.

The SQL is sent through the driver's connection, inside the migration's transaction,
as the files have it. SQLAlchemy's way would pass parameters along, and the driver
would then read every `%` in the files as a placeholder.

Undoing it drops every object of the queue: the jobs waiting in it go too.

Revision: 0009
Follows: 0008
"""

from collections.abc import Sequence

from alembic import op

from actio.core import jobs

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROCRASTINATE = "3.11.0"
"""The version whose schema this builds. Never change it here: write the next
migration."""


def upgrade() -> None:
    for sql in jobs.schema_files(up_to=PROCRASTINATE):
        _send(sql)


def downgrade() -> None:
    # Procrastinate ships nothing that undoes its schema. Everything it made is named
    # `procrastinate_…`, in this schema: its tables, with their indexes, triggers and
    # sequences, then its functions and procedures, then its own types.
    _send(
        r"""
        DO $$
        DECLARE item record;
        BEGIN
            FOR item IN SELECT c.oid::regclass::text AS name FROM pg_class c
                        WHERE c.relnamespace = current_schema()::regnamespace
                          AND c.relkind = 'r' AND c.relname LIKE 'procrastinate\_%' LOOP
                EXECUTE 'DROP TABLE ' || item.name || ' CASCADE';
            END LOOP;
            FOR item IN SELECT p.oid::regprocedure::text AS name, p.prokind FROM pg_proc p
                        WHERE p.pronamespace = current_schema()::regnamespace
                          AND p.proname LIKE 'procrastinate\_%' LOOP
                EXECUTE 'DROP ' || CASE item.prokind WHEN 'p' THEN 'PROCEDURE ' ELSE 'FUNCTION ' END
                        || item.name || ' CASCADE';
            END LOOP;
            FOR item IN SELECT t.oid::regtype::text AS name FROM pg_type t
                        WHERE t.typnamespace = current_schema()::regnamespace
                          AND t.typname LIKE 'procrastinate\_%' AND t.typtype IN ('e', 'c')
                          AND (t.typrelid = 0 OR (SELECT relkind FROM pg_class
                                                  WHERE oid = t.typrelid) = 'c') LOOP
                EXECUTE 'DROP TYPE IF EXISTS ' || item.name || ' CASCADE';
            END LOOP;
        END $$
        """
    )


def _send(sql: str) -> None:
    """Send `sql` to Postgres as it is written, in the migration's transaction."""
    driver = op.get_bind().connection.driver_connection
    if driver is None:
        raise RuntimeError("the migration's connection to the database is closed")
    driver.execute(sql)
