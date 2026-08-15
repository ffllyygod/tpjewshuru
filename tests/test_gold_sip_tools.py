"""Direct tests of the Gold SIP tool functions against the live seeded DB —
no LLM involved. Same style/discipline as tests/test_coupon_tools.py.
"""

from __future__ import annotations

import uuid

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools import gold_sip_tools, purchase_tools


def _any_customer_id() -> str:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id FROM customers LIMIT 1")
        return str(cur.fetchone()["id"])


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


def test_start_gold_sip_snapshots_plan_terms():
    customer_id = _any_customer_id()
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT tenure_months, bonus_percent FROM gold_sip_plans WHERE name = 'Classic 6-Month'")
        plan = cur.fetchone()

    result = gold_sip_tools.start_gold_sip(customer_id, "Classic 6-Month", 5000)
    assert result["started"] is True
    assert result["tenure_months"] == plan["tenure_months"]
    assert result["bonus_percent"] == float(plan["bonus_percent"])
    assert result["monthly_amount_cents"] == 500_000  # 5000 rupees -> paise


def test_start_gold_sip_unknown_plan_rejected():
    customer_id = _any_customer_id()
    result = gold_sip_tools.start_gold_sip(customer_id, "not-a-real-plan", 5000)
    assert result["started"] is False
    assert result["reason"] == "plan_not_found"


def test_pay_sip_installment_accumulates_and_matures():
    customer_id = _any_customer_id()
    started = gold_sip_tools.start_gold_sip(customer_id, "Classic 6-Month", 5000)
    code = started["subscription_code"]
    tenure = started["tenure_months"]

    last = None
    for i in range(1, tenure + 1):
        last = gold_sip_tools.pay_sip_installment(customer_id, code)
        assert last["paid"] is True
        assert last["installment_number"] == i
        assert last["installments_paid"] == i

    assert last["matured"] is True
    expected_redeemable = round(500_000 * tenure * (1 + started["bonus_percent"] / 100))
    assert last["redeemable_cents"] == expected_redeemable

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status, remaining_cents, redeemable_cents FROM gold_sip_subscriptions WHERE code = %s", (code,))
        row = cur.fetchone()
        assert row["status"] == "MATURED"
        assert row["remaining_cents"] == expected_redeemable
        assert row["redeemable_cents"] == expected_redeemable


def test_pay_sip_installment_rejects_wrong_customer():
    customer_id = _any_customer_id()
    other = _other_customer_id(exclude=customer_id)
    started = gold_sip_tools.start_gold_sip(customer_id, "Classic 6-Month", 5000)

    result = gold_sip_tools.pay_sip_installment(other, started["subscription_code"])
    assert result["paid"] is False
    assert result["reason"] == "not_found"


def test_cancel_gold_sip_issues_penalty_adjusted_coupon():
    customer_id = _any_customer_id()
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT early_exit_penalty_percent FROM gold_sip_plans WHERE name = 'Standard 11-Month'")
        penalty = float(cur.fetchone()["early_exit_penalty_percent"])

    started = gold_sip_tools.start_gold_sip(customer_id, "Standard 11-Month", 3000)
    code = started["subscription_code"]
    gold_sip_tools.pay_sip_installment(customer_id, code)
    gold_sip_tools.pay_sip_installment(customer_id, code)  # 2 installments paid, not matured

    total_paid = 300_000 * 2
    expected_payout = round(total_paid * (1 - penalty / 100))

    result = gold_sip_tools.cancel_gold_sip(customer_id, code)
    assert result["cancelled"] is True
    assert result["coupon_total_cents"] == expected_payout
    assert result["penalty_percent"] == penalty

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT bonus_percent_applied, total_cents FROM coupons WHERE code = %s", (result["coupon_code"],))
        coupon_row = cur.fetchone()
        assert float(coupon_row["bonus_percent_applied"]) == 0
        assert coupon_row["total_cents"] == expected_payout

        cur.execute("SELECT status, exit_coupon_id FROM gold_sip_subscriptions WHERE code = %s", (code,))
        sub_row = cur.fetchone()
        assert sub_row["status"] == "CANCELLED"
        assert sub_row["exit_coupon_id"] is not None

    # Cancelling an already-cancelled subscription is rejected, not a silent no-op.
    second = gold_sip_tools.cancel_gold_sip(customer_id, code)
    assert second["cancelled"] is False
    assert second["reason"] == "not_active"


def test_redeem_gold_sip_cross_customer_rejected():
    customer_id = _any_customer_id()
    other = _other_customer_id(exclude=customer_id)

    started = gold_sip_tools.start_gold_sip(customer_id, "Classic 6-Month", 5000)
    code = started["subscription_code"]
    for _ in range(started["tenure_months"]):
        gold_sip_tools.pay_sip_installment(customer_id, code)

    sku, size = _any_product_with_stock()
    victim_order = purchase_tools.place_order(other, sku, quantity=1, size=size)
    assert victim_order["placed"] is True

    result = gold_sip_tools.redeem_gold_sip(other, code, victim_order["order_number"])
    assert result["redeemed"] is False
    assert result["reason"] == "not_found"


def test_redeem_gold_sip_before_maturity_rejected():
    customer_id = _any_customer_id()
    started = gold_sip_tools.start_gold_sip(customer_id, "Premium 12-Month", 4000)
    code = started["subscription_code"]
    gold_sip_tools.pay_sip_installment(customer_id, code)  # not matured yet

    sku, size = _any_product_with_stock()
    order = purchase_tools.place_order(customer_id, sku, quantity=1, size=size)

    result = gold_sip_tools.redeem_gold_sip(customer_id, code, order["order_number"])
    assert result["redeemed"] is False
    assert result["reason"] == "not_matured"


def test_place_order_with_gold_sip_full_flow():
    customer_id = _any_customer_id()
    started = gold_sip_tools.start_gold_sip(customer_id, "Classic 6-Month", 20000)
    code = started["subscription_code"]
    last = None
    for _ in range(started["tenure_months"]):
        last = gold_sip_tools.pay_sip_installment(customer_id, code)
    redeemable = last["redeemable_cents"]

    sku, size = _any_product_with_stock()
    order = purchase_tools.place_order(customer_id, sku, quantity=1, size=size, gold_sip_code=code)
    assert order["placed"] is True
    assert order["gold_sip_applied"] is True

    expected_deduction = min(redeemable, order["total_amount_cents"])
    assert order["discount_cents"] == expected_deduction

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT discount_cents, gold_sip_subscription_id FROM orders WHERE order_number = %s", (order["order_number"],)
        )
        order_row = cur.fetchone()
        assert order_row["discount_cents"] == expected_deduction
        assert order_row["gold_sip_subscription_id"] is not None

        cur.execute("SELECT remaining_cents, status FROM gold_sip_subscriptions WHERE code = %s", (code,))
        sub_row = cur.fetchone()
        assert sub_row["remaining_cents"] == redeemable - expected_deduction
        if sub_row["remaining_cents"] == 0:
            assert sub_row["status"] == "REDEEMED"
        else:
            assert sub_row["status"] == "MATURED"


def test_place_order_rejects_both_coupon_and_gold_sip():
    customer_id = _any_customer_id()
    sku, size = _any_product_with_stock()
    order = purchase_tools.place_order(
        customer_id, sku, quantity=1, size=size, coupon_code="TPJ-CPN-FAKE", gold_sip_code="TPJ-SIP-FAKE"
    )
    assert order["placed"] is True  # order itself still succeeds
    assert "discount_error" in order
