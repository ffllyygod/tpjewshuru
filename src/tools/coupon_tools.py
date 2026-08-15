"""Coupon-instead-of-refund tools: offer the choice, issue a coupon, record a
cash-refund request, list a customer's coupons, redeem coupon balance.

Business rationale: see JOURNAL.md. Short version — a cash refund is real
money leaving the business with no guarantee of a future sale; a store-credit
coupon is a claim on a *future in-house purchase*, structurally retaining the
customer. The bonus % (coupon_policy) is the cost of buying that retention,
and is deliberately team-editable in the DB, not hardcoded, so it can be
tuned against observed redemption/breakage economics.

Security guardrails (this touches real balances — treated accordingly):
  - `issue_coupon` re-verifies the order's status from the DB itself; it never
    trusts anything the conversation/model asserts about whether an order was
    cancelled or returned.
  - One coupon per order, enforced by `coupons.source_order_id UNIQUE` at the
    DB level — not just an application-level check that a bug could bypass.
    Calling `issue_coupon` again for an already-issued order returns the
    existing coupon rather than erroring or duplicating (idempotent).
  - `redeem_coupon` always checks `coupons.customer_id` against the caller's
    (server-injected) customer_id — a correct-but-leaked/guessed code alone
    is never sufficient. Codes themselves are `secrets.token_urlsafe`-random.
  - The balance decrement is a single atomic
    `UPDATE ... SET remaining_cents = remaining_cents - %s WHERE remaining_cents >= %s`
    statement — not a read-check-write in Python — so two concurrent
    redemption attempts against the same coupon can't both succeed and drive
    the balance negative (the DB's row lock makes it indivisible). The
    `coupons` table also has a `CHECK (remaining_cents >= 0)` constraint as a
    second line of defense.
  - No tool here accepts a caller-supplied amount/price/discount as a raw
    number — everything is computed server-side from `orders.total_amount_cents`
    and the current `coupon_policy` row.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

from src.db.connection import get_conn

SETTLEMENT_SOURCE_STATUSES = {"CANCELLED": "cancellation", "RETURNED": "return"}


def _get_policy(cur) -> dict:
    cur.execute("SELECT * FROM coupon_policy WHERE name = 'default'")
    policy = cur.fetchone()
    if not policy:
        raise RuntimeError("coupon_policy is not seeded — run scripts/seed_db.py")
    return policy


def offer_settlement_options(customer_id: str, order_number: str) -> dict:
    """Show the cash-refund vs. instant-coupon choice for an already
    cancelled/returned order. Read-only — creates nothing."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, status, total_amount_cents FROM orders WHERE order_number = %s AND customer_id = %s",
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"error": "not_found", "message": f"No order {order_number} found for this customer."}

        source_type = SETTLEMENT_SOURCE_STATUSES.get(order["status"])
        if not source_type:
            return {
                "error": "not_settleable",
                "message": f"Order is {order['status']} — settlement options only apply to a cancelled or returned order.",
            }

        # Already issued? Show the existing coupon rather than a fresh offer.
        cur.execute("SELECT code, total_cents, status FROM coupons WHERE source_order_id = %s", (order["id"],))
        existing = cur.fetchone()
        if existing:
            return {
                "already_settled": True,
                "coupon_code": existing["code"],
                "coupon_total_cents": existing["total_cents"],
                "coupon_status": existing["status"],
            }

        policy = _get_policy(cur)
        bonus_percent = policy["cancellation_bonus_percent"] if source_type == "cancellation" else policy["return_bonus_percent"]
        coupon_total_cents = round(order["total_amount_cents"] * (1 + float(bonus_percent) / 100))

    return {
        "already_settled": False,
        "source_type": source_type,
        "currency": "USD",
        "cash_refund_cents": order["total_amount_cents"],
        "cash_refund_note": "Cash refund typically takes 5-7 business days to process.",
        "coupon_total_cents": coupon_total_cents,
        "coupon_bonus_percent": float(bonus_percent),
        "coupon_note": f"Instant store credit, worth {bonus_percent}% more than the cash refund, usable on any future purchase.",
        "coupon_expiry_days": policy["expiry_days"],
    }


def issue_coupon(customer_id: str, order_number: str, conversation_id: str) -> dict:
    """Issue the store-credit coupon for a cancelled/returned order. Idempotent —
    re-calling for an already-settled order returns the existing coupon."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, status, total_amount_cents FROM orders WHERE order_number = %s AND customer_id = %s",
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"error": "not_found", "message": f"No order {order_number} found for this customer."}

        source_type = SETTLEMENT_SOURCE_STATUSES.get(order["status"])
        if not source_type:
            return {"error": "not_settleable", "message": f"Order is {order['status']} — not eligible for a coupon."}

        cur.execute("SELECT code, total_cents, expires_at, status FROM coupons WHERE source_order_id = %s", (order["id"],))
        existing = cur.fetchone()
        if existing:
            return {
                "issued": True,
                "already_existed": True,
                "coupon_code": existing["code"],
                "total_cents": existing["total_cents"],
                "expires_at": existing["expires_at"].isoformat(),
            }

        policy = _get_policy(cur)
        bonus_percent = policy["cancellation_bonus_percent"] if source_type == "cancellation" else policy["return_bonus_percent"]
        total_cents = round(order["total_amount_cents"] * (1 + float(bonus_percent) / 100))
        code = f"TPJ-CPN-{secrets.token_urlsafe(9).replace('_', '').replace('-', '').upper()[:12]}"
        expires_at = datetime.now(timezone.utc) + timedelta(days=policy["expiry_days"])

        try:
            cur.execute(
                """
                INSERT INTO coupons (code, customer_id, source_type, source_order_id,
                                      amount_cents, bonus_percent_applied, total_cents,
                                      remaining_cents, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (code, customer_id, source_type, order["id"], order["total_amount_cents"],
                 bonus_percent, total_cents, total_cents, expires_at),
            )
        except Exception:
            # Race: another concurrent call already issued one for this order
            # (source_order_id UNIQUE). Fall back to returning that one.
            conn.rollback()
            cur.execute("SELECT code, total_cents, expires_at FROM coupons WHERE source_order_id = %s", (order["id"],))
            existing = cur.fetchone()
            if existing:
                return {
                    "issued": True,
                    "already_existed": True,
                    "coupon_code": existing["code"],
                    "total_cents": existing["total_cents"],
                    "expires_at": existing["expires_at"].isoformat(),
                }
            raise
        conn.commit()

    return {
        "issued": True,
        "already_existed": False,
        "coupon_code": code,
        "currency": "USD",
        "total_cents": total_cents,
        "bonus_percent": float(bonus_percent),
        "expires_at": expires_at.isoformat(),
    }


def request_cash_refund(customer_id: str, order_number: str) -> dict:
    """Record that the customer chose a cash refund instead of a coupon. No
    real payment gateway exists — this records the decision (and the
    resulting payment_status), it doesn't move money."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, status FROM orders WHERE order_number = %s AND customer_id = %s",
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"error": "not_found", "message": f"No order {order_number} found for this customer."}
        if order["status"] not in SETTLEMENT_SOURCE_STATUSES:
            return {"error": "not_settleable", "message": f"Order is {order['status']} — not eligible for a refund."}

        cur.execute("UPDATE orders SET payment_status = 'refund_pending' WHERE id = %s", (order["id"],))
        cur.execute(
            "INSERT INTO order_status_history (order_id, from_status, to_status, reason) VALUES (%s, %s, %s, 'cash_refund_requested')",
            (order["id"], order["status"], order["status"]),
        )
        conn.commit()

    return {"requested": True, "message": "Cash refund requested — expect it in 5-7 business days."}


def get_my_coupons(customer_id: str) -> dict:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT code, status, total_cents, remaining_cents, issued_at, expires_at
            FROM coupons WHERE customer_id = %s ORDER BY issued_at DESC
            """,
            (customer_id,),
        )
        rows = cur.fetchall()

    return {
        "currency": "USD",
        "coupons": [
            {
                "code": r["code"],
                "status": r["status"],
                "total_cents": r["total_cents"],
                "remaining_cents": r["remaining_cents"],
                "issued_at": r["issued_at"].isoformat(),
                "expires_at": r["expires_at"].isoformat(),
            }
            for r in rows
        ],
    }


def redeem_coupon(customer_id: str, code: str, order_number: str) -> dict:
    """Apply coupon balance to an order's total. Deducts min(remaining, order
    total) atomically; partial use leaves the coupon ACTIVE with a lower
    balance, full use flips it to REDEEMED."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, customer_id, status, remaining_cents, expires_at FROM coupons WHERE code = %s",
            (code,),
        )
        coupon = cur.fetchone()
        if not coupon:
            return {"redeemed": False, "reason": "not_found", "message": "No coupon with that code."}
        if str(coupon["customer_id"]) != str(customer_id):
            # Deliberately the same not_found message as a missing coupon — don't
            # confirm to a caller whether a given code exists but belongs to
            # someone else.
            return {"redeemed": False, "reason": "not_found", "message": "No coupon with that code."}
        if coupon["status"] not in ("ACTIVE",):
            return {"redeemed": False, "reason": "not_active", "message": f"Coupon is {coupon['status'].lower()}, not usable."}
        if coupon["expires_at"] < datetime.now(timezone.utc):
            return {"redeemed": False, "reason": "expired", "message": "This coupon has expired."}
        if coupon["remaining_cents"] <= 0:
            return {"redeemed": False, "reason": "exhausted", "message": "This coupon has no remaining balance."}

        cur.execute(
            "SELECT id, total_amount_cents, discount_cents, coupon_id FROM orders WHERE order_number = %s AND customer_id = %s",
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"redeemed": False, "reason": "order_not_found", "message": f"No order {order_number} found for this customer."}
        if order["coupon_id"] is not None:
            return {"redeemed": False, "reason": "order_already_discounted", "message": "This order already has a coupon applied."}

        deduction = min(coupon["remaining_cents"], order["total_amount_cents"])

        # Atomic, guarded decrement — the WHERE clause re-checks remaining_cents
        # at write time, not just at the SELECT above, so a concurrent redemption
        # of the same coupon can't both succeed past the available balance.
        cur.execute(
            """
            UPDATE coupons
            SET remaining_cents = remaining_cents - %s,
                status = CASE WHEN remaining_cents - %s <= 0 THEN 'REDEEMED' ELSE status END
            WHERE id = %s AND remaining_cents >= %s
            RETURNING remaining_cents, status
            """,
            (deduction, deduction, coupon["id"], deduction),
        )
        updated = cur.fetchone()
        if not updated:
            # Balance changed under us between the SELECT and here (concurrent
            # redemption elsewhere) — fail closed rather than over-apply.
            conn.rollback()
            return {"redeemed": False, "reason": "balance_changed", "message": "Coupon balance changed — please try again."}

        cur.execute(
            "UPDATE orders SET coupon_id = %s, discount_cents = %s WHERE id = %s",
            (coupon["id"], deduction, order["id"]),
        )
        conn.commit()

    return {
        "redeemed": True,
        "currency": "USD",
        "order_number": order_number,
        "discount_cents": deduction,
        "coupon_remaining_cents": updated["remaining_cents"],
        "coupon_status": updated["status"],
    }
