# core/commerce_flow.py
"""
Menu-based commerce conversation state machine (SPEC.md Section 3, built ahead
of Phase 2's order-received webhook handler since a customer needs *some* way
to place an order before Meta's native catalog/cart is wired up in Phase 1).

Same architecture as the pre-Phase-0 core/booking_flow.py this repo was
stripped of: every state sends a fixed WhatsApp interactive list or button
message with a closed set of options; a customer's reply is either a tapped
selection (interactive_reply) or free text/unsupported input, which re-prompts
the current menu. No AI/LLM anywhere in this file (SPEC.md Section 1).

Session (state + context) lives in whatever store core/history.get_session_store()
returns (Redis or in-memory) -- this module takes it as a parameter so it stays
a plain function library with no platform-specific dependency, same portability
rule the old booking_flow.py followed. Every db call and every session store
call here is scoped by tenant_id, resolved per-message in core/main.py from the
incoming webhook's phone_number_id.

Reused-vs-rebuilt note: the brief for this module asked to port over three
CareConnect-lineage patterns (a _cap_rows() 10-row helper, free-text
reset-keyword handling, and a "Talk to Reception" human-handoff flow) rather
than reinvent them, if they already existed somewhere in this codebase's own
history. They don't -- grepping every commit of this repo, including the
deleted core/booking_flow.py, turns up no _cap_rows()-equivalent (its
department/doctor/slot lists were always assumed to fit in 10 rows), no
reset-keyword shortcut (only IDLE's own fallback re-showed the main menu),
and no human-handoff flow of any kind. This repo has no direct access to a
separate CareConnect repository to port real code from, so all three are
built fresh here, in the spirit described:
  - _cap_rows() below is a genuinely new, reusable 10-row-cap helper.
  - RESET_KEYWORDS is a new global shortcut (checked before state dispatch,
    unlike the old IDLE-only fallback), so it works mid-flow, not just at
    the main menu.
  - "Talk to Us" is a plain "coming soon" placeholder, not a real
    human-handoff flow -- there's no existing handoff infrastructure (e.g. a
    staff-notification channel) in this repo to hook it up to yet.
"""
import logging
from datetime import datetime
from decimal import Decimal

import db.repository as db
from core.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

MAX_LIST_ROWS = 10  # Meta's per-message cap (core/whatsapp.py's send_list docstring)

# Free-text shortcuts that reset the conversation to the main menu from
# *any* state, not just IDLE -- e.g. mid-shopping, typing "menu" bails out
# without needing to tap through "Back" options.
RESET_KEYWORDS = {"hi", "hello", "hey", "menu", "restart", "start"}

STATE_SHOP_LIST = "SHOP_LIST"
STATE_PRODUCT_DETAIL = "PRODUCT_DETAIL"
STATE_POST_ADD = "POST_ADD"
STATE_CART = "CART"
STATE_ORDER_LIST = "ORDER_LIST"  # shared by "My Orders" and "Track Order" -- see _start_orders_list
STATE_ORDER_DETAIL = "ORDER_DETAIL"

MENU_SHOP = "menu_shop"
MENU_MY_ORDERS = "menu_my_orders"
MENU_TRACK_ORDER = "menu_track_order"
MENU_OFFERS = "menu_offers"
MENU_ACCOUNT = "menu_account"
MENU_TALK_TO_US = "menu_talk_to_us"

SHOP_MORE = "shop_more"
ADD_TO_CART = "add_to_cart"
BACK_TO_PRODUCTS = "back_to_products"
VIEW_CART = "view_cart"
CONTINUE_SHOPPING = "continue_shopping"
CHECKOUT = "checkout"
MAIN_MENU_BTN = "main_menu"

_PLEASE_CHOOSE = "Please choose an option from the list above"

_STATUS_LABELS = {
    db.ORDER_STATUS_BROWSING: "Browsing",
    db.ORDER_STATUS_PENDING_PAYMENT: "Pending Payment",
    db.ORDER_STATUS_PAID: "Paid",
    db.ORDER_STATUS_FAILED: "Failed",
    db.ORDER_STATUS_CANCELLED: "Cancelled",
    db.ORDER_STATUS_FULFILLED: "Fulfilled",
}


# --- Small formatting/helper utilities ---

def _cap_rows(rows: list[dict], more_row: dict | None = None) -> list[dict]:
    """Caps a WhatsApp interactive list's rows at Meta's MAX_LIST_ROWS-per-message
    limit. If `more_row` is given, one slot is always reserved for it (so the
    combined total never exceeds the cap); otherwise rows are hard-truncated
    with nothing lost silently beyond the cap -- callers that need every row
    reachable (e.g. product browsing) should paginate via `more_row` instead
    of relying on truncation."""
    limit = MAX_LIST_ROWS - 1 if more_row is not None else MAX_LIST_ROWS
    capped = rows[:limit]
    return capped + [more_row] if more_row is not None else capped


def _fmt_money(amount: Decimal) -> str:
    """₹ prefix, thousands-separated, whole-number amounts shown without
    decimals (matching the reference design's "₹2,798" style) -- hardcoded to
    ₹ regardless of products.currency for now (this build's India-first
    default, SPEC.md Section 3.3); multi-currency symbol display isn't built."""
    amount = amount or Decimal("0")
    if amount == amount.to_integral_value():
        return f"₹{int(amount):,}"
    return f"₹{amount:,.2f}"


def _fmt_datetime(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    try:
        return datetime.fromisoformat(iso_str).strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso_str


def _product_row_id(product_id: int) -> str:
    return f"product_{product_id}"


def _parse_product_row_id(row_id: str) -> int | None:
    if not row_id.startswith("product_"):
        return None
    try:
        return int(row_id[len("product_"):])
    except ValueError:
        return None


def _order_row_id(order_id: int) -> str:
    return f"order_{order_id}"


def _parse_order_row_id(row_id: str) -> int | None:
    if not row_id.startswith("order_"):
        return None
    try:
        return int(row_id[len("order_"):])
    except ValueError:
        return None


def _cart_subtotal(tenant_id: int, cart: dict) -> Decimal:
    """Cart totals shown while shopping reflect *current* product prices
    (unlike an already-checked-out order's line items, which snapshot the
    price at creation time -- see db.create_order_item's docstring)."""
    total = Decimal("0")
    for product_id_str, qty in cart.items():
        product = db.get_product(tenant_id, int(product_id_str))
        if product:
            total += product.price * qty
    return total


# --- Outgoing message builders ---

async def _send_main_menu(wa: WhatsAppClient, phone: str, tenant_name: str) -> None:
    rows = [
        {"id": MENU_SHOP, "title": "Shop Now"},
        {"id": MENU_MY_ORDERS, "title": "My Orders"},
        {"id": MENU_TRACK_ORDER, "title": "Track Order"},
        {"id": MENU_OFFERS, "title": "Offers"},
        {"id": MENU_ACCOUNT, "title": "Account"},
        {"id": MENU_TALK_TO_US, "title": "Talk to Us"},
    ]
    await wa.send_list(
        to=phone,
        body_text=f"Hi! Welcome to {tenant_name}. What would you like to do?",
        button_text="Main Menu",
        sections=[{"title": "Main Menu", "rows": rows}],
    )


def _product_row(product: db.Product) -> dict:
    return {
        "id": _product_row_id(product.id),
        "title": (product.name or "")[:24],
        "description": _fmt_money(product.price),
    }


async def _send_product_list(wa: WhatsAppClient, phone: str, tenant_id: int, offset: int) -> bool:
    """Sends the product list page starting at `offset`. Returns False (sends
    nothing) if the tenant has no active products at all -- callers handle
    that as an empty-catalog case, not an error.

    Pagination choice: with more than MAX_LIST_ROWS active products, this
    shows the first (MAX_LIST_ROWS - 1) and a "See more products" row rather
    than silently truncating the catalog -- tapping it re-runs this at
    offset + (MAX_LIST_ROWS - 1), so every product stays reachable. The
    alternative (just show the first 10, no more) was simpler but would make
    products past #9 permanently unbrowsable for any tenant with a real
    catalog; flagging this as the deliberate choice, not the only option."""
    products = db.get_active_products(tenant_id)
    if not products:
        return False

    window = products[offset:offset + MAX_LIST_ROWS]
    has_more = offset + MAX_LIST_ROWS < len(products)
    rows = [_product_row(p) for p in window]
    more_row = {"id": SHOP_MORE, "title": "See more products"} if has_more else None
    rows = _cap_rows(rows, more_row)

    await wa.send_list(
        to=phone,
        body_text="Browse our products:",
        button_text="View Products",
        sections=[{"title": "Products", "rows": rows}],
    )
    return True


async def _send_product_detail(wa: WhatsAppClient, phone: str, product: db.Product) -> None:
    body = f"{product.name}\n{_fmt_money(product.price)}"
    if product.description:
        body += f"\n\n{product.description}"
    await wa.send_buttons(
        to=phone,
        body_text=body,
        buttons=[
            {"id": ADD_TO_CART, "title": "Add to Cart"},
            {"id": BACK_TO_PRODUCTS, "title": "Back to Products"},
        ],
    )


async def _send_add_to_cart_prompt(wa: WhatsAppClient, phone: str, product_name: str, cart: dict, tenant_id: int) -> None:
    total_qty = sum(cart.values())
    subtotal = _cart_subtotal(tenant_id, cart)
    await wa.send_buttons(
        to=phone,
        body_text=(
            f"Added {product_name} to your cart.\n\n"
            f"Cart ({total_qty} item{'s' if total_qty != 1 else ''}) — {_fmt_money(subtotal)}"
        ),
        buttons=[
            {"id": VIEW_CART, "title": "View Cart"},
            {"id": CONTINUE_SHOPPING, "title": "Continue Shopping"},
        ],
    )


async def _send_cart(wa: WhatsAppClient, phone: str, tenant_id: int, cart: dict) -> bool:
    """Returns False (sends an "empty cart" text instead) if the cart has no
    items -- can happen if every product in it was deactivated since being
    added."""
    lines = []
    subtotal = Decimal("0")
    for product_id_str, qty in cart.items():
        product = db.get_product(tenant_id, int(product_id_str))
        if not product:
            continue
        line_total = product.price * qty
        subtotal += line_total
        lines.append(f"{qty} x {product.name} — {_fmt_money(line_total)}")

    if not lines:
        await wa.send_text(phone, "Your cart is empty.")
        return False

    body = "Your Cart:\n\n" + "\n".join(lines) + f"\n\nSubtotal: {_fmt_money(subtotal)}"
    await wa.send_buttons(
        to=phone,
        body_text=body,
        buttons=[
            {"id": CHECKOUT, "title": "Checkout"},
            {"id": CONTINUE_SHOPPING, "title": "Continue Shopping"},
        ],
    )
    return True


async def _send_orders_list(wa: WhatsAppClient, phone: str, tenant_id: int, customer_phone: str) -> bool:
    """Returns False (sends nothing) if the customer has no orders at this
    tenant yet. get_orders_for_phone already sorts most-recent-first, so
    capping at MAX_LIST_ROWS naturally keeps the soonest/most recent ones --
    no "see more" here (unlike the product list) since older order history
    being one tap further away isn't the same usability problem as products
    being permanently unbrowsable."""
    orders = db.get_orders_for_phone(tenant_id, customer_phone)
    if not orders:
        return False

    rows = _cap_rows([
        {
            "id": _order_row_id(o.id),
            "title": f"Order #{o.id}",
            "description": f"{_STATUS_LABELS.get(o.status, o.status)} — {_fmt_money(o.total)}",
        }
        for o in orders
    ])
    await wa.send_list(
        to=phone,
        body_text="Your recent orders:",
        button_text="View Orders",
        sections=[{"title": "Orders", "rows": rows}],
    )
    return True


async def _send_order_detail(wa: WhatsAppClient, phone: str, tenant_id: int, order: db.Order) -> None:
    items = db.get_order_items(tenant_id, order.id)
    lines = []
    for item in items:
        product = db.get_product(tenant_id, item.product_id)
        name = product.name if product else f"Product #{item.product_id}"
        lines.append(f"{item.quantity} x {name} — {_fmt_money(item.unit_price_at_order_time * item.quantity)}")

    body = (
        f"Order #{order.id}\n"
        f"Status: {_STATUS_LABELS.get(order.status, order.status)}\n"
        f"Placed: {_fmt_datetime(order.created_at)}\n\n"
        + "\n".join(lines)
        + f"\n\nTotal: {_fmt_money(order.total)}"
    )
    await wa.send_buttons(
        to=phone,
        body_text=body,
        buttons=[{"id": MAIN_MENU_BTN, "title": "Main Menu"}],
    )


# --- Flow starters (called from IDLE) ---

async def _start_shop(wa: WhatsAppClient, sessions, phone: str, tenant_id: int) -> None:
    sent = await _send_product_list(wa, phone, tenant_id, 0)
    if not sent:
        sessions.reset(tenant_id, phone)
        await wa.send_text(phone, "Sorry, we don't have any products available right now.")
        return
    sessions.set(tenant_id, phone, STATE_SHOP_LIST, {"shop_offset": 0, "cart": {}})


async def _start_orders_list(wa: WhatsAppClient, sessions, phone: str, tenant_id: int) -> None:
    sent = await _send_orders_list(wa, phone, tenant_id, phone)
    if not sent:
        sessions.reset(tenant_id, phone)
        await wa.send_text(phone, "You don't have any orders yet.")
        return
    sessions.set(tenant_id, phone, STATE_ORDER_LIST, {})


# --- State handlers ---
# Each handler either advances the session to the next state (on a valid tap)
# or refreshes the session at the *same* state (on free text/unsupported
# input), same pattern as the old booking_flow.py.

async def _handle_idle(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, tenant_name: str) -> None:
    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == MENU_SHOP:
            await _start_shop(wa, sessions, phone, tenant_id)
            return
        if rid in (MENU_MY_ORDERS, MENU_TRACK_ORDER):
            await _start_orders_list(wa, sessions, phone, tenant_id)
            return
        if rid == MENU_OFFERS:
            sessions.reset(tenant_id, phone)
            await wa.send_text(phone, "Offers are coming soon!")
            return
        if rid == MENU_ACCOUNT:
            sessions.reset(tenant_id, phone)
            await wa.send_text(phone, "Account management is coming soon!")
            return
        if rid == MENU_TALK_TO_US:
            sessions.reset(tenant_id, phone)
            await wa.send_text(phone, "Talk to Us is coming soon — for now, please reach out to us directly.")
            return
    # Any other message while IDLE (first contact, free text, stale/unknown id):
    # always responds with the welcome message + main menu.
    sessions.reset(tenant_id, phone)
    await _send_main_menu(wa, phone, tenant_name)


async def _handle_shop_list(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == SHOP_MORE:
            products = db.get_active_products(tenant_id)
            next_offset = context.get("shop_offset", 0) + (MAX_LIST_ROWS - 1)
            if next_offset >= len(products):
                next_offset = 0
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, {**context, "shop_offset": next_offset})
            await _send_product_list(wa, phone, tenant_id, next_offset)
            return

        product_id = _parse_product_row_id(rid)
        if product_id is not None:
            product = db.get_product(tenant_id, product_id)
            if product and product.is_active:
                sessions.set(tenant_id, phone, STATE_PRODUCT_DETAIL, {**context, "product_id": product.id})
                await _send_product_detail(wa, phone, product)
                return
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
            await wa.send_text(phone, "Sorry, that item is no longer available.")
            await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0))
            return

    sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
    await wa.send_text(phone, _PLEASE_CHOOSE)
    await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0))


async def _handle_product_detail(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    product_id = context.get("product_id")
    if product_id is None:
        # Corrupted/incomplete session context -- fail safe back to the main
        # menu, same "the store" fallback name booking_flow.py used ("the
        # hospital") for this exact class of defensive case.
        sessions.reset(tenant_id, phone)
        await _send_main_menu(wa, phone, "the store")
        return

    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == BACK_TO_PRODUCTS:
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
            await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0))
            return
        if rid == ADD_TO_CART:
            product = db.get_product(tenant_id, product_id)
            if not product or not product.is_active:
                sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
                await wa.send_text(phone, "Sorry, that item is no longer available.")
                await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0))
                return
            cart = dict(context.get("cart", {}))
            key = str(product.id)
            cart[key] = cart.get(key, 0) + 1
            new_context = {**context, "cart": cart}
            sessions.set(tenant_id, phone, STATE_POST_ADD, new_context)
            await _send_add_to_cart_prompt(wa, phone, product.name, cart, tenant_id)
            return

    sessions.set(tenant_id, phone, STATE_PRODUCT_DETAIL, context)
    await wa.send_text(phone, _PLEASE_CHOOSE)
    product = db.get_product(tenant_id, product_id)
    if product:
        await _send_product_detail(wa, phone, product)


async def _handle_post_add(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == VIEW_CART:
            sessions.set(tenant_id, phone, STATE_CART, context)
            await _send_cart(wa, phone, tenant_id, context.get("cart", {}))
            return
        if rid == CONTINUE_SHOPPING:
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
            await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0))
            return

    sessions.set(tenant_id, phone, STATE_POST_ADD, context)
    await wa.send_text(phone, _PLEASE_CHOOSE)
    cart = context.get("cart", {})
    # Re-send the same view/continue prompt with an up-to-date cart summary.
    total_qty = sum(cart.values())
    subtotal = _cart_subtotal(tenant_id, cart)
    await wa.send_buttons(
        to=phone,
        body_text=f"Cart ({total_qty} item{'s' if total_qty != 1 else ''}) — {_fmt_money(subtotal)}",
        buttons=[
            {"id": VIEW_CART, "title": "View Cart"},
            {"id": CONTINUE_SHOPPING, "title": "Continue Shopping"},
        ],
    )


async def _handle_cart(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    cart = context.get("cart", {})
    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == CONTINUE_SHOPPING:
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
            await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0))
            return
        if rid == CHECKOUT:
            await _checkout(wa, sessions, phone, tenant_id, cart)
            return

    sessions.set(tenant_id, phone, STATE_CART, context)
    await wa.send_text(phone, _PLEASE_CHOOSE)
    await _send_cart(wa, phone, tenant_id, cart)


async def _checkout(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, cart: dict) -> None:
    """Creates a real orders row (ORDER_STATUS_PENDING_PAYMENT) + order_items
    from the cart, then stubs the payment step -- SPEC.md Section 3.3's
    Razorpay payment-link generation is the next build, not done here."""
    if not cart:
        sessions.reset(tenant_id, phone)
        await wa.send_text(phone, "Your cart is empty. Send any message to start shopping again.")
        return

    line_items: list[tuple[db.Product, int]] = []
    subtotal = Decimal("0")
    for product_id_str, qty in cart.items():
        product = db.get_product(tenant_id, int(product_id_str))
        if not product or not product.is_active:
            continue  # dropped between add-to-cart and checkout -- skip rather than fail the whole order
        line_items.append((product, qty))
        subtotal += product.price * qty

    if not line_items:
        sessions.reset(tenant_id, phone)
        await wa.send_text(phone, "Sorry, the items in your cart are no longer available. Please start shopping again.")
        return

    order = db.create_order(
        tenant_id, customer_phone=phone, status=db.ORDER_STATUS_PENDING_PAYMENT,
        subtotal=subtotal, total=subtotal,  # no tax/shipping logic yet (SPEC.md Section 3.2)
    )
    for product, qty in line_items:
        db.create_order_item(tenant_id, order.id, product.id, quantity=qty, unit_price_at_order_time=product.price)

    lines = "\n".join(f"{qty} x {product.name} — {_fmt_money(product.price * qty)}" for product, qty in line_items)
    await wa.send_text(
        phone,
        f"Order #{order.id} placed!\n\n{lines}\n\nTotal: {_fmt_money(subtotal)}\n\n"
        "Payment integration coming next — we'll follow up with a payment link shortly.",
    )
    sessions.reset(tenant_id, phone)


async def _handle_order_list(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    if reply["type"] == "interactive_reply":
        order_id = _parse_order_row_id(reply["id"])
        if order_id is not None:
            order = db.get_order(tenant_id, order_id)
            if order and order.customer_phone == phone:
                sessions.set(tenant_id, phone, STATE_ORDER_DETAIL, {"order_id": order.id})
                await _send_order_detail(wa, phone, tenant_id, order)
                return

    sessions.set(tenant_id, phone, STATE_ORDER_LIST, context)
    await wa.send_text(phone, _PLEASE_CHOOSE)
    await _send_orders_list(wa, phone, tenant_id, phone)


async def _handle_order_detail(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    if reply["type"] == "interactive_reply" and reply["id"] == MAIN_MENU_BTN:
        sessions.reset(tenant_id, phone)
        await _send_main_menu(wa, phone, "the store")
        return

    order_id = context.get("order_id")
    order = db.get_order(tenant_id, order_id) if order_id is not None else None
    if not order:
        sessions.reset(tenant_id, phone)
        await wa.send_text(phone, "Something went wrong finding that order. Please start over.")
        return

    sessions.set(tenant_id, phone, STATE_ORDER_DETAIL, context)
    await wa.send_text(phone, _PLEASE_CHOOSE)
    await _send_order_detail(wa, phone, tenant_id, order)


_HANDLERS = {
    STATE_SHOP_LIST: _handle_shop_list,
    STATE_PRODUCT_DETAIL: _handle_product_detail,
    STATE_POST_ADD: _handle_post_add,
    STATE_CART: _handle_cart,
    STATE_ORDER_LIST: _handle_order_list,
    STATE_ORDER_DETAIL: _handle_order_detail,
}


async def handle_incoming(
    wa: WhatsAppClient,
    sessions,
    phone: str,
    tenant_id: int,
    reply: dict,
    tenant_name: str = "the store",
) -> None:
    """
    Entry point: check for a global reset keyword first (works from *any*
    state, unlike the old booking_flow.py's IDLE-only fallback), then look up
    the customer's current session (sessions.get already resets stale/timed-out
    sessions to IDLE) and dispatch to the matching state handler.
    """
    if reply["type"] == "text" and reply.get("text", "").strip().lower() in RESET_KEYWORDS:
        sessions.reset(tenant_id, phone)
        await _send_main_menu(wa, phone, tenant_name)
        return

    session = sessions.get(tenant_id, phone)
    state = session["state"]
    context = session["context"]

    handler = _HANDLERS.get(state)
    if handler is None:
        # IDLE, or any unrecognized/stale state value -> treat as IDLE.
        await _handle_idle(wa, sessions, phone, tenant_id, reply, tenant_name)
        return

    await handler(wa, sessions, phone, tenant_id, reply, context)
