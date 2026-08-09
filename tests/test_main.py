import os
import json
import hashlib
import hmac

from fastapi.testclient import TestClient

os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")
# core.main calls db.init_db.init_db() at import time (module-level, before any
# pytest fixture runs) -- DATABASE_URL is already pointed at the test Postgres
# instance by tests/conftest.py (loaded before this module), so importing this
# test module never touches a real production database.

from core.main import app

client = TestClient(app)


def test_health_check():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_landing_page_renders():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ShopTransact" in resp.text
    assert "Set up your store" in resp.text
    assert "/admin/onboard-tenant" in resp.text
    assert "/portal/login" in resp.text


def test_webhook_verification():
    resp = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "mytoken",
        "hub.challenge": "12345",
    })
    assert resp.status_code == 200
    assert resp.text == "12345"


def test_webhook_verification_wrong_token():
    resp = client.get("/webhook", params={
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong",
        "hub.challenge": "12345",
    })
    assert resp.status_code == 403


def test_webhook_invalid_signature():
    """Bad signature on an otherwise well-formed payload for a REAL, recognized
    tenant (phone_number_id="123") -- must still be rejected with 403. (A
    structurally-incomplete payload can't reach the signature check at all
    anymore, since resolving which tenant's secret to check against requires
    parsing that far first -- see the malformed-payload tests below instead.)"""
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123"},
            "messages": [{"from": "919999999999", "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=invalid", "Content-Type": "application/json"},
    )
    assert resp.status_code == 403


def test_webhook_status_update_ignored():
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123"},
            "statuses": [{"status": "delivered"}],
        }}]}]
    }).encode()
    sig = "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200


# --- Phase 8 item 3: malformed/unexpected webhook payloads must never 500 ---

def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(b"appsecret", body, hashlib.sha256).hexdigest()


def test_invalid_json_body_returns_200():
    body = b"not valid json{{{"
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})
    assert resp.status_code == 200


def test_non_dict_json_payload_returns_200():
    body = json.dumps(["unexpected", "list", "payload"]).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})
    assert resp.status_code == 200


def test_empty_entry_list_returns_200():
    body = json.dumps({"entry": []}).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})
    assert resp.status_code == 200


def test_entry_present_but_not_a_list_returns_200():
    body = json.dumps({"entry": {"unexpected": "shape"}}).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})
    assert resp.status_code == 200


def test_reaction_message_type_ignored_gracefully(httpx_mock):
    """A message type this app doesn't specifically parse (e.g. a reaction) must
    fall through to the "unsupported" path, not crash — it still reaches
    _process_message (unlike the structurally-malformed cases above), which
    replies with the tenant's welcome message, so the outbound send needs mocking."""
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "wamid.x"}]})
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123"},
            "messages": [
                {"from": "919999999999", "type": "reaction", "reaction": {"emoji": "\U0001F44D", "message_id": "wamid.abc"}}
            ],
        }}]}]
    }).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})
    assert resp.status_code == 200


# --- Phase 8 item 6: a failed outbound send must not crash the webhook handler ---

def test_webhook_returns_200_even_when_whatsapp_send_fails_with_401(httpx_mock):
    """Simulates an expired/invalid access token (the real-world case we hit
    earlier in this project) -- Meta rejects our outbound send, but the
    *incoming* webhook must still be acknowledged with 200."""
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", status_code=401,
                             json={"error": {"message": "Invalid OAuth access token", "code": 190}})
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123"},
            "messages": [{"from": "919999999999", "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})
    assert resp.status_code == 200


def test_webhook_returns_200_even_when_whatsapp_send_fails_with_5xx(httpx_mock):
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", status_code=503)
    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123"},
            "messages": [{"from": "919999999998", "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})
    assert resp.status_code == 200


# --- Phase 8 item 4: the per-phone message lock must prevent double-processing ---
#
# A genuine asyncio.gather()-based concurrency test was tried here first, but
# it's inherently flaky: nothing guarantees the two requests actually
# interleave at the lock check rather than one running to completion (lock
# acquired AND released) before the other starts. These test the same
# mechanism deterministically instead — first by pre-acquiring the lock to
# simulate "another request is already mid-flight" through the real endpoint,
# then directly at the unit level.

def test_locked_phone_skips_processing_but_still_acks_200(httpx_mock, tenant_id):
    import core.main as m
    phone = "919999999997"
    assert m._acquire_message_lock(tenant_id, phone) is True  # simulate a request already in flight

    body = json.dumps({
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "123"},
            "messages": [{"from": phone, "type": "text", "text": {"body": "hi"}}],
        }}]}]
    }).encode()
    resp = client.post("/webhook", content=body,
                        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"})

    assert resp.status_code == 200
    # No WhatsApp send was attempted -- the message was skipped, not processed twice.
    assert len(httpx_mock.get_requests()) == 0

    m._release_message_lock(tenant_id, phone)


def test_acquire_message_lock_blocks_second_call_until_released(tenant_id):
    import core.main as m
    phone = "919999999996"
    assert m._acquire_message_lock(tenant_id, phone) is True
    assert m._acquire_message_lock(tenant_id, phone) is False  # still held
    m._release_message_lock(tenant_id, phone)
    assert m._acquire_message_lock(tenant_id, phone) is True  # available again
    m._release_message_lock(tenant_id, phone)
