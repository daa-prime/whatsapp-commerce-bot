# admin/onboarding.py
"""
Guided onboarding wizard — a plain server-rendered form that lets a new tenant
be added without touching the database or code directly. Deliberately no
Jinja2/JS framework: "minimal maintenance" is a core priority (SPEC.md
Section 1), and this is v1 of the guided wizard, not a later Embedded Signup
flow — the operator still enters the tenant's own Meta credentials by hand
here. The static instructional copy below the page title walks through Meta's
own account/app/verification/token setup so the operator isn't left to go
find that information themselves.

Protected by a shared secret (ADMIN_SECRET env var), same pattern as
core/main.py's INTERNAL_SECRET — basic protection against an unauthenticated
stranger creating tenant rows (and storing real API credentials) on a
deployed instance, not a full auth system.

Phase 0 (SPEC.md Section 8): this only carries over the tenant-identity/Meta-
credential/data-connection-tier fields from the hospital repo's wizard — the
hospital-specific "departments and doctors" step is gone.

Phase 7 (SPEC.md Section 7): the create form now also collects catalog/
payment-gateway fields (meta_catalog_id, payment_gateway_provider/
api_key_ref) as optional fields, since a tenant frequently won't have these
set up yet at initial onboarding. A separate /admin/tenant/{id}/catalog-payment
edit step lets an operator add or update them later without re-running the
whole onboarding form -- submitting that edit form with a field left blank
never overwrites the existing value (db.update_tenant_catalog_and_payment's
COALESCE semantics), unlike the create form where blank simply means "don't
set" (there's nothing to preserve yet on a brand-new tenant). No payment
gateway *integration* logic reads these fields yet -- Phase 1 (catalog) and
Phase 3 (payment) are what will eventually act on them.
"""
import os

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

import db.repository as db
from db.connection import IntegrityError

router = APIRouter()

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")

_VALID_TIERS = {"tier1", "tier2", "tier3"}
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
  .steps { background: #f5f7fa; border: 1px solid #dde3ea; border-radius: 4px; padding: 0.75rem 1rem; }
  .steps ol { margin: 0.5rem 0 0 1.1rem; padding: 0; }
  .steps li { margin-bottom: 0.6rem; }
  .radio-row { font-weight: normal; display: flex; align-items: baseline; gap: 0.4rem; margin-top: 0.5rem; }
  .radio-row input { width: auto; margin: 0; }
  fieldset { border: 1px solid #dde3ea; border-radius: 4px; margin-top: 1rem; padding: 0.75rem 1rem; }
  legend { font-weight: bold; padding: 0 0.3rem; }
</style>
"""

_META_SETUP_STEPS = """
<div class="steps">
  <strong>Before you fill in the credentials below</strong> — Meta requires these
  steps to exist for the business's own WhatsApp number. Do these first if they
  aren't done yet:
  <ol>
    <li><strong>Create a Meta Business Account</strong> at
      <a href="https://business.facebook.com" target="_blank" rel="noopener">business.facebook.com</a> —
      sign in and create a Business Account for the business.</li>
    <li><strong>Set up WhatsApp on a Meta app</strong> at
      <a href="https://developers.facebook.com" target="_blank" rel="noopener">developers.facebook.com</a> —
      create an app, then add the WhatsApp product to it.</li>
    <li><strong>Business verification + production number + payment method</strong> —
      verify the real business, register their production WhatsApp number,
      and add a payment method (it's free to add — you're only charged per message later).</li>
    <li><strong>Generate a permanent token</strong> — in Business Settings → Users →
      System Users, generate a <strong>System User access token</strong>. Do not use
      the default temporary token from the API Setup page — it expires roughly every
      24 hours, while a System User token doesn't.</li>
  </ol>
</div>
"""


def _form_html(errors: list[str] | None = None, values: dict | None = None) -> str:
    import html
    v = values or {}

    def esc(key: str) -> str:
        return html.escape(str(v.get(key, "")))

    def checked(key: str, option: str, default: str) -> str:
        return "checked" if v.get(key, default) == option else ""

    def selected(key: str, option: str, default: str) -> str:
        return "selected" if v.get(key, default) == option else ""

    error_html = ""
    if errors:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
        error_html = f'<div class="error"><strong>Please fix the following:</strong><ul>{items}</ul></div>'

    return f"""<!doctype html>
<html>
<head><title>Onboard a new tenant</title>{_PAGE_STYLE}</head>
<body>
  <h1>Onboard a new tenant</h1>
  {_META_SETUP_STEPS}
  {error_html}
  <form method="post" action="/admin/onboard-tenant">
    <label>Admin secret</label>
    <input type="password" name="admin_secret" value="{esc('admin_secret')}" required>

    <label>Business name</label>
    <input type="text" name="name" value="{esc('name')}" required>

    <label>WhatsApp phone_number_id</label>
    <input type="text" name="whatsapp_phone_number_id" value="{esc('whatsapp_phone_number_id')}" required>

    <label>Access token (the System User token from the setup steps above)</label>
    <input type="text" name="access_token" value="{esc('access_token')}">

    <label>App secret</label>
    <input type="text" name="app_secret" value="{esc('app_secret')}">

    <label>Welcome message text</label>
    <textarea name="welcome_message_text">{esc('welcome_message_text')}</textarea>

    <label>Timezone (IANA name, e.g. Asia/Kolkata)</label>
    <input type="text" name="timezone" value="{esc('timezone')}" placeholder="Asia/Kolkata">
    <p class="hint">
      Used for displaying order/invoice timestamps in the business's local time.
      Leave blank to default to Asia/Kolkata.
    </p>

    <fieldset>
      <legend>Data connection</legend>
      <label class="radio-row"><input type="radio" name="data_tier" value="tier1" {checked('data_tier', 'tier1', 'tier1')}>
        Use this platform to manage my orders (default — no further setup needed)</label>
      <label class="radio-row"><input type="radio" name="data_tier" value="tier2" {checked('data_tier', 'tier2', 'tier1')}>
        Connect my existing system's API</label>
      <label class="radio-row"><input type="radio" name="data_tier" value="tier3" {checked('data_tier', 'tier3', 'tier1')}>
        Connect my database directly</label>

      <label>API base URL (only if "Connect my existing system's API" above)</label>
      <input type="text" name="api_base_url" value="{esc('api_base_url')}">
      <label>API key (only if "Connect my existing system's API" above)</label>
      <input type="text" name="api_key" value="{esc('api_key')}">
      <p class="hint">
        "Connect my database directly" is not self-serve — it requires a secure/VPN-reachable
        connection and a scoped-down database user, set up as a manually-assisted engagement.
        No fields to fill in here for that option; contact us to arrange it.
      </p>
    </fieldset>

    <fieldset>
      <legend>Catalog &amp; payment gateway (optional)</legend>
      <label>Meta Catalog ID</label>
      <input type="text" name="meta_catalog_id" value="{esc('meta_catalog_id')}">
      <p class="hint">From Meta Commerce Manager, once the catalog is linked.
        Leave blank if you haven't set this up yet — you can add it later.</p>

      <label>Payment gateway provider</label>
      <select name="payment_gateway_provider">
        <option value="" {selected('payment_gateway_provider', '', 'razorpay')}>Not set up yet</option>
        <option value="razorpay" {selected('payment_gateway_provider', 'razorpay', 'razorpay')}>Razorpay</option>
      </select>

      <label>Payment gateway API key</label>
      <input type="password" name="payment_gateway_api_key_ref" value="{esc('payment_gateway_api_key_ref')}">
      <p class="hint">Leave blank if you haven't set this up yet — you can add it later.
        No payment integration runs against this yet; it's just stored for when it does.</p>
    </fieldset>

    <button type="submit">Create tenant</button>
  </form>
</body>
</html>"""


def _confirmation_html(tenant) -> str:
    import html
    tier_note = {
        "tier1": "Using this platform's own database to manage orders (Tier 1).",
        "tier2": "Connected to the business's existing API (Tier 2) — the base URL/key were "
                 "stored, but no connector logic runs against them yet.",
        "tier3": "Flagged for direct database connection (Tier 3) — this is a manually-assisted "
                 "engagement, not self-serve; we'll be in touch to arrange secure access.",
    }[tenant.data_tier]
    return f"""<!doctype html>
<html>
<head><title>Tenant onboarded</title>{_PAGE_STYLE}</head>
<body>
  <h1>Tenant created</h1>
  <div class="ok">
    <strong>{html.escape(tenant.name)}</strong> was created with tenant ID
    <strong>{tenant.id}</strong> (phone_number_id: {html.escape(tenant.whatsapp_phone_number_id)}).
  </div>
  <p>{html.escape(tier_note)}</p>
  <p>
    Meta Catalog ID: {html.escape(tenant.meta_catalog_id) if tenant.meta_catalog_id else "<em>not set yet</em>"}<br>
    Payment gateway: {html.escape(tenant.payment_gateway_provider) if tenant.payment_gateway_provider else "<em>not set yet</em>"}
  </p>
  <p class="hint">
    Reminder: this only recorded the credentials you entered — the business's own
    Meta Business/WhatsApp number verification and System User access token must
    already have been set up on Meta's side beforehand. If that wasn't done first,
    outbound messages and webhook signature validation for this tenant will fail
    until it is.
  </p>
  <p>
    <a href="/admin/onboard-tenant">Onboard another tenant</a> ·
    <a href="/admin/tenant/{tenant.id}/catalog-payment">Add catalog/payment details later</a>
  </p>
</body>
</html>"""


@router.get("/admin/onboard-tenant", response_class=HTMLResponse)
async def onboard_tenant_form():
    return _form_html()


@router.post("/admin/onboard-tenant", response_class=HTMLResponse)
async def onboard_tenant_submit(
    admin_secret: str = Form(""),
    name: str = Form(""),
    whatsapp_phone_number_id: str = Form(""),
    access_token: str = Form(""),
    app_secret: str = Form(""),
    welcome_message_text: str = Form(""),
    timezone: str = Form(""),
    data_tier: str = Form("tier1"),
    api_base_url: str = Form(""),
    api_key: str = Form(""),
    meta_catalog_id: str = Form(""),
    payment_gateway_provider: str = Form(""),
    payment_gateway_api_key_ref: str = Form(""),
):
    values = {
        "admin_secret": admin_secret,
        "name": name,
        "whatsapp_phone_number_id": whatsapp_phone_number_id,
        "access_token": access_token,
        "app_secret": app_secret,
        "welcome_message_text": welcome_message_text,
        "timezone": timezone,
        "data_tier": data_tier,
        "api_base_url": api_base_url,
        "api_key": api_key,
        "meta_catalog_id": meta_catalog_id,
        "payment_gateway_provider": payment_gateway_provider,
        "payment_gateway_api_key_ref": payment_gateway_api_key_ref,
    }

    if admin_secret != ADMIN_SECRET:
        return HTMLResponse(_form_html(["Incorrect admin secret."], values), status_code=403)

    errors = []
    name = name.strip()
    whatsapp_phone_number_id = whatsapp_phone_number_id.strip()
    if not name:
        errors.append("Business name is required.")
    if not whatsapp_phone_number_id:
        errors.append("WhatsApp phone_number_id is required.")

    if data_tier not in _VALID_TIERS:
        errors.append(f'Unrecognized data connection tier "{data_tier}".')
    elif data_tier == "tier2" and not (api_base_url.strip() and api_key.strip()):
        errors.append('"Connect my existing system\'s API" requires both an API base URL and an API key.')

    if payment_gateway_provider and payment_gateway_provider not in _VALID_PAYMENT_PROVIDERS:
        errors.append(f'Unrecognized payment gateway provider "{payment_gateway_provider}".')

    if errors:
        return HTMLResponse(_form_html(errors, values), status_code=400)

    # Tier 2's fields only mean something for tier2; tier1/tier3 never store them,
    # regardless of stray values left in the form fields.
    stored_api_base_url = api_base_url.strip() or None if data_tier == "tier2" else None
    stored_api_key = api_key.strip() or None if data_tier == "tier2" else None

    try:
        tenant = db.create_tenant(
            name=name,
            whatsapp_phone_number_id=whatsapp_phone_number_id,
            access_token=access_token.strip() or None,
            app_secret=app_secret.strip() or None,
            welcome_message_text=welcome_message_text.strip() or None,
            timezone=timezone.strip() or "Asia/Kolkata",
            data_tier=data_tier,
            external_api_base_url=stored_api_base_url,
            external_api_key=stored_api_key,
            meta_catalog_id=meta_catalog_id.strip() or None,
            payment_gateway_provider=payment_gateway_provider.strip() or None,
            payment_gateway_api_key_ref=payment_gateway_api_key_ref.strip() or None,
        )
    except IntegrityError:
        errors.append(
            f'A tenant with WhatsApp phone_number_id "{whatsapp_phone_number_id}" already exists — '
            "each tenant must have its own phone_number_id for message routing to work correctly."
        )
        return HTMLResponse(_form_html(errors, values), status_code=400)

    return _confirmation_html(tenant)


# --- Catalog / payment gateway edit step (SPEC.md Section 7, Phase 7) ---
# Separate from the create form above: a tenant frequently won't have its
# catalog/payment gateway set up at initial onboarding time, so this lets an
# operator add or update those fields later. Unlike the create form, blank
# fields here mean "keep the current value" (db.update_tenant_catalog_and_payment's
# COALESCE semantics), not "unset" -- there's something to preserve now.

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
    key_current = "set" if tenant.payment_gateway_api_key_ref else "not set"

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

    <label>Payment gateway provider</label>
    <select name="payment_gateway_provider">
      <option value="" {selected('payment_gateway_provider', '')}>Keep current ({provider_current})</option>
      <option value="razorpay" {selected('payment_gateway_provider', 'razorpay')}>Razorpay</option>
    </select>

    <label>Payment gateway API key</label>
    <input type="password" name="payment_gateway_api_key_ref" value="{esc('payment_gateway_api_key_ref')}">
    <p class="hint">Currently: {key_current}. Leave blank to keep the current key —
      leave blank if you haven't set this up yet — you can add it later.</p>

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
    Payment gateway API key: {"<em>set</em>" if tenant.payment_gateway_api_key_ref else "<em>not set</em>"}
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
    payment_gateway_api_key_ref: str = Form(""),
):
    tenant = db.get_tenant(tenant_id)
    if tenant is None:
        return HTMLResponse("<p>Tenant not found.</p>", status_code=404)

    values = {
        "admin_secret": admin_secret,
        "meta_catalog_id": meta_catalog_id,
        "payment_gateway_provider": payment_gateway_provider,
        "payment_gateway_api_key_ref": payment_gateway_api_key_ref,
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

    db.update_tenant_catalog_and_payment(
        tenant_id,
        meta_catalog_id=meta_catalog_id.strip() or None,
        payment_gateway_provider=payment_gateway_provider.strip() or None,
        payment_gateway_api_key_ref=payment_gateway_api_key_ref.strip() or None,
    )

    return _catalog_payment_confirmation_html(db.get_tenant(tenant_id))
