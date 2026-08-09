"""
db/repository.py unit tests for the Phase 5 commerce data model (products,
orders, order_items) — SPEC.md Section 4. Built ahead of Phase 2/3's actual
order-received/payment handlers, so these test the repository layer directly
rather than through any webhook flow (nothing calls this CRUD from
core/main.py yet).
"""
import threading
from decimal import Decimal

import pytest

import db.repository as db
from db.connection import _connect, get_connection, get_database_url


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


# --- checkout_cart: stock decrement, unavailable items, duplicate-call safety ---

def test_checkout_cart_decrements_stock_and_creates_order(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("100.00"), stock_quantity=5)

    order, unavailable = db.checkout_cart(tenant_id, "919999999999", {str(product.id): 2})

    assert unavailable == []
    assert order is not None
    assert order.status == db.ORDER_STATUS_PENDING_PAYMENT
    assert order.subtotal == Decimal("200.00")
    assert order.total == Decimal("200.00")

    items = db.get_order_items(tenant_id, order.id)
    assert len(items) == 1
    assert items[0].quantity == 2
    assert items[0].unit_price_at_order_time == Decimal("100.00")

    assert db.get_product(tenant_id, product.id).stock_quantity == 3


def test_checkout_cart_rejects_over_quantity_leaving_stock_untouched(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("100.00"), stock_quantity=2)

    order, unavailable = db.checkout_cart(tenant_id, "919999999999", {str(product.id): 5})

    assert order is None
    assert unavailable == [str(product.id)]
    assert db.get_product(tenant_id, product.id).stock_quantity == 2  # untouched, not driven negative


def test_checkout_cart_skips_deactivated_product_but_orders_the_rest(tenant_id):
    available = db.create_product(tenant_id, name="Available", price=Decimal("50.00"), stock_quantity=5)
    deactivated = db.create_product(tenant_id, name="Deactivated", price=Decimal("30.00"), stock_quantity=5)
    conn = get_connection()
    conn.execute("UPDATE products SET is_active = 0 WHERE id = ?", (deactivated.id,))
    conn.commit()

    order, unavailable = db.checkout_cart(
        tenant_id, "919999999999", {str(available.id): 1, str(deactivated.id): 1},
    )

    assert order is not None
    assert unavailable == [str(deactivated.id)]
    items = db.get_order_items(tenant_id, order.id)
    assert len(items) == 1
    assert items[0].product_id == available.id
    assert order.subtotal == Decimal("50.00")
    # The deactivated product's stock was never touched.
    assert db.get_product(tenant_id, deactivated.id).stock_quantity == 5


def test_checkout_cart_returns_none_when_every_item_unavailable(tenant_id):
    product = db.create_product(tenant_id, name="Widget", price=Decimal("10.00"), stock_quantity=0)

    order, unavailable = db.checkout_cart(tenant_id, "919999999999", {str(product.id): 1})

    assert order is None
    assert unavailable == [str(product.id)]
    assert db.get_orders_for_phone(tenant_id, "919999999999") == []


def test_checkout_cart_rolls_back_cleanly_on_mid_transaction_failure(tenant_id, monkeypatch):
    """Forces a real failure *after* the stock decrement has already run but
    before the transaction commits (simulating e.g. the order_items insert
    failing for some unrelated reason) -- same class of question that
    surfaced a real bug in the hospital repo's advisory-lock work: does the
    transaction actually roll back everything, and is the connection cleanly
    usable for the very next unrelated query afterward, or does it get left
    in a poisoned/aborted state?"""
    product = db.create_product(tenant_id, name="Widget", price=Decimal("100.00"), stock_quantity=5)

    conn = get_connection()
    real_execute = conn.execute

    def failing_execute(sql, params=()):
        # Stock UPDATE and the orders INSERT are allowed through normally;
        # only the order_items INSERT (which runs after the stock decrement
        # has already happened inside the transaction) fails.
        if "INSERT INTO order_items" in sql:
            raise RuntimeError("simulated failure mid-transaction")
        return real_execute(sql, params)

    monkeypatch.setattr(conn, "execute", failing_execute)
    try:
        with pytest.raises(RuntimeError, match="simulated failure mid-transaction"):
            db.checkout_cart(tenant_id, "919999999999", {str(product.id): 2})
    finally:
        monkeypatch.setattr(conn, "execute", real_execute)

    # The stock decrement (which ran successfully before the forced failure)
    # must be rolled back along with everything else -- not left at 3 (5-2).
    assert db.get_product(tenant_id, product.id).stock_quantity == 5

    # No order or order_items rows were left behind either.
    assert db.get_orders_for_phone(tenant_id, "919999999999") == []

    # The connection must be cleanly reusable for the very next unrelated
    # query -- no lingering "current transaction is aborted" state, and
    # autocommit correctly restored to True by _Transaction.__exit__'s
    # finally block. Exercise both a plain read and a full new checkout.
    assert db.get_product(tenant_id, product.id) is not None
    other_product = db.create_product(tenant_id, name="Gadget", price=Decimal("10.00"), stock_quantity=1)
    order, unavailable = db.checkout_cart(tenant_id, "919999999999", {str(other_product.id): 1})
    assert order is not None
    assert unavailable == []
    assert db.get_product(tenant_id, other_product.id).stock_quantity == 0


def test_checkout_cart_called_twice_with_same_cart_only_fulfills_once(tenant_id):
    """Simulates a duplicate checkout (double-tap / webhook redelivery) at the
    repository layer directly: the same cart, checked out twice in a row.
    The second call must not oversell the last unit."""
    product = db.create_product(tenant_id, name="Widget", price=Decimal("100.00"), stock_quantity=1)
    cart = {str(product.id): 1}

    order1, unavailable1 = db.checkout_cart(tenant_id, "919999999999", cart)
    order2, unavailable2 = db.checkout_cart(tenant_id, "919999999999", cart)

    assert order1 is not None and unavailable1 == []
    assert order2 is None and unavailable2 == [str(product.id)]
    assert db.get_product(tenant_id, product.id).stock_quantity == 0
    assert len(db.get_orders_for_phone(tenant_id, "919999999999")) == 1


def test_concurrent_checkouts_for_last_unit_exactly_one_succeeds(tenant_id):
    """Real concurrency, not a sequential simulation: two separate physical
    Postgres connections (standing in for two different worker
    processes/requests) race db.checkout_cart() for the single remaining unit
    of a product, started as close to simultaneously as threading.Barrier
    allows. Exactly one must succeed; the other must cleanly report the item
    unavailable -- never a negative stock count, never two orders for one
    unit. This is what actually prevents overselling across two *different*
    customers -- core/main.py's per-(tenant, phone) message lock doesn't
    apply here since it's scoped per phone number, not per product."""
    product = db.create_product(tenant_id, name="Limited Widget", price=Decimal("999.00"), stock_quantity=1)
    cart = {str(product.id): 1}

    dsn = get_database_url()
    conn_a = _connect(dsn)
    conn_b = _connect(dsn)
    barrier = threading.Barrier(2)
    results = {}
    errors = []

    def run(label, conn, phone):
        try:
            barrier.wait(timeout=5)
            results[label] = db.checkout_cart(tenant_id, phone, cart, conn=conn)
        except Exception as exc:  # surfaced via `errors` so the test fails loudly, not silently
            errors.append((label, exc))

    thread_a = threading.Thread(target=run, args=("a", conn_a, "911111111111"))
    thread_b = threading.Thread(target=run, args=("b", conn_b, "922222222222"))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)
    conn_a.close()
    conn_b.close()

    assert errors == []
    assert set(results) == {"a", "b"}

    orders = [order for order, _ in results.values()]
    succeeded = [o for o in orders if o is not None]
    failed = [o for o in orders if o is None]
    assert len(succeeded) == 1, f"expected exactly one checkout to succeed, got: {results}"
    assert len(failed) == 1

    unavailable_from_loser = next(u for o, u in results.values() if o is None)
    assert unavailable_from_loser == [str(product.id)]

    # Stock never went negative, and exactly one order_item of quantity 1 exists total.
    assert db.get_product(tenant_id, product.id).stock_quantity == 0
    winner_phone = "911111111111" if results["a"][0] is not None else "922222222222"
    winner_order = db.get_orders_for_phone(tenant_id, winner_phone)[0]
    items = db.get_order_items(tenant_id, winner_order.id)
    assert len(items) == 1
    assert items[0].quantity == 1
