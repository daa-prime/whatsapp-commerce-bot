# portal/auth.py
"""
Merchant portal login/logout (GET/POST /portal/login, POST /portal/logout).
Merchant self-serve *signup* is explicitly out of scope -- a tenant's portal
password is only ever set/reset by an admin (admin/onboarding.py's
portal-password page); this module only verifies an existing one.

Logs in by whatsapp_phone_number_id + password rather than an email/username
(no such field exists on tenants) -- phone_number_id is already the unique,
merchant-known identifier collected at onboarding.
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db.repository as db
from portal.session import clear_session_cookie, set_session_cookie, verify_password

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/portal/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "portal/login.html", {"error": None, "whatsapp_phone_number_id": ""})


@router.post("/portal/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    whatsapp_phone_number_id = (form.get("whatsapp_phone_number_id") or "").strip()
    password = form.get("password") or ""

    tenant = db.find_tenant_by_phone_number_id(whatsapp_phone_number_id) if whatsapp_phone_number_id else None
    # Same generic error either way (unknown number vs. wrong password) -- don't
    # let a login attempt reveal whether a given phone_number_id is even registered.
    if tenant is None or not verify_password(password, tenant.portal_password_hash):
        return templates.TemplateResponse(
            request, "portal/login.html",
            {"error": "Incorrect phone number ID or password.", "whatsapp_phone_number_id": whatsapp_phone_number_id},
            status_code=401,
        )

    response = RedirectResponse(url="/portal/dashboard", status_code=303)
    set_session_cookie(response, tenant.id)
    return response


@router.post("/portal/logout")
async def logout():
    response = RedirectResponse(url="/portal/login", status_code=303)
    clear_session_cookie(response)
    return response
