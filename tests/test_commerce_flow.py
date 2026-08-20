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
from core.commerce_flow import (
    MAX_LIST_ROWS, handle_incoming, handle_native_order, handle_payment_failure, handle_payment_success,
)
from core.history import InMemorySessionStore

PHONE = "919999999999"
PHONE2 = "919999999998"  # used only by the "--- Language selection ---" tests -- see _default_to_english


class FakeWhatsAppClient:
    def __init__(self):
        self.sent = []
        self.fail_product_list = False  # simulates Meta rejecting a native send (e.g. unapproved catalog)

    async def send_text(self, to, text):
        self.sent.append(("text", {"to": to, "text": text}))

    async def send_list(self, to, body_text, button_text, sections, header_text=None, footer_text=None):
        self.sent.append(("list", {"to": to, "body_text": body_text, "sections": sections}))

    async def send_buttons(self, to, body_text, buttons, header_text=None, header_image_url=None, footer_text=None):
        self.sent.append(("buttons", {
            "to": to, "body_text": body_text, "buttons": buttons, "header_image_url": header_image_url,
        }))

    async def send_product_list(self, to, catalog_id, sections, body_text, header_text, footer_text=None):
        if self.fail_product_list:
            return False  # simulated Meta rejection -- nothing actually delivered
        self.sent.append(("product_list", {
            "to": to, "catalog_id": catalog_id, "sections": sections, "body_text": body_text,
            "header_text": header_text,
        }))
        return True


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


@pytest.fixture(autouse=True)
def _default_to_english(sessions, tenant_id):
    """Every test below except the dedicated "--- Language selection ---"
    section was written (and every test added since) assuming a session is
    immediately ready to show the main menu/product list/etc -- pre-seeding
    English here keeps that true without needing every single test to tap
    a language button first. The language-selection tests use PHONE2
    instead of PHONE specifically so this pre-seeded session (keyed by
    (tenant_id, PHONE)) doesn't mask the real from-scratch behavior they're
    testing."""
    sessions.set(tenant_id, PHONE, "IDLE", {"language": "en"})


# --- Main menu ---

@pytest.mark.asyncio
async def test_first_contact_shows_welcome_and_main_menu(wa, sessions, tenant_id):
    """With English already selected (see _default_to_english), a reset
    keyword shows the welcome message + main menu directly -- real
    first-contact-with-no-language-yet behavior is covered separately
    under "--- Language selection ---" below."""
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

    # Session resets to IDLE (and cart is gone) after checkout -- language is
    # preserved across the reset, not wiped (see _handle_idle's docstring).
    assert sessions.get(tenant_id, PHONE) == {"state": "IDLE", "context": {"language": "en"}}


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
async def test_my_orders_works_when_tapped_from_a_stale_mid_flow_state(wa, sessions, tenant_id):
    """Reproduces a real customer-reported bug: WhatsApp keeps every past
    interactive message tappable indefinitely, so a customer can scroll
    back and tap a stale "My Orders" button from an old main-menu message
    while their session is actually mid-flow elsewhere (e.g. viewing a
    product). That tap must still show order history, not fall into the
    *current* state's handler (which doesn't recognize "menu_my_orders")
    and loop on "Please choose an option from the list above" forever."""
    product = _make_product(tenant_id, name="Widget", price="10.00")
    order = db.create_order(tenant_id, customer_phone=PHONE, status=db.ORDER_STATUS_PAID,
                             subtotal=Decimal("500.00"), total=Decimal("500.00"))

    # Get the session into a non-IDLE state (viewing a product detail) --
    # same as a customer mid-shopping would be.
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{product.id}"))
    assert wa.sent[-1][0] == "buttons"  # confirmed: not IDLE, not the orders flow either

    # Tap a stale "My Orders" button from an earlier main-menu message.
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_my_orders"))
    assert wa.sent[-1][0] == "list"
    rows = [row for section in wa.sent[-1][1]["sections"] for row in section["rows"]]
    assert rows[0]["id"] == f"order_{order.id}"

    # A second tap of the same stale button must behave identically, not
    # repeat/compound the old bug (which looped on the *same* wrong reply).
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_my_orders"))
    assert wa.sent[-1][0] == "list"
    rows2 = [row for section in wa.sent[-1][1]["sections"] for row in section["rows"]]
    assert rows2[0]["id"] == f"order_{order.id}"


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


# --- Category grouping + product images ---

@pytest.mark.asyncio
async def test_product_list_groups_into_sections_by_category(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="10.00", category="Tools")
    gadget = _make_product(tenant_id, name="Gadget", price="20.00", category="Electronics")
    gizmo = _make_product(tenant_id, name="Gizmo", price="30.00", category="Tools")
    uncategorized = _make_product(tenant_id, name="Mystery Item", price="5.00")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    sections = wa.sent[-1][1]["sections"]
    by_title = {s["title"]: {r["id"] for r in s["rows"]} for s in sections}

    assert by_title["Tools"] == {f"product_{widget.id}", f"product_{gizmo.id}"}
    assert by_title["Electronics"] == {f"product_{gadget.id}"}
    assert by_title["Other"] == {f"product_{uncategorized.id}"}


@pytest.mark.asyncio
async def test_product_list_categories_ordered_by_first_appearance(wa, sessions, tenant_id):
    _make_product(tenant_id, name="A", price="10.00", category="Zebra")
    _make_product(tenant_id, name="B", price="10.00", category="Apple")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    titles = [s["title"] for s in wa.sent[-1][1]["sections"]]
    assert titles == ["Zebra", "Apple"]  # not alphabetical -- first-seen order


@pytest.mark.asyncio
async def test_product_detail_sends_image_header_when_set(wa, sessions, tenant_id):
    product = _make_product(tenant_id, name="Widget", price="10.00", image_url="https://example.com/widget.png")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{product.id}"))

    assert wa.sent[-1][0] == "buttons"
    assert wa.sent[-1][1]["header_image_url"] == "https://example.com/widget.png"


@pytest.mark.asyncio
async def test_product_detail_no_image_header_when_unset(wa, sessions, tenant_id):
    product = _make_product(tenant_id, name="Widget", price="10.00")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap(f"product_{product.id}"))

    assert wa.sent[-1][1]["header_image_url"] is None


# --- Native Meta catalog messages (SPEC.md Phase 1/2), with fallback ---

@pytest.mark.asyncio
async def test_shop_falls_back_to_list_message_without_meta_catalog_id(wa, sessions, tenant_id):
    """The default -- and the automatic fallback for any tenant whose
    catalog isn't linked yet -- must keep behaving exactly like today."""
    _make_product(tenant_id, name="Widget", price="10.00")
    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    assert wa.sent[-1][0] == "list"


@pytest.mark.asyncio
async def test_shop_sends_native_product_list_when_meta_catalog_id_set(wa, sessions, tenant_id):
    db.update_tenant_catalog_and_payment(tenant_id, meta_catalog_id="cat_123")
    product = _make_product(tenant_id, name="Widget", price="10.00", category="Tools")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))

    assert wa.sent[-1][0] == "product_list"
    sent = wa.sent[-1][1]
    assert sent["catalog_id"] == "cat_123"
    all_retailer_ids = {
        item["product_retailer_id"] for section in sent["sections"] for item in section["product_items"]
    }
    assert all_retailer_ids == {product.catalog_retailer_id}
    # Required by Meta for product_list -- confirmed against a live rejection
    # ("HeaderObject is Required for 'product_list' type") before this fix.
    assert sent["header_text"] == db.get_tenant(tenant_id).name


@pytest.mark.asyncio
async def test_native_product_list_groups_by_category(wa, sessions, tenant_id):
    db.update_tenant_catalog_and_payment(tenant_id, meta_catalog_id="cat_123")
    _make_product(tenant_id, name="Hammer", price="10.00", category="Tools")
    _make_product(tenant_id, name="Shirt", price="10.00", category="Apparel")

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))
    titles = {s["title"] for s in wa.sent[-1][1]["sections"]}
    assert titles == {"Tools", "Apparel"}


@pytest.mark.asyncio
async def test_shop_falls_back_to_list_message_when_native_send_fails(wa, sessions, tenant_id):
    """meta_catalog_id being set only proves a feed was registered, not
    that Meta has actually approved every item yet -- a native send can
    still fail (simulated here via the fake client), and the customer must
    still get a working list-message browse instead of silence."""
    db.update_tenant_catalog_and_payment(tenant_id, meta_catalog_id="cat_123")
    _make_product(tenant_id, name="Widget", price="10.00")
    wa.fail_product_list = True

    await handle_incoming(wa, sessions, PHONE, tenant_id, tap("menu_shop"))

    assert wa.sent[-1][0] == "list"  # fell back successfully, not silence
    assert not any(kind == "product_list" for kind, _ in wa.sent)  # the failed attempt was never "sent"


def test_get_product_by_retailer_id_resolves_and_isolates_by_tenant(tenant_id, second_tenant_id):
    product = _make_product(tenant_id, name="Widget", price="10.00")

    assert db.get_product_by_retailer_id(tenant_id, product.catalog_retailer_id).id == product.id
    assert db.get_product_by_retailer_id(tenant_id, "does-not-exist") is None
    # Same retailer_id string never resolves under a different tenant.
    assert db.get_product_by_retailer_id(second_tenant_id, product.catalog_retailer_id) is None


@pytest.mark.asyncio
async def test_handle_native_order_creates_order(wa, tenant_id):
    tenant = db.get_tenant(tenant_id)
    product = _make_product(tenant_id, name="Widget", price="199.00")

    await handle_native_order(wa, tenant, PHONE, [
        {"product_retailer_id": product.catalog_retailer_id, "quantity": "2", "item_price": "199.00", "currency": "INR"},
    ])

    orders = db.get_orders_for_phone(tenant_id, PHONE)
    assert len(orders) == 1
    items = db.get_order_items(tenant_id, orders[0].id)
    assert len(items) == 1
    assert items[0].quantity == 2
    assert wa.sent[-1][0] == "text"
    assert f"Order #{orders[0].id}" in wa.sent[-1][1]["text"]


@pytest.mark.asyncio
async def test_handle_native_order_drops_unresolvable_retailer_id(wa, tenant_id):
    tenant = db.get_tenant(tenant_id)
    product = _make_product(tenant_id, name="Widget", price="199.00")

    await handle_native_order(wa, tenant, PHONE, [
        {"product_retailer_id": product.catalog_retailer_id, "quantity": "1", "item_price": "199.00", "currency": "INR"},
        {"product_retailer_id": "stale-retailer-id-not-in-db", "quantity": "1", "item_price": "50.00", "currency": "INR"},
    ])

    orders = db.get_orders_for_phone(tenant_id, PHONE)
    assert len(orders) == 1
    items = db.get_order_items(tenant_id, orders[0].id)
    assert len(items) == 1  # the unresolvable item was dropped, not fatal


@pytest.mark.asyncio
async def test_handle_native_order_all_items_unresolvable_sends_apology_creates_no_order(wa, tenant_id):
    tenant = db.get_tenant(tenant_id)

    await handle_native_order(wa, tenant, PHONE, [
        {"product_retailer_id": "nonexistent", "quantity": "1", "item_price": "10.00", "currency": "INR"},
    ])

    assert db.get_orders_for_phone(tenant_id, PHONE) == []
    assert wa.sent[-1][0] == "text"
    assert "couldn't process" in wa.sent[-1][1]["text"].lower()


@pytest.mark.asyncio
async def test_handle_native_order_generates_payment_link_when_configured(wa, tenant_id):
    _configure_razorpay(tenant_id)
    tenant = db.get_tenant(tenant_id)
    product = _make_product(tenant_id, name="Widget", price="199.00")

    with patch("payments.create_payment_link", return_value=("https://rzp.io/pay/abc", "plink_abc")):
        await handle_native_order(wa, tenant, PHONE, [
            {"product_retailer_id": product.catalog_retailer_id, "quantity": "1"},
        ])

    assert "https://rzp.io/pay/abc" in wa.sent[-1][1]["text"]


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

    # Language is preserved across the reset, not wiped (see _handle_idle's docstring).
    assert sessions.get(tenant_id, PHONE) == {"state": "IDLE", "context": {"language": "en"}}
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


# --- Language selection ---
# Uses PHONE2, not PHONE -- _default_to_english's autouse fixture only
# pre-seeds (tenant_id, PHONE), so these tests see the real from-scratch
# "no language chosen yet" behavior every other test in this file is
# deliberately shielded from.

def _lang_tap(lang_id: str):
    return tap(lang_id)


@pytest.mark.asyncio
async def test_first_contact_shows_language_prompt_not_main_menu(wa, sessions, tenant_id):
    await handle_incoming(wa, sessions, PHONE2, tenant_id, text_reply("hi"), "Test Store")

    assert wa.sent[-1][0] == "buttons"
    body = wa.sent[-1][1]
    assert "select your language" in body["body_text"] and "भाषा" in body["body_text"]
    assert {b["id"]: b["title"] for b in body["buttons"]} == {"lang_en": "English", "lang_hi": "हिन्दी"}
    # No main menu shown yet -- language wasn't chosen.
    assert not any(kind == "list" for kind, _ in wa.sent)


@pytest.mark.asyncio
async def test_selecting_english_shows_main_menu_in_english(wa, sessions, tenant_id):
    await handle_incoming(wa, sessions, PHONE2, tenant_id, text_reply("hi"), "Test Store")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, _lang_tap("lang_en"), "Test Store")

    assert wa.sent[-1][0] == "list"
    body = wa.sent[-1][1]
    assert "Welcome to Test Store" in body["body_text"]
    assert {row["title"] for section in body["sections"] for row in section["rows"]} == {
        "Shop Now", "My Orders", "Track Order", "Offers", "Account", "Talk to Us",
    }
    assert sessions.get(tenant_id, PHONE2)["context"]["language"] == "en"


@pytest.mark.asyncio
async def test_selecting_hindi_shows_main_menu_in_hindi(wa, sessions, tenant_id):
    await handle_incoming(wa, sessions, PHONE2, tenant_id, text_reply("hi"), "Test Store")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, _lang_tap("lang_hi"), "Test Store")

    assert wa.sent[-1][0] == "list"
    body = wa.sent[-1][1]
    assert "नमस्ते!" in body["body_text"] and "Test Store" in body["body_text"]
    assert {row["title"] for section in body["sections"] for row in section["rows"]} == {
        "अभी खरीदें", "मेरे ऑर्डर", "ऑर्डर ट्रैक करें", "ऑफ़र", "खाता", "हमसे बात करें",
    }
    assert sessions.get(tenant_id, PHONE2)["context"]["language"] == "hi"


@pytest.mark.asyncio
async def test_hindi_reset_keywords_work_and_preserve_hindi(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="10.00")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, text_reply("hi"), "Test Store")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, _lang_tap("lang_hi"), "Test Store")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap(f"product_{widget.id}"))
    assert sessions.get(tenant_id, PHONE2)["state"] == "PRODUCT_DETAIL"

    for keyword in ("नमस्ते", "मेनू"):
        await handle_incoming(wa, sessions, PHONE2, tenant_id, text_reply(keyword), "Test Store")
        assert sessions.get(tenant_id, PHONE2) == {"state": "IDLE", "context": {"language": "hi"}}
        assert wa.sent[-1][0] == "list"
        assert wa.sent[-1][1]["sections"][0]["title"] == "मुख्य मेनू"  # main menu shown, still in Hindi, not re-prompted


@pytest.mark.asyncio
async def test_full_shop_checkout_flow_in_english(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="199.00")

    await handle_incoming(wa, sessions, PHONE2, tenant_id, text_reply("hi"), "Test Store")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, _lang_tap("lang_en"), "Test Store")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap(f"product_{widget.id}"))
    assert {b["title"] for b in wa.sent[-1][1]["buttons"]} == {"Add to Cart", "Back to Products"}

    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("add_to_cart"))
    assert "Added Widget to your cart" in wa.sent[-1][1]["body_text"]

    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("view_cart"))
    assert "Your Cart:" in wa.sent[-1][1]["body_text"]

    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("checkout"))
    assert "Order #" in wa.sent[-1][1]["text"] and "placed!" in wa.sent[-1][1]["text"]

    order = db.get_orders_for_phone(tenant_id, PHONE2)[0]
    assert order.language == "en"


@pytest.mark.asyncio
async def test_full_shop_checkout_flow_in_hindi(wa, sessions, tenant_id):
    widget = _make_product(tenant_id, name="Widget", price="199.00")

    await handle_incoming(wa, sessions, PHONE2, tenant_id, text_reply("hi"), "Test Store")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, _lang_tap("lang_hi"), "Test Store")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("menu_shop"))
    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap(f"product_{widget.id}"))
    assert {b["title"] for b in wa.sent[-1][1]["buttons"]} == {"कार्ट में जोड़ें", "उत्पादों पर वापस जाएं"}

    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("add_to_cart"))
    assert "आपके कार्ट में जोड़ दिया गया" in wa.sent[-1][1]["body_text"]

    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("view_cart"))
    assert "आपका कार्ट:" in wa.sent[-1][1]["body_text"]

    await handle_incoming(wa, sessions, PHONE2, tenant_id, tap("checkout"))
    assert "दर्ज हो गया" in wa.sent[-1][1]["text"]

    order = db.get_orders_for_phone(tenant_id, PHONE2)[0]
    assert order.language == "hi"
    items = db.get_order_items(tenant_id, order.id)
    assert len(items) == 1
    assert items[0].product_id == widget.id  # the flow actually completed correctly, not just the text


@pytest.mark.asyncio
async def test_language_choice_isolated_across_tenants(wa, sessions, tenant_id, second_tenant_id):
    """Two different tenants, same customer phone number -- the session
    store keys by (tenant_id, phone), so one tenant's chosen language must
    never leak into the other's conversation with that same customer."""
    await handle_incoming(wa, sessions, PHONE2, tenant_id, text_reply("hi"), "Store A")
    await handle_incoming(wa, sessions, PHONE2, tenant_id, _lang_tap("lang_hi"), "Store A")

    await handle_incoming(wa, sessions, PHONE2, second_tenant_id, text_reply("hi"), "Store B")
    assert wa.sent[-1][0] == "buttons"  # tenant B's conversation with the same phone starts fresh -- language prompt, not Hindi main menu
    await handle_incoming(wa, sessions, PHONE2, second_tenant_id, _lang_tap("lang_en"), "Store B")

    assert sessions.get(tenant_id, PHONE2)["context"]["language"] == "hi"
    assert sessions.get(second_tenant_id, PHONE2)["context"]["language"] == "en"


@pytest.mark.asyncio
async def test_native_order_messages_use_default_language(wa, tenant_id):
    """Documents a real, structural limitation (see handle_native_order's
    docstring): there is no conversational touchpoint for a native-catalog
    customer to ever choose a language in, so these messages are always
    English regardless of what a tap-driven customer on the same tenant
    might have chosen."""
    tenant = db.get_tenant(tenant_id)
    await handle_native_order(wa, tenant, PHONE2, [{"product_retailer_id": "nonexistent", "quantity": "1"}])
    assert wa.sent[-1] == ("text", {
        "to": PHONE2,
        "text": "Sorry, we couldn't process your order — none of the items are available right now.",
    })


@pytest.mark.asyncio
async def test_payment_success_message_uses_order_language_not_a_live_session(wa, tenant_id):
    """handle_payment_success has no session to read a language from (the
    conversation may have expired long before payment happens) -- it must
    use order.language, snapshotted at checkout time."""
    order = db.create_order(tenant_id, PHONE2, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("100.00"), total=Decimal("100.00"), language="hi")

    await handle_payment_success(wa, tenant_id, order.id, "pay_abc123")

    assert wa.sent[-1][0] == "text"
    assert "भुगतान प्राप्त हुआ" in wa.sent[-1][1]["text"]


@pytest.mark.asyncio
async def test_payment_failure_message_uses_order_language(wa, tenant_id):
    order = db.create_order(tenant_id, PHONE2, status=db.ORDER_STATUS_PENDING_PAYMENT,
                             subtotal=Decimal("100.00"), total=Decimal("100.00"), language="hi")
    tenant = db.get_tenant(tenant_id)

    await handle_payment_failure(wa, tenant, order.id, link_expired=False)

    assert wa.sent[-1][0] == "text"
    assert "भुगतान सफल नहीं हुआ" in wa.sent[-1][1]["text"]
