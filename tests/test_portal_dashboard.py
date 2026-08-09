# tests/test_portal_dashboard.py
"""
Merchant portal dashboard (portal/dashboard.py, db.repository.get_dashboard_stats).
Builds known orders/products directly via db.repository (not through the
WhatsApp conversation flow -- that's already covered by
tests/test_commerce_flow.py) and asserts the dashboard's numbers against
them, plus cross-tenant isolation.
"""
import os
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

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


def _paid_order(tenant_id, phone, total, paid_at_iso, payment_method="upi", category=None):
    order = db.create_order(tenant_id, phone, status=db.ORDER_STATUS_PENDING_PAYMENT, subtotal=total, total=total)
    if category is not None:
        product = db.create_product(tenant_id, name="Widget", price=total, category=category)
        db.create_order_item(tenant_id, order.id, product.id, quantity=1, unit_price_at_order_time=total)
    db.mark_order_paid(tenant_id, order.id, "pay_test", paid_at_iso, payment_method=payment_method)
    return order


def test_dashboard_requires_login(tenant_id):
    client = TestClient(app)
    resp = client.get("/portal/dashboard", follow_redirects=False)
    assert resp.status_code == 303


def test_stat_tiles_reflect_seeded_data(tenant_id):
    tz = ZoneInfo("Asia/Kolkata")
    today_noon = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)

    _paid_order(tenant_id, "111", Decimal("100.00"), today_noon.isoformat())
    _paid_order(tenant_id, "222", Decimal("200.00"), today_noon.isoformat())
    db.create_order(tenant_id, "333", status=db.ORDER_STATUS_PENDING_PAYMENT, subtotal=Decimal("50"), total=Decimal("50"))

    client = _login(tenant_id)
    resp = client.get("/portal/dashboard")
    assert resp.status_code == 200

    stats = db.get_dashboard_stats(tenant_id, "Asia/Kolkata")
    assert stats["sales_today"] == Decimal("300.00")
    assert stats["total_customers"] == 3  # 111, 222, 333
    assert stats["pending_orders"] == 1
    assert stats["avg_order_value"] == Decimal("150.00")  # (100+200)/2 paid orders
    assert f"₹{stats['sales_today']:.2f}" in resp.text or "300.00" in resp.text


def test_sales_delta_pct_compares_today_to_yesterday(tenant_id):
    tz = ZoneInfo("Asia/Kolkata")
    today_noon = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    yesterday_noon = today_noon - timedelta(days=1)

    _paid_order(tenant_id, "111", Decimal("100.00"), yesterday_noon.isoformat())
    _paid_order(tenant_id, "222", Decimal("150.00"), today_noon.isoformat())

    stats = db.get_dashboard_stats(tenant_id, "Asia/Kolkata")
    assert stats["sales_today"] == Decimal("150.00")
    assert stats["sales_delta_pct"] == 50  # (150-100)/100 * 100


def test_sales_delta_pct_none_without_yesterday_baseline(tenant_id):
    tz = ZoneInfo("Asia/Kolkata")
    today_noon = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    _paid_order(tenant_id, "111", Decimal("100.00"), today_noon.isoformat())

    stats = db.get_dashboard_stats(tenant_id, "Asia/Kolkata")
    assert stats["sales_delta_pct"] is None


def test_payment_method_breakdown(tenant_id):
    tz = ZoneInfo("Asia/Kolkata")
    now_iso = datetime.now(tz).isoformat()
    _paid_order(tenant_id, "111", Decimal("100.00"), now_iso, payment_method="upi")
    _paid_order(tenant_id, "222", Decimal("100.00"), now_iso, payment_method="upi")
    _paid_order(tenant_id, "333", Decimal("100.00"), now_iso, payment_method="card")

    stats = db.get_dashboard_stats(tenant_id, "Asia/Kolkata")
    breakdown = {row["method"]: row["count"] for row in stats["payment_method_breakdown"]}
    assert breakdown == {"upi": 2, "card": 1}


def test_top_categories_reflects_paid_order_items(tenant_id):
    tz = ZoneInfo("Asia/Kolkata")
    now_iso = datetime.now(tz).isoformat()
    _paid_order(tenant_id, "111", Decimal("300.00"), now_iso, category="Electronics")
    _paid_order(tenant_id, "222", Decimal("100.00"), now_iso, category="Books")

    stats = db.get_dashboard_stats(tenant_id, "Asia/Kolkata")
    top = {row["category"]: row["revenue"] for row in stats["top_categories"]}
    assert top["Electronics"] == Decimal("300.00")
    assert top["Books"] == Decimal("100.00")


def test_recent_orders_shows_up_to_ten_most_recent(tenant_id):
    tz = ZoneInfo("Asia/Kolkata")
    now_iso = datetime.now(tz).isoformat()
    for i in range(12):
        _paid_order(tenant_id, f"phone-{i}", Decimal("10.00"), now_iso)

    stats = db.get_dashboard_stats(tenant_id, "Asia/Kolkata")
    assert len(stats["recent_orders"]) == 10


def test_cross_tenant_isolation(tenant_id, second_tenant_id):
    tz = ZoneInfo("Asia/Kolkata")
    now_iso = datetime.now(tz).isoformat()
    _paid_order(tenant_id, "111", Decimal("500.00"), now_iso)

    other_stats = db.get_dashboard_stats(second_tenant_id, "Asia/Kolkata")
    assert other_stats["sales_today"] == Decimal("0")
    assert other_stats["total_customers"] == 0
    assert other_stats["recent_orders"] == []
