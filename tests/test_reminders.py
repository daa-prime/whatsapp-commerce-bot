# tests/test_reminders.py
"""
reminders/scheduler.py — the abandoned-cart recovery job (SPEC.md Section
3.3/Phase 6). Exercises send_abandoned_cart_nudges() directly with a fake
WhatsApp client, same pattern tests/test_commerce_flow.py uses for
core/commerce_flow.py. The /internal/send-abandoned-cart-nudges HTTP route
that wires this in per active tenant is covered in tests/test_multi_tenant.py.
"""
from decimal import Decimal

import pytest

import db.repository as db
from db.connection import get_connection
from reminders.scheduler import send_abandoned_cart_nudges

PHONE = "919999999999"


class FakeWhatsAppClient:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, text):
        self.sent.append(("text", {"to": to, "text": text}))


def _backdate_order(tenant_id, order_id, hours_ago):
    conn = get_connection()
    conn.execute(
        "UPDATE orders SET created_at = (now() - (? * interval '1 hour'))::text WHERE tenant_id = ? AND id = ?",
        (hours_ago, tenant_id, order_id),
    )
    conn.commit()


def _abandoned_order(tenant_id, phone=PHONE, hours_ago=5, payment_link_url=None):
    order = db.create_order(tenant_id, customer_phone=phone, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("199.00"), total=Decimal("199.00"))
    if payment_link_url:
        db.update_order_payment_link(tenant_id, order.id, payment_link_url, "plink_xxx")
    _backdate_order(tenant_id, order.id, hours_ago)
    return db.get_order(tenant_id, order.id)


@pytest.fixture
def wa():
    return FakeWhatsAppClient()


@pytest.mark.asyncio
async def test_genuinely_abandoned_order_gets_nudged(wa, tenant_id):
    order = _abandoned_order(tenant_id, hours_ago=3)  # default threshold is 2h

    sent = await send_abandoned_cart_nudges(wa, tenant_id)

    assert sent == 1
    assert len(wa.sent) == 1
    assert wa.sent[0][1]["to"] == PHONE
    assert f"order #{order.id}" in wa.sent[0][1]["text"]
    assert db.get_order(tenant_id, order.id).nudge_sent_at is not None


@pytest.mark.asyncio
async def test_recent_order_not_nudged(wa, tenant_id):
    _abandoned_order(tenant_id, hours_ago=1)  # younger than the 2h default threshold

    sent = await send_abandoned_cart_nudges(wa, tenant_id)

    assert sent == 0
    assert wa.sent == []


@pytest.mark.asyncio
async def test_already_nudged_order_not_nudged_twice(wa, tenant_id):
    order = _abandoned_order(tenant_id, hours_ago=5)

    first = await send_abandoned_cart_nudges(wa, tenant_id)
    wa.sent.clear()
    second = await send_abandoned_cart_nudges(wa, tenant_id)

    assert first == 1
    assert second == 0  # already nudged -- not sent again
    assert wa.sent == []
    assert db.get_order(tenant_id, order.id).nudge_sent_at is not None


@pytest.mark.asyncio
async def test_nudge_includes_existing_payment_link(wa, tenant_id):
    _abandoned_order(tenant_id, hours_ago=5, payment_link_url="https://rzp.io/l/existing")

    await send_abandoned_cart_nudges(wa, tenant_id)

    assert "https://rzp.io/l/existing" in wa.sent[0][1]["text"]


@pytest.mark.asyncio
async def test_nudge_prompts_checkout_again_when_no_payment_link_exists(wa, tenant_id):
    _abandoned_order(tenant_id, hours_ago=5)  # no payment_link_url

    await send_abandoned_cart_nudges(wa, tenant_id)

    text = wa.sent[0][1]["text"]
    assert "rzp.io" not in text
    assert "message" in text.lower() or "checkout" in text.lower()


@pytest.mark.asyncio
async def test_cross_tenant_isolation(wa, tenant_id, second_tenant_id):
    """Nudging one tenant must only touch that tenant's own abandoned orders
    -- a different tenant's order is neither read nor marked nudged."""
    mine = _abandoned_order(tenant_id, hours_ago=5)
    theirs = _abandoned_order(second_tenant_id, hours_ago=5)

    sent = await send_abandoned_cart_nudges(wa, tenant_id)

    assert sent == 1
    assert len(wa.sent) == 1
    assert f"order #{mine.id}" in wa.sent[0][1]["text"]
    assert f"order #{theirs.id}" not in wa.sent[0][1]["text"]
    # tenant B's order was never touched by tenant A's nudge run.
    assert db.get_order(second_tenant_id, theirs.id).nudge_sent_at is None


@pytest.mark.asyncio
async def test_respects_tenant_specific_nudge_threshold(wa, tenant_id):
    db.update_tenant_catalog_and_payment(tenant_id, abandoned_cart_nudge_hours=6)
    _abandoned_order(tenant_id, hours_ago=3)  # older than the default 2h, but not the tenant's own 6h

    sent = await send_abandoned_cart_nudges(wa, tenant_id)

    assert sent == 0
    assert wa.sent == []


@pytest.mark.asyncio
async def test_unknown_tenant_returns_zero(wa):
    assert await send_abandoned_cart_nudges(wa, 999999) == 0
