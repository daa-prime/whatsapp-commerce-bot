# payments.py
"""
Razorpay payment-link generation and webhook payload/signature handling
(SPEC.md Section 3.3, Phase 3). Same separation as core/whatsapp.py vs.
core/commerce_flow.py: this module only knows how to talk to Razorpay's API
and how to parse/verify its webhook payloads -- it has no opinion on what an
order status transition should *do* (send a WhatsApp message, generate a
retry link, etc.); that's core/commerce_flow.py's job.

Credential reading: a tenant's Razorpay credentials are read straight off
its `tenants` row via db.get_tenant() -- the exact same pattern
core/whatsapp.py's WhatsAppClient uses for Meta credentials. There is no
separate secrets-manager lookup or decryption step anywhere in this
codebase; the "_ref" suffix on these columns (payment_gateway_api_key_ref,
meta_access_token_ref) suggests that may have been the original intent, but
nothing implements it. Flagging this rather than inventing new encryption
infrastructure this task didn't ask for.

Uses Razorpay's Payment Links product (client.payment_link.create), not the
lower-level Orders API -- Payment Links are the simpler of the two for "send
this customer a link for this amount" (no separate client-side checkout
integration or capture step), matching SPEC.md Section 3.3's plain "generate
a payment link" framing.

Payload shape caveat (same class of flag as core/whatsapp.py's order-message
parsing): the webhook payload paths below are written from Razorpay's
published webhook documentation, not yet verified against a live payment
link and a real webhook delivery. Treat parse_payment_webhook()'s exact
field paths as something to confirm once a real Razorpay account is wired
up, same as SPEC.md Section 8 already flags for Meta's order webhook.
"""
import hashlib
import hmac
import logging
from dataclasses import dataclass
from decimal import Decimal

import razorpay

import db.repository as db

logger = logging.getLogger(__name__)

# Event names as Razorpay sends them in the top-level "event" field of a
# webhook payload.
_EVENT_PAYMENT_LINK_PAID = "payment_link.paid"
_EVENT_PAYMENT_LINK_EXPIRED = "payment_link.expired"
_EVENT_PAYMENT_FAILED = "payment.failed"

# Normalized event_type values this module hands back to callers, decoupling
# core/commerce_flow.py from Razorpay's exact event-name strings.
EVENT_PAID = "paid"
EVENT_EXPIRED = "expired"
EVENT_FAILED = "failed"
EVENT_UNKNOWN = "unknown"

_EVENT_MAP = {
    _EVENT_PAYMENT_LINK_PAID: EVENT_PAID,
    _EVENT_PAYMENT_LINK_EXPIRED: EVENT_EXPIRED,
    _EVENT_PAYMENT_FAILED: EVENT_FAILED,
}


class PaymentGatewayNotConfigured(Exception):
    """Raised when a tenant hasn't set up Razorpay credentials yet (SPEC.md
    Section 7's onboarding fields are all optional -- 'leave blank if you
    haven't set this up yet'). Callers (core/commerce_flow.py) catch this and
    fall back to a placeholder message rather than letting checkout fail."""


@dataclass
class PaymentWebhookEvent:
    event_type: str  # EVENT_PAID / EVENT_EXPIRED / EVENT_FAILED / EVENT_UNKNOWN
    tenant_id: int | None
    order_id: int | None
    payment_gateway_reference: str | None  # Razorpay's payment id (pay_xxx) or payment_link id (plink_xxx)


def _build_reference_id(tenant_id: int, order_id: int) -> str:
    """The one merchant-controlled field Razorpay reliably round-trips back
    on every payment-link-related webhook event -- this is what lets
    parse_payment_webhook() resolve a payload back to the exact tenant+order
    it belongs to, across however many different tenants' Razorpay accounts
    are sending webhooks to this one shared endpoint (SPEC.md's multi-tenant
    routing concern, same as Meta's phone_number_id serves for the WhatsApp
    webhook -- except here it has to be embedded in payload content we
    control, since Razorpay has no equivalent of a routable "which number
    received this" field)."""
    return f"{tenant_id}:{order_id}"


def _parse_reference_id(reference_id: str | None) -> tuple[int | None, int | None]:
    if not reference_id or ":" not in reference_id:
        return None, None
    tenant_str, _, order_str = reference_id.partition(":")
    try:
        return int(tenant_str), int(order_str)
    except ValueError:
        return None, None


def _client_for_tenant(tenant: db.Tenant) -> razorpay.Client:
    if not tenant.payment_gateway_key_id or not tenant.payment_gateway_api_key_ref:
        raise PaymentGatewayNotConfigured(f"Tenant {tenant.id} has no Razorpay key_id/key_secret configured yet.")
    return razorpay.Client(auth=(tenant.payment_gateway_key_id, tenant.payment_gateway_api_key_ref))


def create_payment_link(tenant: db.Tenant, order: db.Order) -> tuple[str, str]:
    """Creates a Razorpay Payment Link for `order.total` and returns
    (short_url, payment_link_id). Raises PaymentGatewayNotConfigured if the
    tenant hasn't set up Razorpay credentials, or lets razorpay.errors.*
    propagate for a genuine API failure (network issue, bad credentials,
    etc.) -- callers are responsible for deciding what to tell the customer
    in either case; this function only talks to Razorpay.

    Does not write the link back onto the order row -- callers (core/
    commerce_flow.py) do that via db.update_order_payment_link(), same
    separation as db.create_order_item not reading products.price itself."""
    client = _client_for_tenant(tenant)
    reference_id = _build_reference_id(tenant.id, order.id)
    # Razorpay amounts are in the smallest currency unit (paise for INR).
    # order.total is NUMERIC(12,2); multiplying by 100 is exact for a value
    # with at most 2 decimal places.
    amount_paise = int(order.total * 100)

    link = client.payment_link.create({
        "amount": amount_paise,
        "currency": "INR",
        "description": f"Order #{order.id}",
        "customer": {"contact": order.customer_phone},
        # WhatsApp is the notification channel here (this bot sends the link
        # itself) -- don't have Razorpay separately SMS/email the customer.
        "notify": {"sms": False, "email": False},
        "reference_id": reference_id,
        # Duplicated into notes as a fallback lookup path: Razorpay's
        # `payment.failed` event isn't always guaranteed to carry the
        # originating payment_link's reference_id in the same payload shape
        # `payment_link.paid` does (unverified against a live payload, see
        # module docstring) -- notes are commonly copied through to the
        # associated payment entity, so this is a defensive second path for
        # parse_payment_webhook() to try.
        "notes": {"reference_id": reference_id},
    })
    return link["short_url"], link["id"]


def validate_payment_webhook_signature(body: bytes, signature: str, webhook_secret: str | None) -> bool:
    """Validates Razorpay's webhook HMAC-SHA256 signature. Hand-rolled rather
    than razorpay.Client().utility.verify_webhook_signature() for two
    reasons: that method takes body as str (re-encoding it internally),
    where every other signature check in this codebase (core/whatsapp.py's
    validate_webhook_signature) operates on the raw request bytes directly;
    and it raises on failure rather than returning bool, a different
    convention than the rest of this codebase uses. Note Razorpay's signature
    header has no "sha256=" prefix, unlike Meta's X-Hub-Signature-256.

    webhook_secret is per-tenant (SPEC.md Section 7, same multi-tenant
    pattern as Meta's app_secret) and can be None for a tenant that hasn't
    configured one yet -- fail closed rather than raising, same reasoning as
    core/whatsapp.py's validate_webhook_signature."""
    if not webhook_secret or not signature:
        return False
    expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_reference_id(payload: dict) -> str | None:
    """Tries every known path a reference_id could show up on, since
    different Razorpay webhook events nest payload.payment_link vs.
    payload.payment differently (see module docstring's payload-shape
    caveat)."""
    inner = payload.get("payload", {})
    for entity_key in ("payment_link", "payment"):
        entity = inner.get(entity_key, {}).get("entity", {})
        if entity.get("reference_id"):
            return entity["reference_id"]
        notes = entity.get("notes") or {}
        if notes.get("reference_id"):
            return notes["reference_id"]
    return None


def _extract_payment_gateway_reference(payload: dict) -> str | None:
    """Prefers the actual payment's id (pay_xxx) -- the more meaningful
    "proof of payment" reference -- falling back to the payment_link's id
    (plink_xxx) for events where no payment entity is present yet (e.g. a
    link expiring with no payment ever attempted)."""
    inner = payload.get("payload", {})
    payment_entity = inner.get("payment", {}).get("entity", {})
    if payment_entity.get("id"):
        return payment_entity["id"]
    link_entity = inner.get("payment_link", {}).get("entity", {})
    return link_entity.get("id")


def parse_payment_webhook(payload: dict) -> PaymentWebhookEvent:
    """Normalizes a raw Razorpay webhook payload into a PaymentWebhookEvent.
    Centralizes payload parsing so nothing else in the app touches
    Razorpay's raw shape, same reason core/whatsapp.py's
    parse_incoming_message exists for Meta's payloads."""
    raw_event = payload.get("event", "")
    event_type = _EVENT_MAP.get(raw_event, EVENT_UNKNOWN)
    reference_id = _extract_reference_id(payload)
    tenant_id, order_id = _parse_reference_id(reference_id)
    payment_gateway_reference = _extract_payment_gateway_reference(payload)
    return PaymentWebhookEvent(
        event_type=event_type,
        tenant_id=tenant_id,
        order_id=order_id,
        payment_gateway_reference=payment_gateway_reference,
    )
