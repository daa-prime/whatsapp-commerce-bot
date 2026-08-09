# tests/test_commerce_flow.py
"""
core/commerce_flow.py — the menu-driven shop/cart/checkout/orders state
machine. Exercises handle_incoming() directly (same pattern the old
tests/test_booking_flow.py used for core/booking_flow.py), not through the
HTTP webhook — that plumbing is covered separately in tests/test_main.py.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest

import db.repository as db
from core.commerce_flow import MAX_LIST_ROWS, handle_incoming, handle_payment_failure, handle_payment_success
from core.history import InMemorySessionStore

PHONE = "919999999999"


class FakeWhatsAppClient:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, text):
        self.sent.append(("text", {"to": to, "text": text}))

    async def send_list(self, to, body_text, button_text, sections, header_text=None, footer_text=None):
        self.sent.append(("list", {"to": to, "body_text": body_text, "sections": sections}))

    async def send_buttons(self, to, body_text, buttons, header_text=None, footer_text=None):
        self.sent.append(("buttons", {"to": to, "body_text": body_text, "buttons": buttons}))


def tap(option_id, title=""):
    return {"type": "interactive_reply", "id": option_id, "title": title}


def text_reply(text):
    return {"type": "text", "text": text}


def _row_ids(kind_kwargs):
    return {row["id"] for section in kind_kwargs["sections"] for row in section["rows"]}


def _make_product(tenant_id, name="Widget", price="199.00", stock_quantity=100, **kwargs):
    # Defaults to well-stocked so tests not specifically about stock limits
    # (create_product's own stock_quantity default is 0) don't trip the
    # add-to-cart stock check added by the hardening pass.
    return db.create_product(tenant_id, name=name, price=Decimal(price), stock_quantity=stock_quantity, **kwargs)


def _configure_razorpay(tenant_id):
    db.update_tenant_catalog_and_payment(
        tenant_id,
        payment_gateway_provider="razorpay",
        payment_gateway_key_id="rzp_test_key",
        payment_gateway_api_key_ref="rzp_test_secret",
        payment_gateway_webhook_secret="whsec_test",
    )


@pytest.fixture
def wa():
    return FakeWhatsAppClient()


@pytest.fixture
def sessions():
    return InMemorySessionStore()


# --- Main menu ---

@pytest.mark.asyncio
async def test_first_contact_shows_welcome_and_main_menu(wa, sessions, tenant_id):
    await handle_incoming(wa, sessions, PHONE, tenant_id, text_reply("hi"), "Test Store")

    assert wa.sent[-1][0] == "list"
    body = wa.sent[-1][1]
    assert "Welcome to Test Store" in body["body_text"]
    assert _row_ids(body) == {
        "menu_shop", "menu_my_orders", "menu_track_order",
        "menu_offers", "menu_account", "menu_talk_to_us",
    }


@pytest.mark.asyncio
async def test_offers_and_account_are_coming_soon_placeholders(wa, sessions, tenant_id):
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_offers"))
    assert "coming soon" in wa.sent[-1][1]["text"].lower()

    wa.sent.clear()
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_account"))
    assert "coming soon" in wa.sent[-1][1]["text"].lower()


@pytest.mark.asyncio
async def test_talk_to_us_is_a_coming_soon_placeholder(wa, sessions, tenant_id):
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_talk_to_us"))
    assert "coming soon" in wa.sent[-1][1]["text"].lower()


# --- Full shop -> cart -> checkout flow ---

@pytest.mark.asyncio
async def test_full_shop_flow_browse_add_to_cart_view_cart_checkout_creates_order(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="199.00")
    gadget = _make_product(tenant_id, name="Gadget", price="299.00")

    # Shop Now -> product list
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    assert wa.sent[-1][0] == "list"
    assert _row_ids(wa.sent[-1][1]) == {f"product_{widget.id}", f"product_{gadget.id}"}

    # Tap a product -> detail view with Add to Cart / Back to Products
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    assert wa.sent[-1][0] == "buttons"
    detail = wa.sent[-1][1]
    assert "Widget" in detail["body_text"]
    assert "₹199" in detail["body_text"]
    assert {b["id"] for b in detail["buttons"]} == {"add_to_cart", "back_to_products"}

    # Add to Cart -> confirmation with running cart summary
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    assert wa.sent[-1][0] == "buttons"
    add_confirm = wa.sent[-1][1]
    assert "Cart (1 item)" in add_confirm["body_text"]
    assert "₹199" in add_confirm["body_text"]
    assert {b["id"] for b in add_confirm["buttons"]} == {"view_cart", "continue_shopping"}

    # View Cart -> itemized summary with Checkout / Continue Shopping
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    assert wa.sent[-1][0] == "buttons"
    cart_view = wa.sent[-1][1]
    assert "Widget" in cart_view["body_text"]
    assert "Subtotal: ₹199" in cart_view["body_text"]
    assert {b["id"] for b in cart_view["buttons"]} == {"checkout", "continue_shopping"}

    # Checkout -> real order + order_items rows, pending_payment, payment stub message
    assert db.get_orders_for_phone(tenant_id, PHONE) == []
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("checkout"))
    assert wa.sent[-1][0] == "text"
    confirm_text = wa.sent[-1][1]["text"]
    assert "Order #" in confirm_text
    assert "Payment integration coming next" in confirm_text

    orders = db.get_orders_for_phone(tenant_id, PHONE)
    assert len(orders) == 1
    order = orders[0]
    assert order.status == db.ORDER_STATUS_PENDING_PAYMENT
    assert order.subtotal == Decimal("199.00")
    assert order.total == Decimal("199.00")

    items = db.get_order_items(tenant_id, order.id)
    assert len(items) == 1
    assert items[0].product_id == widget.id
    assert items[0].quantity == 1
    assert items[0].unit_price_at_order_time == Decimal("199.00")

    # Session resets to IDLE (and cart is gone) after checkout.
    assert sessions.get(tenant_id, PHONE) == {"state": "IDLE", "context": {}}


@pytest.mark.asyncio
async def test_cart_quantity_increments_when_adding_same_product_twice(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="100.00")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    # Back to the product (simulating another browse-add) via continue shopping -> product -> add again
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("continue_shopping"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))

    add_confirm = wa.sent[-1][1]
    assert "Cart (2 items)" in add_confirm["body_text"]
    assert "₹200" in add_confirm["body_text"]  # 2 x ₹100

    session = sessions.get(tenant_id, PHONE)
    assert session["context"]["cart"] == {str(widget.id): 2}

    # Checkout produces ONE order_item with quantity=2, not two separate lines.
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("checkout"))
    order = db.get_orders_for_phone(tenant_id, PHONE)[0]
    items = db.get_order_items(tenant_id, order.id)
    assert len(items) == 1
    assert items[0].quantity == 2


# --- My Orders / Track Order ---

@pytest.mark.asyncio
async def test_my_orders_shows_status_and_total(wa, sessions, tenant_id):
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PAID,
                             subtotal=Decimal("500.00"), total=Decimal("500.00"))

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_my_orders"))
    assert wa.sent[-1][0] == "list"
    rows = [row for section in wa.sent[-1][1]["sections"] for row in section["rows"]]
    assert len(rows) == 1
    assert rows[0]["id"] == f"order_{order.id}"
    assert "Paid" in rows[0]["description"]
    assert "₹500" in rows[0]["description"]

    # Tap the order -> detail view with status + total + a Main Menu button.
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"order_{order.id}"))
    assert wa.sent[-1][0] == "buttons"
    detail = wa.sent[-1][1]
    assert f"Order #{order.id}" in detail["body_text"]
    assert "Paid" in detail["body_text"]
    assert "₹500" in detail["body_text"]
    assert detail["buttons"] == [{"id": "main_menu", "title": "Main Menu"}]


@pytest.mark.asyncio
async def test_my_orders_empty_state(wa, sessions, tenant_id):
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_my_orders"))
    assert wa.sent[-1] == ("text", {"to": PHONE, "text": "You don't have any orders yet."})


@pytest.mark.asyncio
async def test_track_order_shows_order_status(wa, sessions, tenant_id):
    """Implemented identically to My Orders (pick from a recent-orders list)
    rather than free-text order-ref entry -- see commerce_flow.py's
    STATE_ORDER_LIST docstring for why."""
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_FULFILLED,
                             subtotal=Decimal("50.00"), total=Decimal("50.00"))

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_track_order"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"order_{order.id}"))
    assert "Fulfilled" in wa.sent[-1][1]["body_text"]


# --- 10-row cap ---

@pytest.mark.asyncio
async def test_product_list_respects_ten_row_cap_with_see_more(wa, sessions, tenant_id):
    products = [_make_product(tenant_id, name=f"Product {i}", price="10.00") for i in range(12)]

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    rows = [row for section in wa.sent[-1][1]["sections"] for row in section["rows"]]
    assert len(rows) == MAX_LIST_ROWS
    assert rows[-1]["id"] == "shop_more"
    assert {r["id"] for r in rows[:-1]} == {f"product_{p.id}" for p in products[:9]}

    # Tapping "See more products" pages to the remaining 3.
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("shop_more"))
    rows2 = [row for section in wa.sent[-1][1]["sections"] for row in section["rows"]]
    assert {r["id"] for r in rows2} == {f"product_{p.id}" for p in products[9:]}


# --- Cross-tenant isolation ---

@pytest.mark.asyncio
async def test_products_never_leak_across_tenants(wa, sessions, tenant_id, second_tenant_id):
    mine = _make_product(tenant_id, name="Mine", price="10.00")
    theirs = _make_product(second_tenant_id, name="Theirs", price="10.00")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    rows = _row_ids(wa.sent[-1][1])
    assert rows == {f"product_{mine.id}"}
    assert f"product_{theirs.id}" not in rows


@pytest.mark.asyncio
async def test_tenant_a_product_id_rejected_when_tapped_against_tenant_b(wa, sessions, tenant_id, second_tenant_id):
    """Even if a customer's client somehow replayed tenant A's product row id
    against tenant B's conversation, it must not resolve."""
    theirs = _make_product(second_tenant_id, name="Theirs", price="10.00")
    sessions.set(tenant_id, PHONE, "SHOP_LIST", {"shop_offset": 0, "cart": {}})

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{theirs.id}"))

    assert wa.sent[0] == ("text", {"to": PHONE, "text": "Sorry, that item is no longer available."})


@pytest.mark.asyncio
async def test_orders_never_leak_across_tenants(wa, sessions, tenant_id, second_tenant_id):
    db.create_order(second_tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PAID,
                     subtotal=Decimal("10.00"), total=Decimal("10.00"))

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_my_orders"))
    assert wa.sent[-1] == ("text", {"to": PHONE, "text": "You don't have any orders yet."})


# --- Reset keyword mid-flow ---

@pytest.mark.asyncio
async def test_reset_keyword_works_mid_shopping_flow(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="100.00")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    assert sessions.get(tenant_id, PHONE)["state"] == "PRODUCT_DETAIL"

    await handle_incoming(wa, sessions, PHONE, tenant_id, text_reply("menu"), "Test Store")

    assert sessions.get(tenant_id, PHONE) == {"state": "IDLE", "context": {}}
    assert wa.sent[-1][0] == "list"
    assert "Welcome to Test Store" in wa.sent[-1][1]["body_text"]


@pytest.mark.asyncio
async def test_reset_keyword_works_from_cart_with_items(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="100.00")
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    assert sessions.get(tenant_id, PHONE)["state"] == "CART"

    await handle_incoming(wa, sessions, PHONE, tenant_id, text_reply("restart"))

    assert sessions.get(tenant_id, PHONE)["state"] == "IDLE"
    # No order was ever created -- reset abandons the cart rather than
    # silently checking out.
    assert db.get_orders_for_phone(tenant_id, PHONE) == []


# --- Hardening: stock checks, race safety, duplicate checkout, unavailable items ---

@pytest.mark.asyncio
async def test_add_to_cart_blocks_over_quantity(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="100.00", stock_quantity=1)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))  # 1st unit -> fine
    assert sessions.get(tenant_id, PHONE)["context"]["cart"] == {str(widget.id): 1}

    # Second add attempts quantity 2, but only 1 is in stock.
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("continue_shopping"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))

    assert wa.sent[-2] == ("text", {"to": PHONE, "text": "Sorry, only 1 left in stock."})
    # Cart is untouched -- still just the 1 unit from before.
    assert sessions.get(tenant_id, PHONE)["context"]["cart"] == {str(widget.id): 1}


@pytest.mark.asyncio
async def test_add_to_cart_out_of_stock_message(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="100.00", stock_quantity=0)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))

    assert wa.sent[-2] == ("text", {"to": PHONE, "text": "Sorry, this item is out of stock."})
    assert sessions.get(tenant_id, PHONE)["context"].get("cart", {}) == {}


@pytest.mark.asyncio
async def test_checkout_decrements_stock(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="100.00", stock_quantity=5)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("checkout"))

    assert db.get_product(tenant_id, widget.id).stock_quantity == 4


@pytest.mark.asyncio
async def test_duplicate_checkout_tap_does_not_create_two_orders(wa, sessions, tenant_id):
    """Simulates a double-tap / webhook redelivery: the same "checkout" reply
    processed twice in a row against the same session. The cart-clear-before-
    DB-write in _checkout() means the second tap lands in IDLE (session
    already reset), not a second checkout."""
    widget = _make_product(tenant_id, name="Widget", price="100.00", stock_quantity=5)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    assert sessions.get(tenant_id, PHONE)["state"] == "CART"

    checkout_tap = tap("checkout")
    await handle_incoming(wa, sessions, PHONE, tenant_id, checkout_tap)
    await handle_incoming(wa, sessions, PHONE, tenant_id, checkout_tap)  # duplicate delivery

    orders = db.get_orders_for_phone(tenant_id, PHONE)
    assert len(orders) == 1
    assert db.get_product(tenant_id, widget.id).stock_quantity == 4  # decremented once, not twice


@pytest.mark.asyncio
async def test_checkout_gracefully_drops_item_that_went_out_of_stock(wa, sessions, tenant_id):
    """A product goes out of stock (e.g. another customer bought the last
    unit) between being added to this customer's cart and checkout -- it
    must be dropped from the order with a clear message, not crash or get
    silently ordered."""
    widget = _make_product(tenant_id, name="Widget", price="100.00", stock_quantity=5)
    limited = _make_product(tenant_id, name="Limited Gadget", price="50.00", stock_quantity=1)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("continue_shopping"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{limited.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))

    # "Sells out" from under this customer between add-to-cart and checkout.
    db.update_product_stock(tenant_id, limited.id, 0)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("checkout"))

    confirm_text = wa.sent[-1][1]["text"]
    assert "Order #" in confirm_text
    assert "Limited Gadget went out of stock and was removed from your order." in confirm_text

    order = db.get_orders_for_phone(tenant_id, PHONE)[0]
    items = db.get_order_items(tenant_id, order.id)
    assert len(items) == 1
    assert items[0].product_id == widget.id
    assert order.total == Decimal("100.00")  # only the still-available item is billed


@pytest.mark.asyncio
async def test_checkout_all_items_unavailable_shows_apology_and_creates_no_order(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="100.00", stock_quantity=1)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))

    db.update_product_stock(tenant_id, widget.id, 0)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("checkout"))

    assert wa.sent[-1] == (
        "text",
        {"to": PHONE, "text": "Sorry, the items in your cart are no longer available. Please start shopping again."},
    )


# --- Payment link generation at checkout (SPEC.md Section 3.3, Phase 3) ---

@pytest.mark.asyncio
async def test_checkout_generates_payment_link_when_tenant_configured(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="199.00")
    _configure_razorpay(tenant_id)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    with patch("payments.create_payment_link", return_value=("https://rzp.io/l/xyz", "plink_xyz")):
        await handle_incoming(wa, sessions, PHONE, tenant_id, tap("checkout"))

    confirm_text = wa.sent[-1][1]["text"]
    assert "https://rzp.io/l/xyz" in confirm_text
    assert "Payment integration coming next" not in confirm_text

    order = db.get_orders_for_phone(tenant_id, PHONE)[0]
    assert order.payment_link_url == "https://rzp.io/l/xyz"
    assert order.payment_gateway_reference == "plink_xyz"


@pytest.mark.asyncio
async def test_checkout_falls_back_to_placeholder_when_gateway_not_configured(wa, sessions, tenant_id):
    """The seeded tenant has no Razorpay credentials -- checkout must still
    succeed, just without a real payment link."""
    widget = _make_product(tenant_id, name="Widget", price="199.00")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("checkout"))

    assert "Payment integration coming next" in wa.sent[-1][1]["text"]
    order = db.get_orders_for_phone(tenant_id, PHONE)[0]
    assert order.payment_link_url is None


@pytest.mark.asyncio
async def test_checkout_handles_razorpay_api_failure_gracefully(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="199.00")
    _configure_razorpay(tenant_id)

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{widget.id}"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("add_to_cart"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("view_cart"))
    with patch("payments.create_payment_link", side_effect=RuntimeError("network error")):
        await handle_incoming(wa, sessions, PHONE, tenant_id, tap("checkout"))

    # The order still exists (checkout itself succeeded) -- only the payment
    # link generation failed, and the webhook handler must not crash.
    assert "couldn't generate a payment link" in wa.sent[-1][1]["text"].lower()
    assert len(db.get_orders_for_phone(tenant_id, PHONE)) == 1


# --- Payment webhook handlers (SPEC.md Section 3.3, Phase 3) ---

@pytest.mark.asyncio
async def test_handle_payment_success_marks_paid_and_sends_confirmation(wa, tenant_id):
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("100.00"), total=Decimal("100.00"))

    await handle_payment_success(wa, tenant_id, order.id, "pay_abc123")

    updated = db.get_order(tenant_id, order.id)
    assert updated.status == db.ORDER_STATUS_PAID
    assert updated.payment_gateway_reference == "pay_abc123"
    assert updated.paid_at is not None
    assert wa.sent[-1] == ("text", {"to": PHONE, "text": f"Payment received! Your order #{order.id} is confirmed."})


@pytest.mark.asyncio
async def test_handle_payment_success_idempotent_on_duplicate_delivery(wa, tenant_id):
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("100.00"), total=Decimal("100.00"))

    await handle_payment_success(wa, tenant_id, order.id, "pay_abc123")
    wa.sent.clear()
    await handle_payment_success(wa, tenant_id, order.id, "pay_abc123")  # duplicate webhook delivery

    assert wa.sent == []  # no second confirmation


@pytest.mark.asyncio
async def test_handle_payment_failure_link_still_valid_offers_same_link(wa, tenant_id):
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("100.00"), total=Decimal("100.00"))
    db.update_order_payment_link(tenant_id, order.id, "https://rzp.io/l/original", "plink_orig")
    tenant = db.get_tenant(tenant_id)

    await handle_payment_failure(wa, tenant, order.id, link_expired=False)

    assert db.get_order(tenant_id, order.id).status == db.ORDER_STATUS_FAILED
    assert "https://rzp.io/l/original" in wa.sent[-1][1]["text"]


@pytest.mark.asyncio
async def test_handle_payment_failure_link_expired_generates_fresh_link(wa, tenant_id):
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("100.00"), total=Decimal("100.00"))
    db.update_order_payment_link(tenant_id, order.id, "https://rzp.io/l/original", "plink_orig")
    tenant = db.get_tenant(tenant_id)

    with patch("payments.create_payment_link", return_value=("https://rzp.io/l/fresh", "plink_fresh")):
        await handle_payment_failure(wa, tenant, order.id, link_expired=True)

    updated = db.get_order(tenant_id, order.id)
    assert updated.status == db.ORDER_STATUS_FAILED
    assert updated.payment_link_url == "https://rzp.io/l/fresh"
    assert "https://rzp.io/l/fresh" in wa.sent[-1][1]["text"]


@pytest.mark.asyncio
async def test_handle_payment_failure_idempotent_on_duplicate_delivery(wa, tenant_id):
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("100.00"), total=Decimal("100.00"))
    tenant = db.get_tenant(tenant_id)

    await handle_payment_failure(wa, tenant, order.id, link_expired=False)
    wa.sent.clear()
    await handle_payment_failure(wa, tenant, order.id, link_expired=False)  # duplicate delivery

    assert wa.sent == []


@pytest.mark.asyncio
async def test_handle_payment_failure_never_downgrades_an_already_paid_order(wa, tenant_id):
    """A late/out-of-order failure webhook must never undo a payment a
    different (already-processed) webhook delivery already confirmed."""
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("100.00"), total=Decimal("100.00"))
    tenant = db.get_tenant(tenant_id)
    await handle_payment_success(wa, tenant_id, order.id, "pay_abc123")
    wa.sent.clear()

    await handle_payment_failure(wa, tenant, order.id, link_expired=False)

    assert db.get_order(tenant_id, order.id).status == db.ORDER_STATUS_PAID  # not downgraded
    assert wa.sent == []
