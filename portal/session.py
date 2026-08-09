# portal/session.py
"""
Merchant portal login/session -- separate from ADMIN_SECRET (which gates
platform-admin operations like onboarding, not a per-tenant merchant login).
Hand-rolled and stdlib-only (hashlib/hmac/time), no new dependency
(itsdangerous/passlib/bcrypt aren't installed and don't need to be) --
consistent with the only auth-adjacent idiom already in this codebase,
hmac.compare_digest for webhook signature verification (core/whatsapp.py,
payments.py).

Password hashing: PBKDF2-HMAC-SHA256, a random salt per password, stored as
"pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>" in tenants.portal_password_hash.

Session: a signed cookie, not a server-side session store -- stateless, so
no session table/Redis entry to expire or clean up. The cookie value is
"<tenant_id>:<expiry_unix_ts>.<hmac_sha256_hex>"; verifying it means
recomputing the HMAC over the payload and comparing with compare_digest,
then checking the embedded expiry. No merchant self-serve signup -- an
admin sets/resets a tenant's password (admin/onboarding.py's
portal-password page); this module never creates a tenant or a password on
its own.
"""
import hashlib
import hmac
import os
import time

from fastapi import Request, Response

import db.repository as db

SESSION_COOKIE_NAME = "portal_session"
SESSION_TTL_SECONDS = 24 * 60 * 60  # 24 hours; re-login after that, no "remember me"

_PBKDF2_ITERATIONS = 260_000  # in line with current (2020s) OWASP guidance for PBKDF2-HMAC-SHA256

PORTAL_SESSION_SECRET = os.environ.get("PORTAL_SESSION_SECRET", "")

# Railway (this app's deploy target) serves everything over HTTPS in
# production, so the cookie defaults to Secure -- PORTAL_COOKIE_SECURE=false
# is the escape hatch for local dev/testing over plain HTTP, not something
# production should ever need to set.
PORTAL_COOKIE_SECURE = os.environ.get("PORTAL_COOKIE_SECURE", "true").strip().lower() != "false"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str | None) -> bool:
    """False for a tenant with no password set yet (password_hash is None)
    -- there is nothing a submitted password could ever correctly match."""
    if not password_hash:
        return False
    try:
        algo, iterations_str, salt_hex, digest_hex = password_hash.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _sign(payload: str) -> str:
    return hmac.new(PORTAL_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_cookie(tenant_id: int) -> str:
    expiry = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{tenant_id}:{expiry}"
    return f"{payload}.{_sign(payload)}"


def verify_session_cookie(cookie_value: str | None) -> int | None:
    """Returns the tenant_id the cookie was issued for, or None if it's
    missing, malformed, tampered with (signature mismatch), or expired."""
    if not cookie_value or "." not in cookie_value:
        return None
    payload, _, signature = cookie_value.rpartition(".")
    if not hmac.compare_digest(_sign(payload), signature):
        return None
    try:
        tenant_id_str, expiry_str = payload.split(":")
        tenant_id = int(tenant_id_str)
        expiry = int(expiry_str)
    except ValueError:
        return None
    if time.time() > expiry:
        return None
    return tenant_id


def require_session(request: Request) -> db.Tenant | None:
    """Called at the top of every portal route -- returns the logged-in
    tenant, or None if there isn't a valid session (callers redirect to
    /portal/login in that case). Every portal route resolves its tenant
    *only* through this function, never from a URL/form parameter -- there
    is no tenant_id anywhere in a portal URL to even try tampering with."""
    tenant_id = verify_session_cookie(request.cookies.get(SESSION_COOKIE_NAME))
    if tenant_id is None:
        return None
    tenant = db.get_tenant(tenant_id)
    if tenant is None or not tenant.is_active:
        return None
    return tenant


def set_session_cookie(response: Response, tenant_id: int) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        create_session_cookie(tenant_id),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=PORTAL_COOKIE_SECURE,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME)
