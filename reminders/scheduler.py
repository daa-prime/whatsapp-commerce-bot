# reminders/scheduler.py
"""
Placeholder for the abandoned-cart recovery job earmarked in SPEC.md Section
3.3/Phase 6 ("abandoned payment links... allow retry, don't leave the order
in limbo silently"). Not wired up yet — there's no order/cart data model
until Phase 5, so send_abandoned_cart_nudges() is a no-op stub.

Kept as a skeleton so the cron-triggered-endpoint shape (core/main.py's
/internal/* endpoints, looping over every active tenant with that tenant's
own WhatsAppClient) is already in place once Phase 6 needs it — same pattern
this repo's old appointment-reminder job used, just without any real logic
plugged in yet.
"""
from core.whatsapp import WhatsAppClient


async def send_abandoned_cart_nudges(wa: WhatsAppClient, tenant_id: int) -> int:
    """Earmarked for Phase 6 (SPEC.md): find this tenant's abandoned/pending
    orders and nudge the customer to complete payment. Not implemented —
    always returns 0 until the order data model (Phase 5) exists."""
    return 0
