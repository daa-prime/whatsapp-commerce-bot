# admin/onboarding.py
"""
Catalog/payment-gateway edit step (SPEC.md Section 7, Phase 7) -- lets an
operator add or update a tenant's catalog/payment/abandoned-cart-nudge
settings after initial creation, without re-running the whole onboarding
flow. Submitting this form with a field left blank means "keep the current
value" (db.update_tenant_catalog_and_payment's COALESCE semantics), not
"unset" -- unlike tenant *creation*, there's something to preserve here.

Tenant *creation* itself now lives in admin/onboarding_wizard.py (the guided
step-rail wizard, Jinja2-based) -- this module used to also own the flat
create form at the same /admin/onboard-tenant URL; that's been replaced, not
duplicated. ADMIN_SECRET and _VALID_PAYMENT_PROVIDERS here are the shared
copies admin/onboarding_wizard.py imports rather than redefining.

Protected by a shared secret (ADMIN_SECRET env var), same pattern as
core/main.py's INTERNAL_SECRET — basic protection against an unauthenticated
stranger editing tenant rows (and storing real API credentials) on a
deployed instance, not a full auth system.
"""
import os

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

import db.repository as db
from catalog.feed import feed_url
from portal.session import hash_password

router = APIRouter()

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

_VALID_PAYMENT_PROVIDERS = {"razorpay"}  # blank ("not set up yet" / "keep current") is always allowed separately

_PAGE_STYLE = """
<style>
  body { font-family: sans-serif; max-width: 680px; margin: 2rem auto; padding: 0 1rem; color: #222; }
  label { display: block; margin-top: 1rem; font-weight: bold; }
  input[type=text], input[type=password], textarea { width: 100%; padding: 0.4rem; margin-top: 0.25rem; box-sizing: border-box; font-family: inherit; }
  textarea { height: 6rem; font-family: monospace; }
  button { margin-top: 1.5rem; padding: 0.6rem 1.2rem; font-size: 1rem; }
  .hint { color: #666; font-size: 0.85rem; margin-top: 0.15rem; }
  .error { background: #fdecea; border: 1px solid #f5c2c0; color: #7a1f1a; padding: 0.75rem; border-radius: 4px; }
  .ok { background: #e9f8ee; border: 1px solid #b7e4c7; color: #1e5c34; padding: 0.75rem; border-radius: 4px; }
  fieldset { border: 1px solid #dde3ea; border-radius: 4px; margin-top: 1rem; padding: 0.75rem 1rem; }
  legend { font-weight: bold; padding: 0 0.3rem; }
</style>
"""


# --- Catalog / payment gateway edit step (SPEC.md Section 7, Phase 7) ---
# A tenant frequently won't have its catalog/payment gateway set up at
# initial onboarding time, so this lets an operator add or update those
# fields later. Blank fields here mean "keep the current value"
# (db.update_tenant_catalog_and_payment's COALESCE semantics), not "unset".

def _catalog_payment_form_html(tenant, errors: list[str] | None = None, values: dict | None = None) -> str:
    import html
    v = values or {}

    def esc(key: str) -> str:
        return html.escape(str(v.get(key, "")))

    def selected(key: str, option: str) -> str:
        return "selected" if v.get(key, "") == option else ""

    error_html = ""
    if errors:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
        error_html = f'<div class="error"><strong>Please fix the following:</strong><ul>{items}</ul></div>'

    catalog_current = html.escape(tenant.meta_catalog_id) if tenant.meta_catalog_id else "not set"
    provider_current = html.escape(tenant.payment_gateway_provider) if tenant.payment_gateway_provider else "not set"
    key_id_current = html.escape(tenant.payment_gateway_key_id) if tenant.payment_gateway_key_id else "not set"
    key_secret_current = "set" if tenant.payment_gateway_api_key_ref else "not set"
    webhook_secret_current = "set" if tenant.payment_gateway_webhook_secret else "not set"

    return f"""<!doctype html>
<html>
<head><title>Catalog &amp; payment — {html.escape(tenant.name)}</title>{_PAGE_STYLE}</head>
<body>
  <h1>Catalog &amp; payment gateway — {html.escape(tenant.name)}</h1>
  {error_html}
  <form method="post" action="/admin/tenant/{tenant.id}/catalog-payment">
    <label>Admin secret</label>
    <input type="password" name="admin_secret" value="{esc('admin_secret')}" required>

    <label>Meta Catalog ID</label>
    <input type="text" name="meta_catalog_id" value="{esc('meta_catalog_id')}" placeholder="Currently: {catalog_current}">
    <p class="hint">Currently: {catalog_current}. Leave blank to keep it as-is —
      leave blank if you haven't set this up yet — you can add it later.</p>
    <p class="hint">Product feed URL to register in Commerce Manager (Catalog &rarr; Data Sources &rarr;
      Add Items &rarr; Data Feed): <strong>{html.escape(feed_url(tenant.id) or "not available yet — PUBLIC_BASE_URL isn't configured")}</strong></p>

    <label>Payment gateway provider</label>
    <select name="payment_gateway_provider">
      <option value="" {selected('payment_gateway_provider', '')}>Keep current ({provider_current})</option>
      <option value="razorpay" {selected('payment_gateway_provider', 'razorpay')}>Razorpay</option>
    </select>

    <label>Razorpay Key ID</label>
    <input type="text" name="payment_gateway_key_id" value="{esc('payment_gateway_key_id')}" placeholder="Currently: {key_id_current}">
    <p class="hint">Currently: {key_id_current}. Leave blank to keep it as-is.</p>

    <label>Razorpay Key Secret</label>
    <input type="password" name="payment_gateway_api_key_ref" value="{esc('payment_gateway_api_key_ref')}">
    <p class="hint">Currently: {key_secret_current}. Leave blank to keep the current key —
      leave blank if you haven't set this up yet — you can add it later.</p>

    <label>Razorpay Webhook Secret</label>
    <input type="password" name="payment_gateway_webhook_secret" value="{esc('payment_gateway_webhook_secret')}">
    <p class="hint">Currently: {webhook_secret_current}. Leave blank to keep the current value —
      leave blank if you haven't set this up yet — you can add it later.</p>

    <label>Abandoned cart nudge (hours)</label>
    <input type="text" name="abandoned_cart_nudge_hours" value="{esc('abandoned_cart_nudge_hours')}"
           placeholder="Currently: {tenant.abandoned_cart_nudge_hours}">
    <p class="hint">Currently: {tenant.abandoned_cart_nudge_hours} hour{'s' if tenant.abandoned_cart_nudge_hours != 1 else ''}.
      Leave blank to keep it as-is.</p>

    <button type="submit">Save</button>
  </form>
</body>
</html>"""


def _catalog_payment_confirmation_html(tenant) -> str:
    import html
    return f"""<!doctype html>
<html>
<head><title>Catalog &amp; payment updated</title>{_PAGE_STYLE}</head>
<body>
  <h1>Catalog &amp; payment gateway updated</h1>
  <div class="ok"><strong>{html.escape(tenant.name)}</strong>'s catalog/payment settings were updated.</div>
  <p>
    Meta Catalog ID: {html.escape(tenant.meta_catalog_id) if tenant.meta_catalog_id else "<em>not set</em>"}<br>
    Payment gateway provider: {html.escape(tenant.payment_gateway_provider) if tenant.payment_gateway_provider else "<em>not set</em>"}<br>
    Razorpay Key ID: {html.escape(tenant.payment_gateway_key_id) if tenant.payment_gateway_key_id else "<em>not set</em>"}<br>
    Razorpay Key Secret: {"<em>set</em>" if tenant.payment_gateway_api_key_ref else "<em>not set</em>"}<br>
    Razorpay Webhook Secret: {"<em>set</em>" if tenant.payment_gateway_webhook_secret else "<em>not set</em>"}<br>
    Abandoned cart nudge: {tenant.abandoned_cart_nudge_hours} hour{'s' if tenant.abandoned_cart_nudge_hours != 1 else ''}
  </p>
  <p><a href="/admin/tenant/{tenant.id}/catalog-payment">Edit again</a></p>
</body>
</html>"""


@router.get("/admin/tenant/{tenant_id}/catalog-payment", response_class=HTMLResponse)
async def edit_tenant_catalog_payment_form(tenant_id: int):
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return HTMLResponse("<p>Tenant not found.</p>", status_code=404)
    return _catalog_payment_form_html(tenant)


@router.post("/admin/tenant/{tenant_id}/catalog-payment", response_class=HTMLResponse)
async def edit_tenant_catalog_payment_submit(
    tenant_id: int,
    admin_secret: str = Form(""),
    meta_catalog_id: str = Form(""),
    payment_gateway_provider: str = Form(""),
    payment_gateway_key_id: str = Form(""),
    payment_gateway_api_key_ref: str = Form(""),
    payment_gateway_webhook_secret: str = Form(""),
    abandoned_cart_nudge_hours: str = Form(""),
):
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return HTMLResponse("<p>Tenant not found.</p>", status_code=404)

    values = {
        "admin_secret": admin_secret,
        "meta_catalog_id": meta_catalog_id,
        "payment_gateway_provider": payment_gateway_provider,
        "payment_gateway_key_id": payment_gateway_key_id,
        "payment_gateway_api_key_ref": payment_gateway_api_key_ref,
        "payment_gateway_webhook_secret": payment_gateway_webhook_secret,
        "abandoned_cart_nudge_hours": abandoned_cart_nudge_hours,
    }

    if admin_secret != ADMIN_SECRET:
        return HTMLResponse(_catalog_payment_form_html(tenant, ["Incorrect admin secret."], values), status_code=403)

    if payment_gateway_provider and payment_gateway_provider not in _VALID_PAYMENT_PROVIDERS:
        return HTMLResponse(
            _catalog_payment_form_html(
                tenant, [f'Unrecognized payment gateway provider "{payment_gateway_provider}".'], values,
            ),
            status_code=400,
        )

    nudge_hours = None
    if abandoned_cart_nudge_hours.strip():
        try:
            nudge_hours = int(abandoned_cart_nudge_hours.strip())
            if nudge_hours <= 0:
                raise ValueError
        except ValueError:
            return HTMLResponse(
                _catalog_payment_form_html(
                    tenant, ['Abandoned cart nudge (hours) must be a positive whole number.'], values,
                ),
                status_code=400,
            )

    db.update_tenant_catalog_and_payment(
        tenant_id,
        meta_catalog_id=meta_catalog_id.strip() or None,
        payment_gateway_provider=payment_gateway_provider.strip() or None,
        payment_gateway_key_id=payment_gateway_key_id.strip() or None,
        payment_gateway_api_key_ref=payment_gateway_api_key_ref.strip() or None,
        payment_gateway_webhook_secret=payment_gateway_webhook_secret.strip() or None,
        abandoned_cart_nudge_hours=nudge_hours,
    )

    return _catalog_payment_confirmation_html(db.get_tenant(tenant_id))


# --- Merchant portal login password (set/reset only -- no self-serve signup) ---
# The merchant portal (portal/*.py) needs a password to log in with; there's
# no self-serve signup flow, so an admin sets/resets it here, same
# ADMIN_SECRET-gated pattern as the catalog/payment edit step above.

def _portal_password_form_html(tenant, errors: list[str] | None = None, values: dict | None = None) -> str:
    import html
    v = values or {}

    def esc(key: str) -> str:
        return html.escape(str(v.get(key, "")))

    error_html = ""
    if errors:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
        error_html = f'<div class="error"><strong>Please fix the following:</strong><ul>{items}</ul></div>'

    password_current = "set" if tenant.portal_password_hash else "not set"

    return f"""<!doctype html>
<html>
<head><title>Portal password — {html.escape(tenant.name)}</title>{_PAGE_STYLE}</head>
<body>
  <h1>Merchant portal password — {html.escape(tenant.name)}</h1>
  {error_html}
  <form method="post" action="/admin/tenant/{tenant.id}/portal-password">
    <label>Admin secret</label>
    <input type="password" name="admin_secret" value="{esc('admin_secret')}" required>

    <label>New password</label>
    <input type="password" name="new_password" required>
    <p class="hint">Currently: {password_current}. Must be at least 8 characters. The merchant logs in at
      /portal/login with their WhatsApp phone number ID and this password.</p>

    <label>Confirm new password</label>
    <input type="password" name="confirm_password" required>

    <button type="submit">Save</button>
  </form>
</body>
</html>"""


def _portal_password_confirmation_html(tenant) -> str:
    import html
    return f"""<!doctype html>
<html>
<head><title>Portal password updated</title>{_PAGE_STYLE}</head>
<body>
  <h1>Merchant portal password updated</h1>
  <div class="ok"><strong>{html.escape(tenant.name)}</strong>'s portal login password was set.
    They can now log in at /portal/login with phone number ID <strong>{html.escape(tenant.whatsapp_phone_number_id or "")}</strong>.</div>
  <p><a href="/admin/tenant/{tenant.id}/portal-password">Reset again</a></p>
</body>
</html>"""


@router.get("/admin/tenant/{tenant_id}/portal-password", response_class=HTMLResponse)
async def portal_password_form(tenant_id: int):
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return HTMLResponse("<p>Tenant not found.</p>", status_code=404)
    return _portal_password_form_html(tenant)


@router.post("/admin/tenant/{tenant_id}/portal-password", response_class=HTMLResponse)
async def portal_password_submit(
    tenant_id: int,
    admin_secret: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return HTMLResponse("<p>Tenant not found.</p>", status_code=404)

    values = {"admin_secret": admin_secret}

    if admin_secret != ADMIN_SECRET:
        return HTMLResponse(_portal_password_form_html(tenant, ["Incorrect admin secret."], values), status_code=403)

    if len(new_password) < 8:
        return HTMLResponse(
            _portal_password_form_html(tenant, ["Password must be at least 8 characters."], values), status_code=400,
        )

    if new_password != confirm_password:
        return HTMLResponse(
            _portal_password_form_html(tenant, ["Password and confirmation do not match."], values), status_code=400,
        )

    db.set_tenant_portal_password_hash(tenant_id, hash_password(new_password))

    return _portal_password_confirmation_html(db.get_tenant(tenant_id))
