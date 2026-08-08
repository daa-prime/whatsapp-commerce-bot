import atexit
import os

import pytest

# Must be set before any test module's first `from core.main import app` (which
# transitively imports admin/onboarding.py) -- that module reads ADMIN_SECRET
# from the environment once, at import time, and Python only executes a module
# once per process. conftest.py is guaranteed to load before every test module
# in this directory, so this is the one place that setting can't lose a race
# against which test file pytest happens to collect first.
os.environ.setdefault("ADMIN_SECRET", "test-admin-secret")

# The app runs on Postgres (Neon in production), so tests need a real Postgres
# to run against -- an in-memory swap-in-a-connection trick (a plain
# sqlite3.connect(":memory:") fixture) has no Postgres equivalent. Two ways to
# provide that Postgres, both supported here:
#
#   1. testcontainers (default, no setup required) -- provisions a throwaway
#      Postgres container automatically via the local Docker daemon. Chosen
#      as the default because it reproduces what an in-memory fixture gives
#      every contributor for free: `pytest` just works on a fresh clone, with
#      no manual "go install/configure Postgres yourself" step, on any machine
#      or CI runner that already has Docker (which this project's Docker-based
#      deploy path assumes anyway).
#   2. TEST_DATABASE_URL env var -- if set, tests connect directly to that
#      Postgres instead (a free-tier Neon branch, a local install, or a CI
#      "services:" container) and testcontainers is never imported/started.
#      Use this where Docker-in-Docker isn't available/wanted, or to test
#      against the exact same Postgres version/provider (Neon) production uses.
#
# This has to happen at module level (not inside a fixture) and set the real
# DATABASE_URL env var: core/main.py calls db.init_db.init_db() at *import*
# time (before any pytest fixture runs), and that reads DATABASE_URL via
# db.connection.get_database_url() -- so it must already be set the moment
# the first test file does `from core.main import app`, which can happen
# during collection, before any fixture below has had a chance to run.
_test_database_url = os.environ.get("TEST_DATABASE_URL")
if not _test_database_url:
    from testcontainers.community.postgres import PostgresContainer

    _pg_container = PostgresContainer("postgres:16-alpine")
    _pg_container.start()
    atexit.register(_pg_container.stop)
    _test_database_url = _pg_container.get_connection_url(driver=None)

os.environ["DATABASE_URL"] = _test_database_url

import db.connection as db_connection
from db.init_db import init_db_on_connection
from db.seed import seed_test_tenant


@pytest.fixture(autouse=True)
def _fresh_test_db():
    """
    Fresh Postgres schema per test -- DROP/CREATE SCHEMA public gives every
    test the same "nothing pre-exists" starting point an in-memory SQLite file
    used to give for free, since a real Postgres instance can't be recreated
    from scratch anywhere near as cheaply.

    Also seeds a second, entirely fake tenant — present in every test's DB so
    multi-tenant isolation tests don't each need to seed it themselves, but
    never seeded in a real deployment (only this fixture calls
    seed_test_tenant(), not db/init_db.py's production path).
    """
    conn = db_connection._connect(_test_database_url)
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    seeded_tenant_id = init_db_on_connection(conn)
    second_tenant_id = seed_test_tenant(conn)
    db_connection.set_connection(conn)
    yield seeded_tenant_id, second_tenant_id
    db_connection.reset_connection()


@pytest.fixture
def tenant_id(_fresh_test_db):
    """The id of the one 'real' tenant seeded into this test's fresh database."""
    return _fresh_test_db[0]


@pytest.fixture
def second_tenant_id(_fresh_test_db):
    """The id of the second, fake tenant seeded purely for isolation tests."""
    return _fresh_test_db[1]
