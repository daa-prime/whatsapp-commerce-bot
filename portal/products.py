# portal/products.py
"""
Merchant portal product management: list all products (active + inactive),
add, edit, toggle active/inactive, and bulk CSV import.

CSV import reuses admin.onboarding_wizard._parse_csv_products directly
(same columns: name, price, description, image_url, sku, stock_quantity,
category; same per-row error handling) rather than re-implementing it --
the wizard's version isn't tenant-aware itself (it returns parsed dicts,
the caller creates the products), so it's a clean fit here too.
"""
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db.repository as db
from admin.onboarding_wizard import _parse_csv_products
from portal.session import require_session

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _product_form_values(form) -> dict:
    return {
        "name": (form.get("name") or "").strip(),
        "price": (form.get("price") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "image_url": (form.get("image_url") or "").strip(),
        "sku": (form.get("sku") or "").strip(),
        "stock_quantity": (form.get("stock_quantity") or "").strip(),
        "category": (form.get("category") or "").strip(),
    }


def _validate_product_form(v: dict, errors: list[str]) -> tuple[Decimal | None, int]:
    if not v["name"]:
        errors.append("Product name is required.")

    price = None
    try:
        price = Decimal(v["price"]) if v["price"] else None
        if price is None or price < 0:
            raise InvalidOperation
    except InvalidOperation:
        errors.append(f'Price "{v["price"]}" is not a valid number.')

    stock_quantity = 0
    if v["stock_quantity"]:
        try:
            stock_quantity = int(v["stock_quantity"])
        except ValueError:
            errors.append(f'Stock quantity "{v["stock_quantity"]}" is not a whole number.')

    return price, stock_quantity


@router.get("/portal/products", response_class=HTMLResponse)
async def products_list(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    products = db.get_all_products(tenant.id)
    return templates.TemplateResponse(request, "portal/products_list.html", {
        "tenant": tenant, "active_nav": "products", "products": products,
    })


@router.get("/portal/products/new", response_class=HTMLResponse)
async def product_new_form(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)
    return templates.TemplateResponse(request, "portal/product_form.html", {
        "tenant": tenant, "active_nav": "products", "errors": [], "v": {}, "product": None,
    })


@router.post("/portal/products/new", response_class=HTMLResponse)
async def product_new_submit(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    form = await request.form()
    v = _product_form_values(form)
    errors: list[str] = []
    price, stock_quantity = _validate_product_form(v, errors)

    if errors:
        return templates.TemplateResponse(request, "portal/product_form.html", {
            "tenant": tenant, "active_nav": "products", "errors": errors, "v": v, "product": None,
        }, status_code=400)

    db.create_product(
        tenant.id, name=v["name"], price=price, description=v["description"] or None,
        image_url=v["image_url"] or None, sku=v["sku"] or None, stock_quantity=stock_quantity,
        category=v["category"] or None,
    )
    return RedirectResponse(url="/portal/products", status_code=303)


@router.get("/portal/products/{product_id}/edit", response_class=HTMLResponse)
async def product_edit_form(request: Request, product_id: int):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    product = db.get_product(tenant.id, product_id)
    if product is None:
        return HTMLResponse("<p>Product not found.</p>", status_code=404)

    v = {
        "name": product.name, "price": str(product.price), "description": product.description or "",
        "image_url": product.image_url or "", "sku": product.sku or "",
        "stock_quantity": str(product.stock_quantity), "category": product.category or "",
    }
    return templates.TemplateResponse(request, "portal/product_form.html", {
        "tenant": tenant, "active_nav": "products", "errors": [], "v": v, "product": product,
    })


@router.post("/portal/products/{product_id}/edit", response_class=HTMLResponse)
async def product_edit_submit(request: Request, product_id: int):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    product = db.get_product(tenant.id, product_id)
    if product is None:
        return HTMLResponse("<p>Product not found.</p>", status_code=404)

    form = await request.form()
    v = _product_form_values(form)
    errors: list[str] = []
    price, stock_quantity = _validate_product_form(v, errors)

    if errors:
        return templates.TemplateResponse(request, "portal/product_form.html", {
            "tenant": tenant, "active_nav": "products", "errors": errors, "v": v, "product": product,
        }, status_code=400)

    db.update_product(
        tenant.id, product_id, name=v["name"], price=price, description=v["description"] or None,
        image_url=v["image_url"] or None, sku=v["sku"] or None, stock_quantity=stock_quantity,
        category=v["category"] or None,
    )
    return RedirectResponse(url="/portal/products", status_code=303)


@router.post("/portal/products/{product_id}/toggle-active")
async def product_toggle_active(request: Request, product_id: int):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    product = db.get_product(tenant.id, product_id)
    if product is None:
        return HTMLResponse("<p>Product not found.</p>", status_code=404)

    db.set_product_active(tenant.id, product_id, not product.is_active)
    return RedirectResponse(url="/portal/products", status_code=303)


@router.get("/portal/products/import", response_class=HTMLResponse)
async def products_import_form(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)
    return templates.TemplateResponse(request, "portal/products_import.html", {
        "tenant": tenant, "active_nav": "products", "errors": [], "imported_count": None,
    })


@router.post("/portal/products/import", response_class=HTMLResponse)
async def products_import_submit(request: Request):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    form = await request.form()
    errors: list[str] = []
    upload = form.get("csv_file")
    if upload is None or not getattr(upload, "filename", ""):
        errors.append("Please choose a CSV file to upload.")
        products = []
    else:
        content = await upload.read()
        products = _parse_csv_products(content, errors)

    if errors:
        return templates.TemplateResponse(request, "portal/products_import.html", {
            "tenant": tenant, "active_nav": "products", "errors": errors, "imported_count": None,
        }, status_code=400)

    for product in products:
        db.create_product(tenant.id, **product)

    return templates.TemplateResponse(request, "portal/products_import.html", {
        "tenant": tenant, "active_nav": "products", "errors": [], "imported_count": len(products),
    })
