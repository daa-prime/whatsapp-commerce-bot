# db/seed.py
"""
Seed data. Two kinds:

- seed_default_tenant(): the one real tenant this build actually serves.
  Real per-tenant catalog/payment-gateway data comes from Phase 7's onboarding
  wizard extension or Phase 1's Meta Commerce Manager setup; this is just the
  tenant row itself.

- seed_test_tenant(): a second, entirely fake tenant used ONLY by
  tests/conftest.py to prove multi-tenant isolation -- never called from
  init_db_on_connection, so it never exists in a real deployment's database.
"""


def seed_default_tenant(
    conn,
    tenant_name: str = "Default Tenant",
    whatsapp_phone_number_id: str | None = None,
    access_token: str | None = None,
    app_secret: str | None = None,
) -> int:
    """Idempotent: if a tenant with this name already exists, returns its id
    without inserting again. Returns the tenant's id."""
    row = conn.execute("SELECT id FROM tenants WHERE name = ?", (tenant_name,)).fetchone()
    if row:
        return row["id"]

    cur = conn.execute(
        "INSERT INTO tenants (name, whatsapp_phone_number_id, meta_access_token_ref, app_secret_ref) "
        "VALUES (?, ?, ?, ?) RETURNING id",
        (tenant_name, whatsapp_phone_number_id, access_token, app_secret),
    )
    tenant_id = cur.fetchone()["id"]
    conn.commit()
    return tenant_id


def seed_test_tenant(
    conn,
    tenant_name: str = "Test Tenant 2",
    whatsapp_phone_number_id: str = "TEST_TENANT_2_PHONE_ID",
) -> int:
    """A second tenant purely for multi-tenant isolation tests -- a fake
    phone_number_id, no real Meta credentials, never wired to an actual number."""
    row = conn.execute("SELECT id FROM tenants WHERE name = ?", (tenant_name,)).fetchone()
    if row:
        return row["id"]

    cur = conn.execute(
        "INSERT INTO tenants (name, whatsapp_phone_number_id) VALUES (?, ?) RETURNING id",
        (tenant_name, whatsapp_phone_number_id),
    )
    tenant_id = cur.fetchone()["id"]
    conn.commit()
    return tenant_id
