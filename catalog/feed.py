# catalog/feed.py
"""
Meta commerce product feed (SPEC.md Section 3.1's "v1: manual catalog setup
per business (upload a product feed/CSV to Meta Commerce Manager)") --
generates the CSV a merchant/admin registers once as a data feed source in
Meta Commerce Manager, which Meta then polls periodically (typically
hourly) to sync products into the catalog linked to a tenant's WhatsApp
Business Account.

Chosen over the Catalog Batch API (real-time, programmatic item create/
update via Graph API) because the Batch API needs a `catalog_management`
Graph API permission that requires Meta App Review for a production app --
an external, non-code approval process. A feed needs no such permission:
catalog creation and feed registration are both manual Commerce Manager UI
steps regardless of sync method. This can be swapped for/supplemented by
the Batch API later without changing the id scheme below (products.
catalog_retailer_id, db/repository.py) -- only how items get to Meta would
change, not how they're identified.

Column set is Meta's required commerce feed columns: id, title,
description, availability, condition, price, link, image_link. `id` is
products.catalog_retailer_id (see db/repository.py's _catalog_retailer_id
docstring for why this isn't sourced from products.sku). `link` needs an
absolute URL per Meta's feed spec even though this is a WhatsApp-only
storefront with no real per-product web page -- PUBLIC_BASE_URL + "/" is
used as a placeholder landing page; not yet confirmed against Meta's feed
validator whether this is acceptable for a catalog that's only ever used
for WhatsApp messaging (not Facebook/Instagram Shops) -- flagged as
something to verify once a live feed is registered.
"""
import csv
import io
import os

import db.repository as db

_FEED_FIELDNAMES = ["id", "title", "description", "availability", "condition", "price", "link", "image_link"]


class PublicBaseURLNotConfigured(Exception):
    """Raised when PUBLIC_BASE_URL isn't set -- the feed's `link` column
    needs an absolute URL (Meta's feed spec requirement), and there's no
    sensible default the way DATABASE_URL/WHATSAPP_VERIFY_TOKEN don't have
    one either -- fail clearly rather than emit a feed with broken links."""


def _public_base_url() -> str:
    url = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if not url:
        raise PublicBaseURLNotConfigured(
            "PUBLIC_BASE_URL environment variable is required to generate the product feed "
            "(Meta's feed spec requires an absolute URL in the 'link' column) — e.g. "
            "https://yourapp.example.com or the current ngrok tunnel URL during local testing."
        )
    return url.rstrip("/")


def feed_url(tenant_id: int) -> str | None:
    """The exact URL an admin/merchant pastes into Commerce Manager's data
    feed registration -- returns None (rather than raising) if
    PUBLIC_BASE_URL isn't configured yet, so pages that just want to *show*
    this URL (the onboarding confirmation page, the catalog/payment edit
    page) can display "not available yet" instead of crashing."""
    try:
        base_url = _public_base_url()
    except PublicBaseURLNotConfigured:
        return None
    return f"{base_url}/catalog/{tenant_id}.csv"


def build_feed_csv(tenant_id: int) -> str | None:
    """Returns the feed CSV text for a tenant's active products, or None if
    the tenant doesn't exist/isn't active -- callers (core/main.py's feed
    route) turn that into a 404 rather than serving an empty feed for a
    typo'd or deactivated tenant_id."""
    tenant = db.get_tenant(tenant_id)
    if tenant is None or not tenant.is_active:
        return None

    base_url = _public_base_url()
    products = db.get_active_products(tenant_id)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_FEED_FIELDNAMES)
    writer.writeheader()
    for p in products:
        writer.writerow({
            "id": p.catalog_retailer_id,
            "title": p.name,
            "description": p.description or p.name,
            "availability": "in stock" if p.stock_quantity > 0 else "out of stock",
            "condition": "new",
            "price": f"{p.price} {p.currency}",
            "link": f"{base_url}/",
            "image_link": p.image_url or "",
        })
    return buffer.getvalue()
