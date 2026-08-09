# tests/test_onboarding.py
"""
The catalog/payment-gateway edit step (POST/GET /admin/tenant/{id}/catalog-payment)
-- lets an operator add or update a tenant's catalog/payment/abandoned-cart-nudge
settings after initial creation, without re-running the whole onboarding flow.

Tenant *creation* itself moved to the guided step-rail wizard
(admin/onboarding_wizard.py, tests/test_onboarding_wizard.py) -- the old flat
create-form tests that used to live here were migrated there, since the old
/admin/onboard-tenant flat form no longer exists at that URL.
"""
import os

import db.repository as db

# Same defensive env-var setup as tests/test_main.py and tests/test_multi_tenant.py
# -- core.main (and admin.onboarding, which it imports) is only actually
# imported -- and its module-level os.environ[...] reads executed -- the first
# time any test file does so, so this must be safe regardless of which test
# module pytest happens to collect/import first.
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")
# ADMIN_SECRET itself is set in tests/conftest.py, before any test module (this
# one included) gets a chance to trigger core.main's first import -- see the
# comment there for why it can't safely be set here instead.
# DATABASE_URL is already pointed at the test Postgres instance by
# tests/conftest.py (loaded before this module).

from core.main import app  # noqa: E402  (must follow the env var setdefaults above)
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


# --- Catalog / payment gateway fields (SPEC.md Section 7, Phase 7) ---

def test_catalog_payment_edit_form_renders_with_current_values(tenant_id):
    resp = client.get(f"/admin/tenant/{tenant_id}/catalog-payment")
    assert resp.status_code == 200
    assert "not set" in resp.text.lower()  # nothing set yet on the seeded tenant


def test_catalog_payment_edit_form_404s_for_unknown_tenant():
    resp = client.get("/admin/tenant/999999/catalog-payment")
    assert resp.status_code == 404


def test_catalog_payment_edit_saves_new_values(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "meta_catalog_id": "999888777",
        "payment_gateway_provider": "razorpay",
        "payment_gateway_api_key_ref": "rzp_live_xyz",
    })
    assert resp.status_code == 200
    assert "updated" in resp.text.lower()

    tenant = db.get_tenant(tenant_id)
    assert tenant.meta_catalog_id == "999888777"
    assert tenant.payment_gateway_provider == "razorpay"
    assert tenant.payment_gateway_api_key_ref == "rzp_live_xyz"


def test_catalog_payment_edit_blank_fields_do_not_overwrite_existing_values(tenant_id):
    """The core correctness requirement: submitting the edit form with fields
    left blank (e.g. because only one of the two is being updated right now)
    must never clobber an already-set value with NULL."""
    client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "meta_catalog_id": "111222333",
        "payment_gateway_provider": "razorpay",
        "payment_gateway_api_key_ref": "rzp_original_key",
    })

    # Second edit only updates the catalog ID, leaving payment fields blank.
    resp = client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "meta_catalog_id": "444555666",
        "payment_gateway_provider": "",
        "payment_gateway_api_key_ref": "",
    })
    assert resp.status_code == 200

    tenant = db.get_tenant(tenant_id)
    assert tenant.meta_catalog_id == "444555666"  # updated
    assert tenant.payment_gateway_provider == "razorpay"  # preserved, not blanked
    assert tenant.payment_gateway_api_key_ref == "rzp_original_key"  # preserved, not blanked


def test_catalog_payment_edit_all_blank_is_a_no_op(tenant_id):
    client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "meta_catalog_id": "keep-me",
        "payment_gateway_provider": "razorpay",
        "payment_gateway_api_key_ref": "keep-this-key",
    })

    resp = client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "meta_catalog_id": "",
        "payment_gateway_provider": "",
        "payment_gateway_api_key_ref": "",
    })
    assert resp.status_code == 200

    tenant = db.get_tenant(tenant_id)
    assert tenant.meta_catalog_id == "keep-me"
    assert tenant.payment_gateway_provider == "razorpay"
    assert tenant.payment_gateway_api_key_ref == "keep-this-key"


def test_catalog_payment_edit_wrong_admin_secret_rejected(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "wrong-secret",
        "meta_catalog_id": "should-not-save",
    })
    assert resp.status_code == 403
    assert db.get_tenant(tenant_id).meta_catalog_id is None


def test_catalog_payment_edit_invalid_provider_rejected(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "payment_gateway_provider": "stripe",
    })
    assert resp.status_code == 400
    assert db.get_tenant(tenant_id).payment_gateway_provider is None


def test_catalog_payment_edit_isolated_across_tenants(tenant_id, second_tenant_id):
    """Editing one tenant's catalog/payment fields must never touch another
    tenant's row."""
    client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "meta_catalog_id": "tenant-a-catalog",
        "payment_gateway_provider": "razorpay",
        "payment_gateway_api_key_ref": "tenant-a-key",
    })

    other = db.get_tenant(second_tenant_id)
    assert other.meta_catalog_id is None
    assert other.payment_gateway_provider is None
    assert other.payment_gateway_api_key_ref is None

    client.post(f"/admin/tenant/{second_tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "meta_catalog_id": "tenant-b-catalog",
        "payment_gateway_provider": "razorpay",
        "payment_gateway_api_key_ref": "tenant-b-key",
    })

    mine = db.get_tenant(tenant_id)
    theirs = db.get_tenant(second_tenant_id)
    assert mine.meta_catalog_id == "tenant-a-catalog"
    assert mine.payment_gateway_api_key_ref == "tenant-a-key"
    assert theirs.meta_catalog_id == "tenant-b-catalog"
    assert theirs.payment_gateway_api_key_ref == "tenant-b-key"


# --- Abandoned cart nudge hours (SPEC.md Phase 6) ---

def test_abandoned_cart_nudge_hours_editable_via_catalog_payment_form(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "abandoned_cart_nudge_hours": "8",
    })
    assert resp.status_code == 200
    assert db.get_tenant(tenant_id).abandoned_cart_nudge_hours == 8


def test_abandoned_cart_nudge_hours_blank_keeps_current_value_on_edit(tenant_id):
    client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "abandoned_cart_nudge_hours": "8",
    })

    resp = client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "meta_catalog_id": "some-catalog-id",  # only updating this field this time
    })

    assert resp.status_code == 200
    assert db.get_tenant(tenant_id).abandoned_cart_nudge_hours == 8  # preserved, not reset to blank/default


def test_abandoned_cart_nudge_hours_invalid_value_rejected_on_edit(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/catalog-payment", data={
        "admin_secret": "test-admin-secret",
        "abandoned_cart_nudge_hours": "-3",
    })
    assert resp.status_code == 400
    assert db.get_tenant(tenant_id).abandoned_cart_nudge_hours == 2  # untouched, still the default
