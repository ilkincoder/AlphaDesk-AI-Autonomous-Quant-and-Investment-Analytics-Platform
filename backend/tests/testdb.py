"""An isolated PostgreSQL database for the schema tests.

Nothing in here may ever touch the application database. The database name is
derived from `DATABASE_URL` by appending `_test`, and it is checked against a strict
pattern *before* any connection is opened or any DDL runs. A name that does not match
is a hard error rather than a warning, because the alternative -- running a downgrade
against the database holding a real portfolio -- is not recoverable by being careful
later.

The schema is built by running the real Alembic migrations in a subprocess, not by
calling `Base.metadata.create_all`. That is deliberate. `create_all` builds the schema
the *models* describe, so a migration that had drifted from the models would still
show green; running the migration means the tests can only pass against the schema the
migration actually produces, and it makes the downgrade round trip in
`test_migration_roundtrip.py` meaningful.
"""

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url

from app.config import settings

# Where alembic.ini lives. Resolved from this file rather than from the working
# directory, so the subprocess runs correctly however the tests were started.
_BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Deliberately strict. The name is interpolated into CREATE DATABASE, which cannot
# take a bound parameter; restricting it to word characters is what makes that
# interpolation safe, and the `_test` suffix is the guard that actually matters.
_TEST_DATABASE_NAME = re.compile(r"^[A-Za-z0-9_]+_test$")

_engine: Engine | None = None


def test_database_url() -> str:
    """The isolated test database's URL, derived from the application's.

    Raises RuntimeError rather than returning a URL it does not fully trust. Nothing
    downstream re-checks this, so the check has to happen here.
    """
    app_url = make_url(settings.database_url)
    if not app_url.database:
        raise RuntimeError(f"DATABASE_URL names no database: {app_url!r}")

    test_name = f"{app_url.database}_test"
    if not _TEST_DATABASE_NAME.match(test_name):
        raise RuntimeError(
            f"refusing to use {test_name!r} as a test database: the name must be "
            "word characters ending in '_test'"
        )

    test_url = app_url.set(database=test_name)
    if test_url == app_url:
        raise RuntimeError("the test database URL resolved to the application's own")

    # hide_password=False, because SQLAlchemy masks passwords in str()/repr() by
    # default and the masked form is useless as a connection string.
    return test_url.render_as_string(hide_password=False)


def ensure_test_database() -> None:
    """Create the test database if it is not already there. Safe to repeat."""
    url = make_url(test_database_url())
    database = url.database

    # Connected to the *application* database, not the test one -- the test one may
    # not exist yet, and you cannot connect to a database to create itself.
    #
    # AUTOCOMMIT because CREATE DATABASE cannot run inside a transaction block.
    admin_engine = create_engine(
        url.set(database=make_url(settings.database_url).database),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin_engine.connect() as connection:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database},
            )
            if exists:
                return
            connection.execute(text(f'CREATE DATABASE "{database}"'))
            print(f"created test database {database!r}")
    finally:
        admin_engine.dispose()


def run_alembic(*args: str) -> None:
    """Run an Alembic command against the test database, in a subprocess.

    A subprocess rather than an in-process call: `app.config.settings` is a
    module-level singleton that `alembic/env.py` imports, so pointing Alembic at the
    test database is only unambiguous if it gets a fresh process with `DATABASE_URL`
    already set to it.

    Output is captured and re-raised as RuntimeError rather than left on a
    CalledProcessError. Alembic's real message -- a bad constraint name, say -- is in
    its stderr, and a failing test that reports only "returned non-zero exit status 1"
    sends you looking in the wrong place.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": test_database_url()},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic {' '.join(args)} failed against the test database:\n"
            f"{result.stderr.strip()}"
        )


def test_engine() -> Engine:
    """An engine for the test database, prepared at head on first use.

    Cached, so the migration runs once per test session rather than once per test.
    """
    global _engine
    if _engine is None:
        ensure_test_database()
        run_alembic("upgrade", "head")
        _engine = create_engine(test_database_url())
    return _engine


def table_names(engine: Engine) -> set[str]:
    """The tables in the test database's `public` schema."""
    with engine.connect() as connection:
        return set(
            connection.scalars(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        )


def alembic_revision(engine: Engine) -> str:
    """The revision the test database currently records."""
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version"))


@contextmanager
def rolled_back(engine: Engine) -> Iterator[Connection]:
    """A connection whose transaction is always rolled back.

    Every insert in the schema tests happens inside one of these, so no test can
    leave a row behind even if it fails partway. The test database is disposable, but
    a test that depends on another test's leftovers is a test that passes or fails
    according to the order it happened to run in.
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()