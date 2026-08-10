# tests/test_catalog_feed.py
"""
catalog/feed.py -- the product feed a merchant registers once in Meta
Commerce Manager as a data feed source (SPEC.md Section 3.1's "v1: manual
catalog setup... upload a product feed/CSV"). Chosen over the Catalog Batch
API specifically to avoid needing Meta App Review for the catalog_management
permission -- see the module's own docstring.
"""
import csv
import io
import os
from decimal import Decimal

import db.repository as db

os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")

from catalog.feed import PublicBaseURLNotConfigured, build_feed_csv, feed_url  # noqa: E402
from core.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


def test_feed_url_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert feed_url(1) is None


def test_feed_url_builds_expected_path():
    assert feed_url(42) == "https://test.example.com/catalog/42.csv"


def test_build_feed_csv_returns_none_for_unknown_tenant():
    assert build_feed_csv(999999) is None


def test_build_feed_csv_raises_when_public_base_url_unset(tenant_id, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    try:
        build_feed_csv(tenant_id)
        assert False, "expected PublicBaseURLNotConfigured"
    except PublicBaseURLNotConfigured:
        pass


def test_feed_csv_contains_active_products_only(tenant_id):
    active = db.create_product(tenant_id, name="Active Widget", price=Decimal("99.00"), stock_quantity=5)
    inactive = db.create_product(tenant_id, name="Inactive Widget", price=Decimal("50.00"))
    db.set_product_active(tenant_id, inactive.id, False)

    csv_text = build_feed_csv(tenant_id)
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    titles = {r["title"] for r in rows}
    assert "Active Widget" in titles
    assert "Inactive Widget" not in titles

    active_row = next(r for r in rows if r["title"] == "Active Widget")
    assert active_row["id"] == active.catalog_retailer_id
    assert active_row["price"] == "99.00 INR"
    assert active_row["availability"] == "in stock"


def test_feed_csv_marks_zero_stock_out_of_stock(tenant_id):
    db.create_product(tenant_id, name="Sold Out", price=Decimal("10.00"), stock_quantity=0)
    csv_text = build_feed_csv(tenant_id)
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    row = next(r for r in rows if r["title"] == "Sold Out")
    assert row["availability"] == "out of stock"


def test_feed_csv_columns_match_meta_feed_spec(tenant_id):
    db.create_product(tenant_id, name="Widget", price=Decimal("10.00"))
    csv_text = build_feed_csv(tenant_id)
    header = next(csv.reader(io.StringIO(csv_text)))
    assert header == ["id", "title", "description", "availability", "condition", "price", "link", "image_link"]


def test_feed_csv_cross_tenant_isolation(tenant_id, second_tenant_id):
    db.create_product(tenant_id, name="Mine", price=Decimal("10.00"))
    db.create_product(second_tenant_id, name="Theirs", price=Decimal("10.00"))

    csv_text = build_feed_csv(tenant_id)
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    titles = {r["title"] for r in rows}
    assert "Mine" in titles
    assert "Theirs" not in titles


def test_feed_route_serves_csv(tenant_id):
    db.create_product(tenant_id, name="Widget", price=Decimal("10.00"))
    resp = client.get(f"/catalog/{tenant_id}.csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "Widget" in resp.text


def test_feed_route_404s_for_unknown_tenant():
    resp = client.get("/catalog/999999.csv")
    assert resp.status_code == 404
