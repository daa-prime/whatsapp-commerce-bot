# db/init_db.py
"""
Creates the schema (db/schema.sql, SPEC.md Section 4) and seeds the default
tenant (db/seed.py). Safe to re-run — every CREATE is IF NOT EXISTS and
seed_default_tenant() no-ops if that tenant already exists.

Run directly to set up (or update) the on-disk database:
    python -m db.init_db
core/main.py also calls init_db() once at startup, so a fresh clone works
without a manual step.
"""
import os
import re
from pathlib import Path

from db import seed
from db.connection import get_connection, get_database_url

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def init_db_on_connection(conn) -> int:
    """Apply schema + seed data to an already-open connection. Used directly by
    tests (against an in-memory DB) and internally by init_db() below."""
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    tenant_name = os.environ.get("TENANT_NAME", "Default Tenant")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    # Populating these from .env keeps the one real tenant's row usable for
    # per-message routing without requiring a manual DB edit — core/main.py no
    # longer reads WHATSAPP_ACCESS_TOKEN directly.
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    app_secret = os.environ.get("WHATSAPP_APP_SECRET")
    tenant_id = seed.seed_default_tenant(
        conn, tenant_name=tenant_name, whatsapp_phone_number_id=phone_number_id,
        access_token=access_token, app_secret=app_secret,
    )
    conn.commit()
    return tenant_id


def init_db() -> int:
    """
    Initializes whichever connection db.connection.get_connection() resolves to
    — the Postgres database at DATABASE_URL. Deliberately reuses that same
    shared connection (rather than opening + closing its own) so init_db() and
    every db/repository.py call afterward operate against the exact same
    connection object.
    Returns the seeded tenant's id.
    """
    conn = get_connection()
    return init_db_on_connection(conn)


def _redact_credentials(database_url: str) -> str:
    """Never print a password to stdout, even in a diagnostic CLI message --
    someone pasting this output into a bug report/Slack thread is the
    realistic leak vector, not an attacker with a debugger."""
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", database_url)


if __name__ == "__main__":
    seeded_tenant_id = init_db()
    print(f"Database initialized at {_redact_credentials(get_database_url())} (tenant_id={seeded_tenant_id})")
