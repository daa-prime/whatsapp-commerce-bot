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
    is_active: bool
    data_tier: str
    external_api_base_url: str | None
    external_api_key: str | None


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
        is_active=bool(row["is_active"]),
        data_tier=row["data_tier"],
        external_api_base_url=row["external_api_base_url"],
        external_api_key=row["external_api_key"],
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

    data_tier: "tier1" (default, this product's own database), "tier2"
    (external_api_base_url/external_api_key are only stored here -- no
    connector logic reads them yet), or "tier3" (direct DB connection, not
    self-serve -- neither field is meaningful for it)."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO tenants (name, whatsapp_phone_number_id, meta_access_token_ref, app_secret_ref, "
        "welcome_message_text, timezone, meta_catalog_id, payment_gateway_provider, payment_gateway_key_id, "
        "payment_gateway_api_key_ref, payment_gateway_webhook_secret, "
        "data_tier, external_api_base_url, external_api_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (name, whatsapp_phone_number_id, access_token, app_secret, welcome_message_text, timezone,
         meta_catalog_id, payment_gateway_provider, payment_gateway_key_id,
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
) -> None:
    """Onboarding wizard's catalog/payment-gateway edit step (SPEC.md Section
    7, Phase 7) -- Phase 3's real Razorpay integration (payments.py) reads
    payment_gateway_key_id/payment_gateway_api_key_ref (key_secret) to call
    the API and payment_gateway_webhook_secret to verify webhook signatures.

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
        "payment_gateway_webhook_secret = COALESCE(?, payment_gateway_webhook_secret) "
        "WHERE id = ?",
        (meta_catalog_id, payment_gateway_provider, payment_gateway_key_id,
         payment_gateway_api_key_ref, payment_gateway_webhook_secret, tenant_id),
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
    )


def get_product(tenant_id: int, product_id: int) -> Product | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM products WHERE tenant_id = ? AND id = ?",
        (tenant_id, product_id),
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
    """Manual product entry for now -- Phase 1's real catalog sync (SPEC.md
    Section 3.1) will call this (or its own bulk-upsert variant) once a
    tenant's Meta Commerce Manager catalog is linked."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO products (tenant_id, name, price, currency, description, image_url, sku, "
        "stock_quantity, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (tenant_id, name, price, currency, description, image_url, sku, stock_quantity, category),
    )
    new_id = cur.fetchone()["id"]
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


def create_order(
    tenant_id: int,
    customer_phone: str,
    status: str = ORDER_STATUS_BROWSING,
    subtotal: Decimal | None = None,
    total: Decimal | None = None,
) -> Order:
    """Defaults to ORDER_STATUS_BROWSING -- an order row can exist before any
    line items or pricing are known yet (SPEC.md Section 4's conversation_sessions
    note), which is why subtotal/total are optional here."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO orders (tenant_id, customer_phone, status, subtotal, total) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        (tenant_id, customer_phone, status, subtotal, total),
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
        order_row = conn.execute(
            "INSERT INTO orders (tenant_id, customer_phone, status, subtotal, total) "
            "VALUES (?, ?, ?, ?, ?) RETURNING id",
            (tenant_id, customer_phone, ORDER_STATUS_PENDING_PAYMENT, subtotal, subtotal),
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
) -> bool:
    """Atomically transitions an order to ORDER_STATUS_PAID -- `WHERE status
    != 'paid'` means a duplicate/concurrent payment webhook delivery for an
    order that's already paid is a safe no-op, not a double-transition. This
    is a single conditional UPDATE, not a separate SELECT-then-UPDATE, for
    the same reason as checkout_cart()'s stock decrement: two genuinely
    concurrent webhook deliveries for the same order must not both see
    "not yet paid" and both act on it.

    Returns True only if THIS call is the one that actually made the
    transition -- callers (core/commerce_flow.py's handle_payment_success)
    use that to decide whether to send the "payment received" WhatsApp
    confirmation, so a duplicate delivery never double-sends it."""
    conn = get_connection()
    cur = conn.execute(
        "UPDATE orders SET status = ?, "
        "payment_gateway_reference = COALESCE(?, payment_gateway_reference), "
        "paid_at = ? "
        "WHERE tenant_id = ? AND id = ? AND status != ?",
        (ORDER_STATUS_PAID, payment_gateway_reference, paid_at, tenant_id, order_id, ORDER_STATUS_PAID),
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
