# tests/test_portal_products.py
"""
Merchant portal product management (portal/products.py): list (active +
inactive), add, edit, toggle active/inactive, and bulk CSV import (reusing
admin.onboarding_wizard's CSV parser).
"""
import os
from decimal import Decimal

import db.repository as db

os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")

from core.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from portal.session import hash_password  # noqa: E402


def _login(tenant_id: int, phone_number_id: str = "123", password: str = "secret123") -> TestClient:
    db.set_tenant_portal_password_hash(tenant_id, hash_password(password))
    client = TestClient(app)
    client.post("/portal/login", data={"whatsapp_phone_number_id": phone_number_id, "password": password})
    return client


def test_products_list_requires_login(tenant_id):
    client = TestClient(app)
    resp = client.get("/portal/products", follow_redirects=False)
    assert resp.status_code == 303


def test_products_list_shows_active_and_inactive(tenant_id):
    active = db.create_product(tenant_id, name="Active Widget", price=Decimal("10.00"))
    inactive = db.create_product(tenant_id, name="Inactive Widget", price=Decimal("20.00"))
    db.set_product_active(tenant_id, inactive.id, False)

    client = _login(tenant_id)
    resp = client.get("/portal/products")
    assert resp.status_code == 200
    assert "Active Widget" in resp.text
    assert "Inactive Widget" in resp.text


def test_create_product(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/products/new", data={
        "name": "New Product", "price": "99.99", "description": "A great thing",
        "image_url": "https://example.com/x.png", "sku": "SKU-1", "stock_quantity": "5", "category": "Toys",
    })
    assert resp.status_code == 200

    products = db.get_all_products(tenant_id)
    assert len(products) == 1
    assert products[0].name == "New Product"
    assert products[0].price == Decimal("99.99")
    assert products[0].category == "Toys"


def test_create_product_invalid_price_rejected(tenant_id):
    client = _login(tenant_id)
    resp = client.post("/portal/products/new", data={"name": "Bad", "price": "not-a-number"})
    assert resp.status_code == 400
    assert db.get_all_products(tenant_id) == []


def test_edit_product_overwrites_all_fields(tenant_id):
    product = db.create_product(
        tenant_id, name="Old Name", price=Decimal("10.00"), description="old desc",
        sku="OLD-SKU", category="OldCat",
    )
    client = _login(tenant_id)
    resp = client.post(f"/portal/products/{product.id}/edit", data={
        "name": "New Name", "price": "15.00", "description": "", "image_url": "", "sku": "", "stock_quantity": "3", "category": "",
    })
    assert resp.status_code == 200

    updated = db.get_product(tenant_id, product.id)
    assert updated.name == "New Name"
    assert updated.price == Decimal("15.00")
    assert updated.description is None  # blank on a full-overwrite edit clears it
    assert updated.sku is None
    assert updated.category is None
    assert updated.stock_quantity == 3


def test_toggle_active(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("10.00"))
    assert product.is_active is True

    client = _login(tenant_id)
    client.post(f"/portal/products/{product.id}/toggle-active")
    assert db.get_product(tenant_id, product.id).is_active is False

    client.post(f"/portal/products/{product.id}/toggle-active")
    assert db.get_product(tenant_id, product.id).is_active is True


def test_csv_import_creates_products(tenant_id):
    csv_content = (
        "name,price,description,image_url,sku,stock_quantity,category\n"
        "Widget,199.00,A fine widget,,SKU-1,10,Tools\n"
        "Gadget,349.50,,,SKU-2,5,Electronics\n"
    ).encode()

    client = _login(tenant_id)
    resp = client.post(
        "/portal/products/import",
        files={"csv_file": ("products.csv", csv_content, "text/csv")},
    )
    assert resp.status_code == 200
    assert "Imported 2 product" in resp.text

    products = db.get_all_products(tenant_id)
    assert len(products) == 2


def test_csv_import_malformed_row_reports_error(tenant_id):
    csv_content = (
        "name,price,description,image_url,sku,stock_quantity,category\n"
        "Widget,not-a-price,,,SKU-1,10,Tools\n"
    ).encode()

    client = _login(tenant_id)
    resp = client.post(
        "/portal/products/import",
        files={"csv_file": ("products.csv", csv_content, "text/csv")},
    )
    assert resp.status_code == 400
    assert "not a valid number" in resp.text
    assert db.get_all_products(tenant_id) == []


def test_cross_tenant_isolation(tenant_id, second_tenant_id):
    product_a = db.create_product(tenant_id, name="Tenant A Product", price=Decimal("10.00"))
    product_b = db.create_product(second_tenant_id, name="Tenant B Product", price=Decimal("20.00"))

    client_a = _login(tenant_id)
    list_resp = client_a.get("/portal/products")
    assert "Tenant A Product" in list_resp.text
    assert "Tenant B Product" not in list_resp.text

    edit_resp = client_a.get(f"/portal/products/{product_b.id}/edit")
    assert edit_resp.status_code == 404

    toggle_resp = client_a.post(f"/portal/products/{product_b.id}/toggle-active")
    assert toggle_resp.status_code == 404
    assert db.get_product(second_tenant_id, product_b.id).is_active is True
