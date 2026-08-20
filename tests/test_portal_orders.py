# tests/test_portal_orders.py
"""
Merchant portal order management (portal/orders.py): list all orders
(optionally filtered by status), view one order's detail, and mark a paid
order fulfilled.
"""
import json
import os
from decimal import Decimal

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


def test_orders_list_requires_login(tenant_id):
    client = TestClient(app)
    resp = client.get("/portal/orders", follow_redirects=False)
    assert resp.status_code == 303


def test_orders_list_shows_all_orders(tenant_id):
    order_paid = db.create_order(tenant_id, "111", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    order_pending = db.create_order(tenant_id, "222", status=db.ORDER_STATUS_PENDING_PAYMENT, subtotal=Decimal("30"), total=Decimal("30"))

    client = _login(tenant_id)
    resp = client.get("/portal/orders")
    assert resp.status_code == 200
    assert f"#{order_paid.id}" in resp.text
    assert f"#{order_pending.id}" in resp.text


def test_orders_list_status_filter(tenant_id):
    order_paid = db.create_order(tenant_id, "111", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    order_pending = db.create_order(tenant_id, "222", status=db.ORDER_STATUS_PENDING_PAYMENT, subtotal=Decimal("30"), total=Decimal("30"))

    client = _login(tenant_id)
    resp = client.get("/portal/orders?status=paid")
    assert resp.status_code == 200
    assert f"#{order_paid.id}" in resp.text
    assert f"#{order_pending.id}" not in resp.text


def test_order_detail_shows_items_and_customer(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("25.00"))
    order = db.create_order(tenant_id, "9998887777", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    db.create_order_item(tenant_id, order.id, product.id, quantity=2, unit_price_at_order_time=Decimal("25.00"))

    client = _login(tenant_id)
    resp = client.get(f"/portal/orders/{order.id}")
    assert resp.status_code == 200
    assert "9998887777" in resp.text
    assert "Widget" in resp.text
    assert "50.00" in resp.text  # line total (2 * 25.00)


def test_order_detail_404s_for_unknown_order(tenant_id):
    client = _login(tenant_id)
    resp = client.get("/portal/orders/999999")
    assert resp.status_code == 404


def test_fulfill_succeeds_from_paid(tenant_id):
    order = db.create_order(tenant_id, "111", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    client = _login(tenant_id)
    resp = client.post(f"/portal/orders/{order.id}/fulfill")
    assert resp.status_code == 200

    updated = db.get_order(tenant_id, order.id)
    assert updated.status == db.ORDER_STATUS_FULFILLED


def test_fulfill_no_ops_from_pending_payment(tenant_id):
    order = db.create_order(tenant_id, "111", status=db.ORDER_STATUS_PENDING_PAYMENT, subtotal=Decimal("50"), total=Decimal("50"))
    client = _login(tenant_id)
    client.post(f"/portal/orders/{order.id}/fulfill")

    updated = db.get_order(tenant_id, order.id)
    assert updated.status == db.ORDER_STATUS_PENDING_PAYMENT  # unchanged, not fulfilled


def test_fulfill_sends_whatsapp_notification(tenant_id, httpx_mock):
    """Marking a paid order fulfilled must notify the customer -- same
    "notify on the transition that actually happened" pattern
    handle_payment_success/handle_payment_failure already use for the
    Razorpay webhook path (tests/test_payment_webhook.py)."""
    order = db.create_order(tenant_id, "9998887777", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "wamid.x"}]})

    client = _login(tenant_id)
    resp = client.post(f"/portal/orders/{order.id}/fulfill")
    assert resp.status_code == 200

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    sent_body = json.loads(requests[0].content)
    assert sent_body["to"] == "9998887777"
    assert f"#{order.id}" in sent_body["text"]["body"]
    assert "fulfilled" in sent_body["text"]["body"].lower()


def test_fulfill_notification_uses_correct_tenant_whatsapp_credentials(httpx_mock):
    """The notification must go out through the fulfilling tenant's own
    WhatsApp phone number/access token, not whichever tenant happens to be
    seeded first -- same cross-tenant-credentials concern
    tests/test_payment_webhook.py's own tenant-scoping tests cover for the
    payment-confirmation path."""
    tenant = db.create_tenant("Tenant B", whatsapp_phone_number_id="999888777", access_token="tenant-b-secret-token")
    order = db.create_order(tenant.id, "9998887777", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/999888777/messages", json={"messages": [{"id": "wamid.x"}]})

    client = _login(tenant.id, phone_number_id="999888777")
    resp = client.post(f"/portal/orders/{order.id}/fulfill")
    assert resp.status_code == 200

    requests = httpx_mock.get_requests()
    assert len(requests) == 1
    assert requests[0].url == "https://graph.facebook.com/v22.0/999888777/messages"
    assert requests[0].headers["Authorization"] == "Bearer tenant-b-secret-token"


def test_fulfill_no_op_from_pending_payment_sends_no_notification(tenant_id, httpx_mock):
    """A fulfill click against a not-yet-paid order is a no-op
    (db.mark_order_fulfilled's `WHERE status = 'paid'` guard) -- since
    nothing actually transitioned, no WhatsApp message should go out
    either. Covers the "doesn't fire on unrelated status changes" case."""
    order = db.create_order(tenant_id, "111", status=db.ORDER_STATUS_PENDING_PAYMENT, subtotal=Decimal("50"), total=Decimal("50"))

    client = _login(tenant_id)
    client.post(f"/portal/orders/{order.id}/fulfill")

    assert httpx_mock.get_requests() == []


def test_fulfill_already_fulfilled_does_not_resend_notification(tenant_id, httpx_mock):
    """A second fulfill click against an already-fulfilled order is also a
    no-op transition -- guards against a merchant double-clicking (or
    reloading the redirect) re-notifying the customer."""
    order = db.create_order(tenant_id, "111", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "wamid.x"}]})

    client = _login(tenant_id)
    client.post(f"/portal/orders/{order.id}/fulfill")
    client.post(f"/portal/orders/{order.id}/fulfill")

    assert len(httpx_mock.get_requests()) == 1  # only the first click actually transitioned the order


def test_orders_list_shows_customer_name_when_on_file(tenant_id):
    order = db.create_order(tenant_id, "9998887777", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    db.set_customer_name(tenant_id, "9998887777", "Ravi Kumar")

    client = _login(tenant_id)
    resp = client.get("/portal/orders")

    assert "Ravi Kumar" in resp.text
    assert f"#{order.id}" in resp.text


def test_orders_list_falls_back_to_phone_when_no_name_on_file(tenant_id):
    db.create_order(tenant_id, "9998887777", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))

    client = _login(tenant_id)
    resp = client.get("/portal/orders")

    assert "9998887777" in resp.text


def test_order_detail_shows_customer_name_when_on_file(tenant_id):
    order = db.create_order(tenant_id, "9998887777", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    db.set_customer_name(tenant_id, "9998887777", "Ravi Kumar")

    client = _login(tenant_id)
    resp = client.get(f"/portal/orders/{order.id}")

    assert "Ravi Kumar" in resp.text
    assert "9998887777" in resp.text  # phone still shown too


def test_order_detail_shows_applied_coupon(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("100.00"), stock_quantity=5)
    db.create_coupon(tenant_id, "SAVE10", db.COUPON_TYPE_PERCENTAGE, Decimal("10"))
    order, _ = db.checkout_cart(tenant_id, "9998887777", {str(product.id): 1}, coupon_code="SAVE10")

    client = _login(tenant_id)
    resp = client.get(f"/portal/orders/{order.id}")

    assert "SAVE10" in resp.text
    assert "10.00" in resp.text  # discount amount


def test_cross_tenant_isolation(tenant_id, second_tenant_id):
    order_a = db.create_order(tenant_id, "111", status=db.ORDER_STATUS_PAID, subtotal=Decimal("50"), total=Decimal("50"))
    order_b = db.create_order(second_tenant_id, "222", status=db.ORDER_STATUS_PAID, subtotal=Decimal("75"), total=Decimal("75"))

    client_a = _login(tenant_id)
    list_resp = client_a.get("/portal/orders")
    assert f"#{order_a.id}" in list_resp.text
    assert "222" not in list_resp.text  # tenant B's customer phone never shown

    detail_resp = client_a.get(f"/portal/orders/{order_b.id}")
    assert detail_resp.status_code == 404  # tenant A can't view tenant B's order by id

    fulfill_resp = client_a.post(f"/portal/orders/{order_b.id}/fulfill")
    assert fulfill_resp.status_code == 404
    assert db.get_order(second_tenant_id, order_b.id).status == db.ORDER_STATUS_PAID  # untouched
