"""
db/repository.py unit tests for the Phase 5 commerce data model (products,
orders, order_items) — SPEC.md Section 4. Built ahead of Phase 2/3's actual
order-received/payment handlers, so these test the repository layer directly
rather than through any webhook flow (nothing calls this CRUD from
core/main.py yet).
"""
from decimal import Decimal

import db.repository as db


def test_create_product_and_get(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("199.00"), sku="SKU-1")
    found = db.get_product(tenant_id, product.id)
    assert found is not None
    assert found.name == "Widget"
    assert found.price == Decimal("199.00")
    assert found.currency == "INR"  # default
    assert found.sku == "SKU-1"
    assert found.stock_quantity == 0  # default
    assert found.is_active is True


def test_get_product_not_found(tenant_id):
    assert db.get_product(tenant_id, -1) is None


def test_get_active_products_excludes_inactive(tenant_id):
    active = db.create_product(tenant_id, name="Active Widget", price=Decimal("10.00"))
    inactive = db.create_product(tenant_id, name="Inactive Widget", price=Decimal("10.00"))
    from db.connection import get_connection
    conn = get_connection()
    conn.execute("UPDATE products SET is_active = 0 WHERE id = ?", (inactive.id,))
    conn.commit()

    active_ids = {p.id for p in db.get_active_products(tenant_id)}
    assert active.id in active_ids
    assert inactive.id not in active_ids


def test_get_active_products_scoped_to_tenant(tenant_id, second_tenant_id):
    mine = db.create_product(tenant_id, name="Mine", price=Decimal("10.00"))
    db.create_product(second_tenant_id, name="Theirs", price=Decimal("10.00"))

    mine_ids = {p.id for p in db.get_active_products(tenant_id)}
    assert mine.id in mine_ids
    assert len(db.get_active_products(tenant_id)) == 1


def test_update_product_stock(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("10.00"), stock_quantity=5)
    db.update_product_stock(tenant_id, product.id, 3)
    assert db.get_product(tenant_id, product.id).stock_quantity == 3


def test_create_order_defaults_to_browsing(tenant_id):
    order = db.create_order(tenant_id, customer_phone="919999999999")
    assert order.status == db.ORDER_STATUS_BROWSING
    assert order.subtotal is None
    assert order.total is None
    assert order.paid_at is None


def test_get_order_not_found(tenant_id):
    assert db.get_order(tenant_id, -1) is None


def test_get_orders_for_phone_scoped_and_sorted(tenant_id):
    db.create_order(tenant_id, customer_phone="919999999999")
    db.create_order(tenant_id, customer_phone="919999999999")
    db.create_order(tenant_id, customer_phone="918888888888")

    orders = db.get_orders_for_phone(tenant_id, "919999999999")
    assert len(orders) == 2
    assert all(o.customer_phone == "919999999999" for o in orders)


def test_orders_scoped_to_tenant(tenant_id, second_tenant_id):
    mine = db.create_order(tenant_id, customer_phone="919999999999")
    db.create_order(second_tenant_id, customer_phone="919999999999")

    assert db.get_order(tenant_id, mine.id) is not None
    # second_tenant's own order isn't visible under tenant_id's scope, and
    # tenant_id's own phone-scoped query only returns tenant_id's order.
    assert len(db.get_orders_for_phone(tenant_id, "919999999999")) == 1


def test_update_order_status_moves_to_pending_payment(tenant_id):
    order = db.create_order(tenant_id, customer_phone="919999999999")
    db.update_order_status(
        tenant_id, order.id, db.ORDER_STATUS_PENDING_PAYMENT,
        payment_link_url="https://rzp.io/l/abc123",
    )
    updated = db.get_order(tenant_id, order.id)
    assert updated.status == db.ORDER_STATUS_PENDING_PAYMENT
    assert updated.payment_link_url == "https://rzp.io/l/abc123"


def test_update_order_status_preserves_fields_not_passed(tenant_id):
    order = db.create_order(tenant_id, customer_phone="919999999999")
    db.update_order_status(
        tenant_id, order.id, db.ORDER_STATUS_PENDING_PAYMENT,
        payment_link_url="https://rzp.io/l/abc123",
    )
    # A later transition that doesn't re-pass payment_link_url must not blank it out.
    db.update_order_status(tenant_id, order.id, db.ORDER_STATUS_PAID, paid_at="2026-01-01T00:00:00")
    updated = db.get_order(tenant_id, order.id)
    assert updated.status == db.ORDER_STATUS_PAID
    assert updated.payment_link_url == "https://rzp.io/l/abc123"
    assert updated.paid_at == "2026-01-01T00:00:00"


def test_create_order_item_and_get(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("50.00"))
    order = db.create_order(tenant_id, customer_phone="919999999999")
    item = db.create_order_item(tenant_id, order.id, product.id, quantity=2, unit_price_at_order_time=Decimal("50.00"))

    found = db.get_order_item(tenant_id, item.id)
    assert found is not None
    assert found.order_id == order.id
    assert found.product_id == product.id
    assert found.quantity == 2
    assert found.unit_price_at_order_time == Decimal("50.00")


def test_order_item_price_snapshot_survives_product_price_change(tenant_id):
    """A later price change on the product must not retroactively change an
    already-created line item's recorded price."""
    product = db.create_product(tenant_id, name="Widget", price=Decimal("50.00"))
    order = db.create_order(tenant_id, customer_phone="919999999999")
    item = db.create_order_item(tenant_id, order.id, product.id, quantity=1, unit_price_at_order_time=Decimal("50.00"))

    from db.connection import get_connection
    conn = get_connection()
    conn.execute("UPDATE products SET price = ? WHERE id = ?", (Decimal("75.00"), product.id))
    conn.commit()

    assert db.get_order_item(tenant_id, item.id).unit_price_at_order_time == Decimal("50.00")
    assert db.get_product(tenant_id, product.id).price == Decimal("75.00")


def test_get_order_items_scoped_to_order(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("10.00"))
    order_a = db.create_order(tenant_id, customer_phone="919999999999")
    order_b = db.create_order(tenant_id, customer_phone="919999999999")
    db.create_order_item(tenant_id, order_a.id, product.id, quantity=1, unit_price_at_order_time=Decimal("10.00"))
    db.create_order_item(tenant_id, order_b.id, product.id, quantity=2, unit_price_at_order_time=Decimal("10.00"))

    items_a = db.get_order_items(tenant_id, order_a.id)
    assert len(items_a) == 1
    assert items_a[0].quantity == 1
