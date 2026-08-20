# portal/coupons.py
"""
Merchant portal coupon management: list every coupon (active + inactive),
create a new one, and deactivate/reactivate an existing one. Same shape as
portal/products.py's list/new/toggle-active routes -- coupons are never
deleted, only toggled (see db.set_coupon_active's docstring for why: a past
order's orders.coupon_code needs to keep meaning something regardless of
whether the coupon is still usable for new checkouts).
"""
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db.repository as db
from db.connection import IntegrityError
from portal.session import require_session

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _coupon_form_values(form) -> dict:
    return {
        "code": (form.get("code") or "").strip(),
        "discount_type": (form.get("discount_type") or "").strip(),
        "discount_value": (form.get("discount_value") or "").strip(),
        "expires_at": (form.get("expires_at") or "").strip(),
    }


def _validate_coupon_form(v: dict, errors: list[str]) -> Decimal | None:
    if not v["code"]:
        errors.append("Coupon code is required.")

    if v["discount_type"] not in (db.COUPON_TYPE_PERCENTAGE, db.COUPON_TYPE_FLAT):
        errors.append("Discount type must be percentage or flat.")

    discount_value = None
    try:
        discount_value = Decimal(v["discount_value"]) if v["discount_value"] else None
        if discount_value is None or discount_value <= 0:
            raise InvalidOperation
        if v["discount_type"] == db.COUPON_TYPE_PERCENTAGE and discount_value > 100:
            errors.append("A percentage discount can't exceed 100.")
    except InvalidOperation:
        errors.append(f'Discount value "{v["discount_value"]}" is not a valid positive number.')

    return discount_value


@router.get("/portal/coupons", response_class=HTMLResponse)
async def coupons_list(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    coupons = db.list_coupons(tenant.id)
    return templates.TemplateResponse(request, "portal/coupons_list.html", {
        "tenant": tenant, "active_nav": "coupons", "coupons": coupons,
    })


@router.get("/portal/coupons/new", response_class=HTMLResponse)
async def coupon_new_form(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)
    return templates.TemplateResponse(request, "portal/coupon_form.html", {
        "tenant": tenant, "active_nav": "coupons", "errors": [], "v": {},
    })


@router.post("/portal/coupons/new", response_class=HTMLResponse)
async def coupon_new_submit(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    form = await request.form()
    v = _coupon_form_values(form)
    errors: list[str] = []
    discount_value = _validate_coupon_form(v, errors)

    if errors:
        return templates.TemplateResponse(request, "portal/coupon_form.html", {
            "tenant": tenant, "active_nav": "coupons", "errors": errors, "v": v,
        }, status_code=400)

    try:
        db.create_coupon(
            tenant.id, code=v["code"], discount_type=v["discount_type"],
            discount_value=discount_value, expires_at=v["expires_at"] or None,
        )
    except IntegrityError:
        # UNIQUE(tenant_id, code) -- same duplicate-key pattern
        # admin/onboarding.py's tenant creation already handles.
        errors.append(f'A coupon with code "{v["code"].upper()}" already exists.')
        return templates.TemplateResponse(request, "portal/coupon_form.html", {
            "tenant": tenant, "active_nav": "coupons", "errors": errors, "v": v,
        }, status_code=400)

    return RedirectResponse(url="/portal/coupons", status_code=303)


@router.post("/portal/coupons/{coupon_id}/toggle-active")
async def coupon_toggle_active(request: Request, coupon_id: int):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    coupon = db.get_coupon(tenant.id, coupon_id)
    if coupon is None:
        return HTMLResponse("<p>Coupon not found.</p>", status_code=404)

    db.set_coupon_active(tenant.id, coupon_id, not coupon.is_active)
    return RedirectResponse(url="/portal/coupons", status_code=303)
