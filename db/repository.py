# db/repository.py
"""
The single data-access surface core/main.py talks to. No raw SQL anywhere
outside this file. Every function that reads/writes tenant-scoped data takes
tenant_id and filters by it (SPEC.md Section 4/8: "no query should ever return
or write data without this filter" -- carried over from the hospital repo's
hospital_id scoping rule).

Phase 0 (SPEC.md Section 8) stripped the hospital product's departments/
doctors/doctor_slots/appointments/appointment_reminders logic, leaving just
the multi-tenant routing surface (tenants). Phase 5's products/orders/
order_items CRUD below was built ahead of schedule (nothing in core/main.py
writes to it yet -- that's Phase 2/3 work) since the order-received handler
and payment integration both depend on this data model existing first.
"""
import json as json_lib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from db.connection import get_connection


@dataclass
class Tenant:
    id: int
    name: str
    whatsapp_phone_number_id: str | None
    access_token: str | None  # DB column: meta_access_token_ref
    app_secret: str | None  # DB column: app_secret_ref
    meta_catalog_id: str | None
    payment_gateway_provider: str | None
    payment_gateway_key_id: str | None  # Razorpay key_id (public)
    payment_gateway_api_key_ref: str | None  # Razorpay key_secret (private)
    payment_gateway_webhook_secret: str | None  # verifies payments.py's webhook signatures
    welcome_message_text: str | None
    timezone: str
    abandoned_cart_nudge_hours: int
    is_active: bool
    data_tier: str
    external_api_base_url: str | None
    external_api_key: str | None
    portal_password_hash: str | None  # merchant portal login (portal/session.py); NULL until an admin sets one


def _row_to_tenant(row) -> Tenant:
    return Tenant(
        id=row["id"],
        name=row["name"],
        whatsapp_phone_number_id=row["whatsapp_phone_number_id"],
        access_token=row["meta_access_token_ref"],
        app_secret=row["app_secret_ref"],
        meta_catalog_id=row["meta_catalog_id"],
        payment_gateway_provider=row["payment_gateway_provider"],
        payment_gateway_key_id=row["payment_gateway_key_id"],
        payment_gateway_api_key_ref=row["payment_gateway_api_key_ref"],
        payment_gateway_webhook_secret=row["payment_gateway_webhook_secret"],
        welcome_message_text=row["welcome_message_text"],
        timezone=row["timezone"],
        abandoned_cart_nudge_hours=row["abandoned_cart_nudge_hours"],
        is_active=bool(row["is_active"]),
        data_tier=row["data_tier"],
        external_api_base_url=row["external_api_base_url"],
        external_api_key=row["external_api_key"],
        portal_password_hash=row["portal_password_hash"],
    )


def find_tenant_by_phone_number_id(phone_number_id: str) -> Tenant | None:
    """The entry point for per-message multi-tenant routing: given the
    phone_number_id from an incoming webhook's `metadata`, resolve which
    tenant received it. Only active tenants are matched -- a deactivated
    tenant's number should be treated the same as an unrecognized one."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM tenants WHERE whatsapp_phone_number_id = ? AND is_active = 1",
        (phone_number_id,),
    ).fetchone()
    return _row_to_tenant(row) if row else None


def get_active_tenants() -> list[Tenant]:
    """Used by core/main.py's /internal/* endpoints to loop over every active
    tenant rather than a single startup-resolved one."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM tenants WHERE is_active = 1 ORDER BY id").fetchall()
    return [_row_to_tenant(r) for r in rows]


def get_tenant(tenant_id: int) -> Tenant | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return _row_to_tenant(row) if row else None


def create_tenant(
    name: str,
    whatsapp_phone_number_id: str,
    access_token: str | None = None,
    app_secret: str | None = None,
    welcome_message_text: str | None = None,
    timezone: str = "Asia/Kolkata",
    abandoned_cart_nudge_hours: int = 2,
    meta_catalog_id: str | None = None,
    payment_gateway_provider: str | None = None,
    payment_gateway_key_id: str | None = None,
    payment_gateway_api_key_ref: str | None = None,
    payment_gateway_webhook_secret: str | None = None,
    data_tier: str = "tier1",
    external_api_base_url: str | None = None,
    external_api_key: str | None = None,
) -> Tenant:
    """Onboarding wizard's entry point. Raises db.connection.IntegrityError if
    whatsapp_phone_number_id is already used by another tenant -- db/schema.sql's
    UNIQUE constraint is the actual guard against breaking per-message routing,
    not application logic; callers (admin/onboarding.py) are responsible for
    catching it.

    timezone: IANA name for displaying order/invoice timestamps in the
    tenant's local time (SPEC.md Section 3.4) and Phase 6's abandoned-cart
    timing logic. Defaults to Asia/Kolkata (Razorpay targets Indian
    businesses, SPEC.md Section 3.3) if the onboarding wizard doesn't override it.

    abandoned_cart_nudge_hours: how long a pending_payment order sits
    untouched before reminders/scheduler.py nudges the customer. Defaults to 2.

    data_tier: "tier1" (default, this product's own database), "tier2"
    (external_api_base_url/external_api_key are only stored here -- no
    connector logic reads them yet), or "tier3" (direct DB connection, not
    self-serve -- neither field is meaningful for it)."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO tenants (name, whatsapp_phone_number_id, meta_access_token_ref, app_secret_ref, "
        "welcome_message_text, timezone, abandoned_cart_nudge_hours, meta_catalog_id, payment_gateway_provider, "
        "payment_gateway_key_id, payment_gateway_api_key_ref, payment_gateway_webhook_secret, "
        "data_tier, external_api_base_url, external_api_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (name, whatsapp_phone_number_id, access_token, app_secret, welcome_message_text, timezone,
         abandoned_cart_nudge_hours, meta_catalog_id, payment_gateway_provider, payment_gateway_key_id,
         payment_gateway_api_key_ref, payment_gateway_webhook_secret,
         data_tier, external_api_base_url, external_api_key),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return get_tenant(new_id)


def update_tenant_catalog_and_payment(
    tenant_id: int,
    meta_catalog_id: str | None = None,
    payment_gateway_provider: str | None = None,
    payment_gateway_key_id: str | None = None,
    payment_gateway_api_key_ref: str | None = None,
    payment_gateway_webhook_secret: str | None = None,
    abandoned_cart_nudge_hours: int | None = None,
) -> None:
    """Onboarding wizard's catalog/payment-gateway edit step (SPEC.md Section
    7, Phase 7) -- Phase 3's real Razorpay integration (payments.py) reads
    payment_gateway_key_id/payment_gateway_api_key_ref (key_secret) to call
    the API and payment_gateway_webhook_secret to verify webhook signatures.
    abandoned_cart_nudge_hours (Phase 6, reminders/scheduler.py) lives on this
    same edit step rather than a separate page -- it's the only other
    per-tenant "settings that aren't needed at initial onboarding" field so far.

    Each field is only overwritten when a caller actually passes a value --
    COALESCE keeps whatever was already stored otherwise (same convention as
    update_order_status), so submitting the edit form with a field left
    blank (e.g. because it isn't set up yet) never clobbers an existing
    catalog ID or payment credential with NULL."""
    conn = get_connection()
    conn.execute(
        "UPDATE tenants SET "
        "meta_catalog_id = COALESCE(?, meta_catalog_id), "
        "payment_gateway_provider = COALESCE(?, payment_gateway_provider), "
        "payment_gateway_key_id = COALESCE(?, payment_gateway_key_id), "
        "payment_gateway_api_key_ref = COALESCE(?, payment_gateway_api_key_ref), "
        "payment_gateway_webhook_secret = COALESCE(?, payment_gateway_webhook_secret), "
        "abandoned_cart_nudge_hours = COALESCE(?, abandoned_cart_nudge_hours) "
        "WHERE id = ?",
        (meta_catalog_id, payment_gateway_provider, payment_gateway_key_id,
         payment_gateway_api_key_ref, payment_gateway_webhook_secret,
         abandoned_cart_nudge_hours, tenant_id),
    )
    conn.commit()


def set_tenant_portal_password_hash(tenant_id: int, password_hash: str) -> None:
    """Sets/resets a tenant's merchant-portal login password (portal/session.py
    hashes it before calling this -- this function never sees a plaintext
    password). Admin-only (admin/onboarding.py's portal-password page) since
    merchant self-serve signup is out of scope for now -- always a full
    overwrite, never COALESCE, since resetting a password always means
    replacing whatever hash (or NULL) was there before."""
    conn = get_connection()
    conn.execute(
        "UPDATE tenants SET portal_password_hash = ? WHERE id = ?",
        (password_hash, tenant_id),
    )
    conn.commit()


# --- Products (SPEC.md Section 4, mirrors Meta's catalog for pricing/stock lookups) ---

@dataclass
class Product:
    id: int
    tenant_id: int
    name: str
    price: Decimal
    currency: str
    description: str | None
    image_url: str | None
    sku: str | None
    stock_quantity: int
    category: str | None
    is_active: bool
    catalog_retailer_id: str | None  # the id Meta's catalog/order-webhook knows this product by


def _row_to_product(row) -> Product:
    return Product(
        id=row["id"],
        tenant_id=row["tenant_id"],
        name=row["name"],
        price=row["price"],
        currency=row["currency"],
        description=row["description"],
        image_url=row["image_url"],
        sku=row["sku"],
        stock_quantity=row["stock_quantity"],
        category=row["category"],
        is_active=bool(row["is_active"]),
        catalog_retailer_id=row["catalog_retailer_id"],
    )


def _catalog_retailer_id(tenant_id: int, product_id: int) -> str:
    """Deterministic, always-unique-by-construction id for Meta's catalog
    feed/order-webhook to reference this product by -- not sourced from
    products.sku, which real merchant data can't be relied on to have (a
    real client's Shopify export had a SKU on only 7 of 1173 rows)."""
    return f"t{tenant_id}-p{product_id}"


def get_product(tenant_id: int, product_id: int) -> Product | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM products WHERE tenant_id = ? AND id = ?",
        (tenant_id, product_id),
    ).fetchone()
    return _row_to_product(row) if row else None


def get_product_by_retailer_id(tenant_id: int, retailer_id: str) -> Product | None:
    """Resolves a Meta catalog/order-webhook product_retailer_id back to the
    local product it refers to -- the reverse direction of
    _catalog_retailer_id(). Returns None for a retailer_id that doesn't
    resolve (a stale/rejected catalog item) rather than raising; callers
    (core/commerce_flow.py's handle_native_order) drop unresolvable items
    from the order instead of failing the whole thing."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM products WHERE tenant_id = ? AND catalog_retailer_id = ?",
        (tenant_id, retailer_id),
    ).fetchone()
    return _row_to_product(row) if row else None


def get_active_products(tenant_id: int) -> list[Product]:
    """A tenant's currently sellable products -- scoped by tenant_id same as
    every other query here, never returned across tenants."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM products WHERE tenant_id = ? AND is_active = 1 ORDER BY id",
        (tenant_id,),
    ).fetchall()
    return [_row_to_product(r) for r in rows]


def create_product(
    tenant_id: int,
    name: str,
    price: Decimal,
    currency: str = "INR",
    description: str | None = None,
    image_url: str | None = None,
    sku: str | None = None,
    stock_quantity: int = 0,
    category: str | None = None,
) -> Product:
    """Manual product entry (also the target of the wizard's/portal's CSV
    bulk import) -- Phase 1's real catalog sync (SPEC.md Section 3.1) reads
    catalog_retailer_id (set below, right after insert since it's derived
    from the row's own new id) rather than writing to Meta's catalog directly."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO products (tenant_id, name, price, currency, description, image_url, sku, "
        "stock_quantity, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (tenant_id, name, price, currency, description, image_url, sku, stock_quantity, category),
    )
    new_id = cur.fetchone()["id"]
    conn.execute(
        "UPDATE products SET catalog_retailer_id = ? WHERE tenant_id = ? AND id = ?",
        (_catalog_retailer_id(tenant_id, new_id), tenant_id, new_id),
    )
    conn.commit()
    return get_product(tenant_id, new_id)


def update_product_stock(tenant_id: int, product_id: int, stock_quantity: int) -> None:
    """Availability here is stock quantity, not a slot concept (SPEC.md
    Section 4's note) -- decremented on order or on payment confirmation,
    depending on the tenant's tolerance for overselling during checkout
    (Phase 2/3 decision, not made here)."""
    conn = get_connection()
    conn.execute(
        "UPDATE products SET stock_quantity = ? WHERE tenant_id = ? AND id = ?",
        (stock_quantity, tenant_id, product_id),
    )
    conn.commit()


def get_all_products(tenant_id: int) -> list[Product]:
    """Every product for a tenant, active or not -- unlike get_active_products
    (which the customer-facing shop flow uses), the merchant portal's product
    list needs to show deactivated products too so they can be re-activated."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM products WHERE tenant_id = ? ORDER BY id",
        (tenant_id,),
    ).fetchall()
    return [_row_to_product(r) for r in rows]


def update_product(
    tenant_id: int,
    product_id: int,
    name: str,
    price: Decimal,
    description: str | None,
    image_url: str | None,
    sku: str | None,
    stock_quantity: int,
    category: str | None,
) -> Product | None:
    """The merchant portal's product-edit form -- always a full overwrite of
    every field (the edit form pre-fills current values, so a blank field on
    submit means "clear it"), unlike update_tenant_catalog_and_payment's
    COALESCE "blank means keep current" convention (which exists for a form
    an operator may only be touching up partially, not a full record edit)."""
    conn = get_connection()
    conn.execute(
        "UPDATE products SET name = ?, price = ?, description = ?, image_url = ?, "
        "sku = ?, stock_quantity = ?, category = ? WHERE tenant_id = ? AND id = ?",
        (name, price, description, image_url, sku, stock_quantity, category, tenant_id, product_id),
    )
    conn.commit()
    return get_product(tenant_id, product_id)


def set_product_active(tenant_id: int, product_id: int, is_active: bool) -> None:
    """Toggle a product's visibility to customers without deleting it --
    deactivated products still show up in get_all_products (portal) and past
    orders' order_items, just excluded from get_active_products (the
    customer-facing shop flow)."""
    conn = get_connection()
    conn.execute(
        "UPDATE products SET is_active = ? WHERE tenant_id = ? AND id = ?",
        (1 if is_active else 0, tenant_id, product_id),
    )
    conn.commit()


# --- Orders (SPEC.md Section 4) ---

ORDER_STATUS_BROWSING = "browsing"
ORDER_STATUS_PENDING_PAYMENT = "pending_payment"
ORDER_STATUS_PAID = "paid"
ORDER_STATUS_FAILED = "failed"
ORDER_STATUS_CANCELLED = "cancelled"
ORDER_STATUS_FULFILLED = "fulfilled"


@dataclass
class Order:
    id: int
    tenant_id: int
    customer_phone: str
    status: str
    payment_link_url: str | None
    payment_gateway_reference: str | None
    subtotal: Decimal | None
    total: Decimal | None
    created_at: str
    paid_at: str | None
    nudge_sent_at: str | None
    payment_method: str | None  # e.g. "upi"/"card"/"netbanking"/"wallet" (payments.py); NULL until paid
    language: str  # core/strings.py's LANG_EN/LANG_HI, snapshotted at checkout -- see schema.sql's column comment
    coupon_code: str | None  # the coupon actually applied at checkout, if any -- see schema.sql's column comment
    discount_amount: Decimal | None  # amount knocked off subtotal by coupon_code, snapshotted at checkout


def _row_to_order(row) -> Order:
    return Order(
        id=row["id"],
        tenant_id=row["tenant_id"],
        customer_phone=row["customer_phone"],
        status=row["status"],
        payment_link_url=row["payment_link_url"],
        payment_gateway_reference=row["payment_gateway_reference"],
        subtotal=row["subtotal"],
        total=row["total"],
        created_at=row["created_at"],
        paid_at=row["paid_at"],
        nudge_sent_at=row["nudge_sent_at"],
        payment_method=row["payment_method"],
        language=row["language"],
        coupon_code=row["coupon_code"],
        discount_amount=row["discount_amount"],
    )


def get_order(tenant_id: int, order_id: int) -> Order | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM orders WHERE tenant_id = ? AND id = ?",
        (tenant_id, order_id),
    ).fetchone()
    return _row_to_order(row) if row else None


def get_orders_for_phone(tenant_id: int, customer_phone: str) -> list[Order]:
    """A customer's own order history at this tenant, most recent first."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE tenant_id = ? AND customer_phone = ? ORDER BY created_at DESC",
        (tenant_id, customer_phone),
    ).fetchall()
    return [_row_to_order(r) for r in rows]


def list_orders(tenant_id: int, status: str | None = None, limit: int | None = None) -> list[Order]:
    """The merchant portal's tenant-wide order list (unlike
    get_orders_for_phone, which is scoped to one customer) -- optionally
    filtered by status (the portal's orders page) or capped to the N most
    recent (the dashboard's recent-orders table), most recent first."""
    conn = get_connection()
    sql = "SELECT * FROM orders WHERE tenant_id = ?"
    params: list = [tenant_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_row_to_order(r) for r in rows]


def create_order(
    tenant_id: int,
    customer_phone: str,
    status: str = ORDER_STATUS_BROWSING,
    subtotal: Decimal | None = None,
    total: Decimal | None = None,
    language: str = "en",
) -> Order:
    """Defaults to ORDER_STATUS_BROWSING -- an order row can exist before any
    line items or pricing are known yet (SPEC.md Section 4's conversation_sessions
    note), which is why subtotal/total are optional here.

    language: snapshots the customer's chosen conversation language at
    creation time (core/strings.py) -- see schema.sql's column comment for
    why this can't just be looked up later from session context."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO orders (tenant_id, customer_phone, status, subtotal, total, language) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
        (tenant_id, customer_phone, status, subtotal, total, language),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return get_order(tenant_id, new_id)


def update_order_status(
    tenant_id: int,
    order_id: int,
    status: str,
    payment_link_url: str | None = None,
    payment_gateway_reference: str | None = None,
    paid_at: str | None = None,
) -> None:
    """Moves an order to a new status (e.g. ORDER_STATUS_PAID once the payment
    gateway's webhook confirms it, Phase 3). payment_link_url/
    payment_gateway_reference/paid_at are only overwritten when a caller
    actually passes a value -- COALESCE keeps whatever was already stored
    otherwise, so e.g. cancelling an order doesn't need to re-pass its
    existing payment_link_url just to avoid blanking it out."""
    conn = get_connection()
    conn.execute(
        "UPDATE orders SET status = ?, "
        "payment_link_url = COALESCE(?, payment_link_url), "
        "payment_gateway_reference = COALESCE(?, payment_gateway_reference), "
        "paid_at = COALESCE(?, paid_at) "
        "WHERE tenant_id = ? AND id = ?",
        (status, payment_link_url, payment_gateway_reference, paid_at, tenant_id, order_id),
    )
    conn.commit()


# --- Order items (SPEC.md Section 4) ---

@dataclass
class OrderItem:
    id: int
    tenant_id: int
    order_id: int
    product_id: int
    quantity: int
    unit_price_at_order_time: Decimal


def _row_to_order_item(row) -> OrderItem:
    return OrderItem(
        id=row["id"],
        tenant_id=row["tenant_id"],
        order_id=row["order_id"],
        product_id=row["product_id"],
        quantity=row["quantity"],
        unit_price_at_order_time=row["unit_price_at_order_time"],
    )


def get_order_item(tenant_id: int, order_item_id: int) -> OrderItem | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM order_items WHERE tenant_id = ? AND id = ?",
        (tenant_id, order_item_id),
    ).fetchone()
    return _row_to_order_item(row) if row else None


def get_order_items(tenant_id: int, order_id: int) -> list[OrderItem]:
    """All line items for one order, in the order they were added."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM order_items WHERE tenant_id = ? AND order_id = ? ORDER BY id",
        (tenant_id, order_id),
    ).fetchall()
    return [_row_to_order_item(r) for r in rows]


def create_order_item(
    tenant_id: int,
    order_id: int,
    product_id: int,
    quantity: int,
    unit_price_at_order_time: Decimal,
) -> OrderItem:
    """unit_price_at_order_time is a snapshot of products.price at the moment
    this line item is created -- a later price change on the product must
    never retroactively change the total of an order that already references
    it, so callers pass the price explicitly rather than this function
    reading products.price itself."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO order_items (tenant_id, order_id, product_id, quantity, unit_price_at_order_time) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        (tenant_id, order_id, product_id, quantity, unit_price_at_order_time),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return get_order_item(tenant_id, new_id)


def checkout_cart(
    tenant_id: int,
    customer_phone: str,
    cart: dict[str, int],
    conn=None,
    language: str = "en",
    coupon_code: str | None = None,
) -> tuple[Order | None, list[str]]:
    """Atomically checks stock, decrements it, and creates the order +
    order_items for a customer's cart in a single DB transaction (core/
    commerce_flow.py's checkout step) -- so a crash or error partway through
    never leaves stock decremented without a matching order, or vice versa.

    Race safety: each product's stock decrement is one conditional UPDATE
    (`stock_quantity = stock_quantity - ? WHERE stock_quantity >= ?`), not a
    separate SELECT-then-UPDATE -- Postgres serializes concurrent UPDATEs to
    the same row at the row-lock level, so two checkouts racing for the last
    unit can never both succeed. This is a DB-level guard, the same
    architectural idea as the hospital repo's UNIQUE INDEX double-booking
    guard, just expressed as a conditional write instead of a conflicting
    insert (there's no natural uniqueness constraint for "don't oversell a
    counter" the way there was for "don't double-book an exact timestamp").

    cart: {str(product_id): quantity}, same shape core/commerce_flow.py's
    session context stores it in.

    conn: optional explicit connection (rather than get_connection()) so
    tests can drive two genuinely concurrent checkouts against the same
    database from two separate physical connections -- same reason the old
    hospital repo's generate_slots_for_doctor() took one.

    Returns (order, unavailable_product_ids). `order` is None if every item
    in the cart turned out unavailable (out of stock, deactivated, or
    deleted since being added) -- nothing was created. `unavailable_product_ids`
    lists which cart entries were dropped, present or not, so the caller can
    tell the customer what got left out."""
    conn = conn or get_connection()
    unavailable: list[str] = []
    line_items: list[tuple[int, int, Decimal]] = []  # (product_id, quantity, unit_price)

    with conn.transaction():
        for product_id_str, quantity in cart.items():
            product_id = int(product_id_str)
            row = conn.execute(
                "UPDATE products SET stock_quantity = stock_quantity - ? "
                "WHERE tenant_id = ? AND id = ? AND is_active = 1 AND stock_quantity >= ? "
                "RETURNING price",
                (quantity, tenant_id, product_id, quantity),
            ).fetchone()
            if row is None:
                unavailable.append(product_id_str)
                continue
            line_items.append((product_id, quantity, row["price"]))

        if not line_items:
            return None, unavailable

        subtotal = sum((price * qty for _, qty, price in line_items), Decimal("0"))

        # Coupon is validated (and its discount computed) against this
        # transaction's own freshly-computed subtotal, inside the same
        # transaction as the stock decrement -- not against whatever
        # subtotal the cart-view preview showed earlier, which could be
        # stale by the time checkout actually completes. An invalid/expired/
        # deactivated code at this point is silently not applied (order
        # still goes through at full price) rather than failing checkout --
        # same "never let a downstream edge case break checkout" spirit as
        # the unavailable-item handling above; core/commerce_flow.py already
        # validates the code up front when the customer enters it, so this
        # is only a rare last-moment race, not the primary validation path.
        applied_coupon_code = None
        discount_amount = None
        if coupon_code:
            coupon_row = conn.execute(
                "SELECT * FROM coupons WHERE tenant_id = ? AND UPPER(code) = UPPER(?)",
                (tenant_id, coupon_code),
            ).fetchone()
            coupon = _row_to_coupon(coupon_row) if coupon_row else None
            if coupon_validity_error(coupon) is None:
                applied_coupon_code = coupon.code
                discount_amount = compute_discount(subtotal, coupon.discount_type, coupon.discount_value)

        total = subtotal - discount_amount if discount_amount is not None else subtotal
        order_row = conn.execute(
            "INSERT INTO orders (tenant_id, customer_phone, status, subtotal, total, language, coupon_code, discount_amount) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (tenant_id, customer_phone, ORDER_STATUS_PENDING_PAYMENT, subtotal, total, language,
             applied_coupon_code, discount_amount),
        ).fetchone()
        order_id = order_row["id"]

        for product_id, quantity, unit_price in line_items:
            conn.execute(
                "INSERT INTO order_items (tenant_id, order_id, product_id, quantity, unit_price_at_order_time) "
                "VALUES (?, ?, ?, ?, ?)",
                (tenant_id, order_id, product_id, quantity, unit_price),
            )

    row = conn.execute(
        "SELECT * FROM orders WHERE tenant_id = ? AND id = ?", (tenant_id, order_id),
    ).fetchone()
    return _row_to_order(row), unavailable


def update_order_payment_link(
    tenant_id: int, order_id: int, payment_link_url: str, payment_gateway_reference: str | None,
) -> None:
    """Stores a freshly-created payment link (checkout) or a replacement one
    (a link-expiry retry, payments.py) -- unlike update_order_status's
    COALESCE fields, payment_link_url is always overwritten here since a new
    link genuinely replaces the old one."""
    conn = get_connection()
    conn.execute(
        "UPDATE orders SET payment_link_url = ?, "
        "payment_gateway_reference = COALESCE(?, payment_gateway_reference) "
        "WHERE tenant_id = ? AND id = ?",
        (payment_link_url, payment_gateway_reference, tenant_id, order_id),
    )
    conn.commit()


def mark_order_paid(
    tenant_id: int, order_id: int, payment_gateway_reference: str | None, paid_at: str,
    payment_method: str | None = None,
) -> bool:
    """Atomically transitions an order to ORDER_STATUS_PAID -- `WHERE status
    != 'paid'` means a duplicate/concurrent payment webhook delivery for an
    order that's already paid is a safe no-op, not a double-transition. This
    is a single conditional UPDATE, not a separate SELECT-then-UPDATE, for
    the same reason as checkout_cart()'s stock decrement: two genuinely
    concurrent webhook deliveries for the same order must not both see
    "not yet paid" and both act on it.

    payment_method (e.g. "upi"/"card"/"netbanking") is COALESCEd like
    payment_gateway_reference -- optional (defaults to None for any caller
    that doesn't have it, e.g. existing tests), never overwrites a value
    that's somehow already there.

    Returns True only if THIS call is the one that actually made the
    transition -- callers (core/commerce_flow.py's handle_payment_success)
    use that to decide whether to send the "payment received" WhatsApp
    confirmation, so a duplicate delivery never double-sends it."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE orders SET status = ?, "
        "payment_gateway_reference = COALESCE(?, payment_gateway_reference), "
        "payment_method = COALESCE(?, payment_method), "
        "paid_at = ? "
        "WHERE tenant_id = ? AND id = ? AND status != ?",
        (ORDER_STATUS_PAID, payment_gateway_reference, payment_method, paid_at,
         tenant_id, order_id, ORDER_STATUS_PAID),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_order_failed(tenant_id: int, order_id: int) -> bool:
    """Same atomic-conditional-transition pattern as mark_order_paid, for the
    failure path. `WHERE status NOT IN ('paid', 'failed')` serves two
    purposes: it makes a duplicate failure-webhook delivery a no-op (same as
    mark_order_paid's duplicate protection), and it means a late/out-of-order
    failure event can never downgrade an order that a *different* webhook
    delivery already marked paid.

    Returns True only if THIS call made the transition -- callers use that to
    decide whether to notify the customer / generate a retry link, so a
    duplicate delivery never re-sends that either."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE orders SET status = ? WHERE tenant_id = ? AND id = ? AND status NOT IN (?, ?)",
        (ORDER_STATUS_FAILED, tenant_id, order_id, ORDER_STATUS_PAID, ORDER_STATUS_FAILED),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_order_fulfilled(tenant_id: int, order_id: int) -> bool:
    """The merchant portal's "mark as shipped/delivered" action -- same
    atomic-conditional-transition pattern as mark_order_paid/mark_order_failed.
    `WHERE status = 'paid'` means this can only ever move a *paid* order to
    fulfilled (an unpaid order has nothing to fulfill), and a duplicate click
    is a safe no-op.

    Returns True only if THIS call made the transition -- callers use that to
    show a clear "already fulfilled" vs "done" message rather than silently
    succeeding either way."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE orders SET status = ? WHERE tenant_id = ? AND id = ? AND status = ?",
        (ORDER_STATUS_FULFILLED, tenant_id, order_id, ORDER_STATUS_PAID),
    )
    conn.commit()
    return cur.rowcount > 0


def get_dashboard_stats(tenant_id: int, timezone: str) -> dict:
    """Everything the merchant portal's dashboard needs, in one call --
    stat tiles (today vs. yesterday sales/orders, all-time paid AOV, distinct
    customers, pending count), a 7-day paid-sales trend, a payment-method
    breakdown, top-5 categories by paid revenue, and the 10 most recent
    orders. One function rather than several smaller ones because every
    query here shares the same "today" boundary computed once from the
    tenant's own timezone (SPEC.md Section 3.4's existing per-tenant
    timezone use) -- computing it separately per call site would risk two
    queries disagreeing about where "today" starts.

    Day boundaries are computed in Python (zoneinfo, stdlib) rather than in
    SQL, then compared against created_at/paid_at cast to timestamptz -- same
    "avoid TEXT-format mismatches, compare as timestamptz" reasoning as
    get_abandoned_orders above."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    conn = get_connection()
    tz = ZoneInfo(timezone)
    now_local = datetime.now(tz)
    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)
    yesterday_start = today_start - timedelta(days=1)
    week_start = today_start - timedelta(days=6)  # last 7 days, inclusive of today

    def _one(sql: str, params: tuple):
        row = conn.execute(sql, params).fetchone()
        return list(row.values())[0] if row else None

    sales_today = _one(
        "SELECT COALESCE(SUM(total), 0) FROM orders WHERE tenant_id = ? AND status = ? "
        "AND paid_at::timestamptz >= ? AND paid_at::timestamptz < ?",
        (tenant_id, ORDER_STATUS_PAID, today_start, tomorrow_start),
    )
    sales_yesterday = _one(
        "SELECT COALESCE(SUM(total), 0) FROM orders WHERE tenant_id = ? AND status = ? "
        "AND paid_at::timestamptz >= ? AND paid_at::timestamptz < ?",
        (tenant_id, ORDER_STATUS_PAID, yesterday_start, today_start),
    )
    orders_today = _one(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = ? "
        "AND created_at::timestamptz >= ? AND created_at::timestamptz < ?",
        (tenant_id, today_start, tomorrow_start),
    )
    orders_yesterday = _one(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = ? "
        "AND created_at::timestamptz >= ? AND created_at::timestamptz < ?",
        (tenant_id, yesterday_start, today_start),
    )
    avg_order_value = _one(
        "SELECT AVG(total) FROM orders WHERE tenant_id = ? AND status = ?",
        (tenant_id, ORDER_STATUS_PAID),
    ) or Decimal("0")
    total_customers = _one(
        "SELECT COUNT(DISTINCT customer_phone) FROM orders WHERE tenant_id = ?",
        (tenant_id,),
    )
    pending_orders = _one(
        "SELECT COUNT(*) FROM orders WHERE tenant_id = ? AND status = ?",
        (tenant_id, ORDER_STATUS_PENDING_PAYMENT),
    )

    def _delta_pct(today_val, yesterday_val):
        if not yesterday_val:
            return None  # no comparable baseline -- template shows "--" rather than a misleading 0%/infinite%
        return round((float(today_val) - float(yesterday_val)) / float(yesterday_val) * 100)

    trend_rows = conn.execute(
        "SELECT (paid_at::timestamptz AT TIME ZONE ?)::date AS day, SUM(total) AS total "
        "FROM orders WHERE tenant_id = ? AND status = ? AND paid_at::timestamptz >= ? "
        "GROUP BY day ORDER BY day",
        (timezone, tenant_id, ORDER_STATUS_PAID, week_start),
    ).fetchall()
    by_day = {r["day"]: r["total"] for r in trend_rows}
    weekly_trend = []
    for i in range(7):
        day = (week_start + timedelta(days=i)).date()
        weekly_trend.append({"label": day.strftime("%a"), "total": by_day.get(day, Decimal("0"))})

    payment_method_rows = conn.execute(
        "SELECT COALESCE(payment_method, 'unknown') AS method, COUNT(*) AS cnt "
        "FROM orders WHERE tenant_id = ? AND status = ? GROUP BY method ORDER BY cnt DESC",
        (tenant_id, ORDER_STATUS_PAID),
    ).fetchall()
    payment_method_breakdown = [{"method": r["method"], "count": r["cnt"]} for r in payment_method_rows]

    top_category_rows = conn.execute(
        "SELECT COALESCE(p.category, 'Uncategorized') AS category, "
        "SUM(oi.quantity * oi.unit_price_at_order_time) AS revenue "
        "FROM order_items oi "
        "JOIN orders o ON o.id = oi.order_id AND o.tenant_id = oi.tenant_id "
        "JOIN products p ON p.id = oi.product_id AND p.tenant_id = oi.tenant_id "
        "WHERE oi.tenant_id = ? AND o.status = ? "
        "GROUP BY category ORDER BY revenue DESC LIMIT 5",
        (tenant_id, ORDER_STATUS_PAID),
    ).fetchall()
    top_categories = [{"category": r["category"], "revenue": r["revenue"]} for r in top_category_rows]

    return {
        "sales_today": sales_today,
        "sales_delta_pct": _delta_pct(sales_today, sales_yesterday),
        "orders_today": orders_today,
        "orders_delta_pct": _delta_pct(orders_today, orders_yesterday),
        "avg_order_value": avg_order_value,
        "total_customers": total_customers,
        "pending_orders": pending_orders,
        "weekly_trend": weekly_trend,
        "payment_method_breakdown": payment_method_breakdown,
        "top_categories": top_categories,
        "recent_orders": list_orders(tenant_id, limit=10),
    }


def get_abandoned_orders(tenant_id: int, older_than_hours: float) -> list[Order]:
    """Pending-payment orders old enough to nudge (SPEC.md Phase 6's
    abandoned-cart recovery, reminders/scheduler.py) that haven't already
    received one -- nudge_sent_at IS NULL is the "not yet nudged" filter,
    oldest first so a backlog gets worked through in order.

    older_than_hours is compared against created_at directly in SQL (cast to
    timestamptz), not by computing a cutoff in Python and comparing as TEXT:
    created_at is always DB-generated (`now()::text`, Postgres's
    space-separated text output, e.g. "2026-08-09 08:44:12.08121+00"), which
    would sort incorrectly against a Python-computed ISO-8601 'T'-separated
    cutoff string under plain lexical TEXT comparison -- ' ' (0x20) sorts
    before 'T' (0x54) regardless of the actual time each string represents.
    Casting and comparing as timestamptz sidesteps the format mismatch
    entirely."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE tenant_id = ? AND status = ? AND nudge_sent_at IS NULL "
        "AND created_at::timestamptz <= now() - (? * interval '1 hour') "
        "ORDER BY created_at ASC",
        (tenant_id, ORDER_STATUS_PENDING_PAYMENT, older_than_hours),
    ).fetchall()
    return [_row_to_order(r) for r in rows]


def mark_nudge_sent(tenant_id: int, order_id: int) -> bool:
    """Atomically claims the right to nudge this order -- `WHERE
    nudge_sent_at IS NULL` means a duplicate/overlapping run of
    reminders/scheduler.py's job (e.g. two cron ticks close together) can
    never both send a nudge for the same order, same conditional-UPDATE
    idempotency technique as mark_order_paid/mark_order_failed.

    Returns True only if THIS call made the claim -- callers
    (reminders/scheduler.py) send the WhatsApp nudge only if this returns
    True, and skip it entirely otherwise, same "atomic claim before acting"
    order core/commerce_flow.py's handle_payment_success already
    established (not "send first, mark after" -- marking only after an
    irreversible send can't actually prevent a double-send under a
    concurrent/duplicate run, only avoid double-counting it)."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE orders SET nudge_sent_at = now()::text WHERE tenant_id = ? AND id = ? AND nudge_sent_at IS NULL",
        (tenant_id, order_id),
    )
    conn.commit()
    return cur.rowcount > 0


# --- Customers (lightweight name-on-file, CareConnect's patient-name pattern) ---

@dataclass
class Customer:
    tenant_id: int
    phone: str
    name: str
    created_at: str
    updated_at: str


def _row_to_customer(row) -> Customer:
    return Customer(
        tenant_id=row["tenant_id"],
        phone=row["phone"],
        name=row["name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_customer(tenant_id: int, phone: str) -> Customer | None:
    """None means this phone number has never given a name at this tenant
    -- core/commerce_flow.py's checkout step uses that to decide whether to
    ask for one."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM customers WHERE tenant_id = ? AND phone = ?",
        (tenant_id, phone),
    ).fetchone()
    return _row_to_customer(row) if row else None


def set_customer_name(tenant_id: int, phone: str, name: str) -> Customer:
    """Upsert -- a customer giving their name again (e.g. if they ever get
    asked twice for some reason) just overwrites the previous value rather
    than erroring on the (tenant_id, phone) primary key."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO customers (tenant_id, phone, name, updated_at) VALUES (?, ?, ?, now()::text) "
        "ON CONFLICT (tenant_id, phone) DO UPDATE SET name = EXCLUDED.name, updated_at = EXCLUDED.updated_at",
        (tenant_id, phone, name),
    )
    conn.commit()
    return get_customer(tenant_id, phone)


def get_customer_names(tenant_id: int, phones: list[str]) -> dict[str, str]:
    """Bulk name lookup for the merchant portal's order list -- one query for
    every phone on the page instead of one per row."""
    if not phones:
        return {}
    conn = get_connection()
    placeholders = ", ".join("?" for _ in phones)
    rows = conn.execute(
        f"SELECT phone, name FROM customers WHERE tenant_id = ? AND phone IN ({placeholders})",
        (tenant_id, *phones),
    ).fetchall()
    return {r["phone"]: r["name"] for r in rows}


# --- Coupons (SPEC.md "Offers" menu item) ---

COUPON_TYPE_PERCENTAGE = "percentage"
COUPON_TYPE_FLAT = "flat"


@dataclass
class Coupon:
    id: int
    tenant_id: int
    code: str
    discount_type: str  # COUPON_TYPE_PERCENTAGE or COUPON_TYPE_FLAT
    discount_value: Decimal
    is_active: bool
    expires_at: str | None  # a date string ("YYYY-MM-DD"), valid through the end of that day -- see coupon_validity_error
    created_at: str


def _row_to_coupon(row) -> Coupon:
    return Coupon(
        id=row["id"],
        tenant_id=row["tenant_id"],
        code=row["code"],
        discount_type=row["discount_type"],
        discount_value=row["discount_value"],
        is_active=bool(row["is_active"]),
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def create_coupon(
    tenant_id: int,
    code: str,
    discount_type: str,
    discount_value: Decimal,
    expires_at: str | None = None,
) -> Coupon:
    """code is normalized to uppercase on save so "save10"/"SAVE10"/"Save10"
    are always the same coupon -- get_coupon_by_code also compares
    case-insensitively as a defensive second layer, but storing it
    normalized keeps the portal's own coupon list unambiguous too.

    Raises db.connection.IntegrityError if this tenant already has a coupon
    with this code (schema.sql's UNIQUE(tenant_id, code)) -- portal/coupons.py
    is responsible for catching it and showing a friendly "code already
    exists" error, same pattern admin/onboarding.py uses for a duplicate
    whatsapp_phone_number_id."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO coupons (tenant_id, code, discount_type, discount_value, expires_at) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        (tenant_id, code.strip().upper(), discount_type, discount_value, expires_at),
    )
    new_id = cur.fetchone()["id"]
    conn.commit()
    return get_coupon(tenant_id, new_id)


def get_coupon(tenant_id: int, coupon_id: int) -> Coupon | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM coupons WHERE tenant_id = ? AND id = ?",
        (tenant_id, coupon_id),
    ).fetchone()
    return _row_to_coupon(row) if row else None


def get_coupon_by_code(tenant_id: int, code: str) -> Coupon | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM coupons WHERE tenant_id = ? AND UPPER(code) = UPPER(?)",
        (tenant_id, code),
    ).fetchone()
    return _row_to_coupon(row) if row else None


def list_coupons(tenant_id: int) -> list[Coupon]:
    """The merchant portal's /portal/coupons page -- newest first."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM coupons WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,),
    ).fetchall()
    return [_row_to_coupon(r) for r in rows]


def list_active_coupons(tenant_id: int) -> list[Coupon]:
    """Active, unexpired coupons only -- what the bot's "Offers" menu item
    shows a customer, as opposed to list_coupons' unfiltered merchant view."""
    return [c for c in list_coupons(tenant_id) if coupon_validity_error(c) is None]


def set_coupon_active(tenant_id: int, coupon_id: int, is_active: bool) -> None:
    """The portal's deactivate/reactivate action -- coupons are never
    deleted (orders.coupon_code stores the raw code text specifically so a
    past order's record of its own discount survives regardless, see
    schema.sql's column comment), only toggled off so they stop validating
    for new checkouts."""
    conn = get_connection()
    conn.execute(
        "UPDATE coupons SET is_active = ? WHERE tenant_id = ? AND id = ?",
        (1 if is_active else 0, tenant_id, coupon_id),
    )
    conn.commit()


def coupon_validity_error(coupon: Coupon | None) -> str | None:
    """None if `coupon` can be applied right now; otherwise one of
    "not_found"/"inactive"/"expired" -- core/commerce_flow.py maps each to a
    clear customer-facing message via core/strings.py. Centralized here
    (rather than duplicated between checkout_cart's last-moment recheck and
    core/commerce_flow.py's up-front validation) so both places agree on
    exactly what "valid" means."""
    if coupon is None:
        return "not_found"
    if not coupon.is_active:
        return "inactive"
    if coupon.expires_at and coupon.expires_at < datetime.now().date().isoformat():
        return "expired"
    return None


def compute_discount(subtotal: Decimal, discount_type: str, discount_value: Decimal) -> Decimal:
    """Pure arithmetic, no DB access -- shared by checkout_cart's
    authoritative discount calculation and core/commerce_flow.py's
    cart-preview display, so the number a customer sees before confirming
    always matches what checkout_cart actually charges. Flat discounts are
    capped at the subtotal itself (never a negative total); percentage
    discounts can't exceed 100% by construction (portal/coupons.py's create
    form should reasonably cap this, but nothing here assumes it has)."""
    if discount_type == COUPON_TYPE_PERCENTAGE:
        raw = subtotal * discount_value / Decimal("100")
    else:
        raw = discount_value
    return min(raw, subtotal).quantize(Decimal("0.01"))
