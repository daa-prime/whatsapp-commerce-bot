-- db/schema.sql
-- SPEC.md Section 4 data model. Postgres (Neon), per Section 6/12.6 (of the
-- hospital repo this was seeded from) -- db/repository.py is the only module
-- that knows this is Postgres (via db/connection.py). Datetimes are stored as
-- ISO-8601 TEXT (written by Python's .isoformat(), read back with
-- datetime.fromisoformat()), since Postgres's TEXT type and lexical ISO-8601
-- string ordering behave identically for our purposes.
--
-- tenant_id is on every table from day one (SPEC Section 4/12.2 of the seed
-- repo) -- the point is to never have to retrofit this column onto live data
-- later, same reasoning that applied when this was `hospital_id`.
--
-- Safe to re-run: every statement is IF NOT EXISTS.
--
-- Phase 0 (SPEC.md Section 8) removed the hospital product's departments/
-- doctors/doctor_slots/appointments/appointment_reminders tables entirely,
-- leaving just tenants + conversation_sessions. Phase 5's products/orders/
-- order_items below were built ahead of the phases they'd normally follow
-- (Phase 1's catalog integration, Phase 2's order-received handler, Phase 3's
-- payment integration) since those all depend on this data model existing
-- first -- the tables exist, but nothing in core/main.py writes to them yet.

CREATE TABLE IF NOT EXISTS tenants (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    -- UNIQUE: per-message routing (core/main.py) relies on this constraint,
    -- not application logic, to reject a duplicate phone_number_id that would
    -- otherwise break routing (two tenants can't both claim the same incoming
    -- number). CREATE TABLE IF NOT EXISTS won't retroactively add this to a
    -- database created before this change.
    whatsapp_phone_number_id TEXT UNIQUE,
    meta_access_token_ref TEXT,
    app_secret_ref TEXT,
    -- SPEC.md Section 7 additions, per-tenant (not global env vars) same
    -- pattern as the Meta credentials above. Populated by Phase 7's onboarding
    -- wizard extension once catalog/payment-gateway setup exists; NULL until then.
    meta_catalog_id TEXT,
    payment_gateway_provider TEXT,
    -- Razorpay needs three separate values to actually integrate (SPEC.md
    -- Section 7 only anticipated one "api_key_ref" per tenant, written before
    -- a real integration existed to clarify the requirement): key_id (public,
    -- safe to display), key_secret (private, used to authenticate API calls
    -- -- stored in the pre-existing payment_gateway_api_key_ref column), and
    -- a webhook_secret (private, used only to verify payments.py's webhook
    -- signatures, never sent on outbound API calls). See payments.py.
    payment_gateway_key_id TEXT,
    payment_gateway_api_key_ref TEXT,  -- Razorpay key_secret
    payment_gateway_webhook_secret TEXT,
    welcome_message_text TEXT,
    -- IANA timezone name, used for displaying order/invoice timestamps in the
    -- tenant's local time (SPEC.md Section 3.4) and for Phase 6's abandoned-cart
    -- timing logic. Defaults to Asia/Kolkata since Razorpay (SPEC.md Section
    -- 3.3) targets Indian businesses; onboarding can override it per tenant.
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    -- Data connection tier (connector-interface pattern, SPEC.md Section 8
    -- point 4 -- same architectural idea as the hospital repo's Section
    -- 12.6.2). Tier 2's api_base_url/api_key are only ever stored here, not
    -- acted on -- no connector logic exists yet (built only once a real Tier 2
    -- tenant needs it). Tier 3 stores neither; it's a manually assisted,
    -- non-self-serve case.
    data_tier TEXT NOT NULL DEFAULT 'tier1' CHECK (data_tier IN ('tier1', 'tier2', 'tier3')),
    external_api_base_url TEXT,
    external_api_key TEXT,
    -- How long a pending_payment order sits untouched (SPEC.md Phase 6)
    -- before reminders/scheduler.py nudges the customer. Editable per-tenant
    -- via the onboarding/catalog-payment edit form; defaults to 2 hours.
    abandoned_cart_nudge_hours INTEGER NOT NULL DEFAULT 2,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (now()::text)
);

-- payment_gateway_key_id/payment_gateway_webhook_secret/abandoned_cart_nudge_hours
-- were added after tenants already had committed history -- CREATE TABLE IF
-- NOT EXISTS above won't retroactively add columns to an already-existing
-- table, so these use Postgres's ADD COLUMN IF NOT EXISTS instead, same
-- idempotent-migration goal for a database that already ran an earlier
-- version of this file.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS payment_gateway_key_id TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS payment_gateway_webhook_secret TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS abandoned_cart_nudge_hours INTEGER NOT NULL DEFAULT 2;
-- The merchant portal's login (portal/session.py) -- separate from
-- ADMIN_SECRET, which gates platform-admin operations, not a per-tenant
-- merchant login. NULL until an admin sets/resets it via
-- admin/onboarding.py's portal-password page; a NULL hash can never match
-- any submitted password, so login is correctly refused until then.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS portal_password_hash TEXT;

-- Present per SPEC.md Section 4's schema, but not wired up yet -- core/history.py's
-- Redis/in-memory session store (get_session_store()) is the actual mechanism
-- in use for conversation state, same as it was for the hospital product.
-- Migrating session storage onto this table is a separate future change, not
-- part of Phase 0.
CREATE TABLE IF NOT EXISTS conversation_sessions (
    id SERIAL PRIMARY KEY,
    customer_phone TEXT NOT NULL,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    current_state TEXT NOT NULL DEFAULT 'IDLE',
    context TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (now()::text),
    UNIQUE(customer_phone, tenant_id)
);

-- Mirrors what's synced from Meta's Commerce Manager catalog (SPEC.md Section
-- 3.1), for local pricing/stock lookups when an order-received webhook
-- arrives (Section 3.2) -- db/repository.py, not this table directly, is
-- what the order handler will read from.
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    -- NUMERIC, not FLOAT/DOUBLE, to avoid binary floating-point rounding on
    -- money -- same reasoning applies to orders.subtotal/total and
    -- order_items.unit_price_at_order_time below.
    price NUMERIC(12, 2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    description TEXT,
    image_url TEXT,
    sku TEXT,
    stock_quantity INTEGER NOT NULL DEFAULT 0,
    category TEXT,
    -- INTEGER 0/1, not BOOLEAN, matching tenants.is_active's existing
    -- convention (db/repository.py casts it with bool(row["is_active"])).
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (now()::text)
);

-- The stable id Meta's catalog (and the order webhook it sends back) knows
-- this product by -- "retailer_id" in Meta's own terminology. Deterministic
-- (f"t{tenant_id}-p{product.id}"), not sourced from products.sku: a real
-- client's Shopify export had a SKU on only 7 of 1173 rows, so SKU can't be
-- relied on as a stable, always-present, always-unique catalog identity.
-- Added after products already had committed history, hence ADD COLUMN IF
-- NOT EXISTS rather than being in the CREATE TABLE above.
ALTER TABLE products ADD COLUMN IF NOT EXISTS catalog_retailer_id TEXT;

-- 'browsing' is the pre-order state: a row created as soon as a customer
-- starts interacting, before any line items or pricing are known yet, which
-- is why subtotal/total are nullable rather than NOT NULL like the hospital
-- product's appointments.status default ('booked', assigned only once
-- fully formed) was.
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    customer_phone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'browsing'
        CHECK (status IN ('browsing', 'pending_payment', 'paid', 'failed', 'cancelled', 'fulfilled')),
    payment_link_url TEXT,
    payment_gateway_reference TEXT,
    subtotal NUMERIC(12, 2),
    total NUMERIC(12, 2),
    created_at TEXT NOT NULL DEFAULT (now()::text),
    paid_at TEXT
);

-- nudge_sent_at was added after orders already had committed history, same
-- reason tenants' ADD COLUMN IF NOT EXISTS statements above exist -- a single
-- timestamp is enough here (SPEC.md Phase 6's abandoned-cart recovery sends
-- one nudge per order, not the hospital product's multiple-offsets
-- appointment_reminders pattern; flagged as worth adding later if a two-stage
-- nudge is ever wanted, not built now). NULL means "not yet nudged" -- the
-- filter db.get_abandoned_orders() and the idempotency guard
-- db.mark_nudge_sent() both key off.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS nudge_sent_at TEXT;

-- Populated from Razorpay's webhook payload (payment.entity.method --
-- "upi"/"card"/"netbanking"/"wallet"/etc, see payments.py's
-- _extract_payment_method) once a payment is captured -- NULL for any order
-- that hasn't been paid yet, or was paid before this column existed. Used
-- by the merchant portal's dashboard payment-method breakdown.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT;

-- tenant_id is included directly here (not just reachable via order_id ->
-- orders.tenant_id) for the same reason the hospital schema's
-- appointment_reminders carried hospital_id alongside appointment_id: every
-- repository query filters by tenant_id directly, without needing a join
-- just to enforce tenant scoping.
CREATE TABLE IF NOT EXISTS order_items (
    id SERIAL PRIMARY KEY,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,
    -- Snapshot of products.price at the moment this line item was created --
    -- a later price change on the product must never retroactively change
    -- the total of an order that already references it.
    unit_price_at_order_time NUMERIC(12, 2) NOT NULL
);
