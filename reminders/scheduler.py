# reminders/scheduler.py
"""
Abandoned-cart recovery job (SPEC.md Section 3.3/Phase 6: "abandoned payment
links... allow retry, don't leave the order in limbo silently"). Designed to
run on a fixed interval via an *external* trigger -- POST
/internal/send-abandoned-cart-nudges in core/main.py, meant to be hit by a
cron job -- rather than an in-process scheduler, same pattern the hospital
repo's old appointment-reminder job used (and this file's own predecessor,
back when it was just a no-op stub).

Single nudge per order, not the hospital product's multiple-offsets
appointment_reminders pattern (e.g. 24h-before AND 1h-before) -- orders.nudge_sent_at
is one column, not a per-offset join table. A two-stage nudge (e.g. a second,
more urgent one a day later) would be a real, worthwhile addition following
that same multiple-offsets shape if abandoned-cart recovery data ever
justifies it; flagged here rather than built, since one nudge is what was
asked for.
"""
import logging

import db.repository as db
from core.strings import t
from core.whatsapp import WhatsAppClient

logger = logging.getLogger(__name__)


def _nudge_message(order: db.Order) -> str:
    """order.language (snapshotted at checkout, core/commerce_flow.py's
    _complete_checkout_from_cart) is used here for the same reason
    core/commerce_flow.py's payment-webhook handlers use it -- this job
    runs on an external cron trigger, potentially days after the customer's
    conversation session expired, so there's no live session to read a
    language preference from."""
    if order.payment_link_url:
        return t("nudge_with_link", order.language, id=order.id, url=order.payment_link_url)
    return t("nudge_without_link", order.language, id=order.id)


async def send_abandoned_cart_nudges(wa: WhatsAppClient, tenant_id: int) -> int:
    """Finds this tenant's pending_payment orders older than its configured
    abandoned_cart_nudge_hours (default 2, db.Tenant.abandoned_cart_nudge_hours)
    that haven't been nudged yet, and sends each customer a reminder -- their
    existing payment link if checkout already generated one, otherwise a
    prompt to message the bot again to complete checkout (no payment link
    exists yet e.g. if the tenant hadn't configured Razorpay at checkout time).

    Returns the number of nudges actually sent."""
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return 0

    orders = db.get_abandoned_orders(tenant_id, tenant.abandoned_cart_nudge_hours)
    sent = 0
    for order in orders:
        # Atomic claim BEFORE sending, not after -- db.mark_nudge_sent()'s
        # docstring explains why "mark after send" can't actually guarantee
        # a duplicate/overlapping run never double-sends.
        claimed = db.mark_nudge_sent(tenant_id, order.id)
        if not claimed:
            logger.info("Order %s (tenant %s) already nudged by a concurrent run, skipping", order.id, tenant_id)
            continue

        await wa.send_text(order.customer_phone, _nudge_message(order))
        sent += 1
        logger.info(
            "Abandoned-cart nudge sent to %s for order %s (tenant %s)",
            order.customer_phone, order.id, tenant_id,
        )

    return sent
