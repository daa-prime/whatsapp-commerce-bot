# tests/test_onboarding_wizard.py
"""
The guided onboarding wizard (admin/onboarding_wizard.py) -- replaces the
old flat form at the same URL (/admin/onboard-tenant). Proves a new tenant
+ its first products can be created end-to-end through the wizard's single
final POST, for both the manual product-entry path and the CSV-import path,
and that the newly created tenant immediately works with per-message
routing (same proof the old flat-form tests carried over from).

Note on "review screen accurately reflects entered data": the review step
is client-side JS reading the same unsubmitted form fields -- TestClient
can't execute JS, so that's verified indirectly here (the POST body *is*
what the review screen would show; these tests assert the DB ends up
matching exactly what was submitted) rather than by scraping rendered HTML
from a live browser.
"""
import hashlib
import hmac
import json
import os

import db.repository as db

os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")
# ADMIN_SECRET itself is set in tests/conftest.py, before any test module
# gets a chance to trigger core.main's first import.
# DATABASE_URL is already pointed at the test Postgres instance by
# tests/conftest.py (loaded before this module).

from core.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

BASE_FORM = {
    "admin_secret": "test-admin-secret",
    "name": "St. Jude Storefront",
    "whatsapp_phone_number_id": "NEW_TENANT_PHONE_ID",
    "access_token": "new-tenant-token",
    "app_secret": "new-tenant-secret",
    "welcome_message_text": "Welcome to St. Jude Storefront!",
    "product_entry_mode": "manual",
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


def test_get_wizard_renders(tenant_id):
    resp = client.get("/admin/onboard-tenant")
    assert resp.status_code == 200
    assert "Onboard a new tenant" in resp.text


def test_full_wizard_manual_entry_creates_tenant_and_products(tenant_id, httpx_mock):
    form = dict(BASE_FORM)
    form["product_name"] = ["Widget", "Gadget"]
    form["product_price"] = ["199.00", "349.50"]
    form["product_stock_quantity"] = ["10", "5"]
    form["product_category"] = ["Tools", "Electronics"]

    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200
    assert "Tenant created" in resp.text

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant is not None
    assert tenant.name == "St. Jude Storefront"
    assert tenant.access_token == "new-tenant-token"
    assert tenant.app_secret == "new-tenant-secret"
    assert tenant.timezone == "Asia/Kolkata"  # left blank -> default
    assert tenant.abandoned_cart_nudge_hours == 2  # left blank -> default
    assert str(tenant.id) in resp.text

    products = db.get_active_products(tenant.id)
    assert len(products) == 2
    by_name = {p.name: p for p in products}
    assert by_name["Widget"].price == __import__("decimal").Decimal("199.00")
    assert by_name["Widget"].stock_quantity == 10
    assert by_name["Widget"].category == "Tools"
    assert by_name["Gadget"].price == __import__("decimal").Decimal("349.50")

    # Prove per-message routing works immediately for this brand-new tenant.
    httpx_mock.add_response(
        url="https://graph.facebook.com/v22.0/NEW_TENANT_PHONE_ID/messages",
        json={"messages": [{"id": "wamid.new"}]},
    )
    body = _webhook_body("NEW_TENANT_PHONE_ID", "5490009999", "hi")
    resp2 = client.post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": _sign(body, "new-tenant-secret"), "Content-Type": "application/json"},
    )
    assert resp2.status_code == 200
    assert len(httpx_mock.get_requests()) == 1


def test_manual_entry_skips_empty_rows_without_error(tenant_id):
    form = dict(BASE_FORM)
    form["product_name"] = ["Widget", ""]  # a never-filled-in leftover row
    form["product_price"] = ["199.00", ""]

    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert len(db.get_active_products(tenant.id)) == 1


def test_manual_entry_invalid_price_rejected(tenant_id):
    form = dict(BASE_FORM)
    form["product_name"] = ["Widget"]
    form["product_price"] = ["not-a-number"]

    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 400
    assert db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID") is None


def test_full_wizard_csv_import_creates_tenant_and_products(tenant_id):
    csv_content = (
        "name,price,description,image_url,sku,stock_quantity,category\n"
        "Widget,199.00,A fine widget,,SKU-1,10,Tools\n"
        "Gadget,349.50,,,SKU-2,5,Electronics\n"
    ).encode()
    form = dict(BASE_FORM)
    form["product_entry_mode"] = "csv"

    resp = client.post(
        "/admin/onboard-tenant", data=form,
        files={"csv_file": ("products.csv", csv_content, "text/csv")},
    )
    assert resp.status_code == 200

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    products = db.get_active_products(tenant.id)
    assert len(products) == 2
    by_name = {p.name: p for p in products}
    assert by_name["Widget"].sku == "SKU-1"
    assert by_name["Widget"].description == "A fine widget"
    assert by_name["Gadget"].category == "Electronics"


def test_csv_import_missing_required_columns_rejected(tenant_id):
    csv_content = b"foo,bar\n1,2\n"
    form = dict(BASE_FORM)
    form["product_entry_mode"] = "csv"

    resp = client.post(
        "/admin/onboard-tenant", data=form,
        files={"csv_file": ("products.csv", csv_content, "text/csv")},
    )
    assert resp.status_code == 400
    assert db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID") is None


def test_csv_import_bad_row_reports_error_creates_no_partial_tenant(tenant_id):
    csv_content = (
        "name,price,description,image_url,sku,stock_quantity,category\n"
        "Widget,not-a-price,,,SKU-1,10,Tools\n"
    ).encode()
    form = dict(BASE_FORM)
    form["product_entry_mode"] = "csv"

    resp = client.post(
        "/admin/onboard-tenant", data=form,
        files={"csv_file": ("products.csv", csv_content, "text/csv")},
    )
    assert resp.status_code == 400
    assert "not a valid number" in resp.text
    assert db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID") is None


def test_csv_import_with_no_file_creates_tenant_with_zero_products(tenant_id):
    form = dict(BASE_FORM)
    form["product_entry_mode"] = "csv"  # but no csv_file provided

    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert db.get_active_products(tenant.id) == []


def test_duplicate_phone_number_id_rejected(tenant_id):
    form = dict(BASE_FORM, whatsapp_phone_number_id="123")  # "123" is the already-seeded tenant's number
    before = db.find_tenant_by_phone_number_id("123")

    resp = client.post("/admin/onboard-tenant", data=form)

    assert resp.status_code == 400
    assert "already exists" in resp.text
    after = db.find_tenant_by_phone_number_id("123")
    assert before.id == after.id  # untouched, no duplicate/overwrite


def test_wrong_admin_secret_rejected(tenant_id):
    form = dict(BASE_FORM, admin_secret="wrong-secret")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 403
    assert db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID") is None


def test_missing_business_name_rejected(tenant_id):
    form = dict(BASE_FORM, name="")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 400
    assert "name is required" in resp.text.lower()


def test_catalog_and_payment_fields_save_correctly(tenant_id):
    form = dict(
        BASE_FORM,
        meta_catalog_id="1234567890",
        payment_gateway_provider="razorpay",
        payment_gateway_key_id="rzp_test_key",
        payment_gateway_api_key_ref="rzp_test_secret",
        payment_gateway_webhook_secret="whsec_test",
    )
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200

    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant.meta_catalog_id == "1234567890"
    assert tenant.payment_gateway_provider == "razorpay"
    assert tenant.payment_gateway_key_id == "rzp_test_key"
    assert tenant.payment_gateway_api_key_ref == "rzp_test_secret"
    assert tenant.payment_gateway_webhook_secret == "whsec_test"


def test_invalid_payment_gateway_provider_rejected(tenant_id):
    form = dict(BASE_FORM, payment_gateway_provider="stripe")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 400
    assert db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID") is None


def test_abandoned_cart_nudge_hours_can_be_set(tenant_id):
    form = dict(BASE_FORM, abandoned_cart_nudge_hours="6")
    resp = client.post("/admin/onboard-tenant", data=form)
    assert resp.status_code == 200
    tenant = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    assert tenant.abandoned_cart_nudge_hours == 6


# --- Cross-tenant isolation ---

def test_cross_tenant_isolation(tenant_id):
    """Two wizard submissions must produce fully separate tenants and
    products -- never mixed."""
    form_a = dict(BASE_FORM)
    form_a["product_name"] = ["Tenant A Widget"]
    form_a["product_price"] = ["100.00"]
    resp_a = client.post("/admin/onboard-tenant", data=form_a)
    assert resp_a.status_code == 200

    form_b = dict(BASE_FORM, whatsapp_phone_number_id="ANOTHER_NEW_TENANT_PHONE_ID", name="Another Storefront")
    form_b["product_name"] = ["Tenant B Widget"]
    form_b["product_price"] = ["200.00"]
    resp_b = client.post("/admin/onboard-tenant", data=form_b)
    assert resp_b.status_code == 200

    tenant_a = db.find_tenant_by_phone_number_id("NEW_TENANT_PHONE_ID")
    tenant_b = db.find_tenant_by_phone_number_id("ANOTHER_NEW_TENANT_PHONE_ID")
    assert tenant_a.id != tenant_b.id

    products_a = db.get_active_products(tenant_a.id)
    products_b = db.get_active_products(tenant_b.id)
    assert [p.name for p in products_a] == ["Tenant A Widget"]
    assert [p.name for p in products_b] == ["Tenant B Widget"]
