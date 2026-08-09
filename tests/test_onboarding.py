# tests/test_onboarding.py
"""
The guided onboarding wizard. Proves a new tenant can be added end-to-end
through the plain HTML form (POST /admin/onboard-tenant) without touching the
database or code directly, and that the newly created tenant immediately
works with per-message routing (a webhook for its own phone_number_id, signed
with its own app_secret, is accepted and processed).

Phase 0: the hospital product's departments-and-doctors step (Step 7) is
gone along with that domain logic — the doctor-line-DSL validation tests that
went with it are gone too. What's left is the tenant-identity/Meta-credential/
data-connection-tier surface, which is genuinely reusable infrastructure.
"""
import hashlib
import hmac
import json
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

VALID_FORM = {
    "admin_secret": "test-admin-secret",
    "name": "St. Jude Storefront",
    "whatsapp_phone_number_id": "NEW_TENANT_PHONE_ID",
    "access_token": "new-tenant-token",
    "app_secret": "new-tenant-secret",
    "welcome_message_text": "Welcome to St. Jude Storefront!",
    "data_tier": "tier1",
}


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_body(phone_number_id: str, from_phone: str, text: str) -> bytes:
    return json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [{"from": from_phone, "type": "text", "text": {"body": text}}],
        }}]}]
    }).encode()


def test_get_form_renders(tenant_id):
    resp = client.get("/admin/onboard-tenant")
    assert resp.status_code == 200
    assert "Onboard a new tenant" in resp.text


def test_successful_onboarding_creates_real_row_and_routing_works(tenant_id, httpx_mock):
    resp = client.post("/admin/onboard-tenant", data=VALID_FORM)
    assert resp.status_code == 200
    assert "Tenant created" in resp.text
    assert "St. Jude Storefront" in resp.text

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant is not None
    assert tenant.name == "St. Jude Storefront"
    assert tenant.access_token == "new-tenant-token"
    assert tenant.app_secret == "new-tenant-secret"
    assert tenant.timezone == "Asia/Kolkata"  # VALID_FORM leaves timezone blank -> sensible default
    assert str(tenant.id) in resp.text

    # Prove per-message routing actually works for this brand-new tenant: a
    # webhook signed with ITS OWN app_secret, for ITS OWN phone_number_id, must
    # be accepted and processed (not silently ignored).
    httpx_mock.add_response(
        url="https://graph.facebook.com/v22.0/NEW_TENANT_PHONE_ID/messages",
        json={"messages": [{"id": "wamid.new"}]},
    )
    body = _webhook_body("NEW_TENANT_PHONE_ID", "5490009999", "hi")
    resp2 = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, "new-tenant-secret"), "Content-Type": "application/json"},
    )
    assert resp2.status_code == 200
    assert len(httpx_mock.get_requests()) == 1  # the new tenant's own token/number were actually used


def test_duplicate_phone_number_id_rejected(tenant_id):
    form = dict(VALID_FORM, whatsapp_phone_number_id="123")  # "123" is the already-seeded tenant's number
    before = db.find_tenant_by_phone_number_id("123")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 400
    assert "already exists" in resp.text

    after = db.find_tenant_by_phone_number_id("123")
    assert before.id == after.id  # untouched, no duplicate/overwrite


def test_timezone_can_be_overridden_at_onboarding(tenant_id):
    form = dict(VALID_FORM, timezone="America/New_York")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant.timezone == "America/New_York"


def test_tier2_fields_save_correctly(tenant_id):
    form = dict(VALID_FORM, data_tier="tier2", api_base_url="https://erp.stjude.example/api", api_key="tier2-secret-key")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200
    assert "Tier 2" in resp.text or "tier2" in resp.text.lower()

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant.data_tier == "tier2"
    assert tenant.external_api_base_url == "https://erp.stjude.example/api"
    assert tenant.external_api_key == "tier2-secret-key"


def test_tier2_without_api_fields_rejected(tenant_id):
    form = dict(VALID_FORM, data_tier="tier2", api_base_url="", api_key="")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 400
    assert db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID") is None


def test_tier3_shows_contact_us_message_with_no_fields_required(tenant_id):
    """Tier 3 (direct DB connection) is explicitly not self-serve -- selecting
    it must succeed with zero API fields submitted, and the confirmation page
    must make clear this is a manually-assisted follow-up, not something the
    wizard itself completed."""
    form = dict(VALID_FORM, data_tier="tier3", api_base_url="", api_key="")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200
    assert "manually-assisted" in resp.text.lower() or "contact" in resp.text.lower() or "we'll be in touch" in resp.text.lower()

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant.data_tier == "tier3"
    assert tenant.external_api_base_url is None
    assert tenant.external_api_key is None


def test_missing_tenant_name_rejected(tenant_id):
    form = dict(VALID_FORM, name="")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 400
    assert "name is required" in resp.text.lower()


def test_wrong_admin_secret_rejected(tenant_id):
    form = dict(VALID_FORM, admin_secret="wrong-secret")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 403
    assert db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID") is None


def test_missing_admin_secret_rejected(tenant_id):
    form = dict(VALID_FORM)
    del form["admin_secret"]
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 403


# --- Catalog / payment gateway fields (SPEC.md Section 7, Phase 7) ---

def test_catalog_and_payment_fields_save_correctly_at_create(tenant_id):
    form = dict(
        VALID_FORM,
        meta_catalog_id="1234567890",
        payment_gateway_provider="razorpay",
        payment_gateway_api_key_ref="rzp_test_abc123",
    )
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant.meta_catalog_id == "1234567890"
    assert tenant.payment_gateway_provider == "razorpay"
    assert tenant.payment_gateway_api_key_ref == "rzp_test_abc123"


def test_catalog_and_payment_fields_optional_at_create(tenant_id):
    """A tenant frequently won't have catalog/payment set up yet at initial
    onboarding -- leaving these blank must succeed, not be treated as a
    validation error."""
    resp = client.post("/admin/onboard-tenant", data=VALID_FORM)  # VALID_FORM has none of these fields
    assert resp.status_code == 200

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant.meta_catalog_id is None
    assert tenant.payment_gateway_provider is None
    assert tenant.payment_gateway_api_key_ref is None


def test_invalid_payment_gateway_provider_rejected_at_create(tenant_id):
    form = dict(VALID_FORM, payment_gateway_provider="stripe")  # not a supported provider yet
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 400
    assert db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID") is None


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
