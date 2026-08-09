# tests/test_multi_tenant.py
"""
Multi-tenant routing isolation.

tenant_id/second_tenant_id fixtures come from tests/conftest.py — every test
here gets a fresh DB with both the one "real" tenant (whatsapp_phone_number_id
"123" by default) and a second, entirely fake one (whatsapp_phone_number_id
"TEST_TENANT_2_PHONE_ID") already seeded.

Phase 0: the hospital product's department/appointment isolation tests are
gone along with that domain logic. What's left here is the tenant-routing
plumbing (signature validation, unrecognized/deactivated numbers, per-tenant
session isolation) that's genuinely reusable infrastructure — plus tests for
the /internal/send-abandoned-cart-nudges endpoint that wires reminders/
scheduler.py's real logic (SPEC.md Phase 6) in per active tenant. The
scheduler's own nudge logic (thresholds, idempotency, message content) is
covered directly in tests/test_reminders.py; these just prove the endpoint
loops every active tenant with its own credentials.
"""
import hashlib
import hmac
import json
import os
from decimal import Decimal

import pytest

import db.repository as db
from core.history import InMemorySessionStore

# Same defensive env-var setup as tests/test_main.py — core.main is only
# actually imported (and its module-level os.environ[...] reads executed)
# the first time any test file does so, so this must be safe regardless of
# which test module pytest happens to collect/import first.
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")
# DATABASE_URL is already pointed at the test Postgres instance by
# tests/conftest.py (loaded before this module).

from core.main import app  # noqa: E402  (must follow the env var setdefaults above)
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

PHONE = "5491112345678"  # deliberately the SAME phone used against both tenants below


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()


# --- Conversation/session state never leaks across tenants ---

def test_same_phone_has_independent_conversation_state_per_tenant():
    """The session store is keyed by (tenant_id, phone) — the same customer
    mid-conversation at tenant A must not resume that state when messaging
    tenant B."""
    sessions = InMemorySessionStore()  # a single shared store, as core/main.py's SESSIONS actually is

    sessions.set(1, PHONE, "AWAITING_PAYMENT", {"order_id": 1})
    assert sessions.get(1, PHONE)["state"] == "AWAITING_PAYMENT"

    # Tenant 2's conversation for the same phone starts completely fresh.
    assert sessions.get(2, PHONE) == {"state": "IDLE", "context": {}}

    # And tenant 1's state is untouched by reading tenant 2's.
    assert sessions.get(1, PHONE)["state"] == "AWAITING_PAYMENT"


# --- Unrecognized phone_number_id ---

def test_unrecognized_phone_number_id_ignored_without_crashing(httpx_mock):
    """A webhook for a phone_number_id that doesn't match any tenant in the
    database must be safely ignored: 200 OK, no processing, no outbound send —
    not a crash, not a silent default-tenant assumption."""
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "SOME_UNKNOWN_NUMBER_NOBODY_OWNS"},
            "messages": [{"from": "919999999999", "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})

    assert resp.status_code == 200
    assert len(httpx_mock.get_requests()) == 0  # nothing was ever sent


def test_recognized_phone_number_id_is_processed_normally(httpx_mock):
    """Sanity complement to the above: a *known* phone_number_id must still work,
    proving the unrecognized case isn't just "nothing ever gets processed"."""
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "wamid.x"}]})
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123"},
            "messages": [{"from": "919999999995", "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})

    assert resp.status_code == 200
    assert len(httpx_mock.get_requests()) == 1


def test_deactivated_tenant_phone_number_id_treated_as_unrecognized(tenant_id):
    """is_active=0 must be treated the same as "no such tenant" — not routed to."""
    import db.connection as db_connection
    conn = db_connection.get_connection()
    conn.execute("UPDATE tenants SET is_active = 0 WHERE id = ?", (tenant_id,))
    conn.commit()

    assert db.find_tenant_by_phone_number_id("123") is None


# --- Per-tenant webhook signature validation ---

def _set_app_secret(tenant_id: int, secret: str) -> None:
    import db.connection as db_connection
    conn = db_connection.get_connection()
    conn.execute("UPDATE tenants SET app_secret_ref = ? WHERE id = ?", (secret, tenant_id))
    conn.commit()


def test_tenant_a_secret_cannot_validate_a_payload_claiming_to_be_tenant_b(tenant_id, second_tenant_id, httpx_mock):
    """Each tenant has its own app_secret. A payload claiming to be for tenant
    B's phone_number_id but signed with tenant A's secret must be rejected,
    even though both tenants share this one webhook endpoint."""
    _set_app_secret(second_tenant_id, "tenant-b-real-secret")

    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "TEST_TENANT_2_PHONE_ID"},  # claims to be tenant B
            "messages": [{"from": "919999999994", "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    forged_sig = _sign(body)  # _sign() = tenant A's real secret ("appsecret"), not tenant B's

    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": forged_sig, "Content-Type": "application/json"})

    assert resp.status_code == 403
    assert len(httpx_mock.get_requests()) == 0  # never got far enough to process/send anything


def test_tenant_b_own_secret_correctly_validates_its_own_payload(tenant_id, second_tenant_id, httpx_mock):
    """Sanity complement to the test above: tenant B's OWN secret correctly
    signs and passes for tenant B's own phone_number_id — proves this is
    genuine per-tenant validation, not "everything for tenant B fails"."""
    _set_app_secret(second_tenant_id, "tenant-b-real-secret")
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/TEST_TENANT_2_PHONE_ID/messages", json={"messages": [{"id": "x"}]})

    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "TEST_TENANT_2_PHONE_ID"},
            "messages": [{"from": "919999999993", "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    real_sig = "sha256=" + hmac.new(b"tenant-b-real-secret", body, hashlib.sha256).hexdigest()

    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": real_sig, "Content-Type": "application/json"})

    assert resp.status_code == 200
    assert len(httpx_mock.get_requests()) == 1


def test_tenant_with_no_app_secret_configured_rejects_all_signatures(second_tenant_id):
    """A tenant that hasn't had app_secret_ref set yet (e.g. mid-onboarding)
    must fail closed, not crash — see core/whatsapp.py:validate_webhook_signature."""
    # second_tenant_id's app_secret is NULL by default (seed_test_tenant doesn't set one).
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "TEST_TENANT_2_PHONE_ID"},
            "messages": [{"from": "919999999992", "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    sig = "sha256=" + hmac.new(b"anything", body, hashlib.sha256).hexdigest()

    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})

    assert resp.status_code == 403  # rejected cleanly, not a 500


# --- Abandoned-cart-nudges endpoint loops over active tenants ---

def _backdate_order(tenant_id, order_id, hours_ago):
    import db.connection as db_connection
    conn = db_connection.get_connection()
    conn.execute(
        "UPDATE orders SET created_at = (now() - (? * interval '1 hour'))::text WHERE tenant_id = ? AND id = ?",
        (hours_ago, tenant_id, order_id),
    )
    conn.commit()


def test_abandoned_cart_nudges_endpoint_reports_per_tenant_with_nothing_to_nudge(tenant_id, second_tenant_id):
    """No abandoned orders exist for either tenant, but the /internal/*
    cron-triggered-endpoint shape itself — looping every active tenant with
    its own client — must still work end-to-end and report 0 sent."""
    resp = client.post("/internal/send-abandoned-cart-nudges", headers={"X-Internal-Secret": "internalsecret"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] == 0
    assert body["by_tenant"]["Default Tenant"] == 0
    assert body["by_tenant"]["Test Tenant 2"] == 0


def test_abandoned_cart_nudges_endpoint_sends_real_nudges_per_tenant(tenant_id, second_tenant_id, httpx_mock):
    """End-to-end: each active tenant's own abandoned orders get nudged with
    that tenant's own WhatsApp credentials, via the real scheduler logic."""
    order_a = db.create_order(tenant_id, customer_phone="911111111111", status=db.ORDER_STATUS_PENDING_PAYMENT,
                               subtotal=Decimal("100.00"), total=Decimal("100.00"))
    _backdate_order(tenant_id, order_a.id, hours_ago=5)
    order_b = db.create_order(second_tenant_id, customer_phone="922222222222", status=db.ORDER_STATUS_PENDING_PAYMENT,
                               subtotal=Decimal("50.00"), total=Decimal("50.00"))
    _backdate_order(second_tenant_id, order_b.id, hours_ago=5)

    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "a"}]})
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/TEST_TENANT_2_PHONE_ID/messages", json={"messages": [{"id": "b"}]})

    resp = client.post("/internal/send-abandoned-cart-nudges", headers={"X-Internal-Secret": "internalsecret"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] == 2
    assert body["by_tenant"]["Default Tenant"] == 1
    assert body["by_tenant"]["Test Tenant 2"] == 1

    requests = httpx_mock.get_requests()
    urls = {str(r.url) for r in requests}
    assert "https://graph.facebook.com/v22.0/123/messages" in urls
    assert "https://graph.facebook.com/v22.0/TEST_TENANT_2_PHONE_ID/messages" in urls
    assert db.get_order(tenant_id, order_a.id).nudge_sent_at is not None
    assert db.get_order(second_tenant_id, order_b.id).nudge_sent_at is not None


def test_abandoned_cart_nudges_endpoint_requires_internal_secret():
    resp = client.post("/internal/send-abandoned-cart-nudges", headers={"X-Internal-Secret": "wrong"})
    assert resp.status_code == 403
