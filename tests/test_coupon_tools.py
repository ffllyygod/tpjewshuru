"""Direct tests of the coupon/purchase tool functions against the live seeded
DB — no LLM involved. These are the guardrail-critical functions (ownership,
idempotency, atomic balance handling) called out explicitly in the plan's
security section; verified here independent of prompt behavior.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools import coupon_tools, purchase_tools


def _customer_and_order(order_number: str) -> tuple[str, str]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT customer_id FROM orders WHERE order_number = %s", (order_number,))
        row = cur.fetchone()
    return str(row["customer_id"]), order_number


def _new_conversation(customer_id: str) -> str:
    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (id, customer_id) VALUES (%s, %s)", (conv_id, customer_id))
        conn.commit()
    return conv_id


def _other_customer_id(exclude: str) -> str:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id FROM customers WHERE id != %s LIMIT 1", (exclude,))
        return str(cur.fetchone()["id"])


def _any_product_with_stock() -> tuple[str, str | None]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT sku, sizes_available, stock_by_size FROM products")
        rows = cur.fetchall()
    for r in rows:
        if r["sizes_available"]:
            for size, qty in r["stock_by_size"].items():
                if qty and qty > 0:
                    return r["sku"], size
        elif r["stock_by_size"].get("_default", 0) > 0:
            return r["sku"], None
    raise RuntimeError("no product with stock found in seed data")


# TPJ-10004 is seeded as already CANCELLED (see scripts/seed_db.py) — use it
# directly rather than cancelling something ourselves, so these tests don't
# collide with tests/test_tools.py's use of TPJ-10000.
CANCELLED_ORDER = "TPJ-10004"


def test_settlement_options_for_cancelled_order():
    customer_id, order_number = _customer_and_order(CANCELLED_ORDER)
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT total_amount_cents FROM orders WHERE order_number = %s", (order_number,))
        total = cur.fetchone()["total_amount_cents"]
        cur.execute("SELECT cancellation_bonus_percent FROM coupon_policy WHERE name = 'default'")
        bonus = float(cur.fetchone()["cancellation_bonus_percent"])

    result = coupon_tools.offer_settlement_options(customer_id, order_number)
    assert result["cash_refund_cents"] == total
    assert result["coupon_total_cents"] == round(total * (1 + bonus / 100))
    assert result["coupon_bonus_percent"] == bonus


def test_settlement_options_rejects_non_settleable_order():
    # TPJ-10002 is seeded as SHIPPED — not cancelled or returned.
    customer_id, order_number = _customer_and_order("TPJ-10002")
    result = coupon_tools.offer_settlement_options(customer_id, order_number)
    assert result["error"] == "not_settleable"


def test_issue_coupon_is_idempotent():
    customer_id, order_number = _customer_and_order(CANCELLED_ORDER)
    conv_id = _new_conversation(customer_id)

    first = coupon_tools.issue_coupon(customer_id, order_number, conv_id)
    assert first["issued"] is True

    second = coupon_tools.issue_coupon(customer_id, order_number, conv_id)
    assert second["issued"] is True
    assert second["already_existed"] is True
    assert second["coupon_code"] == first["coupon_code"]

    # DB-level check: exactly one coupon row for this order, not two.
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS c FROM coupons WHERE source_order_id = (SELECT id FROM orders WHERE order_number = %s)",
            (order_number,),
        )
        assert cur.fetchone()["c"] == 1


def test_cross_customer_redeem_rejected():
    customer_id, order_number = _customer_and_order(CANCELLED_ORDER)
    conv_id = _new_conversation(customer_id)
    issued = coupon_tools.issue_coupon(customer_id, order_number, conv_id)
    code = issued["coupon_code"]

    other_customer = _other_customer_id(exclude=customer_id)
    sku, size = _any_product_with_stock()
    victim_order = purchase_tools.place_order(other_customer, sku, quantity=1, size=size)
    assert victim_order["placed"] is True

    result = coupon_tools.redeem_coupon(other_customer, code, victim_order["order_number"])
    assert result["redeemed"] is False
    assert result["reason"] == "not_found"  # deliberately indistinguishable from a nonexistent code


def test_expired_coupon_rejected():
    customer_id, order_number = _customer_and_order(CANCELLED_ORDER)
    # Insert an already-expired coupon directly (issue_coupon always uses the
    # policy's expiry_days, so this bypasses it deliberately to exercise the
    # expiry check itself).
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id FROM orders WHERE order_number = %s", ("TPJ-10001",))
        other_order_id = cur.fetchone()["id"]
        code = "TPJ-CPN-TESTEXPIRED"
        cur.execute(
            """
            INSERT INTO coupons (code, customer_id, source_type, source_order_id,
                                  amount_cents, bonus_percent_applied, total_cents, remaining_cents, expires_at)
            VALUES (%s, %s, 'cancellation', %s, 10000, 10, 11000, 11000, %s)
            ON CONFLICT (source_order_id) WHERE source_order_id IS NOT NULL DO NOTHING
            """,
            (code, customer_id, other_order_id, datetime.now(timezone.utc) - timedelta(days=1)),
        )
        conn.commit()
        cur.execute("SELECT code FROM coupons WHERE source_order_id = %s", (other_order_id,))
        real_code = cur.fetchone()["code"]

    sku, size = _any_product_with_stock()
    order = purchase_tools.place_order(customer_id, sku, quantity=1, size=size)
    result = coupon_tools.redeem_coupon(customer_id, real_code, order["order_number"])
    assert result["redeemed"] is False
    assert result["reason"] == "expired"


def test_place_order_insufficient_stock():
    customer_id, _ = _customer_and_order(CANCELLED_ORDER)
    sku, size = _any_product_with_stock()
    result = purchase_tools.place_order(customer_id, sku, quantity=999999, size=size)
    assert result["placed"] is False
    assert result["reason"] == "insufficient_stock"


def test_place_order_and_redeem_coupon_full_flow():
    customer_id, order_number = _customer_and_order(CANCELLED_ORDER)
    conv_id = _new_conversation(customer_id)
    issued = coupon_tools.issue_coupon(customer_id, order_number, conv_id)
    code = issued["coupon_code"]
    coupon_total = issued["total_cents"]

    sku, size = _any_product_with_stock()
    order = purchase_tools.place_order(customer_id, sku, quantity=1, size=size)
    assert order["placed"] is True

    result = coupon_tools.redeem_coupon(customer_id, code, order["order_number"])
    assert result["redeemed"] is True
    expected_deduction = min(coupon_total, order["total_amount_cents"])
    assert result["discount_cents"] == expected_deduction

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT discount_cents, coupon_id FROM orders WHERE order_number = %s", (order["order_number"],)
        )
        row = cur.fetchone()
        assert row["discount_cents"] == expected_deduction
        assert row["coupon_id"] is not None

        cur.execute("SELECT remaining_cents, status FROM coupons WHERE code = %s", (code,))
        coupon_row = cur.fetchone()
        assert coupon_row["remaining_cents"] == coupon_total - expected_deduction
        if coupon_row["remaining_cents"] == 0:
            assert coupon_row["status"] == "REDEEMED"
        else:
            assert coupon_row["status"] == "ACTIVE"


def test_place_order_place_order_bad_quantity():
    customer_id, _ = _customer_and_order(CANCELLED_ORDER)
    sku, size = _any_product_with_stock()
    result = purchase_tools.place_order(customer_id, sku, quantity=0, size=size)
    assert result["placed"] is False
    assert result["reason"] == "bad_quantity"
