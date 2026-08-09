# Architecture Decisions

Technical decisions made during development, with rationale.

## Fork the hospital-booking infra rather than start from scratch

**Decision:** This repo was seeded from a working WhatsApp hospital-booking product (webhook receipt, multi-tenant routing, Postgres connection layer, Railway/Neon deployment) instead of building a new WhatsApp bot from zero.

**Why:** The plumbing a WhatsApp-native business bot needs — verified webhook signatures, per-tenant credential routing, a Redis-or-in-memory session/lock layer, a Postgres connection adapter — is identical regardless of whether the domain is appointments or orders. That infrastructure was already proven in production; only the domain logic (booking flow, doctor/slot scheduling) was hospital-specific and needed to be replaced.

**How:** Phase 0 (see [Spec.md](Spec.md) Section 8) started with an explicit audit — every file/function classified as either domain logic (deleted) or reusable infrastructure (kept, renamed `hospital_id`/`hospitals` → `tenant_id`/`tenants`) — before anything was deleted, so the reuse boundary was a deliberate decision, not a guess.

**Trade-off:** Some naming/shape still echoes the hospital domain (e.g. the connector-interface "data tier" concept in `tenants`, mirrored from the hospital repo's Tier 1/2/3 pattern) rather than being designed fresh for commerce. Acceptable — the pattern itself (self-serve DB vs. connect-your-own-API vs. manually-assisted direct connection) is genuinely domain-agnostic.

## Strip and stub before building — infra first, domain logic later

**Decision:** Phase 0 replaced the hospital-specific message handler with a placeholder that just echoes the tenant's welcome message, rather than building any commerce logic in the same pass.

**Why:** Deleting `core/booking_flow.py` left nothing to dispatch incoming messages to. Building a real order-handling flow at the same time as the strip-and-rename pass would have mixed two different kinds of risk — "did the rename break routing" and "is the new commerce logic correct" — into one change. Keeping the placeholder trivial (send the welcome message, prove the webhook/signature/routing/lock chain still works end-to-end) let Phase 0 be verified in isolation.

**Trade-off:** The app currently can't do anything commerce-related. That's intentional, not an oversight — see [Spec.md](Spec.md) Section 0 for exactly what's built vs. planned.

## Multi-tenant from the first table, not retrofitted later

**Decision:** Every table carries a `tenant_id` column and every repository query filters by it explicitly, even now that (per Section 6 of the last status review) there's no confirmed live pilot tenant yet.

**Why:** Carried over directly from the hospital repo's `hospital_id` convention. Retrofitting a tenant-scoping column onto live data later is much harder than including it from row one — cheap now, expensive after real customer data exists.

## Postgres via a thin sqlite3-shaped adapter, not raw psycopg2 everywhere

**Decision:** `db/connection.py`'s `_PGConnection` wraps psycopg2 to expose `conn.execute(sql, params).fetchone()/.fetchall()` chaining and dict-like row access, and rewrites `?` placeholders to psycopg2's `%s` style.

**Why:** Inherited from the hospital repo's own migration off SQLite — `db/repository.py`'s call sites were already written against `sqlite3.Connection`'s chaining convenience, so this adapter let that migration (and everything built on top of it since) avoid touching every call site individually. Autocommit is on so a caught `IntegrityError` (e.g. a duplicate `whatsapp_phone_number_id` at onboarding) doesn't poison the rest of the connection's transaction state, matching how SQLite behaved.

**Trade-off:** An extra indirection layer over "just use psycopg2 directly." Worth it for how much of the existing repository code it let carry over unchanged.

## Sync psycopg2 over asyncpg despite an async FastAPI app

**Decision:** `db/repository.py` is plain synchronous code, called directly from async FastAPI handlers (blocking the event loop on every DB call).

**Why:** Inherited as-is from the hospital repo. Switching to asyncpg would mean async-ifying every repository function and every caller — a much bigger change than reusing the existing data-access layer. Traffic volume for a single-pilot-tenant storefront doesn't yet justify that rewrite.

**Trade-off:** Won't scale gracefully to high concurrent load without revisiting. Reasonable to defer until there's a real multi-tenant traffic pattern to design against.

## Dual Redis/in-memory backend for session state, history, and message locks

**Decision:** Every stateful component (conversation session store, message history, per-`(tenant, phone)` processing lock) has both a Redis implementation and an in-memory fallback, chosen automatically based on whether `REDIS_URL` is set and reachable.

**Why:** Redis is the right choice in production (persistence, TTL, shared state across worker processes). Requiring it for local development adds friction. The dual backend lets `uvicorn core.main:app --reload` run with zero extra infrastructure.

## Per-tenant HMAC signature validation, not one global secret

**Decision:** Each tenant's `app_secret_ref` is looked up and checked independently; a payload signed with tenant A's secret is never valid for tenant B's `phone_number_id`, even though both tenants share one webhook endpoint.

**Why:** Webhooks are public endpoints. With one deployment serving many businesses, a single shared secret would mean any one tenant's leaked/misconfigured secret compromises every other tenant sharing the deployment. Constant-time comparison (`hmac.compare_digest`) additionally prevents timing attacks. A tenant with no secret configured yet (mid-onboarding) fails closed rather than crashing or accidentally validating.

## Per-`(tenant, phone)` message locking

**Decision:** Each `(tenant_id, phone)` pair has a short-TTL lock (Redis or in-memory) that prevents concurrent processing of messages from the same customer at the same business.

**Why:** WhatsApp can deliver multiple messages from the same customer in rapid succession. Without locking, near-simultaneous messages could be processed out of order or duplicate side effects (once order/payment logic exists, this becomes the difference between one order and two).

**Trade-off:** Messages from the same customer are processed sequentially, adding latency for burst messages. Acceptable for a chat interface; scoped by tenant so one business's traffic can never lock out another's.

## Asia/Kolkata as the default tenant timezone

**Decision:** `tenants.timezone` defaults to `Asia/Kolkata` if not set explicitly at onboarding.

**Why:** Razorpay — the planned default payment gateway (Spec.md Section 3.3) — targets Indian businesses. A sensible default avoids forcing every onboarding to specify a timezone just to get correct order/invoice timestamp display later.

## Stock decrement as one conditional UPDATE, not SELECT-then-UPDATE

**Decision:** Checkout's stock decrement (`db.checkout_cart()`) is a single `UPDATE products SET stock_quantity = stock_quantity - ? WHERE ... AND stock_quantity >= ?`, not a separate read-then-write.

**Why:** A separate `SELECT stock_quantity` followed by an application-level `if enough: UPDATE` has a race window between the two statements — two concurrent checkouts can both read "1 in stock" before either writes, and both succeed, overselling the last unit. Expressing the check and the write as one statement lets Postgres's row-level locking serialize concurrent attempts on the same row: the second UPDATE to reach a given row always sees the first one's already-decremented value, so its own `stock_quantity >= ?` condition correctly fails if there isn't enough left. Proven under genuine concurrency (two real connections, two OS threads, not a sequential simulation) in `tests/test_products_orders.py::test_concurrent_checkouts_for_last_unit_exactly_one_succeeds`.

**Relation to the hospital repo's double-booking guard:** same architectural idea (a DB-level constraint does the real work, not application logic) as the hospital schema's `UNIQUE INDEX ... WHERE status = 'booked'`, adapted for a different shape of problem — "don't let two inserts collide on an exact value" doesn't apply to "don't let a counter go negative," so this uses a conditional write instead of a uniqueness constraint.

## A real transaction for exactly one call site, not a connection-wide change

**Decision:** `db/connection.py` gained a `transaction()` context manager (autocommit off for its duration, explicit commit/rollback) used by `db.checkout_cart()` alone. Every other repository function keeps using the connection's default per-statement autocommit, completely unaffected.

**Why:** The connection layer has run in autocommit mode since the hospital repo's SQLite-to-Postgres migration — every existing repository function already assumes each statement is its own transaction (see that migration's own rationale above). Checkout is the first call site that genuinely needs several statements (stock decrement, order insert, order_items inserts) to succeed or fail together — if the process crashed between decrementing stock and creating the order, autocommit would leave stock permanently decremented with no order to show for it. Rather than flipping the whole connection to manual-transaction mode (which would force every other call site to start managing commits explicitly, a much bigger change for one call site's need), `transaction()` is scoped to a `with` block and restores autocommit on exit either way.

## reference_id, not a database lookup, resolves a payment webhook to a tenant

**Decision:** Every Razorpay Payment Link is created with `reference_id = "{tenant_id}:{order_id}"` (`payments.py`'s `_build_reference_id`). `POST /webhook/payment` parses that back out of the incoming payload *before* doing anything else, and that's what determines which tenant's webhook secret to verify the signature against.

**Why:** Meta's webhook payload carries `phone_number_id` — a field Meta itself controls and always includes, which already uniquely identifies the receiving tenant. Razorpay has no equivalent: each tenant has its own separate Razorpay account, and nothing in a generic Razorpay webhook payload says which merchant/tenant it's for beyond content the payment link's *creator* chose to embed. `reference_id` (and a duplicate copy in `notes`, for payload-shape redundancy — see `payments.py`'s module docstring) is that embedded content. This makes tenant resolution a two-phase process, same shape as the Meta webhook: parse the payload structurally (untrusted) to find `reference_id` → look up that tenant → verify the signature with *that* tenant's own webhook secret → only then act. A payload with a missing or malformed `reference_id` can't be resolved to any tenant and is safely ignored.

**Trade-off:** Order ids are per-tenant, not globally unique, so `reference_id` must always carry both halves — `order_id` alone would let tenant B's order collide with tenant A's order of the same number. Tested directly in `tests/test_payment_webhook.py::test_order_id_collision_across_tenants_resolves_to_correct_tenant`.

## Three Razorpay credentials per tenant, not one

**Decision:** `tenants` gained two columns beyond the one Spec.md Section 7 originally anticipated (`payment_gateway_api_key_ref`): `payment_gateway_key_id` (public) and `payment_gateway_webhook_secret` (private, used only for webhook signature verification).

**Why:** Razorpay's API authenticates with a `(key_id, key_secret)` pair, and webhook signature verification uses a *third*, separately-generated secret — none of these three are interchangeable, and a real integration needs all of them. Section 7's env-var list was written before a real Razorpay integration existed to clarify the requirement; discovered and corrected while building `payments.py`.
