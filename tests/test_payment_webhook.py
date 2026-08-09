# tests/test_payment_webhook.py
"""
POST /webhook/payment -- Razorpay's callback route (SPEC.md Section 3.3,
Phase 3). Tests the full HTTP path: signature verification, tenant
resolution via reference_id, order status updates, and the WhatsApp
confirmation/retry messages sent as a result. Same style as
tests/test_multi_tenant.py's tests for the existing Meta webhook.
"""
import hashlib
import hmac
import json
import os
from decimal import Decimal

import db.repository as db

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

PHONE = "919999999999"


def _configure_razorpay(tenant_id, webhook_secret="whsec_test"):
    db.update_tenant_catalog_and_payment(
        tenant_id,
        payment_gateway_provider="razorpay",
        payment_gateway_key_id="rzp_key",
        payment_gateway_api_key_ref="rzp_secret",
        payment_gateway_webhook_secret=webhook_secret,
    )


def _make_order(tenant_id, phone=PHONE):
    return db.create_order(
        tenant_id, customer_phone=phone, status=db.ORDER_STATUS_PENDING_PAYMENT,
        subtotal=Decimal("199.00"), total=Decimal("199.00"),
    )


def _paid_payload(tenant_id, order_id, payment_id="pay_abc123"):
    return {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": "plink_xxx", "reference_id": f"{tenant_id}:{order_id}"}},
            "payment": {"entity": {"id": payment_id}},
        },
    }


def _expired_payload(tenant_id, order_id, link_id="plink_xxx"):
    return {
        "event": "payment_link.expired",
        "payload": {"payment_link": {"entity": {"id": link_id, "reference_id": f"{tenant_id}:{order_id}"}}},
    }


def _failed_payload(tenant_id, order_id, payment_id="pay_fail"):
    return {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": payment_id, "notes": {"reference_id": f"{tenant_id}:{order_id}"}}}},
    }


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_payment_success_webhook_updates_order_and_sends_confirmation(tenant_id, httpx_mock):
    _configure_razorpay(tenant_id)
    order = _make_order(tenant_id)
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "wamid.x"}]})

    body = json.dumps(_paid_payload(tenant_id, order.id)).encode()
    resp = client.post(
        "/webhook/payment", content=body,
        headers={"X-Razorpay-Signature": _sign(body, "whsec_test"), "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    updated = db.get_order(tenant_id, order.id)
    assert updated.status == db.ORDER_STATUS_PAID
    assert updated.payment_gateway_reference == "pay_abc123"
    assert updated.paid_at is not None

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    sent_body = json.loads(requests[0].content)
    assert f"Your order #{order.id} is confirmed" in sent_body["text"]["body"]


def test_invalid_signature_rejected(tenant_id):
    _configure_razorpay(tenant_id)
    order = _make_order(tenant_id)
    body = json.dumps(_paid_payload(tenant_id, order.id)).encode()

    resp = client.post(
        "/webhook/payment", content=body,
        headers={"X-Razorpay-Signature": "0" * 64, "Content-Type": "application/json"},
    )

    assert resp.status_code == 403
    assert db.get_order(tenant_id, order.id).status == db.ORDER_STATUS_PENDING_PAYMENT  # untouched


def test_missing_signature_header_rejected(tenant_id):
    _configure_razorpay(tenant_id)
    order = _make_order(tenant_id)
    body = json.dumps(_paid_payload(tenant_id, order.id)).encode()

    resp = client.post("/webhook/payment", content=body, headers={"Content-Type": "application/json"})
    assert resp.status_code == 403


def test_tenant_with_no_webhook_secret_configured_ignored_not_crashed(tenant_id):
    """A tenant that hasn't set up Razorpay's webhook secret yet must not
    crash the endpoint -- there's nothing valid to verify against, so the
    payload is safely ignored (200, no processing), same fail-safe posture
    as the Meta webhook's unrecognized-tenant case."""
    order = _make_order(tenant_id)  # no _configure_razorpay() call
    body = json.dumps(_paid_payload(tenant_id, order.id)).encode()

    resp = client.post(
        "/webhook/payment", content=body,
        headers={"X-Razorpay-Signature": "0" * 64, "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert db.get_order(tenant_id, order.id).status == db.ORDER_STATUS_PENDING_PAYMENT


def test_malformed_json_body_returns_200(tenant_id):
    resp = client.post(
        "/webhook/payment", content=b"not valid json{{{",
        headers={"X-Razorpay-Signature": "0" * 64, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200


def test_missing_reference_id_ignored(tenant_id):
    body = json.dumps({"event": "payment_link.paid", "payload": {}}).encode()
    resp = client.post(
        "/webhook/payment", content=body,
        headers={"X-Razorpay-Signature": "0" * 64, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200


def test_duplicate_webhook_delivery_does_not_double_send(tenant_id, httpx_mock):
    _configure_razorpay(tenant_id)
    order = _make_order(tenant_id)
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "a"}]})

    body = json.dumps(_paid_payload(tenant_id, order.id)).encode()
    sig = _sign(body, "whsec_test")

    resp1 = client.post("/webhook/payment", content=body, headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})
    resp2 = client.post("/webhook/payment", content=body, headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert len(httpx_mock.get_requests()) == 1  # only one confirmation sent, not two
    assert db.get_order(tenant_id, order.id).status == db.ORDER_STATUS_PAID


def test_payment_failed_event_marks_order_failed_and_offers_retry(tenant_id, httpx_mock):
    _configure_razorpay(tenant_id)
    order = _make_order(tenant_id)
    db.update_order_payment_link(tenant_id, order.id, "https://rzp.io/l/original", "plink_orig")
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "a"}]})

    body = json.dumps(_failed_payload(tenant_id, order.id)).encode()
    resp = client.post(
        "/webhook/payment", content=body,
        headers={"X-Razorpay-Signature": _sign(body, "whsec_test"), "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert db.get_order(tenant_id, order.id).status == db.ORDER_STATUS_FAILED
    sent_body = json.loads(httpx_mock.get_requests()[0].content)
    assert "https://rzp.io/l/original" in sent_body["text"]["body"]


def test_payment_link_expired_generates_fresh_link(tenant_id, httpx_mock, monkeypatch):
    _configure_razorpay(tenant_id)
    order = _make_order(tenant_id)
    db.update_order_payment_link(tenant_id, order.id, "https://rzp.io/l/original", "plink_orig")
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "a"}]})

    import payments
    monkeypatch.setattr(payments, "create_payment_link", lambda tenant, order: ("https://rzp.io/l/fresh", "plink_fresh"))

    body = json.dumps(_expired_payload(tenant_id, order.id)).encode()
    resp = client.post(
        "/webhook/payment", content=body,
        headers={"X-Razorpay-Signature": _sign(body, "whsec_test"), "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    updated = db.get_order(tenant_id, order.id)
    assert updated.status == db.ORDER_STATUS_FAILED
    assert updated.payment_link_url == "https://rzp.io/l/fresh"
    sent_body = json.loads(httpx_mock.get_requests()[0].content)
    assert "https://rzp.io/l/fresh" in sent_body["text"]["body"]


# --- Cross-tenant isolation ---

def test_wrong_tenants_secret_cannot_validate_payload(tenant_id, second_tenant_id, httpx_mock):
    """A payment webhook for tenant A's order, signed with tenant B's webhook
    secret, must be rejected -- never resolved/processed against A's data
    using B's credentials, even though both share this one endpoint."""
    _configure_razorpay(tenant_id, webhook_secret="tenant-a-secret")
    _configure_razorpay(second_tenant_id, webhook_secret="tenant-b-secret")
    order = _make_order(tenant_id)

    body = json.dumps(_paid_payload(tenant_id, order.id)).encode()
    forged_sig = _sign(body, "tenant-b-secret")

    resp = client.post("/webhook/payment", content=body, headers={"X-Razorpay-Signature": forged_sig, "Content-Type": "application/json"})

    assert resp.status_code == 403
    assert db.get_order(tenant_id, order.id).status == db.ORDER_STATUS_PENDING_PAYMENT
    assert len(httpx_mock.get_requests()) == 0


def test_order_id_collision_across_tenants_resolves_to_correct_tenant(tenant_id, second_tenant_id, httpx_mock):
    """Order ids are not globally unique across tenants -- reference_id
    always carries BOTH tenant_id and order_id (payments.py's
    _build_reference_id), so a webhook for tenant B's order must never be
    able to affect tenant A's order, even if they happen to share the same
    numeric id."""
    _configure_razorpay(tenant_id, webhook_secret="tenant-a-secret")
    _configure_razorpay(second_tenant_id, webhook_secret="tenant-b-secret")
    order_a = _make_order(tenant_id, phone="911111111111")
    order_b = _make_order(second_tenant_id, phone="922222222222")
    httpx_mock.add_response(
        url="https://graph.facebook.com/v22.0/TEST_TENANT_2_PHONE_ID/messages", json={"messages": [{"id": "a"}]},
    )

    body = json.dumps(_paid_payload(second_tenant_id, order_b.id)).encode()
    resp = client.post(
        "/webhook/payment", content=body,
        headers={"X-Razorpay-Signature": _sign(body, "tenant-b-secret"), "Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert db.get_order(second_tenant_id, order_b.id).status == db.ORDER_STATUS_PAID
    assert db.get_order(tenant_id, order_a.id).status == db.ORDER_STATUS_PENDING_PAYMENT  # untouched
    assert len(httpx_mock.get_requests()) == 1  # only tenant B's customer was messaged
