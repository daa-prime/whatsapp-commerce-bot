# db/repository.py
"""
The single data-access surface core/main.py talks to. No raw SQL anywhere
outside this file. Every function that reads/writes tenant-scoped data takes
tenant_id and filters by it (SPEC.md Section 4/8: "no query should ever return
or write data without this filter" -- carried over from the hospital repo's
hospital_id scoping rule).

Phase 0 (SPEC.md Section 8): this module only has the multi-tenant routing
surface (tenants) left after stripping the hospital product's departments/
doctors/doctor_slots/appointments/appointment_reminders logic. `products`/
`orders`/`order_items` (SPEC.md Section 4) are Phase 5 work.
"""
import json as json_lib
from dataclasses import dataclass

from db.connection import get_connection


@dataclass
class Tenant:
    id: int
    name: str
    whatsapp_phone_number_id: str | None
    access_token: str | None  # DB column: meta_access_token_ref
    app_secret: str | None  # DB column: app_secret_ref
    meta_catalog_id: str | None
    payment_gateway_provider: str | None
    payment_gateway_api_key_ref: str | None
    welcome_message_text: str | None
    timezone: str
    is_active: bool
    data_tier: str
    external_api_base_url: str | None
    external_api_key: str | None


def _row_to_tenant(row) -> Tenant:
    return Tenant(
        id=row["id"],
        name=row["name"],
        whatsapp_phone_number_id=row["whatsapp_phone_number_id"],
        access_token=row["meta_access_token_ref"],
        app_secret=row["app_secret_ref"],
        meta_catalog_id=row["meta_catalog_id"],
        payment_gateway_provider=row["payment_gateway_provider"],
        payment_gateway_api_key_ref=row["payment_gateway_api_key_ref"],
        welcome_message_text=row["welcome_message_text"],
        timezone=row["timezone"],
        is_active=bool(row["is_active"]),
        data_tier=row["data_tier"],
        external_api_base_url=row["external_api_base_url"],
        external_api_key=row["external_api_key"],
    )


def find_tenant_by_phone_number_id(phone_number_id: str) -> Tenant | None:
    """The entry point for per-message multi-tenant routing: given the
    phone_number_id from an incoming webhook's `metadata`, resolve which
    tenant received it. Only active tenants are matched -- a deactivated
    tenant's number should be treated the same as an unrecognized one."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM tenants WHERE whatsapp_phone_number_id = ? AND is_active = 1",
        (phone_number_id,),
    ).fetchone()
    return _row_to_tenant(row) if row else None


def get_active_tenants() -> list[Tenant]:
    """Used by core/main.py's /internal/* endpoints to loop over every active
    tenant rather than a single startup-resolved one."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM tenants WHERE is_active = 1 ORDER BY id").fetchall()
    return [_row_to_tenant(r) for r in rows]


def get_tenant(tenant_id: int) -> Tenant | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return _row_to_tenant(row) if row else None


def create_tenant(
    name: str,
    whatsapp_phone_number_id: str,
    access_token: str | None = None,
    app_secret: str | None = None,
    welcome_message_text: str | None = None,
    timezone: str = "Asia/Kolkata",
    meta_catalog_id: str | None = None,
    payment_gateway_provider: str | None = None,
    payment_gateway_api_key_ref: str | None = None,
    data_tier: str = "tier1",
    external_api_base_url: str | None = None,
    external_api_key: str | None = None,
) -> Tenant:
    """Onboarding wizard's entry point. Raises db.connection.IntegrityError if
    whatsapp_phone_number_id is already used by another tenant -- db/schema.sql's
    UNIQUE constraint is the actual guard against breaking per-message routing,
    not application logic; callers (admin/onboarding.py) are responsible for
    catching it.

    timezone: IANA name for displaying order/invoice timestamps in the
    tenant's local time (SPEC.md Section 3.4) and Phase 6's abandoned-cart
    timing logic. Defaults to Asia/Kolkata (Razorpay targets Indian
    businesses, SPEC.md Section 3.3) if the onboarding wizard doesn't override it.

    data_tier: "tier1" (default, this product's own database), "tier2"
    (external_api_base_url/external_api_key are only stored here -- no
    connector logic reads them yet), or "tier3" (direct DB connection, not
    self-serve -- neither field is meaningful for it)."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO tenants (name, whatsapp_phone_number_id, meta_access_token_ref, app_secret_ref, "
        "welcome_message_text, timezone, meta_catalog_id, payment_gateway_provider, payment_gateway_api_key_ref, "
        "data_tier, external_api_base_url, external_api_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (name, whatsapp_phone_number_id, access_token, app_secret, welcome_message_text, timezone,
         meta_catalog_id, payment_gateway_provider, payment_gateway_api_key_ref,
         data_tier, external_api_base_url, external_api_key),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return get_tenant(new_id)
