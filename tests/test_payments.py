# tests/test_payments.py
"""
payments.py — Razorpay payment-link generation and webhook payload/signature
parsing. Mocks the actual razorpay.Client calls (no real Razorpay account
available in this environment) -- these tests prove payments.py's own logic
(credential resolution, reference_id round-tripping, signature verification,
payload parsing), not that Razorpay's live API behaves exactly as documented
-- see payments.py's module docstring for that caveat.
"""
import hashlib
import hmac
from decimal import Decimal
from unittest.mock import patch

import pytest

import db.repository as db
import payments


def _tenant_with_razorpay(tenant_id, key_id="rzp_test_key", key_secret="rzp_test_secret", webhook_secret="whsec_test"):
    db.update_tenant_catalog_and_payment(
        tenant_id,
        payment_gateway_provider="razorpay",
        payment_gateway_key_id=key_id,
        payment_gateway_api_key_ref=key_secret,
        payment_gateway_webhook_secret=webhook_secret,
    )
    return db.get_tenant(tenant_id)


def _make_order(tenant_id, total="500.00"):
    return db.create_order(
        tenant_id, customer_phone="919999999999", status=db.ORDER_STATUS_PENDING_PAYMENT,
        subtotal=Decimal(total), total=Decimal(total),
    )


# --- create_payment_link ---

def test_create_payment_link_uses_tenant_credentials_and_returns_url(tenant_id):
    tenant = _tenant_with_razorpay(tenant_id)
    order = _make_order(tenant_id, total="500.00")

    with patch("razorpay.Client") as MockClient:
        mock_client = MockClient.return_value
        mock_client.payment_link.create.return_value = {"id": "plink_abc123", "short_url": "https://rzp.io/l/abc123"}

        url, link_id = payments.create_payment_link(tenant, order)

    assert url == "https://rzp.io/l/abc123"
    assert link_id == "plink_abc123"
    MockClient.assert_called_once_with(auth=("rzp_test_key", "rzp_test_secret"))

    create_kwargs = mock_client.payment_link.create.call_args[0][0]
    assert create_kwargs["amount"] == 50000  # 500.00 INR -> 50000 paise
    assert create_kwargs["currency"] == "INR"
    assert create_kwargs["reference_id"] == f"{tenant.id}:{order.id}"
    assert create_kwargs["notes"]["reference_id"] == f"{tenant.id}:{order.id}"


def test_create_payment_link_raises_when_tenant_not_configured(tenant_id):
    tenant = db.get_tenant(tenant_id)  # no Razorpay credentials set on the seeded tenant
    order = _make_order(tenant_id)

    with pytest.raises(payments.PaymentGatewayNotConfigured):
        payments.create_payment_link(tenant, order)


def test_create_payment_link_raises_when_only_key_id_missing(tenant_id):
    db.update_tenant_catalog_and_payment(tenant_id, payment_gateway_api_key_ref="secret-only")
    tenant = db.get_tenant(tenant_id)
    order = _make_order(tenant_id)

    with pytest.raises(payments.PaymentGatewayNotConfigured):
        payments.create_payment_link(tenant, order)


# --- validate_payment_webhook_signature ---

def test_validate_payment_webhook_signature_valid():
    secret = "whsec_test"
    body = b'{"event": "payment_link.paid"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert payments.validate_payment_webhook_signature(body, sig, secret) is True


def test_validate_payment_webhook_signature_invalid():
    body = b'{"event": "payment_link.paid"}'
    assert payments.validate_payment_webhook_signature(body, "0" * 64, "whsec_test") is False


def test_validate_payment_webhook_signature_no_secret_configured():
    body = b"body"
    sig = hmac.new(b"anything", body, hashlib.sha256).hexdigest()
    assert payments.validate_payment_webhook_signature(body, sig, None) is False


def test_validate_payment_webhook_signature_missing_signature_header():
    assert payments.validate_payment_webhook_signature(b"body", "", "whsec_test") is False


# --- parse_payment_webhook ---

def test_parse_payment_webhook_paid_event():
    payload = {
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"id": "plink_abc", "reference_id": "3:42"}},
            "payment": {"entity": {"id": "pay_xyz"}},
        },
    }
    event = payments.parse_payment_webhook(payload)
    assert event.event_type == payments.EVENT_PAID
    assert event.tenant_id == 3
    assert event.order_id == 42
    assert event.payment_gateway_reference == "pay_xyz"  # prefers the payment id over the link id


def test_parse_payment_webhook_expired_event_falls_back_to_link_id():
    payload = {
        "event": "payment_link.expired",
        "payload": {"payment_link": {"entity": {"id": "plink_abc", "reference_id": "3:42"}}},
    }
    event = payments.parse_payment_webhook(payload)
    assert event.event_type == payments.EVENT_EXPIRED
    assert event.tenant_id == 3
    assert event.order_id == 42
    assert event.payment_gateway_reference == "plink_abc"  # no payment entity yet -> falls back


def test_parse_payment_webhook_failed_event_via_notes_fallback():
    """payment.failed events may not carry reference_id directly on a
    payment_link entity path -- notes is the documented fallback (see
    payments.py's create_payment_link, which sets notes.reference_id
    defensively for exactly this case)."""
    payload = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": "pay_xyz", "notes": {"reference_id": "3:42"}}}},
    }
    event = payments.parse_payment_webhook(payload)
    assert event.event_type == payments.EVENT_FAILED
    assert event.tenant_id == 3
    assert event.order_id == 42
    assert event.payment_gateway_reference == "pay_xyz"


def test_parse_payment_webhook_unrecognized_event_type():
    event = payments.parse_payment_webhook({"event": "payment.authorized", "payload": {}})
    assert event.event_type == payments.EVENT_UNKNOWN


def test_parse_payment_webhook_missing_reference_id():
    payload = {"event": "payment_link.paid", "payload": {"payment_link": {"entity": {"id": "plink_abc"}}}}
    event = payments.parse_payment_webhook(payload)
    assert event.tenant_id is None
    assert event.order_id is None


def test_parse_payment_webhook_malformed_reference_id():
    payload = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {"reference_id": "not-a-valid-reference"}}},
    }
    event = payments.parse_payment_webhook(payload)
    assert event.tenant_id is None
    assert event.order_id is None
