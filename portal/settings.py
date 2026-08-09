# portal/settings.py
"""
Merchant portal settings: catalog/payment-gateway/abandoned-cart-nudge
fields (reuses db.update_tenant_catalog_and_payment directly -- the same
function admin/onboarding.py's operator-run catalog-payment edit step
already uses, just reached without ADMIN_SECRET since the merchant is
already authenticated via their own portal session) and changing the
portal login password.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db.repository as db
from admin.onboarding import _VALID_PAYMENT_PROVIDERS
from portal.session import hash_password, require_session, verify_password

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _parse_nudge_hours(raw: str, errors: list[str]) -> int | None:
    """None means "leave it as-is" (matches update_tenant_catalog_and_payment's
    COALESCE convention) -- unlike admin/onboarding_wizard.py's version, a
    blank value here is not an error, since this form's fields are all
    independently optional touch-ups, not a first-time setup."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        hours = int(raw)
        if hours <= 0:
            raise ValueError
        return hours
    except ValueError:
        errors.append("Abandoned cart nudge (hours) must be a positive whole number.")
        return None


@router.get("/portal/settings", response_class=HTMLResponse)
async def settings_form(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)
    return templates.TemplateResponse(request, "portal/settings.html", {
        "tenant": tenant, "active_nav": "settings", "errors": [], "password_errors": [], "saved": False, "password_saved": False,
    })


@router.post("/portal/settings", response_class=HTMLResponse)
async def settings_submit(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    form = await request.form()
    meta_catalog_id = (form.get("meta_catalog_id") or "").strip()
    payment_gateway_provider = (form.get("payment_gateway_provider") or "").strip()
    payment_gateway_key_id = (form.get("payment_gateway_key_id") or "").strip()
    payment_gateway_api_key_ref = (form.get("payment_gateway_api_key_ref") or "").strip()
    payment_gateway_webhook_secret = (form.get("payment_gateway_webhook_secret") or "").strip()
    abandoned_cart_nudge_hours_raw = (form.get("abandoned_cart_nudge_hours") or "").strip()

    errors: list[str] = []
    if payment_gateway_provider and payment_gateway_provider not in _VALID_PAYMENT_PROVIDERS:
        errors.append(f'Unrecognized payment gateway provider "{payment_gateway_provider}".')
    nudge_hours = _parse_nudge_hours(abandoned_cart_nudge_hours_raw, errors)

    if errors:
        tenant = db.get_tenant(tenant.id)
        return templates.TemplateResponse(request, "portal/settings.html", {
            "tenant": tenant, "active_nav": "settings", "errors": errors, "password_errors": [],
            "saved": False, "password_saved": False,
        }, status_code=400)

    db.update_tenant_catalog_and_payment(
        tenant.id,
        meta_catalog_id=meta_catalog_id or None,
        payment_gateway_provider=payment_gateway_provider or None,
        payment_gateway_key_id=payment_gateway_key_id or None,
        payment_gateway_api_key_ref=payment_gateway_api_key_ref or None,
        payment_gateway_webhook_secret=payment_gateway_webhook_secret or None,
        abandoned_cart_nudge_hours=nudge_hours,
    )

    tenant = db.get_tenant(tenant.id)
    return templates.TemplateResponse(request, "portal/settings.html", {
        "tenant": tenant, "active_nav": "settings", "errors": [], "password_errors": [], "saved": True, "password_saved": False,
    })


@router.post("/portal/settings/password", response_class=HTMLResponse)
async def settings_password_submit(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    form = await request.form()
    current_password = form.get("current_password") or ""
    new_password = form.get("new_password") or ""
    confirm_password = form.get("confirm_password") or ""

    errors: list[str] = []
    if not verify_password(current_password, tenant.portal_password_hash):
        errors.append("Current password is incorrect.")
    elif len(new_password) < 8:
        errors.append("New password must be at least 8 characters.")
    elif new_password != confirm_password:
        errors.append("New password and confirmation do not match.")

    if errors:
        return templates.TemplateResponse(request, "portal/settings.html", {
            "tenant": tenant, "active_nav": "settings", "errors": [], "password_errors": errors,
            "saved": False, "password_saved": False,
        }, status_code=400)

    db.set_tenant_portal_password_hash(tenant.id, hash_password(new_password))

    tenant = db.get_tenant(tenant.id)
    return templates.TemplateResponse(request, "portal/settings.html", {
        "tenant": tenant, "active_nav": "settings", "errors": [], "password_errors": [], "saved": False, "password_saved": True,
    })
