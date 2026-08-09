# db/connection.py
"""
Thin connection layer — the only place that knows this is Postgres (SPEC
Section 6/12.6: moved off SQLite before real production load, onto Neon).
Swapping the backend again later means changing this file and db/schema.sql,
not touching db/repository.py's callers (core/main.py, admin/onboarding.py) —
those only ever call get_connection() and use the connection's
.execute()/.commit() methods.

_PGConnection below is a thin adapter, not a different database abstraction:
it exists purely so db/repository.py's existing conn.execute(sql, params)
.fetchone()/.fetchall() call sites (written against sqlite3.Connection's
chainable-cursor convenience method) keep working unchanged against psycopg2,
which has no such method on the connection object itself. It also rewrites
the `?` placeholders repository.py already uses into psycopg2's `%s` style,
so that conversion didn't have to touch every call site individually.

psycopg2 (sync) was chosen over asyncpg specifically because every
db/repository.py function is already plain sync code called directly from
async FastAPI handlers (blocking the event loop each call, same as the old
sqlite3 driver did) — switching to asyncpg would mean async-ifying every
repository function and every caller, a far bigger change than "swap the
database backend."

A single module-level connection is reused for the process lifetime (this
app's traffic doesn't need pooling yet — a connection pool, e.g. psycopg2's
ThreadedConnectionPool or pgbouncer in front of Neon, is a reasonable next
step once concurrent load justifies it, not done here). Tests swap it out via
set_connection() to point at a real (testcontainers-provisioned) Postgres
instance whose schema gets reset between tests — see tests/conftest.py.
"""
import os
import re

import psycopg2
import psycopg2.extras

# Re-exported so every other module that needs to catch a constraint
# violation (admin/onboarding.py's duplicate phone_number_id) imports it from
# here rather than knowing which driver is underneath — this is the one piece
# of driver knowledge those modules previously had to have directly (as
# `sqlite3.IntegrityError`).
IntegrityError = psycopg2.IntegrityError

_QUESTION_MARK_RE = re.compile(r"\?")


class _PGConnection:
    """Wraps a psycopg2 connection to give it sqlite3.Connection's
    conn.execute(sql, params).fetchone()/.fetchall() chaining convenience,
    dict-like row access (via RealDictCursor), and an executescript() for
    running db/schema.sql's multi-statement script in one call."""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._connect()

    def _connect(self) -> None:
        self._conn = psycopg2.connect(self._dsn, cursor_factory=psycopg2.extras.RealDictCursor)
        # Critical behavioral difference from SQLite: Postgres aborts the
        # *entire* transaction after any failed statement (e.g. the
        # IntegrityError admin/onboarding.py's duplicate-phone_number_id catch
        # is built around) -- every subsequent statement on that connection would raise
        # "current transaction is aborted" until a ROLLBACK, even unrelated
        # SELECTs, unless autocommit is on. Autocommit makes each statement its
        # own implicitly-committed transaction, so a caught IntegrityError
        # doesn't poison anything after it -- matching how SQLite (and this
        # codebase's existing "execute, then explicitly .commit()" pattern,
        # never spanning a transaction across multiple repository calls)
        # already behaved.
        self._conn.autocommit = True

    def execute(self, sql: str, params=()):
        pg_sql = _QUESTION_MARK_RE.sub("%s", sql)
        try:
            cur = self._conn.cursor()
            cur.execute(pg_sql, params)
            return cur
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            # The module-level connection is reused for the process lifetime
            # (see class docstring) -- a managed Postgres provider (e.g. Neon's
            # pooler) can silently close an idle connection between requests.
            # One reconnect-and-retry means a process that's been idle for a
            # while (low-traffic production, or a manual testing session with
            # gaps between messages) recovers on the next request instead of
            # every subsequent query failing until the process is restarted.
            self._connect()
            cur = self._conn.cursor()
            cur.execute(pg_sql, params)
            return cur

    def executescript(self, sql: str) -> None:
        cur = self._conn.cursor()
        cur.execute(sql)
        self._conn.commit()

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def transaction(self) -> "_Transaction":
        """Context manager for a real multi-statement transaction, for the
        rare call site (db/repository.py:checkout_cart) that needs several
        statements to succeed or fail together -- e.g. decrementing stock and
        creating an order must not leave one done without the other if
        something fails partway through. Every other call site in this
        codebase keeps using the default per-statement autocommit above,
        completely unaffected: this only changes behavior for the duration of
        the `with` block, on whichever connection it's called on."""
        return _Transaction(self._conn)


class _Transaction:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self) -> "_Transaction":
        self._conn.autocommit = False
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.autocommit = True
        return False  # never suppress an exception raised inside the block


_connection: _PGConnection | None = None


def get_database_url() -> str:
    """No sensible default exists for Postgres the way a local SQLite file
    path used to have one — Neon (or any Postgres) always requires an
    explicit connection string, so this raises rather than silently falling
    back to something that would just fail later with a worse error."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required (e.g. the connection "
            "string Neon gives you for this database) — there is no default now "
            "that SQLite has been replaced with Postgres."
        )
    return url


def _connect(dsn: str) -> _PGConnection:
    return _PGConnection(dsn)


def get_connection() -> _PGConnection:
    global _connection
    if _connection is None:
        _connection = _connect(get_database_url())
    return _connection


def set_connection(conn) -> None:
    """Test hook: point every repository function at a specific (e.g.
    testcontainers-provisioned) connection."""
    global _connection
    _connection = conn


def reset_connection() -> None:
    global _connection
    if _connection is not None:
        _connection.close()
    _connection = None
