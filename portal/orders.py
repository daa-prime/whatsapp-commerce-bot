# portal/orders.py
"""
Merchant portal order management: list all orders for the logged-in tenant
(optionally filtered by status), view one order's detail (items, customer,
payment link), and mark a paid order fulfilled once shipped/delivered.

Reuses db.repository's existing Order/OrderItem/Product reads and the
ORDER_STATUS_* constants directly -- the only new repository functions this
needed (list_orders, mark_order_fulfilled) were added there, not here, to
keep "no raw SQL outside db/repository.py" intact.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db.repository as db
from core.commerce_flow import handle_order_fulfilled
from core.whatsapp import WhatsAppClient
from portal.session import require_session

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_STATUS_FILTERS = [
    ("", "All"),
    (db.ORDER_STATUS_PENDING_PAYMENT, "Pending payment"),
    (db.ORDER_STATUS_PAID, "Paid"),
    (db.ORDER_STATUS_FULFILLED, "Fulfilled"),
    (db.ORDER_STATUS_FAILED, "Failed"),
    (db.ORDER_STATUS_CANCELLED, "Cancelled"),
]


@router.get("/portal/orders", response_class=HTMLResponse)
async def orders_list(request: Request, status: str = ""):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    orders = db.list_orders(tenant.id, status=status or None)
    customer_names = db.get_customer_names(tenant.id, [o.customer_phone for o in orders])

    return templates.TemplateResponse(request, "portal/orders_list.html", {
        "tenant": tenant,
        "active_nav": "orders",
        "orders": orders,
        "customer_names": customer_names,
        "status_filters": _STATUS_FILTERS,
        "current_status": status,
    })


@router.get("/portal/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    order = db.get_order(tenant.id, order_id)
    if order is None:
        return HTMLResponse("<p>Order not found.</p>", status_code=404)

    items = db.get_order_items(tenant.id, order_id)
    line_items = []
    for item in items:
        product = db.get_product(tenant.id, item.product_id)
        line_items.append({
            "product_name": product.name if product else f"Product #{item.product_id}",
            "quantity": item.quantity,
            "unit_price": item.unit_price_at_order_time,
            "line_total": item.unit_price_at_order_time * item.quantity,
        })

    customer = db.get_customer(tenant.id, order.customer_phone)

    return templates.TemplateResponse(request, "portal/order_detail.html", {
        "tenant": tenant,
        "active_nav": "orders",
        "order": order,
        "line_items": line_items,
        "customer_name": customer.name if customer else None,
        "can_fulfill": order.status == db.ORDER_STATUS_PAID,
    })


@router.post("/portal/orders/{order_id}/fulfill")
async def order_fulfill(request: Request, order_id: int):
    tenant = require_session(request)
    if tenant is None:
        return RedirectResponse(url="/portal/login", status_code=303)

    order = db.get_order(tenant.id, order_id)
    if order is None:
        return HTMLResponse("<p>Order not found.</p>", status_code=404)

    transitioned = db.mark_order_fulfilled(tenant.id, order_id)
    if transitioned:
        # Built per-request rather than cached (unlike core/main.py's
        # _wa_clients) -- fulfillment is a low-frequency merchant click, not
        # the per-message hot path that cache exists for, and caching here
        # would mean either duplicating that cache or importing core.main
        # into portal/orders.py, which core.main already imports (circular).
        wa = WhatsAppClient(phone_number_id=tenant.whatsapp_phone_number_id, access_token=tenant.access_token)
        await handle_order_fulfilled(wa, order)
    return RedirectResponse(url=f"/portal/orders/{order_id}", status_code=303)
