# tests/test_admin_portal_password.py
"""
Admin-only set/reset of a tenant's merchant-portal login password
(GET/POST /admin/tenant/{id}/portal-password, admin/onboarding.py) -- the
only way a tenant gets a portal password, since merchant self-serve signup
is out of scope. Login itself is tested in tests/test_portal_auth.py.
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
from portal.session import verify_password  # noqa: E402

client = TestClient(app)


def test_portal_password_form_renders(tenant_id):
    resp = client.get(f"/admin/tenant/{tenant_id}/portal-password")
    assert resp.status_code == 200
    assert "not set" in resp.text.lower()


def test_portal_password_form_404s_for_unknown_tenant():
    resp = client.get("/admin/tenant/999999/portal-password")
    assert resp.status_code == 404


def test_admin_sets_portal_password(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/portal-password", data={
        "admin_secret": "test-admin-secret",
        "new_password": "correct-horse-battery",
        "confirm_password": "correct-horse-battery",
    })
    assert resp.status_code == 200
    assert "updated" in resp.text.lower()

    tenant = db.get_tenant(tenant_id)
    assert tenant.portal_password_hash is not None
    assert verify_password("correct-horse-battery", tenant.portal_password_hash)
    assert not verify_password("wrong-password", tenant.portal_password_hash)


def test_wrong_admin_secret_rejected(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/portal-password", data={
        "admin_secret": "wrong-secret",
        "new_password": "correct-horse-battery",
        "confirm_password": "correct-horse-battery",
    })
    assert resp.status_code == 403
    assert db.get_tenant(tenant_id).portal_password_hash is None


def test_password_too_short_rejected(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/portal-password", data={
        "admin_secret": "test-admin-secret",
        "new_password": "short",
        "confirm_password": "short",
    })
    assert resp.status_code == 400
    assert db.get_tenant(tenant_id).portal_password_hash is None


def test_mismatched_confirmation_rejected(tenant_id):
    resp = client.post(f"/admin/tenant/{tenant_id}/portal-password", data={
        "admin_secret": "test-admin-secret",
        "new_password": "correct-horse-battery",
        "confirm_password": "does-not-match",
    })
    assert resp.status_code == 400
    assert db.get_tenant(tenant_id).portal_password_hash is None


def test_resetting_replaces_previous_password(tenant_id):
    client.post(f"/admin/tenant/{tenant_id}/portal-password", data={
        "admin_secret": "test-admin-secret",
        "new_password": "first-password-here",
        "confirm_password": "first-password-here",
    })
    client.post(f"/admin/tenant/{tenant_id}/portal-password", data={
        "admin_secret": "test-admin-secret",
        "new_password": "second-password-here",
        "confirm_password": "second-password-here",
    })

    tenant = db.get_tenant(tenant_id)
    assert not verify_password("first-password-here", tenant.portal_password_hash)
    assert verify_password("second-password-here", tenant.portal_password_hash)


def test_cross_tenant_isolation(tenant_id, second_tenant_id):
    client.post(f"/admin/tenant/{tenant_id}/portal-password", data={
        "admin_secret": "test-admin-secret",
        "new_password": "tenant-a-password",
        "confirm_password": "tenant-a-password",
    })

    other = db.get_tenant(second_tenant_id)
    assert other.portal_password_hash is None
