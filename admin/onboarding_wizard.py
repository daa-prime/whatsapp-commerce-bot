# admin/onboarding_wizard.py
"""
Guided onboarding wizard -- a step-rail experience (Jinja2 templates +
vanilla JS, single page, single POST on final submit) replacing
admin/onboarding.py's old flat form at the same URL (/admin/onboard-tenant).
Mirrors CareConnect's wizard structure (Meta setup sub-steps, review-and-
submit with masked credentials), adapted for commerce: catalog/payment setup
and product entry replace CareConnect's doctor/department config.

Still admin-secret-gated (ADMIN_SECRET, reused from admin.onboarding) -- an
operator runs this on the merchant's behalf, same as the flat form it
replaces. Not merchant self-serve signup (that would need a different auth
model entirely -- not what was asked here).

No Tier 1/2/3 data-connection concept in this wizard (unlike the old flat
form) -- every tenant created here defaults to tier1 (this platform's own
database). The old form's Step 0 equivalent (existing-store vs starting-
fresh) only controls whether Step 6 shows a CSV upload or manual product-entry
rows; it isn't persisted anywhere.

Reuses db.create_tenant()/db.create_product() directly (no new bulk-insert
repository function -- a loop here is enough reuse) and
admin.onboarding.ADMIN_SECRET/_VALID_PAYMENT_PROVIDERS rather than
duplicating them.
"""
import csv
import io
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import db.repository as db
from admin.onboarding import ADMIN_SECRET, _VALID_PAYMENT_PROVIDERS
from catalog.feed import feed_url
from db.connection import IntegrityError

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_VALUE_FIELDS = [
    "admin_secret", "name", "whatsapp_phone_number_id", "access_token", "app_secret",
    "welcome_message_text", "timezone", "abandoned_cart_nudge_hours",
    "meta_catalog_id", "payment_gateway_provider", "payment_gateway_key_id",
    "payment_gateway_api_key_ref", "payment_gateway_webhook_secret", "product_entry_mode",
]


def _parse_nudge_hours(raw: str, errors: list[str]) -> int:
    """Same validation admin/onboarding.py's create/edit forms each already
    have their own inline copy of -- not refactored there too (out of scope
    for this task), but this is the one copy the new wizard uses."""
    raw = (raw or "").strip()
    if not raw:
        return 2
    try:
        hours = int(raw)
        if hours <= 0:
            raise ValueError
        return hours
    except ValueError:
        errors.append("Abandoned cart nudge (hours) must be a positive whole number.")
        return 2


def _mask_secret(value: str | None) -> str:
    if not value:
        return "not set"
    if len(value) <= 4:
        return "••••"
    return "••••" + value[-4:]


def _parse_csv_products(content: bytes, errors: list[str]) -> list[dict]:
    """Parses the uploaded product CSV (name, price, description, image_url,
    sku, stock_quantity, category). A row with a missing name or an invalid
    price/stock is skipped with an error appended, rather than silently
    defaulting or aborting the whole import over one bad row."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        errors.append("Product CSV must be UTF-8 encoded text.")
        return []

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "name" not in reader.fieldnames or "price" not in reader.fieldnames:
        errors.append('Product CSV must have a header row including at least "name" and "price" columns.')
        return []

    products = []
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        name = (row.get("name") or "").strip()
        if not name:
            errors.append(f"CSV row {i}: product name is required.")
            continue

        price_raw = (row.get("price") or "").strip()
        try:
            price = Decimal(price_raw)
        except (InvalidOperation, ValueError):
            errors.append(f'CSV row {i} ("{name}"): price "{price_raw}" is not a valid number.')
            continue

        stock_raw = (row.get("stock_quantity") or "0").strip()
        try:
            stock_quantity = int(stock_raw) if stock_raw else 0
        except ValueError:
            errors.append(f'CSV row {i} ("{name}"): stock_quantity "{stock_raw}" is not a whole number.')
            continue

        products.append({
            "name": name,
            "price": price,
            "description": (row.get("description") or "").strip() or None,
            "image_url": (row.get("image_url") or "").strip() or None,
            "sku": (row.get("sku") or "").strip() or None,
            "stock_quantity": stock_quantity,
            "category": (row.get("category") or "").strip() or None,
        })
    return products


def _parse_manual_products(form, errors: list[str]) -> list[dict]:
    """Reads the repeatable manual product-entry rows -- same-name, repeated
    form fields (product_name/product_price/...), one value per row, in
    order. FastAPI's Form(...) parameter declarations don't cleanly support a
    dynamic number of same-name fields, so this reads the raw form directly
    via .getlist() instead."""
    names = form.getlist("product_name")
    prices = form.getlist("product_price")
    descriptions = form.getlist("product_description")
    image_urls = form.getlist("product_image_url")
    skus = form.getlist("product_sku")
    stock_quantities = form.getlist("product_stock_quantity")
    categories = form.getlist("product_category")

    def _at(lst, i):
        return (lst[i] if i < len(lst) else "").strip()

    products = []
    for i in range(len(names)):
        name = _at(names, i)
        if not name:
            continue  # an empty leftover row (added then never filled in) -- skipped, not an error

        price_raw = _at(prices, i)
        try:
            price = Decimal(price_raw)
        except (InvalidOperation, ValueError):
            errors.append(f'Product "{name}": price "{price_raw}" is not a valid number.')
            continue

        stock_raw = _at(stock_quantities, i)
        try:
            stock_quantity = int(stock_raw) if stock_raw else 0
        except ValueError:
            errors.append(f'Product "{name}": stock quantity "{stock_raw}" is not a whole number.')
            continue

        products.append({
            "name": name,
            "price": price,
            "description": _at(descriptions, i) or None,
            "image_url": _at(image_urls, i) or None,
            "sku": _at(skus, i) or None,
            "stock_quantity": stock_quantity,
            "category": _at(categories, i) or None,
        })
    return products


def _values_from_form(form) -> dict:
    """Scalar fields only -- used to re-populate the wizard on a validation
    error. Product rows aren't restored on re-render (re-filling dynamic
    repeated rows from a server response is real added complexity for a
    "just try again" path; not attempted here)."""
    return {k: form.get(k, "") for k in _VALUE_FIELDS}


@router.get("/admin/onboard-tenant", response_class=HTMLResponse)
async def onboard_tenant_wizard_form(request: Request):
    return templates.TemplateResponse(request, "onboarding_wizard.html", {"errors": [], "v": {}})


@router.post("/admin/onboard-tenant", response_class=HTMLResponse)
async def onboard_tenant_wizard_submit(request: Request):
    form = await request.form()
    v = _values_from_form(form)

    if v["admin_secret"] != ADMIN_SECRET:
        return templates.TemplateResponse(
            request, "onboarding_wizard.html", {"errors": ["Incorrect admin secret."], "v": v}, status_code=403,
        )

    errors: list[str] = []
    name = v["name"].strip()
    whatsapp_phone_number_id = v["whatsapp_phone_number_id"].strip()
    if not name:
        errors.append("Business name is required.")
    if not whatsapp_phone_number_id:
        errors.append("WhatsApp phone_number_id is required.")

    payment_gateway_provider = v["payment_gateway_provider"].strip()
    if payment_gateway_provider and payment_gateway_provider not in _VALID_PAYMENT_PROVIDERS:
        errors.append(f'Unrecognized payment gateway provider "{payment_gateway_provider}".')

    nudge_hours = _parse_nudge_hours(v["abandoned_cart_nudge_hours"], errors)

    mode = v["product_entry_mode"] or "manual"
    if mode == "csv":
        upload = form.get("csv_file")
        if upload is not None and getattr(upload, "filename", ""):
            content = await upload.read()
            products = _parse_csv_products(content, errors)
        else:
            products = []
    else:
        products = _parse_manual_products(form, errors)

    if errors:
        return templates.TemplateResponse(
            request, "onboarding_wizard.html", {"errors": errors, "v": v}, status_code=400,
        )

    try:
        tenant = db.create_tenant(
            name=name,
            whatsapp_phone_number_id=whatsapp_phone_number_id,
            access_token=v["access_token"].strip() or None,
            app_secret=v["app_secret"].strip() or None,
            welcome_message_text=v["welcome_message_text"].strip() or None,
            timezone=v["timezone"].strip() or "Asia/Kolkata",
            abandoned_cart_nudge_hours=nudge_hours,
            meta_catalog_id=v["meta_catalog_id"].strip() or None,
            payment_gateway_provider=payment_gateway_provider or None,
            payment_gateway_key_id=v["payment_gateway_key_id"].strip() or None,
            payment_gateway_api_key_ref=v["payment_gateway_api_key_ref"].strip() or None,
            payment_gateway_webhook_secret=v["payment_gateway_webhook_secret"].strip() or None,
        )
    except IntegrityError:
        errors.append(
            f'A tenant with WhatsApp phone_number_id "{whatsapp_phone_number_id}" already exists — '
            "each tenant must have its own phone_number_id for message routing to work correctly."
        )
        return templates.TemplateResponse(
            request, "onboarding_wizard.html", {"errors": errors, "v": v}, status_code=400,
        )

    for product in products:
        db.create_product(tenant.id, **product)

    return templates.TemplateResponse(request, "onboarding_confirmation.html", {
        "tenant": tenant,
        "tier_note": "Using this platform's own database to manage orders (Tier 1).",
        "product_count": len(products),
        "feed_url": feed_url(tenant.id),
    })
