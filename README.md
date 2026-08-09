<h1 align="center">WhatsApp Commerce Storefront</h1>

**A WhatsApp-native storefront for e-commerce businesses with low website footfall.**

Customers message the business's WhatsApp number, browse a product catalog natively inside WhatsApp, add items to a cart, check out via a payment link, and receive an invoice — all without leaving WhatsApp except to complete payment. No custom UI for things Meta's Commerce Platform already provides.

![Python](https://img.shields.io/badge/Python-3.13-blue)
![Tests](https://github.com/daa-prime/whatsapp-commerce-bot/actions/workflows/tests.yml/badge.svg)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Status

**A customer can shop, check out, and pay today — through a manual bot-driven menu, not Meta's native catalog/cart.** This repo was forked from a working WhatsApp hospital-booking product to reuse its multi-tenant webhook infrastructure. See [Spec.md](Spec.md) Section 0 for the current build-phase status in detail, and Section 5 for the full phase plan.

What works right now: a customer messages the bot, taps through Shop Now → browse products → add to cart → view cart → checkout, gets a real Razorpay payment link, and — once they pay — a WhatsApp confirmation with their order automatically marked paid. What doesn't exist yet: Meta Commerce Manager catalog integration (so browsing happens via a bot-built list, not WhatsApp's native catalog UI) and invoicing (a PDF once payment confirms).

---

## What it will do (per [Spec.md](Spec.md))

| Capability | Status |
|---|---|
| Meta webhook receipt, HMAC signature validation | ✅ Working |
| Multi-tenant routing (one deployment, many businesses) | ✅ Working |
| Products/orders/order_items data model + CRUD | ✅ Built |
| Shop/cart/checkout conversation flow, My Orders, Track Order | ✅ Built (menu-driven bot flow, not Meta's native catalog/cart yet) |
| Stock checks + race-safe decrement + duplicate-checkout protection | ✅ Hardened — see [DECISIONS.md](DECISIONS.md) |
| Payment link generation + gateway webhook (Razorpay) | ✅ Built, pending live verification — see Spec.md Section 0's Phase 3 caveat |
| Onboarding wizard: catalog/payment-gateway setup | ✅ Built (`/admin/onboard-tenant` + `/admin/tenant/{id}/catalog-payment`) |
| Product catalog (via Meta Commerce Manager) | ❌ Not started — products are entered manually for now |
| Order-received webhook handling (Meta's native `order` message type) | ❌ Not started — `commerce_flow.py`'s cart is a manual substitute |
| PDF invoice generation + delivery | ❌ Not started |

---

## How it works (today)

```
Customer sends WhatsApp message
        │
        ▼
┌───────────────────┐
│   FastAPI webhook  │ ◄── validates per-tenant HMAC signature
└─────────┬──────────┘
          │
          ▼
┌───────────────────┐
│  Resolve tenant by  │ ◄── multi-tenant routing: one deployment,
│  phone_number_id    │     many businesses, each with its own
└─────────┬──────────┘     Meta credentials
          │
          ▼
┌───────────────────────┐
│  core/commerce_flow.py │ ◄── menu-driven state machine: Shop Now,
│  Shop → Cart → Checkout │     My Orders, Track Order, Offers/Account/
└─────────┬──────────────┘     Talk to Us placeholders (Spec.md 3)
          │
          ▼
┌────────────────────────┐
│  Checkout creates a real │ ◄── orders (pending_payment) + order_items,
│  order + Razorpay link,  │     stock decremented atomically, link sent
│  customer pays           │     to the customer (payments.py)
└─────────┬────────────────┘
          │
          ▼
┌────────────────────────┐
│  POST /webhook/payment   │ ◄── Razorpay's callback (separate from the
│  verifies signature,     │     Meta webhook above) marks the order paid/
│  marks order paid/failed │     failed and confirms via WhatsApp
└────────────────────────┘
```

The shop/cart step is a temporary substitute for Meta's native catalog/cart (Spec.md Section 2) — once Phase 1's real catalog is linked, browsing should move to WhatsApp's own catalog UI, and `commerce_flow.py`'s job narrows to handling the resulting `order` webhook plus My Orders/Track Order/checkout/payment.

---

## Quick start

### 1. Clone and install

```bash
git clone https://github.com/daa-prime/whatsapp-commerce-bot.git
cd whatsapp-commerce-bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

Create a `.env` file with:

- `DATABASE_URL` — a Postgres connection string (Neon recommended)
- `WHATSAPP_ACCESS_TOKEN` + `WHATSAPP_PHONE_NUMBER_ID` + `WHATSAPP_APP_SECRET` — [Meta Developer Portal](https://developers.facebook.com/), for the one tenant seeded at startup
- `WHATSAPP_VERIFY_TOKEN` — any string you choose (must match webhook config)
- `ADMIN_SECRET` — protects the `/admin/onboard-tenant` form
- `INTERNAL_SECRET` — protects the `/internal/*` cron-triggered endpoints
- `REDIS_URL` (optional) — session/history/message-lock storage; falls back to in-memory if unset
- `TENANT_NAME` (optional) — display name for the seeded tenant, defaults to "Default Tenant"

### 3. Run

```bash
uvicorn core.main:app --reload
```

The database schema is created (and the one tenant from your `.env` seeded) automatically on startup — no manual migration step for a fresh clone.

### 4. Expose for WhatsApp

```bash
ngrok http 8000
```

Set the webhook URL in Meta Developer Portal → WhatsApp → Configuration:
- Callback URL: `https://your-ngrok-url.ngrok.io/webhook`
- Verify token: same as your `WHATSAPP_VERIFY_TOKEN`

For payments, set the webhook URL in Razorpay Dashboard → Settings → Webhooks (per tenant, since each has its own Razorpay account):
- URL: `https://your-ngrok-url.ngrok.io/webhook/payment`
- Active events: `payment_link.paid`, `payment_link.expired`, `payment.failed`
- Copy the webhook secret Razorpay generates into that tenant's "Razorpay Webhook Secret" field (see below)

### Onboarding additional tenants

`GET /admin/onboard-tenant` — a plain server-rendered form (protected by `ADMIN_SECRET`) for adding businesses beyond the one seeded from `.env`. Collects tenant identity, Meta credentials, a data-connection tier, and optional catalog/payment-gateway fields (Meta Catalog ID, Razorpay key ID/key secret/webhook secret). `GET /admin/tenant/{tenant_id}/catalog-payment` lets an operator add or update the catalog/payment fields later without re-onboarding — submitting it with a field left blank keeps that field's current value rather than clearing it.

---

## Testing

```bash
pytest tests/ -v
```

155 tests across 11 files, covering the webhook/routing/signature-validation infrastructure, multi-tenant isolation, session/history storage, phone normalization, the onboarding form (including catalog/payment-gateway fields), the products/orders/order_items CRUD layer, the full shop/cart/checkout conversation flow (including cross-tenant isolation and the 10-row list cap), stock/race-condition hardening (a genuine two-connection concurrency test, not a sequential simulation, proving two customers racing for the last unit of a product can never both succeed), and the Razorpay payment integration (link generation, webhook signature validation, success/failure/expiry handling, duplicate-delivery idempotency, cross-tenant isolation). Requires a real Postgres to run against — `tests/conftest.py` provisions a throwaway one automatically via Docker (testcontainers), or set `TEST_DATABASE_URL` to point at one directly.

---

## Deploy

### Railway (recommended)

The repo includes `railway.toml` ready to go:

```bash
railway up
```

Set environment variables in the Railway dashboard, pointed at a Neon Postgres instance. Add a cron job for the abandoned-cart-nudges placeholder (currently a no-op, earmarked for Phase 6):
```
curl -X POST https://your-app.railway.app/internal/send-abandoned-cart-nudges \
  -H "X-Internal-Secret: $INTERNAL_SECRET"
```

### Other platforms

Any platform that runs Python + FastAPI works. The app starts with:

```bash
uvicorn core.main:app --host 0.0.0.0 --port $PORT
```

---

## Architecture

```
whatsapp-commerce-bot/
├── core/
│   ├── main.py          # FastAPI app, webhook receipt/routing, message locking
│   ├── commerce_flow.py  # Shop/cart/checkout/orders + payment-webhook handlers
│   ├── whatsapp.py       # WhatsApp Cloud API client, signature validation, payload parsing
│   ├── history.py        # Conversation history + session store (Redis / in-memory)
│   └── phone.py          # Phone number normalization
├── payments.py           # Razorpay payment links + webhook signature/payload parsing
├── admin/
│   └── onboarding.py     # Tenant onboarding form + catalog/payment edit step
├── db/
│   ├── connection.py      # Postgres connection layer
│   ├── schema.sql         # Data model (tenants, products, orders, order_items)
│   ├── repository.py      # The only module that writes raw SQL
│   ├── init_db.py         # Schema init + seed on startup
│   └── seed.py            # Seed data (default tenant, test tenant)
├── reminders/
│   └── scheduler.py       # Abandoned-cart-nudges placeholder (Phase 6, not implemented)
└── tests/                 # 155 tests
```

See [Spec.md](Spec.md) for the full build spec and phase plan, and [DECISIONS.md](DECISIONS.md) for the architectural rationale carried over from the hospital-booking fork.

---

## Contributing

Contributions are welcome. The codebase is intentionally small and direct — please keep it that way.

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Make sure tests pass (`pytest tests/ -v`)
4. Open a pull request

No issue template, no CLA. Just describe what you changed and why.

---

## License

MIT
