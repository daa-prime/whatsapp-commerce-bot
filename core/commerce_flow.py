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

Language: every fixed bot-facing string is translated (English/Hindi,
core/strings.py) -- merchant-configured content (business name, product
names/descriptions) is never auto-translated, only the bot's own UI text.
The customer's chosen language lives in context["language"], the same
session-context bag as everything else (cart, shop_offset, ...) -- it rides
along for free through every handler's existing `{**context, ...}` merges.
A brand-new conversation (no language chosen yet) sees a language-choice
button message before the main menu; a *reset* (RESET_KEYWORDS, a stale
main-menu tap, any unrecognized-state fallback) preserves whatever language
was already chosen rather than re-asking -- only a conversation that never
had one picked (or one that expired via the 30-min session timeout, which
discards context entirely) sees the prompt again. See _handle_idle.

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

import payments
import db.repository as db
from core.strings import DEFAULT_LANG, LANG_EN, LANG_HI, LANGUAGE_NAMES, status_label, t
from core.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)

MAX_LIST_ROWS = 10  # Meta's per-message cap (core/whatsapp.py's send_list docstring)

STATE_IDLE = "IDLE"  # matches core/history.py's DEFAULT_STATE -- see _handle_idle

# Free-text shortcuts that reset the conversation to the main menu from
# *any* state, not just IDLE -- e.g. mid-shopping, typing "menu" bails out
# without needing to tap through "Back" options. Hindi equivalents included
# so this works the same way regardless of which language a customer is
# typing in -- these are recognized independent of the session's chosen
# language (checked before we necessarily know it, e.g. on a customer's
# very first message).
RESET_KEYWORDS = {
    "hi", "hello", "hey", "menu", "restart", "start",
    "नमस्ते", "मेनू", "शुरू", "हाय",
}

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

LANG_EN_BTN = "lang_en"
LANG_HI_BTN = "lang_hi"

# Main-menu button taps are recognized from *any* session state, not just
# IDLE (see handle_incoming) -- WhatsApp keeps old interactive messages
# tappable indefinitely, so a customer mid-flow can tap a stale main-menu
# button from an earlier message; without this, that tap would be
# misrouted into whatever handler the *current* state maps to instead.
_MAIN_MENU_IDS = {MENU_SHOP, MENU_MY_ORDERS, MENU_TRACK_ORDER, MENU_OFFERS, MENU_ACCOUNT, MENU_TALK_TO_US}


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


def _product_name(tenant_id: int, product_id: int) -> str:
    product = db.get_product(tenant_id, product_id)
    return product.name if product else f"Product #{product_id}"


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

async def _send_language_prompt(wa: WhatsAppClient, phone: str) -> None:
    """Shown once per genuinely new conversation, before the main menu --
    necessarily bilingual (not looked up via core.strings.t) since we don't
    know the customer's language yet; that's the whole point of asking."""
    await wa.send_buttons(
        to=phone,
        body_text="Please select your language / कृपया अपनी भाषा चुनें",
        buttons=[
            {"id": LANG_EN_BTN, "title": LANGUAGE_NAMES[LANG_EN]},
            {"id": LANG_HI_BTN, "title": LANGUAGE_NAMES[LANG_HI]},
        ],
    )


async def _send_main_menu(wa: WhatsAppClient, phone: str, tenant_name: str, lang: str) -> None:
    rows = [
        {"id": MENU_SHOP, "title": t("menu_shop_now", lang)},
        {"id": MENU_MY_ORDERS, "title": t("menu_my_orders", lang)},
        {"id": MENU_TRACK_ORDER, "title": t("menu_track_order", lang)},
        {"id": MENU_OFFERS, "title": t("menu_offers", lang)},
        {"id": MENU_ACCOUNT, "title": t("menu_account", lang)},
        {"id": MENU_TALK_TO_US, "title": t("menu_talk_to_us", lang)},
    ]
    await wa.send_list(
        to=phone,
        body_text=t("welcome", lang, tenant_name=tenant_name),
        button_text=t("main_menu_button", lang),
        sections=[{"title": t("main_menu_button", lang), "rows": rows}],
    )


def _product_row(product: db.Product) -> dict:
    return {
        "id": _product_row_id(product.id),
        "title": (product.name or "")[:24],
        "description": _fmt_money(product.price),
    }


def _group_products_by_category(products: list[db.Product]) -> list[tuple[str, list[db.Product]]]:
    """Groups active products into (category_label, products) pairs, each
    category's products keeping their original (db.get_active_products, id)
    order. Categories are ordered by first appearance in that same order --
    not alphabetically -- so a merchant's catalog order isn't reshuffled just
    because "Accessories" sorts before "Widgets". Blank/NULL category
    (products.category is optional, SPEC.md Section 4) becomes the internal
    "Other" grouping key -- a stable, language-independent key; callers
    translate it for display (only this fallback needs translation, real
    merchant-set category names never do -- see _category_display_label)."""
    groups: dict[str, list[db.Product]] = {}
    order: list[str] = []
    for p in products:
        label = (p.category or "").strip() or "Other"
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(p)
    return [(label, groups[label]) for label in order]


def _category_display_label(label: str, lang: str) -> str:
    """Translates only the "Other" fallback grouping key -- real merchant-set
    category names are content, not UI chrome, and are never auto-translated."""
    return t("category_other", lang) if label == "Other" else label


_MAX_NATIVE_PRODUCTS = 30  # Meta's product_list cap: up to 30 items across up to 10 sections
_MAX_NATIVE_SECTIONS = 10


async def _send_native_product_list(
    wa: WhatsAppClient, phone: str, tenant: db.Tenant, products: list[db.Product], lang: str,
) -> bool:
    """Meta's native multi-product message (core/whatsapp.py's
    send_product_list) -- real product cards with images and full names,
    rendered by WhatsApp itself from the catalog linked to tenant.
    meta_catalog_id, replacing the list-message workaround below entirely
    for any tenant that has one set. No 24-char title truncation, no
    per-row-image limitation (both hard platform limits of list messages,
    not something the old workaround could ever fully solve).

    No in-message pagination beyond the first _MAX_NATIVE_PRODUCTS items --
    unlike send_list, a product_list message's sections only ever contain
    product_items; there's no equivalent of the list-message "See more
    products" row to inject. A tenant with more active products than that
    only sees the first 30 (first-appearance category order) via native
    browsing today -- a known scope limit, not solved here, same as this
    module's other explicitly-flagged deliberate scope cuts.

    Returns whatever wa.send_product_list() reports -- True if Meta
    accepted the send, False otherwise -- so _send_product_list below can
    fall back to the list-message flow rather than leaving the customer
    with silence. tenant.meta_catalog_id being set only proves a feed was
    *registered*, not that Meta has actually ingested/approved every item
    yet (catalog/feed.py's module docstring); this is exactly the failure
    case that gap produces."""
    grouped = _group_products_by_category(products)
    sections = []
    remaining = _MAX_NATIVE_PRODUCTS
    for label, plist in grouped:
        if remaining <= 0 or len(sections) >= _MAX_NATIVE_SECTIONS:
            break
        window = [p for p in plist if p.catalog_retailer_id][:remaining]
        if not window:
            continue
        sections.append({
            "title": _category_display_label(label, lang)[:24],
            "product_items": [{"product_retailer_id": p.catalog_retailer_id} for p in window],
        })
        remaining -= len(window)

    return await wa.send_product_list(
        to=phone,
        catalog_id=tenant.meta_catalog_id,
        sections=sections,
        body_text=t("browse_products_body", lang),
        header_text=tenant.name,  # required by Meta for product_list -- see send_product_list's docstring
    )


async def _send_product_list(wa: WhatsAppClient, phone: str, tenant_id: int, offset: int, lang: str) -> bool:
    """Sends the product list page starting at `offset`. Returns False
    (sends nothing) if the tenant has no active products at all -- callers
    handle that as an empty-catalog case, not an error.

    Branches on tenant.meta_catalog_id: if set, sends Meta's native
    product_list message (_send_native_product_list) instead of the
    list-message workaround below -- every tenant without one set (the
    default, and the automatic fallback for any tenant whose catalog isn't
    linked yet) keeps getting exactly today's behavior, unchanged.

    The list-message path below groups into one section per category
    (Meta's list messages support multiple named sections natively -- this
    isn't a workaround, it's the feature that shows a category as a heading
    in the browse UI). `offset` indexes into the flat, category-grouped
    product order (every category's products consecutive, categories in
    first-appearance order), not raw db id order -- so paginating keeps a
    category's products together across "See more" pages rather than
    splitting one down the middle. Products are then re-grouped into their
    sections after windowing/capping, using the exact same
    MAX_LIST_ROWS-row-total math as before grouping was added (see
    _cap_rows) -- section titles/headers don't count against Meta's 10-row
    cap, only rows do.

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

    tenant = db.get_tenant(tenant_id)
    if tenant and tenant.meta_catalog_id:
        sent_native = await _send_native_product_list(wa, phone, tenant, products, lang)
        if sent_native:
            return True
        # Native send failed (e.g. Meta hasn't finished approving the
        # catalog/feed items yet, even though meta_catalog_id is set) --
        # fall through to the list-message flow below instead of leaving
        # the customer with silence. Same graceful-degradation principle
        # already used for a WhatsApp send failure elsewhere in this app
        # (Phase 8's delivery-failure handling) -- a failed native send is
        # never the end of the conversation.
        logger.warning(
            "Native product_list send failed for tenant_id=%s, falling back to list-message flow", tenant_id,
        )

    grouped = _group_products_by_category(products)
    flat = [p for _, plist in grouped for p in plist]

    window = flat[offset:offset + MAX_LIST_ROWS]
    has_more = offset + MAX_LIST_ROWS < len(flat)
    more_row = {"id": SHOP_MORE, "title": t("see_more_products", lang)} if has_more else None
    limit = MAX_LIST_ROWS - 1 if more_row is not None else MAX_LIST_ROWS
    capped_ids = {p.id for p in window[:limit]}

    sections = []
    for label, plist in grouped:
        rows = [_product_row(p) for p in plist if p.id in capped_ids]
        if rows:
            sections.append({"title": _category_display_label(label, lang)[:24], "rows": rows})
    if more_row is not None:
        sections.append({"title": t("category_more_section", lang), "rows": [more_row]})

    await wa.send_list(
        to=phone,
        body_text=t("browse_products_body", lang),
        button_text=t("view_products_button", lang),
        sections=sections,
    )
    return True


async def _send_product_detail(wa: WhatsAppClient, phone: str, product: db.Product, lang: str) -> None:
    body = f"{product.name}\n{_fmt_money(product.price)}"
    if product.description:
        body += f"\n\n{product.description}"
    await wa.send_buttons(
        to=phone,
        body_text=body,
        header_image_url=product.image_url or None,
        buttons=[
            {"id": ADD_TO_CART, "title": t("add_to_cart_button", lang)},
            {"id": BACK_TO_PRODUCTS, "title": t("back_to_products_button", lang)},
        ],
    )


def _cart_count_summary(cart: dict, tenant_id: int, lang: str) -> str:
    total_qty = sum(cart.values())
    subtotal = _cart_subtotal(tenant_id, cart)
    unit = "item" if total_qty == 1 else "items"  # only referenced by the English template
    return t("cart_count_summary", lang, count=total_qty, unit=unit, amount=_fmt_money(subtotal))


async def _send_add_to_cart_prompt(
    wa: WhatsAppClient, phone: str, product_name: str, cart: dict, tenant_id: int, lang: str,
) -> None:
    await wa.send_buttons(
        to=phone,
        body_text=(
            f"{t('added_to_cart', lang, product_name=product_name)}\n\n"
            f"{_cart_count_summary(cart, tenant_id, lang)}"
        ),
        buttons=[
            {"id": VIEW_CART, "title": t("view_cart_button", lang)},
            {"id": CONTINUE_SHOPPING, "title": t("continue_shopping_button", lang)},
        ],
    )


async def _send_cart(wa: WhatsAppClient, phone: str, tenant_id: int, cart: dict, lang: str) -> bool:
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
        await wa.send_text(phone, t("cart_empty", lang))
        return False

    body = (
        f"{t('your_cart_header', lang)}\n\n" + "\n".join(lines)
        + f"\n\n{t('subtotal_label', lang, amount=_fmt_money(subtotal))}"
    )
    await wa.send_buttons(
        to=phone,
        body_text=body,
        buttons=[
            {"id": CHECKOUT, "title": t("checkout_button", lang)},
            {"id": CONTINUE_SHOPPING, "title": t("continue_shopping_button", lang)},
        ],
    )
    return True


async def _send_orders_list(wa: WhatsAppClient, phone: str, tenant_id: int, customer_phone: str, lang: str) -> bool:
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
            "title": t("order_number", lang, id=o.id),
            "description": f"{status_label(o.status, lang)} — {_fmt_money(o.total)}",
        }
        for o in orders
    ])
    await wa.send_list(
        to=phone,
        body_text=t("your_recent_orders", lang),
        button_text=t("view_orders_button", lang),
        sections=[{"title": t("orders_section_title", lang), "rows": rows}],
    )
    return True


async def _send_order_detail(wa: WhatsAppClient, phone: str, tenant_id: int, order: db.Order, lang: str) -> None:
    items = db.get_order_items(tenant_id, order.id)
    lines = []
    for item in items:
        name = _product_name(tenant_id, item.product_id)
        lines.append(f"{item.quantity} x {name} — {_fmt_money(item.unit_price_at_order_time * item.quantity)}")

    body = (
        f"{t('order_number', lang, id=order.id)}\n"
        f"{t('order_status_label', lang, status=status_label(order.status, lang))}\n"
        f"{t('order_placed_label', lang, date=_fmt_datetime(order.created_at))}\n\n"
        + "\n".join(lines)
        + f"\n\n{t('order_total_label', lang, amount=_fmt_money(order.total))}"
    )
    await wa.send_buttons(
        to=phone,
        body_text=body,
        buttons=[{"id": MAIN_MENU_BTN, "title": t("main_menu_button", lang)}],
    )


SHOP_MORE = "shop_more"
ADD_TO_CART = "add_to_cart"
BACK_TO_PRODUCTS = "back_to_products"
VIEW_CART = "view_cart"
CONTINUE_SHOPPING = "continue_shopping"
CHECKOUT = "checkout"
MAIN_MENU_BTN = "main_menu"


# --- Flow starters (called from IDLE) ---

async def _start_shop(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, lang: str) -> None:
    sent = await _send_product_list(wa, phone, tenant_id, 0, lang)
    if not sent:
        sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
        await wa.send_text(phone, t("no_products_available", lang))
        return
    sessions.set(tenant_id, phone, STATE_SHOP_LIST, {"language": lang, "shop_offset": 0, "cart": {}})


async def _start_orders_list(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, lang: str) -> None:
    sent = await _send_orders_list(wa, phone, tenant_id, phone, lang)
    if not sent:
        sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
        await wa.send_text(phone, t("no_orders_yet", lang))
        return
    sessions.set(tenant_id, phone, STATE_ORDER_LIST, {"language": lang})


# --- State handlers ---
# Each handler either advances the session to the next state (on a valid tap)
# or refreshes the session at the *same* state (on free text/unsupported
# input), same pattern as the old booking_flow.py.

async def _handle_idle(
    wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, tenant_name: str, context: dict,
) -> None:
    """Handles IDLE -- including the language-selection step, which lives
    here rather than as a separate dispatched state: IDLE-with-no-language
    and IDLE-with-a-language are really the same state from the session
    store's point of view (both land here via handle_incoming's "unhandled
    state" fallback), just with different context. This is also the target
    every "go back to idle" path in this module routes through (RESET_KEYWORDS,
    a stale main-menu tap from another state, a completed/abandoned flow),
    so language-preservation logic only has to live in one place."""
    lang = context.get("language")

    if reply["type"] == "interactive_reply" and reply["id"] in (LANG_EN_BTN, LANG_HI_BTN):
        chosen = LANG_EN if reply["id"] == LANG_EN_BTN else LANG_HI
        sessions.set(tenant_id, phone, STATE_IDLE, {"language": chosen})
        await _send_main_menu(wa, phone, tenant_name, chosen)
        return

    if lang is None:
        # No language chosen yet for this conversation -- always (re-)show
        # the prompt regardless of what was tapped/typed, until one is
        # picked. Covers first contact, a reset with no prior language, and
        # any stray input while the prompt is still pending.
        sessions.set(tenant_id, phone, STATE_IDLE, {})
        await _send_language_prompt(wa, phone)
        return

    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == MENU_SHOP:
            await _start_shop(wa, sessions, phone, tenant_id, lang)
            return
        if rid in (MENU_MY_ORDERS, MENU_TRACK_ORDER):
            await _start_orders_list(wa, sessions, phone, tenant_id, lang)
            return
        if rid == MENU_OFFERS:
            sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
            await wa.send_text(phone, t("offers_coming_soon", lang))
            return
        if rid == MENU_ACCOUNT:
            sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
            await wa.send_text(phone, t("account_coming_soon", lang))
            return
        if rid == MENU_TALK_TO_US:
            sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
            await wa.send_text(phone, t("talk_to_us_coming_soon", lang))
            return

    # Any other message while IDLE (free text, stale/unknown id): always
    # responds with the welcome message + main menu, in the already-chosen language.
    sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
    await _send_main_menu(wa, phone, tenant_name, lang)


async def _handle_shop_list(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    lang = context.get("language", DEFAULT_LANG)
    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == SHOP_MORE:
            products = db.get_active_products(tenant_id)
            next_offset = context.get("shop_offset", 0) + (MAX_LIST_ROWS - 1)
            if next_offset >= len(products):
                next_offset = 0
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, {**context, "shop_offset": next_offset})
            await _send_product_list(wa, phone, tenant_id, next_offset, lang)
            return

        product_id = _parse_product_row_id(rid)
        if product_id is not None:
            product = db.get_product(tenant_id, product_id)
            if product and product.is_active:
                sessions.set(tenant_id, phone, STATE_PRODUCT_DETAIL, {**context, "product_id": product.id})
                await _send_product_detail(wa, phone, product, lang)
                return
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
            await wa.send_text(phone, t("item_unavailable", lang))
            await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0), lang)
            return

    sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
    await wa.send_text(phone, t("please_choose", lang))
    await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0), lang)


async def _handle_product_detail(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    lang = context.get("language", DEFAULT_LANG)
    product_id = context.get("product_id")
    if product_id is None:
        # Corrupted/incomplete session context -- fail safe back to the main
        # menu, same "the store" fallback name booking_flow.py used ("the
        # hospital") for this exact class of defensive case.
        sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
        await _send_main_menu(wa, phone, "the store", lang)
        return

    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == BACK_TO_PRODUCTS:
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
            await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0), lang)
            return
        if rid == ADD_TO_CART:
            product = db.get_product(tenant_id, product_id)
            if not product or not product.is_active:
                sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
                await wa.send_text(phone, t("item_unavailable", lang))
                await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0), lang)
                return

            cart = dict(context.get("cart", {}))
            key = str(product.id)
            requested_qty = cart.get(key, 0) + 1
            # Advisory check only -- adding to cart doesn't reserve stock, so
            # this can still race with another customer between now and
            # checkout. The actual enforcement that can never be raced past
            # is db.checkout_cart()'s atomic conditional UPDATE, below.
            if requested_qty > product.stock_quantity:
                sessions.set(tenant_id, phone, STATE_PRODUCT_DETAIL, context)
                if product.stock_quantity <= 0:
                    await wa.send_text(phone, t("out_of_stock", lang))
                else:
                    await wa.send_text(phone, t("only_n_left", lang, n=product.stock_quantity))
                await _send_product_detail(wa, phone, product, lang)
                return

            cart[key] = requested_qty
            new_context = {**context, "cart": cart}
            sessions.set(tenant_id, phone, STATE_POST_ADD, new_context)
            await _send_add_to_cart_prompt(wa, phone, product.name, cart, tenant_id, lang)
            return

    sessions.set(tenant_id, phone, STATE_PRODUCT_DETAIL, context)
    await wa.send_text(phone, t("please_choose", lang))
    product = db.get_product(tenant_id, product_id)
    if product:
        await _send_product_detail(wa, phone, product, lang)


async def _handle_post_add(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    lang = context.get("language", DEFAULT_LANG)
    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == VIEW_CART:
            sessions.set(tenant_id, phone, STATE_CART, context)
            await _send_cart(wa, phone, tenant_id, context.get("cart", {}), lang)
            return
        if rid == CONTINUE_SHOPPING:
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
            await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0), lang)
            return

    sessions.set(tenant_id, phone, STATE_POST_ADD, context)
    await wa.send_text(phone, t("please_choose", lang))
    cart = context.get("cart", {})
    # Re-send the same view/continue prompt with an up-to-date cart summary.
    await wa.send_buttons(
        to=phone,
        body_text=_cart_count_summary(cart, tenant_id, lang),
        buttons=[
            {"id": VIEW_CART, "title": t("view_cart_button", lang)},
            {"id": CONTINUE_SHOPPING, "title": t("continue_shopping_button", lang)},
        ],
    )


async def _handle_cart(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    lang = context.get("language", DEFAULT_LANG)
    cart = context.get("cart", {})
    if reply["type"] == "interactive_reply":
        rid = reply["id"]
        if rid == CONTINUE_SHOPPING:
            sessions.set(tenant_id, phone, STATE_SHOP_LIST, context)
            await _send_product_list(wa, phone, tenant_id, context.get("shop_offset", 0), lang)
            return
        if rid == CHECKOUT:
            await _checkout(wa, sessions, phone, tenant_id, cart, lang)
            return

    sessions.set(tenant_id, phone, STATE_CART, context)
    await wa.send_text(phone, t("please_choose", lang))
    await _send_cart(wa, phone, tenant_id, cart, lang)


async def _checkout(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, cart: dict, lang: str) -> None:
    """Creates a real orders row (ORDER_STATUS_PENDING_PAYMENT) + order_items
    from the cart, decrementing stock atomically, then generates a real
    Razorpay payment link (payments.py) for the order total and sends it as
    part of the confirmation message -- SPEC.md Section 3.3. A tenant that
    hasn't configured Razorpay yet (all of Phase 7's payment fields are
    optional) falls back to the old placeholder message instead of failing
    checkout outright.

    Duplicate-checkout protection: the cart is cleared (sessions.reset) here,
    before the DB write, not after. This composes with two things: (1)
    core/main.py's per-(tenant, phone) message lock, which already prevents
    two genuinely concurrent webhook deliveries for the *same* customer from
    both reaching this function at once, and (2) the fact that once the
    session leaves STATE_CART, a second "checkout" tap doesn't route back
    here at all -- it lands in _handle_idle instead. So a double-tap or a
    webhook redelivery for the same customer can trigger this function at
    most once with a given cart. Overselling *across different customers*
    racing for the same product is a separate problem this doesn't solve --
    that's what db.checkout_cart()'s atomic stock decrement is for."""
    if not cart:
        sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
        await wa.send_text(phone, t("cart_empty_checkout", lang))
        return

    sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
    await _complete_checkout_from_cart(wa, tenant_id, phone, cart, lang)


async def _complete_checkout_from_cart(wa: WhatsAppClient, tenant_id: int, phone: str, cart: dict, lang: str) -> None:
    """The shared tail of both checkout paths: the tap-driven flow above
    (_checkout, after its own empty-cart guard + session reset) and
    handle_native_order below (Meta's native catalog cart, which has no
    session to reset in the first place). Creates the order via
    db.checkout_cart, generates a Razorpay payment link, and sends the
    confirmation -- identical behavior regardless of which UI built the cart.

    lang is snapshotted onto the order row (db.checkout_cart's language
    param) so later payment-webhook confirmations can use it even after the
    conversation session itself has long since expired -- see
    handle_payment_success/handle_payment_failure below and schema.sql's
    orders.language column comment."""
    order, unavailable_ids = db.checkout_cart(tenant_id, phone, cart, language=lang)

    if order is None:
        await wa.send_text(phone, t("cart_items_unavailable", lang))
        return

    items = db.get_order_items(tenant_id, order.id)
    lines = "\n".join(
        f"{item.quantity} x {_product_name(tenant_id, item.product_id)} — "
        f"{_fmt_money(item.unit_price_at_order_time * item.quantity)}"
        for item in items
    )
    message = (
        f"{t('order_placed_confirmation', lang, id=order.id)}\n\n{lines}\n\n"
        f"{t('order_total_label', lang, amount=_fmt_money(order.total))}"
    )

    if unavailable_ids:
        dropped_names = [_product_name(tenant_id, int(pid)) for pid in unavailable_ids]
        verb = "was" if len(dropped_names) == 1 else "were"  # only referenced by the English template
        message += "\n\n" + t("items_removed_note", lang, names=", ".join(dropped_names), verb=verb)

    tenant = db.get_tenant(tenant_id)
    try:
        payment_url, payment_link_id = payments.create_payment_link(tenant, order)
    except payments.PaymentGatewayNotConfigured:
        message += "\n\n" + t("payment_coming_next", lang)
    except Exception:
        # A real Razorpay API failure (network issue, bad credentials, etc.)
        # -- the order still exists and is still payable once the operator
        # sorts out the gateway issue, so this must not crash the webhook
        # handler, same "never let a downstream failure break message
        # acknowledgment" rule as core/whatsapp.py's send methods.
        logger.exception("Razorpay payment link creation failed for tenant=%s order=%s", tenant_id, order.id)
        message += "\n\n" + t("payment_link_failed", lang)
    else:
        db.update_order_payment_link(tenant_id, order.id, payment_url, payment_link_id)
        message += "\n\n" + t("pay_here", lang, url=payment_url)

    await wa.send_text(phone, message)


async def handle_native_order(wa: WhatsAppClient, tenant: db.Tenant, phone: str, product_items: list[dict]) -> None:
    """Entry point for Meta's native order webhook (SPEC.md Section 3.2/
    Phase 2) -- core/main.py dispatches straight here, bypassing the
    tap-driven session state machine entirely, since there's no prior bot
    conversation turn to resume for a cart WhatsApp built inside its own
    native catalog UI (core/whatsapp.py's send_product_list).

    Maps each product_retailer_id back to a local product via
    db.get_product_by_retailer_id, dropping any that don't resolve (a
    stale/rejected/never-synced catalog item -- logged, not fatal, same
    "drop the bad item, don't fail the whole order" spirit as
    db.checkout_cart's own unavailable_product_ids handling), then reuses
    the exact same order-creation + payment-link logic the tap-driven flow
    uses via _complete_checkout_from_cart.

    Language: there is no session for a native-catalog customer to have
    chosen one in -- browsing and cart-building happen entirely inside
    WhatsApp's own UI, with no bot conversation turn until this final
    "Send order" webhook. DEFAULT_LANG (English) is used for this path's
    messages; there's no conversational touchpoint where a native-catalog
    customer could ever be asked, so this is a real, structural limitation
    of Meta's native flow, not something core/strings.py's lookup can work
    around."""
    cart: dict[str, int] = {}
    for item in product_items:
        retailer_id = item.get("product_retailer_id")
        quantity = item.get("quantity")
        if not retailer_id or quantity in (None, ""):
            continue
        try:
            qty = int(quantity)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue

        product = db.get_product_by_retailer_id(tenant.id, retailer_id)
        if product is None or not product.is_active:
            logger.warning(
                "Native order for tenant=%s referenced unknown/inactive retailer_id=%s, dropping item",
                tenant.id, retailer_id,
            )
            continue

        key = str(product.id)
        cart[key] = cart.get(key, 0) + qty

    if not cart:
        await wa.send_text(phone, t("native_order_unavailable", DEFAULT_LANG))
        return

    await _complete_checkout_from_cart(wa, tenant.id, phone, cart, DEFAULT_LANG)


# --- Payment webhook handlers (SPEC.md Section 3.3, Phase 3) ---
# Called from core/main.py's POST /webhook/payment route, after it has
# already resolved the tenant from the payload's reference_id and verified
# Razorpay's signature against that tenant's own webhook secret -- these
# functions assume that's already happened and just act on a trusted event.
# No session/state-machine involvement here (unlike everything above) --
# payment webhooks aren't part of a customer's live conversation turn, they
# can arrive minutes or hours after the customer last messaged, often well
# after their session has expired -- so language comes from order.language
# (snapshotted at checkout, see _complete_checkout_from_cart), never from a
# session lookup.

async def handle_payment_success(
    wa: WhatsAppClient, tenant_id: int, order_id: int, payment_gateway_reference: str | None,
    payment_method: str | None = None,
) -> None:
    """Marks the order paid and sends the customer a confirmation. Idempotent
    against duplicate webhook deliveries (Razorpay, like Meta, may redeliver
    events) via db.mark_order_paid()'s atomic conditional UPDATE -- if this
    specific call isn't the one that actually transitioned the order (i.e.
    it was already paid), no second confirmation is sent.

    payment_method (e.g. "upi"/"card"/"netbanking", from payments.py's
    webhook parsing) is stored on the order for the merchant portal's
    dashboard payment-method breakdown -- optional since not every caller
    (e.g. existing tests) has it."""
    order = db.get_order(tenant_id, order_id)
    if order is None:
        logger.warning("Payment success webhook for unknown order tenant_id=%s order_id=%s", tenant_id, order_id)
        return

    transitioned = db.mark_order_paid(
        tenant_id, order_id, payment_gateway_reference, datetime.now().isoformat(), payment_method=payment_method,
    )
    if not transitioned:
        logger.info("Duplicate payment-success webhook for already-paid order %s, ignoring", order_id)
        return

    await wa.send_text(order.customer_phone, t("payment_received", order.language, id=order.id))


async def handle_payment_failure(wa: WhatsAppClient, tenant: db.Tenant, order_id: int, link_expired: bool) -> None:
    """Marks the order failed and offers the customer a way to retry.
    Idempotent the same way handle_payment_success is, via
    db.mark_order_failed()'s atomic conditional UPDATE.

    link_expired distinguishes two different Razorpay events (see
    payments.py's module docstring on payload-shape uncertainty): a
    payment_link.expired event means the link itself is no longer payable,
    so a fresh one is generated; a plain payment.failed event means one
    payment *attempt* against a still-open link failed, so the existing link
    is still valid and is resent as-is."""
    order = db.get_order(tenant.id, order_id)
    if order is None:
        logger.warning("Payment failure webhook for unknown order tenant_id=%s order_id=%s", tenant.id, order_id)
        return

    transitioned = db.mark_order_failed(tenant.id, order_id)
    if not transitioned:
        logger.info("Duplicate/stale payment-failure webhook for order %s (already failed or paid), ignoring", order_id)
        return

    lang = order.language

    if not link_expired:
        if order.payment_link_url:
            await wa.send_text(order.customer_phone, t("payment_failed_retry", lang, id=order.id, url=order.payment_link_url))
        else:
            await wa.send_text(order.customer_phone, t("payment_failed_no_link", lang, id=order.id))
        return

    try:
        new_url, new_link_id = payments.create_payment_link(tenant, order)
    except payments.PaymentGatewayNotConfigured:
        # Shouldn't normally happen (a payment link already existed for this
        # order, so the tenant was configured at checkout time) -- credentials
        # could have been cleared since. Fail gracefully either way.
        await wa.send_text(order.customer_phone, t("payment_problem_no_gateway", lang, id=order.id))
        return
    except Exception:
        logger.exception("Razorpay retry-link creation failed for tenant=%s order=%s", tenant.id, order_id)
        await wa.send_text(order.customer_phone, t("payment_link_retry_failed", lang, id=order.id))
        return

    db.update_order_payment_link(tenant.id, order_id, new_url, new_link_id)
    await wa.send_text(order.customer_phone, t("payment_link_expired_new", lang, id=order.id, url=new_url))


async def handle_order_fulfilled(wa: WhatsAppClient, order: db.Order) -> None:
    """Notifies the customer once the merchant marks their (already-paid)
    order fulfilled via the portal (portal/orders.py's POST
    /portal/orders/{id}/fulfill). Called only when db.mark_order_fulfilled()
    itself reports it made the transition -- same idempotency contract as
    handle_payment_success/handle_payment_failure, so a merchant re-clicking
    an already-fulfilled order's button never re-sends this.

    No session/state-machine involvement, same reasoning as the payment
    webhook handlers above: this fires from a portal HTTP request, not a
    customer's live conversation turn, so language comes from
    order.language (snapshotted at checkout), never a session lookup."""
    await wa.send_text(order.customer_phone, t("order_fulfilled", order.language, id=order.id))


async def _handle_order_list(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    lang = context.get("language", DEFAULT_LANG)
    if reply["type"] == "interactive_reply":
        order_id = _parse_order_row_id(reply["id"])
        if order_id is not None:
            order = db.get_order(tenant_id, order_id)
            if order and order.customer_phone == phone:
                sessions.set(tenant_id, phone, STATE_ORDER_DETAIL, {**context, "order_id": order.id})
                await _send_order_detail(wa, phone, tenant_id, order, lang)
                return

    sessions.set(tenant_id, phone, STATE_ORDER_LIST, context)
    await wa.send_text(phone, t("please_choose", lang))
    await _send_orders_list(wa, phone, tenant_id, phone, lang)


async def _handle_order_detail(wa: WhatsAppClient, sessions, phone: str, tenant_id: int, reply: dict, context: dict) -> None:
    lang = context.get("language", DEFAULT_LANG)
    if reply["type"] == "interactive_reply" and reply["id"] == MAIN_MENU_BTN:
        sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
        await _send_main_menu(wa, phone, "the store", lang)
        return

    order_id = context.get("order_id")
    order = db.get_order(tenant_id, order_id) if order_id is not None else None
    if not order:
        sessions.set(tenant_id, phone, STATE_IDLE, {"language": lang})
        await wa.send_text(phone, t("order_not_found", lang))
        return

    sessions.set(tenant_id, phone, STATE_ORDER_DETAIL, context)
    await wa.send_text(phone, t("please_choose", lang))
    await _send_order_detail(wa, phone, tenant_id, order, lang)


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
    Entry point: look up the customer's current session (sessions.get
    already resets stale/timed-out sessions to IDLE), then dispatch.

    A global reset keyword (works from *any* state, unlike the old
    booking_flow.py's IDLE-only fallback) and a main-menu button tap (same
    "works from any state" reasoning -- WhatsApp keeps old interactive
    messages tappable indefinitely, see _MAIN_MENU_IDS) both route straight
    to _handle_idle regardless of the session's current state -- which is
    also exactly where the "unrecognized state" fallback below routes.
    _handle_idle itself is what decides whether that means "show the main
    menu" (a language was already chosen this conversation) or "ask for a
    language first" (it wasn't) -- see that function's docstring. Routing
    all three cases through the same function is what makes language
    preservation-across-resets automatic rather than three separate copies
    of the same logic.
    """
    session = sessions.get(tenant_id, phone)
    state = session["state"]
    context = session["context"]

    if reply["type"] == "text" and reply.get("text", "").strip().lower() in RESET_KEYWORDS:
        await _handle_idle(wa, sessions, phone, tenant_id, reply, tenant_name, context)
        return

    if reply["type"] == "interactive_reply" and reply.get("id") in _MAIN_MENU_IDS:
        await _handle_idle(wa, sessions, phone, tenant_id, reply, tenant_name, context)
        return

    handler = _HANDLERS.get(state)
    if handler is None:
        # IDLE, or any unrecognized/stale state value -> treat as IDLE.
        await _handle_idle(wa, sessions, phone, tenant_id, reply, tenant_name, context)
        return

    await handler(wa, sessions, phone, tenant_id, reply, context)
