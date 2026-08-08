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
-- Phase 0 (SPEC.md Section 8): this is the stripped-down schema after removing
-- the hospital product's departments/doctors/doctor_slots/appointments/
-- appointment_reminders tables entirely. `products`/`orders`/`order_items`
-- (SPEC.md Section 4) are Phase 5 work, built once the order-received webhook
-- handler (Phase 2) is in place -- not added here.

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
    payment_gateway_api_key_ref TEXT,
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
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (now()::text)
);

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
