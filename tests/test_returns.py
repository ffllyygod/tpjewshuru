"""Returns, resolution reasons, and the stock-restock invariant.

Two things are pinned here that didn't exist before:

1. **Returns at all.** `RETURNED` was in the enum, in seed data, and in every
   report, but nothing ever set it at runtime — while the store's own Return
   Policy doc told customers to start a return. These tests cover the flow that
   closes that gap.

2. **Stock balances.** `place_order` decremented stock and nothing ever put it
   back, so every cancellation permanently shrank on-hand inventory. The
   `test_stock_*` cases below are the regression tests for that: they assert
   exact before/after equality, not merely "went up".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools import coupon_tools, order_tools, purchase_tools

REASON = "doesnt_fit"


# ---------------------------------------------------------------------------
# Fixtures — self-contained, per tests/conftest.py's note on idempotency.
# ---------------------------------------------------------------------------


@pytest.fixture
def customer() -> str:
    suffix = uuid.uuid4().hex[:8]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (name, email) VALUES (%s, %s) RETURNING id",
            (f"Return Test {suffix}", f"return-test-{suffix}@example.invalid"),
        )
        cid = str(cur.fetchone()[0])
        conn.commit()
    return cid


@pytest.fixture
def conversation(customer) -> str:
    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (id, customer_id) VALUES (%s, %s)", (conv_id, customer))
        conn.commit()
    return conv_id


def _make_product(returnable: bool = True, sized: bool = True, stock: int = 5) -> dict:
    sku = f"TPJ-RET-{uuid.uuid4().hex[:6].upper()}"
    stock_by_size = {"7": stock} if sized else {"_default": stock}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO products (sku, name, category, price_cents, sizes_available, stock_by_size, returnable)
            VALUES (%s, %s, 'ring', 1000000, %s, %s, %s) RETURNING id
            """,
            (sku, f"Return Test Ring {sku[-6:]}",
             '["7"]' if sized else None, '{"7": %d}' % stock if sized else '{"_default": %d}' % stock,
             returnable),
        )
        pid = str(cur.fetchone()[0])
        conn.commit()
    return {"id": pid, "sku": sku, "size": "7" if sized else None, "stock": stock}


def _make_order(customer_id: str, product: dict, status: str = "DELIVERED", delivered_days_ago: int = 3) -> str:
    """An order in a given state, with a line item, without going through
    place_order — so stock is untouched and the restock tests start from a known
    baseline."""
    number = f"TPJ-R{uuid.uuid4().hex[:6].upper()}"
    delivered_at = datetime.now(timezone.utc) - timedelta(days=delivered_days_ago)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders (order_number, customer_id, status, placed_at, shipped_at,
                                delivered_at, total_amount_cents, payment_status)
            VALUES (%s, %s, %s, %s, %s, %s, 1000000, 'PAID') RETURNING id
            """,
            (number, customer_id, status,
             delivered_at - timedelta(days=4), delivered_at - timedelta(days=3),
             delivered_at if status in ("DELIVERED", "RETURNED") else None),
        )
        oid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents, size) VALUES (%s, %s, 1, 1000000, %s)",
            (oid, product["id"], product["size"]),
        )
        conn.commit()
    return number


def _stock(sku: str) -> dict:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT stock_by_size FROM products WHERE sku = %s", (sku,))
        return cur.fetchone()[0]


def _order_row(order_number: str) -> dict:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status::text AS status, return_reason_code, return_reason_note, returned_at FROM orders WHERE order_number = %s",
            (order_number,),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# The reason pick-list
# ---------------------------------------------------------------------------


def test_customer_and_staff_see_different_reasons():
    cust = order_tools.list_resolution_reasons("return", "customer")
    staff = order_tools.list_resolution_reasons("return", "staff")
    cust_codes = {r["code"] for r in cust["reasons"]}
    staff_codes = {r["code"] for r in staff["reasons"]}

    assert "wrong_item_sent" in staff_codes, "staff-only code missing from staff list"
    assert "wrong_item_sent" not in cust_codes, "staff-only code leaked into the customer list"
    # 'both' codes appear in each.
    assert "damaged_on_arrival" in cust_codes and "damaged_on_arrival" in staff_codes


def test_cancellation_and_return_vocabularies_are_separate():
    """A cancellation code passed to a return (or vice versa) must be rejected,
    not quietly accepted — otherwise 'delivery too slow' shows up as a reason
    items came back."""
    cancel_codes = {r["code"] for r in order_tools.list_resolution_reasons("cancellation")["reasons"]}
    return_codes = {r["code"] for r in order_tools.list_resolution_reasons("return")["reasons"]}
    assert not (cancel_codes & return_codes), cancel_codes & return_codes
    assert "delivery_too_slow" in cancel_codes
    assert "doesnt_fit" in return_codes


def test_inactive_legacy_codes_are_never_offered():
    for kind in ("cancellation", "return"):
        for audience in ("customer", "staff"):
            codes = {r["code"] for r in order_tools.list_resolution_reasons(kind, audience)["reasons"]}
            assert not any(c.startswith("unspecified") for c in codes)


def test_bad_kind_is_rejected():
    assert order_tools.list_resolution_reasons("refund")["error"] == "bad_kind"


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [("PLACED", "wrong_status"), ("CONFIRMED", "wrong_status"), ("SHIPPED", "wrong_status")],
)
def test_only_delivered_orders_can_be_returned(customer, conversation, status, expected):
    order = _make_order(customer, _make_product(), status=status)
    result = order_tools.check_return_eligibility(customer, order, conversation)
    assert result["eligible"] is False
    assert result["reason"] == expected


def test_return_window_is_enforced(customer, conversation):
    order = _make_order(customer, _make_product(), delivered_days_ago=order_tools.RETURN_WINDOW_DAYS + 1)
    result = order_tools.check_return_eligibility(customer, order, conversation)
    assert result["eligible"] is False
    assert result["reason"] == "window_expired"


def test_just_inside_the_window_is_eligible(customer, conversation):
    order = _make_order(customer, _make_product(), delivered_days_ago=order_tools.RETURN_WINDOW_DAYS - 1)
    assert order_tools.check_return_eligibility(customer, order, conversation)["eligible"] is True


def test_final_sale_items_cannot_be_returned(customer, conversation):
    """The Return Policy doc says custom-sized and engraved pieces are final
    sale. The assistant quotes that doc, so it must not then accept the return."""
    product = _make_product(returnable=False)
    order = _make_order(customer, product)
    result = order_tools.check_return_eligibility(customer, order, conversation)
    assert result["eligible"] is False
    assert result["reason"] == "final_sale"
    assert product["sku"] in result["message"]


def test_another_customers_order_is_invisible(customer, conversation):
    other = _make_order(str(uuid.uuid4().hex and _other_customer()), _make_product())
    result = order_tools.check_return_eligibility(customer, other, conversation)
    assert result["eligible"] is False
    assert result["reason"] == "not_found"


def _other_customer() -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO customers (name, email) VALUES ('Other', %s) RETURNING id",
                    (f"other-{uuid.uuid4().hex[:8]}@example.invalid",))
        cid = str(cur.fetchone()[0])
        conn.commit()
    return cid


# ---------------------------------------------------------------------------
# The two-step protocol
# ---------------------------------------------------------------------------


def test_return_without_eligibility_check_is_refused(customer, conversation):
    order = _make_order(customer, _make_product())
    result = order_tools.request_return(customer, order, conversation, REASON)
    assert result["returned"] is False
    assert result["reason"] == "no_pending_confirmation"
    assert _order_row(order)["status"] == "DELIVERED"


def test_token_from_another_conversation_is_refused(customer, conversation):
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)

    other_conv = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (id, customer_id) VALUES (%s, %s)", (other_conv, customer))
        conn.commit()

    result = order_tools.request_return(customer, order, other_conv, REASON)
    assert result["returned"] is False
    assert result["reason"] == "no_pending_confirmation"
    assert _order_row(order)["status"] == "DELIVERED"


def test_return_cannot_be_replayed(customer, conversation):
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)
    assert order_tools.request_return(customer, order, conversation, REASON)["returned"] is True

    again = order_tools.request_return(customer, order, conversation, REASON)
    assert again["returned"] is False
    assert again["reason"] == "token_used"


# ---------------------------------------------------------------------------
# Reasons are mandatory and validated
# ---------------------------------------------------------------------------


def test_return_without_a_reason_writes_nothing(customer, conversation):
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)

    result = order_tools.request_return(customer, order, conversation, reason_code=None)
    assert result["returned"] is False
    assert result["reason"] == "reason_required"
    assert _order_row(order)["status"] == "DELIVERED"


def test_a_cancellation_code_is_not_a_valid_return_reason(customer, conversation):
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)

    result = order_tools.request_return(customer, order, conversation, "delivery_too_slow")
    assert result["returned"] is False
    assert result["reason"] == "bad_reason_code"
    assert "doesnt_fit" in result["message"], "the error should list the codes that ARE valid"


def test_staff_only_code_is_refused_on_the_customer_path(customer, conversation):
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)

    result = order_tools.request_return(customer, order, conversation, "wrong_item_sent")
    assert result["returned"] is False
    assert result["reason"] == "bad_reason_code"


def test_other_requires_a_note(customer, conversation):
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)

    result = order_tools.request_return(customer, order, conversation, "other_return")
    assert result["returned"] is False
    assert result["reason"] == "reason_note_required"

    ok = order_tools.request_return(customer, order, conversation, "other_return", "it arrived a different colour")
    assert ok["returned"] is True
    assert _order_row(order)["return_reason_note"] == "it arrived a different colour"


def test_successful_return_records_reason_and_timestamp(customer, conversation):
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)
    result = order_tools.request_return(customer, order, conversation, REASON, "half a size too small")

    assert result["returned"] is True
    row = _order_row(order)
    assert row["status"] == "RETURNED"
    assert row["return_reason_code"] == REASON
    assert row["return_reason_note"] == "half a size too small"
    assert row["returned_at"] is not None


def test_status_history_records_the_reason_code(customer, conversation):
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)
    order_tools.request_return(customer, order, conversation, REASON)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.to_status::text, h.reason FROM order_status_history h
            JOIN orders o ON o.id = h.order_id WHERE o.order_number = %s
            """,
            (order,),
        )
        assert ("RETURNED", f"customer_requested:{REASON}") in cur.fetchall()


# ---------------------------------------------------------------------------
# Stock balances — the regression tests for the one-way ratchet.
# ---------------------------------------------------------------------------


def test_returning_restores_exact_stock(customer, conversation):
    product = _make_product(stock=5)
    before = _stock(product["sku"])
    order = _make_order(customer, product)

    order_tools.check_return_eligibility(customer, order, conversation)
    result = order_tools.request_return(customer, order, conversation, REASON)

    assert result["returned"] is True
    assert _stock(product["sku"])["7"] == before["7"] + 1
    assert result["restocked_items"] == [{"sku": product["sku"], "size": "7", "quantity": 1}]


def test_unsized_products_restock_under_the_default_key(customer, conversation):
    product = _make_product(sized=False, stock=3)
    order = _make_order(customer, product)

    order_tools.check_return_eligibility(customer, order, conversation)
    order_tools.request_return(customer, order, conversation, REASON)

    assert _stock(product["sku"])["_default"] == 4


def test_place_then_cancel_leaves_stock_exactly_as_it_started(customer, conversation):
    """The bug this exists for: place_order took stock out and nothing put it
    back, so cancellation permanently destroyed inventory."""
    product = _make_product(stock=5)
    before = _stock(product["sku"])

    placed = purchase_tools.place_order(customer, product["sku"], quantity=2, size="7")
    assert placed["placed"] is True
    assert _stock(product["sku"])["7"] == before["7"] - 2

    order_tools.check_cancellation_eligibility(customer, placed["order_number"], conversation)
    result = order_tools.cancel_order(customer, placed["order_number"], conversation, "changed_mind")

    assert result["cancelled"] is True
    assert _stock(product["sku"]) == before, "cancelling must restore stock exactly"


def test_failed_return_does_not_restock(customer, conversation):
    """A refused write must not have side effects — restocking on a rejected
    return would inflate inventory out of nothing."""
    product = _make_product(stock=5)
    before = _stock(product["sku"])
    order = _make_order(customer, product)

    order_tools.check_return_eligibility(customer, order, conversation)
    assert order_tools.request_return(customer, order, conversation, "delivery_too_slow")["returned"] is False

    assert _stock(product["sku"]) == before


# ---------------------------------------------------------------------------
# Settlement continues to work on a returned order.
# ---------------------------------------------------------------------------


def test_returned_order_offers_settlement_with_the_return_bonus(customer, conversation):
    """coupon_tools already mapped RETURNED to the higher return bonus; nothing
    could reach that branch until returns existed."""
    order = _make_order(customer, _make_product())
    order_tools.check_return_eligibility(customer, order, conversation)
    order_tools.request_return(customer, order, conversation, REASON)

    options = coupon_tools.offer_settlement_options(customer, order)
    assert options["already_settled"] is False
    assert options["source_type"] == "return"

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT return_bonus_percent FROM coupon_policy WHERE name = 'default'")
        expected_bonus = float(cur.fetchone()["return_bonus_percent"])
    assert options["coupon_bonus_percent"] == expected_bonus

    issued = coupon_tools.issue_coupon(customer, order, conversation)
    assert issued["issued"] is True
