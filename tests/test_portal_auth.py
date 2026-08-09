# tests/test_portal_auth.py
"""
Merchant portal login/session (portal/session.py, portal/auth.py). No
merchant self-serve signup -- these tests set a tenant's portal password
directly via db.set_tenant_portal_password_hash (what the admin-only
/admin/tenant/{id}/portal-password page also calls, tested separately in
tests/test_admin_portal_password.py), then exercise the login/session flow.

Each test builds its own TestClient (rather than a shared module-level one)
so a session cookie from one test can never leak into another -- important
here specifically because tenant ids are SERIAL and reset to the same
values every test (tests/conftest.py's DROP/CREATE SCHEMA), so a stale
cookie from a previous test could otherwise still "work" by accident.
"""
import os
import time

import db.repository as db

os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")

from core.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from portal.session import SESSION_COOKIE_NAME, _sign, hash_password  # noqa: E402


def _set_password(tenant_id: int, password: str = "secret123") -> None:
    db.set_tenant_portal_password_hash(tenant_id, hash_password(password))


def test_login_success_sets_cookie_and_reaches_dashboard(tenant_id):
    _set_password(tenant_id)
    client = TestClient(app)
    resp = client.post(
        "/portal/login",
        data={"whatsapp_phone_number_id": "123", "password": "secret123"},
    )
    assert resp.status_code == 200  # TestClient follows the 303 redirect
    assert "Dashboard" in resp.text
    assert SESSION_COOKIE_NAME in client.cookies


def test_wrong_password_rejected(tenant_id):
    _set_password(tenant_id)
    client = TestClient(app)
    resp = client.post(
        "/portal/login",
        data={"whatsapp_phone_number_id": "123", "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert SESSION_COOKIE_NAME not in client.cookies


def test_login_rejected_when_no_password_set_yet(tenant_id):
    client = TestClient(app)
    resp = client.post(
        "/portal/login",
        data={"whatsapp_phone_number_id": "123", "password": "anything"},
    )
    assert resp.status_code == 401
    assert SESSION_COOKIE_NAME not in client.cookies


def test_login_rejected_for_unknown_phone_number_id(tenant_id):
    client = TestClient(app)
    resp = client.post(
        "/portal/login",
        data={"whatsapp_phone_number_id": "does-not-exist", "password": "anything"},
    )
    assert resp.status_code == 401


def test_logout_clears_cookie_and_locks_out_dashboard(tenant_id):
    _set_password(tenant_id)
    client = TestClient(app)
    client.post("/portal/login", data={"whatsapp_phone_number_id": "123", "password": "secret123"})
    assert SESSION_COOKIE_NAME in client.cookies

    client.post("/portal/logout")
    assert SESSION_COOKIE_NAME not in client.cookies

    resp = client.get("/portal/dashboard")
    assert resp.status_code == 200
    assert "Merchant login" in resp.text  # followed the redirect to /portal/login


def test_dashboard_without_session_redirects_to_login(tenant_id):
    client = TestClient(app)
    resp = client.get("/portal/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/login"


def test_tampered_cookie_rejected(tenant_id):
    _set_password(tenant_id)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "1:9999999999.deadbeef" + "0" * 58)
    resp = client.get("/portal/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/login"


def test_expired_cookie_rejected(tenant_id):
    _set_password(tenant_id)
    client = TestClient(app)
    expired_payload = f"{tenant_id}:{int(time.time()) - 10}"
    expired_cookie = f"{expired_payload}.{_sign(expired_payload)}"
    client.cookies.set(SESSION_COOKIE_NAME, expired_cookie)
    resp = client.get("/portal/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/login"


def test_login_page_never_leaks_which_field_was_wrong(tenant_id):
    """Same generic error for 'no such tenant' and 'wrong password' -- proves
    the response text doesn't distinguish them."""
    _set_password(tenant_id)
    client = TestClient(app)
    unknown_resp = client.post("/portal/login", data={"whatsapp_phone_number_id": "nope", "password": "x"})
    wrong_pw_resp = client.post("/portal/login", data={"whatsapp_phone_number_id": "123", "password": "wrong"})

    def _error_text(resp):
        start = resp.text.index("<strong>")
        end = resp.text.index("</strong>")
        return resp.text[start:end]

    assert _error_text(unknown_resp) == _error_text(wrong_pw_resp)
