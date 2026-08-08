"""
db/repository.py unit tests. Phase 0: the hospital product's department/
doctor/slot/appointment/reminder tests are gone along with that domain
logic — what's left is the multi-tenant routing surface (tenants) plus the
schema-init idempotency check. Product/order/order_item tests (SPEC.md
Section 4) land in Phase 5, once that data model exists.
"""
import db.repository as db
from db.connection import IntegrityError, get_connection
from db.init_db import init_db_on_connection


def test_create_tenant_and_find_by_phone_number_id(tenant_id):
    tenant = db.create_tenant(name="New Business", whatsapp_phone_number_id="NEW_BIZ_PHONE_ID")
    found = db.find_tenant_by_phone_number_id("NEW_BIZ_PHONE_ID")
    assert found is not None
    assert found.id == tenant.id
    assert found.name == "New Business"


def test_find_tenant_by_phone_number_id_not_found(tenant_id):
    assert db.find_tenant_by_phone_number_id("NO_SUCH_NUMBER") is None


def test_create_tenant_duplicate_phone_number_id_raises_integrity_error(tenant_id):
    db.create_tenant(name="Biz A", whatsapp_phone_number_id="DUPLICATE_ID")
    try:
        db.create_tenant(name="Biz B", whatsapp_phone_number_id="DUPLICATE_ID")
        assert False, "expected IntegrityError"
    except IntegrityError:
        pass


def test_get_active_tenants_excludes_inactive(tenant_id):
    conn = get_connection()
    inactive = db.create_tenant(name="Inactive Biz", whatsapp_phone_number_id="INACTIVE_ID")
    conn.execute("UPDATE tenants SET is_active = 0 WHERE id = ?", (inactive.id,))
    conn.commit()

    active_ids = {t.id for t in db.get_active_tenants()}
    assert tenant_id in active_ids
    assert inactive.id not in active_ids


def test_get_tenant_found_and_not_found(tenant_id):
    tenant = db.get_tenant(tenant_id)
    assert tenant is not None
    assert tenant.id == tenant_id
    assert db.get_tenant(-1) is None


def test_create_tenant_defaults_to_asia_kolkata_timezone(tenant_id):
    """Timezone drives order/invoice timestamp display and Phase 6's
    abandoned-cart timing — Asia/Kolkata is the sensible default since
    Razorpay (SPEC.md Section 3.3) targets Indian businesses."""
    tenant = db.create_tenant(name="Default TZ Biz", whatsapp_phone_number_id="DEFAULT_TZ_ID")
    assert tenant.timezone == "Asia/Kolkata"


def test_create_tenant_timezone_can_be_overridden(tenant_id):
    tenant = db.create_tenant(
        name="Custom TZ Biz", whatsapp_phone_number_id="CUSTOM_TZ_ID", timezone="America/New_York",
    )
    assert tenant.timezone == "America/New_York"


def test_init_db_on_connection_is_idempotent(tenant_id):
    conn = get_connection()
    # Re-running against an already-initialized connection must not error or
    # duplicate the seeded tenant row.
    tenant_id_again = init_db_on_connection(conn)
    assert tenant_id_again == tenant_id
