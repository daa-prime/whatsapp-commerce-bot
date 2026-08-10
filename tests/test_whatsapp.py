import hashlib
import hmac
import json

import pytest
from core.whatsapp import parse_incoming_message, validate_webhook_signature, WhatsAppClient

SECRET = "test_app_secret"
BODY = b'{"test": "data"}'

def _make_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

def test_valid_signature():
    sig = _make_sig(BODY, SECRET)
    assert validate_webhook_signature(BODY, sig, SECRET) is True

def test_invalid_signature():
    assert validate_webhook_signature(BODY, "sha256=invalid", SECRET) is False

def test_missing_sha256_prefix():
    assert validate_webhook_signature(BODY, "invalidsig", SECRET) is False

@pytest.mark.asyncio
async def test_client_send_calls_api(httpx_mock):
    from core.whatsapp import WhatsAppClient
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "msg_id"}]})
    client = WhatsAppClient(phone_number_id="123", access_token="token")
    await client.send_text("54911111111", "hola")
    # No exception = pass


@pytest.mark.asyncio
async def test_send_list_builds_interactive_list_payload(httpx_mock):
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "msg_id"}]})
    client = WhatsAppClient(phone_number_id="123", access_token="token")
    sections = [{"title": "Products", "rows": [{"id": "sku_1", "title": "Widget"}]}]
    await client.send_list(to="54911111111", body_text="Pick one", button_text="View", sections=sections)

    request = httpx_mock.get_requests()[0]
    payload = json.loads(request.content)
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "list"
    assert payload["interactive"]["body"]["text"] == "Pick one"
    assert payload["interactive"]["action"]["button"] == "View"
    assert payload["interactive"]["action"]["sections"] == sections


@pytest.mark.asyncio
async def test_send_buttons_builds_interactive_button_payload(httpx_mock):
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "msg_id"}]})
    client = WhatsAppClient(phone_number_id="123", access_token="token")
    buttons = [{"id": "confirm", "title": "Confirm"}, {"id": "cancel", "title": "Cancel"}]
    await client.send_buttons(to="54911111111", body_text="Confirm?", buttons=buttons)

    request = httpx_mock.get_requests()[0]
    payload = json.loads(request.content)
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "button"
    assert payload["interactive"]["action"]["buttons"] == [
        {"type": "reply", "reply": {"id": "confirm", "title": "Confirm"}},
        {"type": "reply", "reply": {"id": "cancel", "title": "Cancel"}},
    ]
    assert "header" not in payload["interactive"]


@pytest.mark.asyncio
async def test_send_buttons_with_header_image_url(httpx_mock):
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "msg_id"}]})
    client = WhatsAppClient(phone_number_id="123", access_token="token")
    await client.send_buttons(
        to="54911111111", body_text="Widget", buttons=[{"id": "add", "title": "Add to Cart"}],
        header_image_url="https://example.com/widget.png", header_text="ignored when image is set",
    )

    request = httpx_mock.get_requests()[0]
    payload = json.loads(request.content)
    assert payload["interactive"]["header"] == {"type": "image", "image": {"link": "https://example.com/widget.png"}}


def test_parse_incoming_message_text():
    message = {"type": "text", "text": {"body": "hello"}}
    assert parse_incoming_message(message) == {"type": "text", "text": "hello"}


def test_parse_incoming_message_list_reply():
    message = {
        "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": "sku_1", "title": "Widget"}},
    }
    assert parse_incoming_message(message) == {
        "type": "interactive_reply", "id": "sku_1", "title": "Widget",
    }


def test_parse_incoming_message_button_reply():
    message = {
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "confirm", "title": "Confirm"}},
    }
    assert parse_incoming_message(message) == {
        "type": "interactive_reply", "id": "confirm", "title": "Confirm",
    }


def test_parse_incoming_message_audio():
    assert parse_incoming_message({"type": "audio", "audio": {"id": "media123"}}) == {"type": "audio"}


def test_parse_incoming_message_order():
    message = {
        "type": "order",
        "order": {
            "catalog_id": "cat_123",
            "product_items": [
                {"product_retailer_id": "t1-p1", "quantity": "2", "item_price": "199.00", "currency": "INR"},
            ],
        },
    }
    assert parse_incoming_message(message) == {
        "type": "order",
        "catalog_id": "cat_123",
        "product_items": [
            {"product_retailer_id": "t1-p1", "quantity": "2", "item_price": "199.00", "currency": "INR"},
        ],
    }


def test_parse_incoming_message_order_malformed_product_items_falls_back_unsupported():
    message = {"type": "order", "order": {"catalog_id": "cat_123", "product_items": "not-a-list"}}
    assert parse_incoming_message(message) == {"type": "unsupported"}


def test_parse_incoming_message_order_missing_order_key():
    assert parse_incoming_message({"type": "order"}) == {"type": "order", "catalog_id": "", "product_items": []}


@pytest.mark.asyncio
async def test_send_product_list_builds_interactive_payload(httpx_mock):
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", json={"messages": [{"id": "msg_id"}]})
    client = WhatsAppClient(phone_number_id="123", access_token="token")
    sections = [{"title": "Electronics", "product_items": [{"product_retailer_id": "t1-p1"}]}]
    await client.send_product_list(to="54911111111", catalog_id="cat_123", sections=sections, body_text="Browse:")

    request = httpx_mock.get_requests()[0]
    payload = json.loads(request.content)
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "product_list"
    assert payload["interactive"]["action"]["catalog_id"] == "cat_123"
    assert payload["interactive"]["action"]["sections"] == sections
    assert payload["interactive"]["body"]["text"] == "Browse:"


def test_parse_incoming_message_unsupported():
    assert parse_incoming_message({"type": "image", "image": {"id": "img123"}}) == {"type": "unsupported"}


# --- Phase 8 item 6: delivery failures must not raise ---

@pytest.mark.asyncio
async def test_send_text_does_not_raise_on_401_expired_token(httpx_mock):
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", status_code=401,
                             json={"error": {"message": "Invalid OAuth access token", "code": 190}})
    client = WhatsAppClient(phone_number_id="123", access_token="expired-token")
    await client.send_text("54911111111", "hola")  # must not raise


@pytest.mark.asyncio
async def test_send_list_does_not_raise_on_5xx(httpx_mock):
    httpx_mock.add_response(url="https://graph.facebook.com/v22.0/123/messages", status_code=503)
    client = WhatsAppClient(phone_number_id="123", access_token="token")
    await client.send_list(to="54911111111", body_text="Pick one", button_text="View",
                            sections=[{"title": "Products", "rows": [{"id": "sku_1", "title": "Widget"}]}])


@pytest.mark.asyncio
async def test_send_buttons_does_not_raise_on_network_error(httpx_mock):
    import httpx
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    client = WhatsAppClient(phone_number_id="123", access_token="token")
    await client.send_buttons(to="54911111111", body_text="Confirm?",
                               buttons=[{"id": "confirm", "title": "Confirm"}, {"id": "cancel", "title": "Cancel"}])
