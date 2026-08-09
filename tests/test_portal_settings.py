# tests/test_portal_settings.py
"""
Merchant portal settings (portal/settings.py): catalog/payment/nudge-hours
fields (thin coverage -- reuses db.update_tenant_catalog_and_payment
directly, already covered in depth by tests/test_onboarding.py's edit-step
tests) and changing the portal login password.
"""
import os

import db.repository as db

os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")

from core.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from portal.session import hash_password, verify_password  # noqa: E402


def _login(tenant_id: int, phone_number_id: str = "123", password: str = "secret123") -> TestClient:
    db.set_tenant_portal_password_hash(tenant_id, hash_password(password))
    client = TestClient(app)
    client.post("/portal/login", data={"whatsapp_phone_number_id": phone_number_id, "password": password})
    return client


def test_settings_requires_login(tenant_id):
    client = TestClient(app)
    resp = client.get("/portal/settings", follow_redirects=False)
    assert resp.status_code == 303


def test_catalog_and_payment_fields_update(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/settings", data={
        "meta_catalog_id": "123456",
        "payment_gateway_provider": "razorpay",
        "payment_gateway_key_id": "rzp_key",
        "payment_gateway_api_key_ref": "rzp_secret",
        "payment_gateway_webhook_secret": "whsec",
        "abandoned_cart_nudge_hours": "5",
    })
    assert resp.status_code == 200
    assert "saved" in resp.text.lower()

    tenant = db.get_tenant(tenant_id)
    assert tenant.meta_catalog_id == "123456"
    assert tenant.payment_gateway_provider == "razorpay"
    assert tenant.abandoned_cart_nudge_hours == 5


def test_invalid_payment_provider_rejected(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/settings", data={"payment_gateway_provider": "stripe"})
    assert resp.status_code == 400
    assert db.get_tenant(tenant_id).payment_gateway_provider is None


def test_blank_fields_keep_current_values(tenant_id):
    client = _login(tenant_id)
    client.post("/portal/settings", data={"meta_catalog_id": "keep-me", "payment_gateway_provider": "razorpay"})
    resp = client.post("/portal/settings", data={"meta_catalog_id": ""})
    assert resp.status_code == 200

    tenant = db.get_tenant(tenant_id)
    assert tenant.meta_catalog_id == "keep-me"
    assert tenant.payment_gateway_provider == "razorpay"


def test_change_password_wrong_current_rejected(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/settings/password", data={
        "current_password": "wrong-current",
        "new_password": "brand-new-password",
        "confirm_password": "brand-new-password",
    })
    assert resp.status_code == 400

    tenant = db.get_tenant(tenant_id)
    assert verify_password("secret123", tenant.portal_password_hash)  # unchanged


def test_change_password_succeeds_and_takes_effect_on_next_login(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/settings/password", data={
        "current_password": "secret123",
        "new_password": "brand-new-password",
        "confirm_password": "brand-new-password",
    })
    assert resp.status_code == 200
    assert "updated" in resp.text.lower()

    fresh_client = TestClient(app)
    old_login = fresh_client.post("/portal/login", data={"whatsapp_phone_number_id": "123", "password": "secret123"})
    assert old_login.status_code == 401

    new_login = fresh_client.post("/portal/login", data={"whatsapp_phone_number_id": "123", "password": "brand-new-password"})
    assert new_login.status_code == 200
