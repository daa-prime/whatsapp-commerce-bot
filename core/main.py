import json
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # must run before os.environ[...] reads below, or db.init_db()'s env reads

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

import db.repository as db
from admin.onboarding import router as onboarding_router
from core.commerce_flow import handle_incoming
from core.history import get_history, get_session_store
from core.whatsapp import WhatsAppClient, extract_phone_number_id, parse_incoming_message, validate_webhook_signature
from db.init_db import init_db
from reminders.scheduler import send_abandoned_cart_nudges

# uvicorn only configures its own "uvicorn"/"uvicorn.error"/"uvicorn.access" loggers
# (see uvicorn.config.LOGGING_CONFIG) — it never touches the root logger. Every logger
# in this app (core.whatsapp, core.main, ...) propagates up to the root logger instead,
# which without this call has no handler and falls back to Python's "last resort"
# handler (WARNING+ only) — so logger.info(...) calls anywhere in the app would be
# silently invisible even though the logging calls themselves are correct.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)

HISTORY = get_history()
SESSIONS = get_session_store()
# Creates the schema + seeds the one real tenant from .env if not already present
# (idempotent, safe on every startup).
init_db()

VERIFY_TOKEN = os.environ["WHATSAPP_VERIFY_TOKEN"]
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")

_redis = None  # None = not yet checked; False = checked and unreachable; client = available

# In-memory fallback for the per-(tenant, phone) message-processing lock (used when Redis is not available)
_message_locks: dict[str, float] = {}  # "tenant_id:phone" -> lock expiry timestamp

# One WhatsAppClient per tenant, built lazily from that tenant's own DB-stored
# credentials and reused for the life of the process — avoids re-creating an
# httpx.AsyncClient (connection pool) on every message.
_wa_clients: dict[int, WhatsAppClient] = {}


def _get_whatsapp_client(tenant: db.Tenant) -> WhatsAppClient:
    client = _wa_clients.get(tenant.id)
    if client is None:
        client = WhatsAppClient(
            phone_number_id=tenant.whatsapp_phone_number_id,
            access_token=tenant.access_token,
        )
        _wa_clients[tenant.id] = client
    return client


def _get_redis():
    """Same connect-once-and-fall-back pattern as core.history's get_history()/
    get_session_store(): ping once, and if Redis isn't reachable, remember that
    (False) instead of returning a client that will raise on first real command."""
    global _redis
    if _redis is None:
        redis_url = os.environ.get("REDIS_URL")
        if redis_url:
            try:
                import redis
                client = redis.from_url(redis_url, decode_responses=True)
                client.ping()
                _redis = client
            except Exception:
                _redis = False
        else:
            _redis = False
    return _redis


def _acquire_message_lock(tenant_id: int, phone: str, ttl: int = 15) -> bool:
    """Try to acquire a processing lock for this (tenant, phone) pair. Returns
    True if acquired. Scoped by tenant_id so the same phone number messaging
    two different tenants can never block itself across them."""
    key_suffix = f"{tenant_id}:{phone}"
    r = _get_redis()
    if r:
        return bool(r.set(f"msg_lock:{key_suffix}", "1", nx=True, ex=ttl))
    # In-memory fallback
    now = time.time()
    expiry = _message_locks.get(key_suffix, 0)
    if now < expiry:
        return False  # Lock still held
    _message_locks[key_suffix] = now + ttl
    return True


def _release_message_lock(tenant_id: int, phone: str):
    key_suffix = f"{tenant_id}:{phone}"
    r = _get_redis()
    if r:
        r.delete(f"msg_lock:{key_suffix}")
    else:
        _message_locks.pop(key_suffix, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(onboarding_router)


@app.get("/")
async def health():
    return {"status": "ok"}


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.body()

    # Covers: invalid JSON body (json.JSONDecodeError, a ValueError subclass),
    # valid JSON that isn't the expected dict-of-dicts shape (a list/string/number
    # anywhere in the payload -> TypeError on subscripting), and payloads missing
    # expected keys/indices (KeyError/IndexError) — e.g. a message type Meta added
    # that we don't parse a field for, a malformed test payload, etc. None of these
    # should crash the webhook handler; Meta gets a 200 either way since there's
    # nothing to usefully retry for a payload shape we don't understand.
    try:
        data = json.loads(body)
        entry = data["entry"][0]
        change = entry["changes"][0]["value"]

        # Resolve which tenant this message is *for* (the WhatsApp number that
        # received it, from `metadata` — not the sender's number, which is
        # `message["from"]` below) BEFORE trusting/validating anything else in
        # the payload. This is a structural read of routing metadata only, not
        # "processing the message" — nothing here acts on the message content,
        # sends anything, or touches order/session state; that all happens
        # further down, strictly after signature verification passes.
        incoming_phone_number_id = extract_phone_number_id(change)
        tenant = db.find_tenant_by_phone_number_id(incoming_phone_number_id)
        if tenant is None:
            logger.warning(
                "Webhook received for unrecognized/inactive phone_number_id=%s — no matching tenant, ignoring",
                incoming_phone_number_id,
            )
            return Response(status_code=200)

        # Each tenant has its own app_secret — verify against *that* tenant's
        # secret, not a single global one. A payload signed with tenant A's
        # secret can never pass for tenant B's phone_number_id, even though
        # both are handled by this one endpoint.
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not validate_webhook_signature(body, signature, tenant.app_secret):
            logger.warning("Invalid webhook signature for tenant_id=%s (phone_number_id=%s)", tenant.id, incoming_phone_number_id)
            raise HTTPException(status_code=403, detail="Invalid signature")

        # Ignore status updates (delivered, read, etc.) — after signature
        # verification, same as everything else, even though there's nothing to
        # act on either way.
        if "statuses" in change:
            return Response(status_code=200)

        message = change["messages"][0]
        phone = message["from"]
        reply = parse_incoming_message(message)
        wa = _get_whatsapp_client(tenant)

        if reply["type"] == "audio":
            # No transcription pipeline (no AI/LLM in this build) — bypass the state
            # machine entirely and tell the customer to use text instead.
            await wa.send_text(phone, "I couldn't process your audio. Could you send it as text instead?")
            return Response(status_code=200)

    except (KeyError, IndexError, TypeError, ValueError):
        logger.warning("Malformed or unexpected webhook payload, ignoring: %s", body[:500])
        return Response(status_code=200)

    # Prevent concurrent processing for the same (tenant, phone) pair
    if not _acquire_message_lock(tenant.id, phone):
        logger.info("Message from %s (tenant %s) skipped (already processing)", phone, tenant.id)
        return Response(status_code=200)

    logger.info("Message lock acquired for %s (tenant %s), type=%s", phone, tenant.id, reply["type"])
    try:
        await _process_message(wa, tenant, phone, reply)
    finally:
        _release_message_lock(tenant.id, phone)

    return Response(status_code=200)


async def _process_message(wa: WhatsAppClient, tenant: db.Tenant, phone: str, reply: dict) -> None:
    """Process a single already-parsed incoming message with the (tenant, phone)
    lock already held.

    Dispatches to core/commerce_flow.py's menu-driven state machine (Shop Now,
    My Orders, Track Order, cart, checkout). This is a manual "type a message
    to get a menu" flow, not yet driven by Meta's native catalog/cart
    messages (SPEC.md Section 2) — Phase 1's real catalog integration and
    Phase 2's `order` message type parsing still replace/extend this once a
    live test catalog exists.
    """
    logger.info("Dispatching message from %s (tenant %s): %s", phone, tenant.id, reply)
    HISTORY.add(phone, "user", reply.get("text") or reply.get("title") or f"[{reply.get('type')}]")
    await handle_incoming(wa, SESSIONS, phone, tenant.id, reply, tenant.name)


@app.post("/internal/send-abandoned-cart-nudges")
async def trigger_abandoned_cart_nudges(request: Request):
    """Hit by an external cron job — not an in-process scheduler, same pattern
    as this endpoint's predecessor. Loops over every active tenant, sending
    each one's abandoned-cart nudges with its own credentials.

    Earmarked for SPEC.md Phase 6 — reminders/scheduler.py's
    send_abandoned_cart_nudges() is a no-op placeholder until the order/cart
    data model (Phase 5) exists, so this always reports 0 sent for now."""
    secret = request.headers.get("X-Internal-Secret", "")
    if secret != INTERNAL_SECRET:
        raise HTTPException(status_code=403)

    sent_by_tenant = {}
    for tenant in db.get_active_tenants():
        wa = _get_whatsapp_client(tenant)
        sent_by_tenant[tenant.name] = await send_abandoned_cart_nudges(wa, tenant.id)

    return {"sent": sum(sent_by_tenant.values()), "by_tenant": sent_by_tenant}
