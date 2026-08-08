<h1 align="center">WhatsApp Commerce Storefront</h1>

**A WhatsApp-native storefront for e-commerce businesses with low website footfall.**

Customers message the business's WhatsApp number, browse a product catalog natively inside WhatsApp, add items to a cart, check out via a payment link, and receive an invoice — all without leaving WhatsApp except to complete payment. No custom UI for things Meta's Commerce Platform already provides.

![Python](https://img.shields.io/badge/Python-3.13-blue)
![Tests](https://github.com/daa-prime/whatsapp-commerce-bot/actions/workflows/tests.yml/badge.svg)
![License](https://img.shields.io/badge/license-MIT-green)

---

## Status

**Early — infrastructure only, no commerce features live yet.** This repo was forked from a working WhatsApp hospital-booking product to reuse its multi-tenant webhook infrastructure. Phase 0 (stripping the hospital domain logic, keeping the infra) is done. Catalog integration, order handling, payment, and invoicing are not built yet. See [Spec.md](Spec.md) Section 0 for the current build-phase status in detail, and Section 5 for the full phase plan.

What works right now: a message sent to a registered WhatsApp number is received, routed to the correct tenant, signature-verified, and replied to with that tenant's welcome message. That's it — there's no catalog, cart, order, or payment flow yet.

---

## What it will do (per [Spec.md](Spec.md))

| Capability | Status |
|---|---|
| Meta webhook receipt, HMAC signature validation | ✅ Working |
| Multi-tenant routing (one deployment, many businesses) | ✅ Working |
| Product catalog (via Meta Commerce Manager) | ❌ Not started |
| Order-received webhook handling | ❌ Not started |
| Payment link generation + gateway webhook (Razorpay) | ❌ Not started |
| PDF invoice generation + delivery | ❌ Not started |
| Onboarding wizard: catalog/payment-gateway setup | ❌ Not started |

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
┌───────────────────┐
│  Reply with tenant's │ ◄── placeholder pending Phase 2's real
│  welcome message     │     order-received handling (Spec.md 3.2)
└───────────────────┘
```

Once catalog/order support lands, the flow becomes: customer browses Meta's native catalog UI → taps "Add to cart" → taps "Send order" → this webhook receives an `order` message → replies with a summary + Razorpay payment link → payment webhook confirms → invoice sent as a WhatsApp document.

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

### Onboarding additional tenants

`GET /admin/onboard-tenant` — a plain server-rendered form (protected by `ADMIN_SECRET`) for adding businesses beyond the one seeded from `.env`. Collects tenant identity, Meta credentials, and a data-connection tier (this platform's own database, or a future connector to the business's existing system). Catalog and payment-gateway setup aren't collected here yet — that's Phase 7.

---

## Testing

```bash
pytest tests/ -v
```

70 tests across 7 files, covering the webhook/routing/signature-validation infrastructure, multi-tenant isolation, session/history storage, phone normalization, and the onboarding form. Requires a real Postgres to run against — `tests/conftest.py` provisions a throwaway one automatically via Docker (testcontainers), or set `TEST_DATABASE_URL` to point at one directly.

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
│   ├── whatsapp.py       # WhatsApp Cloud API client, signature validation, payload parsing
│   ├── history.py        # Conversation history + session store (Redis / in-memory)
│   └── phone.py          # Phone number normalization
├── admin/
│   └── onboarding.py     # Tenant onboarding form (/admin/onboard-tenant)
├── db/
│   ├── connection.py      # Postgres connection layer
│   ├── schema.sql         # Data model (tenants; commerce tables land in Phase 5)
│   ├── repository.py      # The only module that writes raw SQL
│   ├── init_db.py         # Schema init + seed on startup
│   └── seed.py            # Seed data (default tenant, test tenant)
├── reminders/
│   └── scheduler.py       # Abandoned-cart-nudges placeholder (Phase 6, not implemented)
└── tests/                 # 70 tests
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
