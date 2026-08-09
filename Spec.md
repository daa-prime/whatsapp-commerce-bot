# WhatsApp Commerce Store — Build Spec

**Purpose of this doc:** Hand this to Claude Code as the source of truth for adapting the existing WhatsApp hospital-booking codebase into a WhatsApp-native e-commerce storefront. This is a **new, separate repo**, seeded from the hospital repo's infrastructure — most of the plumbing is reused; the domain logic (catalog, cart, orders, payment, invoicing) is new.

---

## 0. Progress Log (update as you go)

**Status (last updated after the menu-driven commerce conversation flow landed):**

- **Phase 0 — done.** Hospital domain logic (`booking_flow.py`, doctor/slot scheduling, appointment reminders) is stripped. Infrastructure (Meta webhook receipt, per-tenant HMAC signature validation, multi-tenant routing, Postgres connection layer, Railway/Neon deployment config) is intact and renamed (`hospital_id`/`hospitals` → `tenant_id`/`tenants`). The orphaned Google Calendar/Mercado Pago prototype (`config/`, `modules/booking/`) that predated even the hospital product is also removed.
- **Phase 1 (catalog setup) — not started.** No Meta Commerce Manager catalog is linked to any tenant; no test catalog message has been sent or verified. Products are entered manually via `db.create_product` for now (no UI yet — see Phase 7).
- **Phase 2 (order-received webhook handling) — partially done, via a manual flow rather than Meta's native cart.** `core/commerce_flow.py` is a new menu-driven state machine (Shop Now → browse → add to cart → view cart → checkout) wired into `core/main.py`'s webhook handler, so a customer can place an order today by typing/tapping through the bot. This is **not** SPEC.md Section 3.2's intended design — it doesn't parse Meta's native `order` message type (`parse_incoming_message()` still has no branch for it), because Phase 1's real catalog/cart isn't linked yet. Once it is, this manual product-browsing flow should likely be replaced or trimmed back in favor of Meta's native catalog UI, with `commerce_flow.py` handling just the resulting `order` webhook plus My Orders/Track Order.
- **Phase 3 (payment integration) — not started.** Checkout creates a real `orders` row (`pending_payment`) + `order_items`, then stops at a "Payment integration coming next" placeholder message. No Razorpay (or any) SDK, no payment-link generation, no gateway webhook route yet.
- **Phase 4 (invoice generation) — not started.** No PDF library, no WhatsApp document-send capability yet (`WhatsAppClient` only has `send_text`/`send_list`/`send_buttons`).
- **Phase 5 (order/inventory data model) — done.** `products`, `orders`, and `order_items` tables in `db/schema.sql` with CRUD in `db/repository.py`, now actually written to by `commerce_flow.py`'s checkout step (previously built but unused).
- **Phase 6 (edge cases) — still mostly a placeholder.** `reminders/scheduler.py::send_abandoned_cart_nudges()` remains a no-op stub. Stock quantity is *not* checked or decremented anywhere in the new shop flow (a product can be "sold" past its `stock_quantity` with no warning) — deferred, matching this phase's explicit scope in the phase table. Payment/order-confirmation races and duplicate order messages remain unaddressed.
- **Phase 7 (onboarding wizard: catalog/payment-gateway steps) — not started.** `admin/onboarding.py` collects tenant identity, Meta credentials, and data-connection tier only; there's still no UI for adding products or catalog/payment-gateway config.
- **Phase 8 (multi-tenant support beyond the pilot) — not applicable yet.**

`core/main.py`'s message handler now dispatches to `core/commerce_flow.py` instead of a placeholder welcome message. A customer can complete a full shop → cart → checkout conversation and get a real `pending_payment` order in the database; nothing past that (payment, invoicing) exists yet.

---

## 1. Product Summary

A WhatsApp-native storefront for e-commerce businesses with low website footfall. Customers message the business's WhatsApp number, browse a product catalog natively inside WhatsApp, add items to a cart, check out via a payment link, and receive an invoice — all without leaving WhatsApp except to complete payment.

**Core priorities (same as the hospital product):** free/low-cost to run, minimal maintenance, reuse Meta's native features wherever possible instead of building custom UI for things Meta already provides.

**Non-goals for v1:** no AI/LLM-driven conversation (menu/catalog-driven, same philosophy as the hospital bot). No in-chat native payment (WhatsApp Pay for businesses isn't broadly available in India for this use case) — checkout happens via a payment gateway link. No multi-language support initially.

---

## 2. What Meta Already Provides (don't rebuild this)

- **Product Catalog** (via Meta Commerce Manager): product images, names, prices, descriptions live here, synced once per business.
- **Native catalog/product browsing messages**: WhatsApp itself renders a scrollable product browsing UI when you send a catalog message — this is not custom UI you build.
- **Native cart mechanism**: customers tap "Add to Cart" on catalog items; WhatsApp accumulates the cart client-side and sends **your webhook an order message** (product IDs, quantities) when the customer taps "Send order" — you don't maintain cart state yourself while the customer is browsing.

**What this means for scope:** the "browse and build a cart" experience is mostly Meta's job. Your bot's real job starts when an order message arrives.

---

## 3. Core Components to Build

### 3.1 Catalog setup (per-tenant)
- Each business's product catalog is created/managed in Meta Commerce Manager, linked to their WhatsApp Business Account.
- **v1**: manual catalog setup per business (upload a product feed/CSV to Meta Commerce Manager) — same "manually-assisted onboarding" philosophy as the hospital product's Tier 2/3.
- **Later**: if a business's e-commerce platform (Shopify, WooCommerce, etc.) has a product API, auto-sync the catalog — explicitly deferred until a real case needs it, same principle as the hospital connector interface (Section 12.6).

### 3.2 Order-received webhook handling
- When WhatsApp sends an order message (via the `messages` webhook field, a specific `order` message type), parse: product IDs, quantities, catalog ID.
- Look up current pricing/stock from the tenant's product data (stored in your own database, mirrored from the catalog).
- Reply with an order summary: line items, quantities, subtotal, any shipping/tax logic the business needs.

### 3.3 Checkout & payment
- Generate a **payment link** via a payment gateway (Razorpay is the practical default for Indian businesses — supports UPI, cards, netbanking).
- Send the payment link as part of the order confirmation message.
- Set up a webhook from the payment gateway (separate from Meta's webhook) that fires on payment success/failure.
- On payment success: mark the order paid, trigger invoice generation, send confirmation + invoice over WhatsApp.
- On payment failure/abandonment: handle gracefully — allow retry, don't leave the order in limbo silently.

### 3.4 Invoice generation
- Generate a PDF invoice (business name, order line items, totals, order ID) once payment is confirmed.
- Send it as a WhatsApp **document message** (Meta's API supports sending PDFs directly as attachments).

### 3.5 Order & inventory data model
- Same architectural shape as the hospital's `appointments` table — a persistent record of every order, its state, and payment status.

---

## 4. Data Model (replaces hospital-specific tables)

```
tenants  (renamed from `hospitals` — same multi-tenant pattern)
  id, name, whatsapp_phone_number_id, meta_access_token_ref, app_secret_ref,
  meta_catalog_id, payment_gateway_provider, payment_gateway_api_key_ref,
  welcome_message_text, is_active, created_at

products  (mirrors what's in Meta's catalog, for pricing/stock lookups)
  id, tenant_id, catalog_product_id (Meta's own product ID), name, price, stock_quantity, is_active

orders
  id, tenant_id, customer_phone, status (pending_payment/paid/failed/cancelled/fulfilled),
  payment_link_url, payment_gateway_reference, subtotal, total, created_at, paid_at

order_items
  id, order_id, product_id, quantity, unit_price_at_order_time

conversation_sessions
  (same pattern as hospital repo — tracks any pre-order conversational state, e.g. an initial welcome/FAQ flow before catalog browsing)
```

**Note:** unlike the hospital product's `doctor_slots`, there's no "slot" concept here — availability is stock quantity, decremented on order (or on payment confirmation, depending on the business's tolerance for overselling during checkout).

---

## 5. Build Phases (in order)

| Phase | Goal | Depends on |
|---|---|---|
| 0 | Strip hospital-specific code (`booking_flow.py`, doctor/slot logic, appointment reminders) from the cloned repo. Keep webhook, multi-tenant routing, DB connection layer, deployment config. | New repo cloned |
| 1 | Meta Commerce Manager catalog set up for the pilot e-commerce client; confirm native catalog browsing + cart works by sending a test catalog message | Phase 0 |
| 2 | Build the order-received webhook handler (Section 3.2) | Phase 1 |
| 3 | Payment gateway integration — generate payment links, handle the gateway's webhook for payment success/failure | Phase 2 |
| 4 | Invoice generation + delivery as a WhatsApp document message | Phase 3 |
| 5 | Order/inventory data model wired in (Section 4) — replacing any hospital-specific tables entirely | Can start in parallel with 2-4 |
| 6 | Edge cases: payment webhook arrives before/after order confirmation race, stock going to zero mid-checkout, abandoned payment links, duplicate order messages | Phase 2-4 stable |
| 7 | Onboarding wizard adapted from the hospital product's Steps 1-5 (Meta account/app/verification/token — identical) + new Steps 6-8 for catalog/payment gateway setup instead of doctor/slot config | Phase 6 stable |
| 8 | Multi-tenant support for additional e-commerce clients beyond the pilot | Phase 7 |

---

## 6. Infrastructure

Identical to the hospital product: Railway (persistent process, needed for payment webhook handling same as reminder scheduling) + Neon (Postgres). No changes needed to this layer — it's designed to be reused across the connector-interface pattern already.

---

## 7. Environment/Secrets Needed (additions beyond the hospital repo's list)

- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` (or equivalent for chosen gateway) — per-tenant, stored in the `tenants` table, not global env vars, same multi-tenant pattern as Meta credentials
- `META_CATALOG_ID` — per-tenant, from Meta Commerce Manager

---

## 8. Instructions for Claude Code

1. Start by auditing the cloned repo: identify every file/function that's hospital-specific (booking_flow.py, doctor/slot logic, appointment reminders, the onboarding wizard's Step 7 doctor fields) vs. genuinely reusable infrastructure (webhook receiver, signature validation, multi-tenant routing, DB connection layer, Railway/Neon config). Report this audit before deleting anything.
2. Delete/replace hospital-specific logic per the audit; keep infrastructure intact.
3. Build in the phase order above — don't skip to payment integration before order-received handling works cleanly.
4. Reuse the connector-interface pattern (same architectural idea as the hospital repo's Section 12.6.2) for order/product operations, so a future Tier 2 (business's own e-commerce platform API) or Tier 3 (direct DB) connection is possible later without a rebuild.
5. Flag clearly if Meta's order-message webhook payload shape differs from what's assumed here — this spec is written from general WhatsApp Commerce documentation, not yet verified against a live test catalog, so treat Section 3.2's exact parsing as something to confirm once Phase 1 is live.