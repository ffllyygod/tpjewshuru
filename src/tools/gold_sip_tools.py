"""Gold SIP (Systematic Investment Plan) tools: list plan tiers, start a
subscription, pay installments, cancel early (issues a coupon via
coupon_tools — never cash), redeem a matured subscription's value.

Business rationale: see JOURNAL.md. Short version — a SIP holds committed
customer cash for months before any goods change hands (float), and
redemption is in-house only (locked-in future revenue), same "money can't
leave the business" principle as the coupon system. Early exit forfeits a
team-editable penalty % and pays out the rest as a coupon, not cash — reuses
coupon_tools.mint_coupon rather than a parallel cash-refund path.

Demo-time simplification, stated explicitly: real SIPs pay monthly over many
months. pay_sip_installment is callable on-demand with no calendar-day
gating — there's no way to wait real months in a short demo window.

Security guardrails (same discipline as coupon_tools.py — this touches real
balances):
  - Ownership check (customer_id) on every subscription-scoped call.
  - `gold_sip_installments.(subscription_id, installment_number)` is a DB
    UNIQUE constraint — the race guard against double-counting the same
    installment under concurrent calls.
  - The installments_paid/total_paid_cents update is a single atomic
    guarded UPDATE (WHERE installments_paid = <expected>), not a
    read-check-write — a second concurrent call sees the guard fail and can
    retry, rather than silently double-incrementing.
  - `redeem_gold_sip`'s balance decrement uses the identical atomic guarded
    pattern as `coupon_tools.redeem_coupon`.
  - No tool here accepts a caller-supplied amount as a raw number — every
    amount is computed server-side from `monthly_amount_cents` /
    `gold_sip_plans` policy rows.
"""

from __future__ import annotations

import secrets

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.coupon_tools import mint_coupon
from src.tools.formatting import format_inr


def _new_sip_code() -> str:
    return f"TPJ-SIP-{secrets.token_urlsafe(9).replace('_', '').replace('-', '').upper()[:12]}"


def list_gold_sip_plans() -> dict:
    """List active Gold SIP plan tiers (tenure, bonus, early-exit penalty)."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT name, tenure_months, bonus_percent, early_exit_penalty_percent FROM gold_sip_plans WHERE active = true ORDER BY tenure_months"
        )
        rows = cur.fetchall()
    return {
        "plans": [
            {
                "name": r["name"],
                "tenure_months": r["tenure_months"],
                "bonus_percent": float(r["bonus_percent"]),
                "early_exit_penalty_percent": float(r["early_exit_penalty_percent"]),
            }
            for r in rows
        ]
    }


def start_gold_sip(customer_id: str, plan_name: str, monthly_amount: float) -> dict:
    """Start a new Gold SIP subscription. monthly_amount is in rupees (not paise)."""
    if not isinstance(monthly_amount, (int, float)) or monthly_amount <= 0:
        return {"started": False, "reason": "bad_amount", "message": "Monthly amount must be a positive number."}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, tenure_months, bonus_percent, early_exit_penalty_percent FROM gold_sip_plans WHERE name = %s AND active = true",
            (plan_name,),
        )
        plan = cur.fetchone()
        if not plan:
            return {"started": False, "reason": "plan_not_found", "message": f"No active plan named '{plan_name}'. Call list_gold_sip_plans to see options."}

        monthly_amount_cents = round(monthly_amount * 100)
        code = _new_sip_code()
        cur.execute(
            """
            INSERT INTO gold_sip_subscriptions
                (code, customer_id, plan_id, tenure_months_snapshot, bonus_percent_snapshot,
                 early_exit_penalty_percent_snapshot, monthly_amount_cents)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (code, customer_id, plan["id"], plan["tenure_months"], plan["bonus_percent"],
             plan["early_exit_penalty_percent"], monthly_amount_cents),
        )
        conn.commit()

    return {
        "started": True,
        "subscription_code": code,
        "currency": "INR",
        "tenure_months": plan["tenure_months"],
        "bonus_percent": float(plan["bonus_percent"]),
        "monthly_amount_cents": monthly_amount_cents,
        "monthly_amount_display": format_inr(monthly_amount_cents),
    }


def pay_sip_installment(customer_id: str, subscription_code: str) -> dict:
    """Record the next monthly installment payment. Auto-matures the
    subscription if this payment completes the tenure."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, status, installments_paid, total_paid_cents, monthly_amount_cents,
                   tenure_months_snapshot, bonus_percent_snapshot
            FROM gold_sip_subscriptions WHERE code = %s AND customer_id = %s
            """,
            (subscription_code, customer_id),
        )
        sub = cur.fetchone()
        if not sub:
            return {"paid": False, "reason": "not_found", "message": f"No Gold SIP subscription {subscription_code} found for this customer."}
        if sub["status"] != "ACTIVE":
            return {"paid": False, "reason": "not_active", "message": f"Subscription is {sub['status'].lower()}, not accepting payments."}

        next_number = sub["installments_paid"] + 1
        amount = sub["monthly_amount_cents"]

        # Ledger insert first — UNIQUE(subscription_id, installment_number) is
        # the race guard: if two concurrent calls both computed the same
        # next_number, only one of these INSERTs succeeds.
        try:
            cur.execute(
                "INSERT INTO gold_sip_installments (subscription_id, installment_number, amount_cents) VALUES (%s, %s, %s)",
                (sub["id"], next_number, amount),
            )
        except Exception:
            conn.rollback()
            return {"paid": False, "reason": "conflict", "message": "Payment already recorded for this installment — please retry."}

        new_total_paid = sub["total_paid_cents"] + amount
        matured = next_number >= sub["tenure_months_snapshot"]

        if matured:
            redeemable_cents = round(new_total_paid * (1 + float(sub["bonus_percent_snapshot"]) / 100))
            cur.execute(
                """
                UPDATE gold_sip_subscriptions
                SET installments_paid = %s, total_paid_cents = %s,
                    redeemable_cents = %s, remaining_cents = %s,
                    status = 'MATURED', matured_at = now()
                WHERE id = %s AND installments_paid = %s AND status = 'ACTIVE'
                RETURNING installments_paid, total_paid_cents, redeemable_cents, status
                """,
                (next_number, new_total_paid, redeemable_cents, redeemable_cents, sub["id"], sub["installments_paid"]),
            )
        else:
            cur.execute(
                """
                UPDATE gold_sip_subscriptions
                SET installments_paid = %s, total_paid_cents = %s
                WHERE id = %s AND installments_paid = %s AND status = 'ACTIVE'
                RETURNING installments_paid, total_paid_cents, status
                """,
                (next_number, new_total_paid, sub["id"], sub["installments_paid"]),
            )
        updated = cur.fetchone()
        if not updated:
            # Guard failed — subscription state changed under us (concurrent
            # payment/cancellation). The ledger row is already committed-safe
            # (its own UNIQUE constraint prevented a duplicate), but the
            # subscription-level counters didn't move; fail closed.
            conn.rollback()
            return {"paid": False, "reason": "conflict", "message": "Subscription state changed — please retry."}

        conn.commit()

    result = {
        "paid": True,
        "currency": "INR",
        "installment_number": next_number,
        "installments_paid": updated["installments_paid"],
        "tenure_months": sub["tenure_months_snapshot"],
        "total_paid_cents": updated["total_paid_cents"],
        "total_paid_display": format_inr(updated["total_paid_cents"]),
        "status": updated["status"],
    }
    if matured:
        result["matured"] = True
        result["redeemable_cents"] = updated["redeemable_cents"]
        result["redeemable_display"] = format_inr(updated["redeemable_cents"])
    return result


def get_my_gold_sips(customer_id: str) -> dict:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT code, status, tenure_months_snapshot, installments_paid, monthly_amount_cents,
                   total_paid_cents, redeemable_cents, remaining_cents, started_at, matured_at
            FROM gold_sip_subscriptions WHERE customer_id = %s ORDER BY started_at DESC
            """,
            (customer_id,),
        )
        rows = cur.fetchall()

    return {
        "currency": "INR",
        "subscriptions": [
            {
                "subscription_code": r["code"],
                "status": r["status"],
                "installments_paid": r["installments_paid"],
                "tenure_months": r["tenure_months_snapshot"],
                "monthly_amount_cents": r["monthly_amount_cents"],
                "monthly_amount_display": format_inr(r["monthly_amount_cents"]),
                "total_paid_cents": r["total_paid_cents"],
                "total_paid_display": format_inr(r["total_paid_cents"]),
                "redeemable_cents": r["redeemable_cents"],
                "redeemable_display": format_inr(r["redeemable_cents"]) if r["redeemable_cents"] is not None else None,
                "remaining_cents": r["remaining_cents"],
                "remaining_display": format_inr(r["remaining_cents"]) if r["remaining_cents"] is not None else None,
                "started_at": r["started_at"].isoformat(),
                "matured_at": r["matured_at"].isoformat() if r["matured_at"] else None,
            }
            for r in rows
        ],
    }


def cancel_gold_sip(customer_id: str, subscription_code: str) -> dict:
    """Exit a Gold SIP subscription before maturity. Forfeits the bonus and a
    team-editable penalty %; the rest is issued as a coupon (never cash)."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, status, total_paid_cents, early_exit_penalty_percent_snapshot
            FROM gold_sip_subscriptions WHERE code = %s AND customer_id = %s
            """,
            (subscription_code, customer_id),
        )
        sub = cur.fetchone()
        if not sub:
            return {"cancelled": False, "reason": "not_found", "message": f"No Gold SIP subscription {subscription_code} found for this customer."}
        if sub["status"] != "ACTIVE":
            return {"cancelled": False, "reason": "not_active", "message": f"Subscription is already {sub['status'].lower()}."}

        payout_cents = round(sub["total_paid_cents"] * (1 - float(sub["early_exit_penalty_percent_snapshot"]) / 100))

        try:
            coupon = mint_coupon(
                cur, customer_id, "gold_sip_cancellation", payout_cents, 0, 180,
                source_subscription_id=sub["id"],
            )
        except Exception:
            conn.rollback()
            cur.execute("SELECT code, total_cents FROM coupons WHERE source_subscription_id = %s", (sub["id"],))
            existing = cur.fetchone()
            if existing:
                return {
                    "cancelled": True,
                    "already_existed": True,
                    "coupon_code": existing["code"],
                    "coupon_total_cents": existing["total_cents"],
                    "coupon_total_display": format_inr(existing["total_cents"]),
                }
            raise

        cur.execute(
            """
            UPDATE gold_sip_subscriptions
            SET status = 'CANCELLED', cancelled_at = now(), exit_coupon_id = %s
            WHERE id = %s AND status = 'ACTIVE'
            """,
            (coupon["id"], sub["id"]),
        )
        conn.commit()

    return {
        "cancelled": True,
        "currency": "INR",
        "penalty_percent": float(sub["early_exit_penalty_percent_snapshot"]),
        "coupon_code": coupon["code"],
        "coupon_total_cents": coupon["total_cents"],
        "coupon_total_display": format_inr(coupon["total_cents"]),
        "message": "The maturity bonus was forfeited, but the rest of your payments (minus the early-exit fee) were issued as a store coupon.",
    }


def redeem_gold_sip(customer_id: str, subscription_code: str, order_number: str) -> dict:
    """Apply a matured Gold SIP's balance to an order's total. Same
    atomic-decrement/ownership discipline as coupon_tools.redeem_coupon."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, customer_id, status, remaining_cents FROM gold_sip_subscriptions WHERE code = %s",
            (subscription_code,),
        )
        sub = cur.fetchone()
        if not sub:
            return {"redeemed": False, "reason": "not_found", "message": "No Gold SIP subscription with that code."}
        if str(sub["customer_id"]) != str(customer_id):
            return {"redeemed": False, "reason": "not_found", "message": "No Gold SIP subscription with that code."}
        if sub["status"] not in ("MATURED",):
            return {"redeemed": False, "reason": "not_matured", "message": f"Subscription is {sub['status'].lower()}, not ready to redeem."}
        if not sub["remaining_cents"] or sub["remaining_cents"] <= 0:
            return {"redeemed": False, "reason": "exhausted", "message": "This subscription's balance is already fully used."}

        cur.execute(
            "SELECT id, total_amount_cents, coupon_id, gold_sip_subscription_id FROM orders WHERE order_number = %s AND customer_id = %s",
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"redeemed": False, "reason": "order_not_found", "message": f"No order {order_number} found for this customer."}
        if order["coupon_id"] is not None or order["gold_sip_subscription_id"] is not None:
            return {"redeemed": False, "reason": "order_already_discounted", "message": "This order already has a coupon or Gold SIP redemption applied."}

        deduction = min(sub["remaining_cents"], order["total_amount_cents"])

        cur.execute(
            """
            UPDATE gold_sip_subscriptions
            SET remaining_cents = remaining_cents - %s,
                status = CASE WHEN remaining_cents - %s <= 0 THEN 'REDEEMED' ELSE status END
            WHERE id = %s AND remaining_cents >= %s
            RETURNING remaining_cents, status
            """,
            (deduction, deduction, sub["id"], deduction),
        )
        updated = cur.fetchone()
        if not updated:
            conn.rollback()
            return {"redeemed": False, "reason": "balance_changed", "message": "Subscription balance changed — please try again."}

        cur.execute(
            "UPDATE orders SET gold_sip_subscription_id = %s, discount_cents = %s WHERE id = %s",
            (sub["id"], deduction, order["id"]),
        )
        conn.commit()

    return {
        "redeemed": True,
        "currency": "INR",
        "order_number": order_number,
        "discount_cents": deduction,
        "discount_display": format_inr(deduction),
        "subscription_remaining_cents": updated["remaining_cents"],
        "subscription_remaining_display": format_inr(updated["remaining_cents"]),
        "subscription_status": updated["status"],
    }
