# tests/test_portal_coupons.py
"""
Merchant portal coupon management (portal/coupons.py): list, create, and
deactivate coupon codes.
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
from portal.session import hash_password  # noqa: E402


def _login(tenant_id: int, phone_number_id: str = "123", password: str = "secret123") -> TestClient:
    db.set_tenant_portal_password_hash(tenant_id, hash_password(password))
    client = TestClient(app)
    client.post("/portal/login", data={"whatsapp_phone_number_id": phone_number_id, "password": password})
    return client


def test_coupons_list_requires_login(tenant_id):
    client = TestClient(app)
    resp = client.get("/portal/coupons", follow_redirects=False)
    assert resp.status_code == 303


def test_create_percentage_coupon(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/coupons/new", data={
        "code": "save10", "discount_type": "percentage", "discount_value": "10",
    })
    assert resp.status_code == 200  # follows the redirect to the coupons list

    coupon = db.get_coupon_by_code(tenant_id, "SAVE10")
    assert coupon is not None
    assert coupon.discount_type == "percentage"
    assert coupon.is_active is True


def test_create_flat_coupon_with_expiry(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/coupons/new", data={
        "code": "FLAT50", "discount_type": "flat", "discount_value": "50", "expires_at": "2030-01-01",
    })
    assert resp.status_code == 200  # follows the redirect to the coupons list

    coupon = db.get_coupon_by_code(tenant_id, "FLAT50")
    assert coupon.discount_type == "flat"
    assert coupon.expires_at == "2030-01-01"


def test_create_coupon_rejects_invalid_discount_value(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/coupons/new", data={
        "code": "BAD", "discount_type": "percentage", "discount_value": "not-a-number",
    })
    assert resp.status_code == 400
    assert db.get_coupon_by_code(tenant_id, "BAD") is None


def test_create_coupon_rejects_percentage_over_100(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/coupons/new", data={
        "code": "TOOMUCH", "discount_type": "percentage", "discount_value": "150",
    })
    assert resp.status_code == 400
    assert db.get_coupon_by_code(tenant_id, "TOOMUCH") is None


def test_create_coupon_rejects_duplicate_code(tenant_id):
    db.create_coupon(tenant_id, "SAVE10", db.COUPON_TYPE_PERCENTAGE, 10)
    client = _login(tenant_id)
    resp = client.post("/portal/coupons/new", data={
        "code": "save10", "discount_type": "flat", "discount_value": "5",
    })
    assert resp.status_code == 400
    assert "already exists" in resp.text


def test_coupons_list_shows_created_coupons(tenant_id):
    db.create_coupon(tenant_id, "SAVE10", db.COUPON_TYPE_PERCENTAGE, 10)
    client = _login(tenant_id)
    resp = client.get("/portal/coupons")
    assert resp.status_code == 200
    assert "SAVE10" in resp.text


def test_toggle_active_deactivates_and_reactivates(tenant_id):
    coupon = db.create_coupon(tenant_id, "SAVE10", db.COUPON_TYPE_PERCENTAGE, 10)
    client = _login(tenant_id)

    client.post(f"/portal/coupons/{coupon.id}/toggle-active")
    assert db.get_coupon(tenant_id, coupon.id).is_active is False

    client.post(f"/portal/coupons/{coupon.id}/toggle-active")
    assert db.get_coupon(tenant_id, coupon.id).is_active is True


def test_toggle_active_404s_for_unknown_coupon(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/coupons/999999/toggle-active")
    assert resp.status_code == 404


def test_cross_tenant_isolation(tenant_id, second_tenant_id):
    mine = db.create_coupon(tenant_id, "MINE10", db.COUPON_TYPE_PERCENTAGE, 10)
    theirs = db.create_coupon(second_tenant_id, "THEIRS10", db.COUPON_TYPE_PERCENTAGE, 10)

    client_a = _login(tenant_id)
    list_resp = client_a.get("/portal/coupons")
    assert "MINE10" in list_resp.text
    assert "THEIRS10" not in list_resp.text

    toggle_resp = client_a.post(f"/portal/coupons/{theirs.id}/toggle-active")
    assert toggle_resp.status_code == 404
    assert db.get_coupon(second_tenant_id, theirs.id).is_active is True  # untouched
