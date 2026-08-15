"""Order-related tools: lookup, status, cancellation-eligibility, cancellation.

Guardrail design (see docs/ARCHITECTURE.md):
  - Every function here takes `customer_id` explicitly and re-checks that the
    order actually belongs to that customer. The agent never gets to pick an
    arbitrary order_id without that ownership check happening server-side.
  - Cancellation is a two-step protocol: `check_cancellation_eligibility`
    issues a short-lived confirmation token; `cancel_order` requires that
    exact token. The agent cannot cancel an order in one shot, and cannot
    forge a token — it's random, single-use, and tied to (order, action).
  - Every tool call that touches an order is written to tool_call_log by the
    orchestrator (not here) so there's a full audit trail.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

from src.db.connection import get_conn

CANCELLATION_WINDOW_HOURS = 24
CANCELLABLE_FROM_STATUSES = {"PLACED", "CONFIRMED"}
TOKEN_TTL_MINUTES = 10


def _row_to_order_summary(row: dict) -> dict:
    return {
        "order_number": row["order_number"],
        "status": row["status"],
        "placed_at": row["placed_at"].isoformat() if row["placed_at"] else None,
        "total_amount_cents": row["total_amount_cents"],
        "payment_status": row["payment_status"],
    }


def list_customer_orders(customer_id: str, limit: int = 10) -> dict:
    """List recent orders for a customer (ownership is implicit: only their own)."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT order_number, status, placed_at, total_amount_cents, payment_status
            FROM orders
            WHERE customer_id = %s
            ORDER BY placed_at DESC
            LIMIT %s
            """,
            (customer_id, limit),
        )
        rows = cur.fetchall()
    return {"orders": [_row_to_order_summary(r) for r in rows]}


def get_order_status(customer_id: str, order_number: str) -> dict:
    """Get full status + item detail for one order, scoped to the caller's customer_id."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, order_number, status, placed_at, shipped_at, delivered_at,
                   total_amount_cents, payment_status, shipping_address
            FROM orders
            WHERE order_number = %s AND customer_id = %s
            """,
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"error": "not_found", "message": f"No order {order_number} found for this customer."}

        cur.execute(
            """
            SELECT p.name, p.sku, oi.quantity, oi.unit_price_cents, oi.size
            FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = %s
            """,
            (order["id"],),
        )
        items = cur.fetchall()

    return {
        "order_number": order["order_number"],
        "status": order["status"],
        "placed_at": order["placed_at"].isoformat() if order["placed_at"] else None,
        "shipped_at": order["shipped_at"].isoformat() if order["shipped_at"] else None,
        "delivered_at": order["delivered_at"].isoformat() if order["delivered_at"] else None,
        "total_amount_cents": order["total_amount_cents"],
        "payment_status": order["payment_status"],
        "items": [
            {
                "name": i["name"],
                "sku": i["sku"],
                "quantity": i["quantity"],
                "unit_price_cents": i["unit_price_cents"],
                "size": i["size"],
            }
            for i in items
        ],
    }


def check_cancellation_eligibility(customer_id: str, order_number: str, conversation_id: str) -> dict:
    """Determine whether an order can be cancelled, and if so issue a confirmation token.

    This MUST be called before cancel_order — cancel_order will reject any
    call that doesn't present a valid, unexpired token from this function.
    The token is scoped to (conversation, action, order): it can't be replayed
    from a different conversation even if somehow leaked.
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, status, placed_at
            FROM orders
            WHERE order_number = %s AND customer_id = %s
            """,
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"eligible": False, "reason": "not_found", "message": f"No order {order_number} found for this customer."}

        if order["status"] not in CANCELLABLE_FROM_STATUSES:
            return {
                "eligible": False,
                "reason": "wrong_status",
                "message": f"Order is {order['status']} and can no longer be cancelled.",
            }

        placed_at = order["placed_at"]
        if placed_at.tzinfo is None:
            placed_at = placed_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - placed_at
        if age > timedelta(hours=CANCELLATION_WINDOW_HOURS):
            return {
                "eligible": False,
                "reason": "window_expired",
                "message": f"Order was placed more than {CANCELLATION_WINDOW_HOURS}h ago and is outside the cancellation window.",
            }

        # Eligible — issue a single-use confirmation token.
        token = secrets.token_urlsafe(16)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)
        cur.execute(
            """
            INSERT INTO confirmation_tokens (conversation_id, action, target_id, token, expires_at)
            VALUES (%s, 'cancel_order', %s, %s, %s)
            """,
            (conversation_id, order["id"], token, expires_at),
        )
        conn.commit()

    return {
        "eligible": True,
        "confirmation_token": token,
        "expires_in_minutes": TOKEN_TTL_MINUTES,
        "message": (
            "Order is eligible for cancellation. Ask the customer to explicitly "
            "confirm before calling cancel_order with this token."
        ),
    }


def cancel_order(customer_id: str, order_number: str, conversation_id: str) -> dict:
    """Cancel an order. Requires check_cancellation_eligibility to have already been
    called for this exact order in this exact conversation (that's what issues the
    confirmation token) — the model doesn't need to (and can't be trusted to
    perfectly) transcribe the token itself; this looks up the most recent valid,
    unused, unexpired one for (conversation, order, cancel_order) automatically.

    Re-validates ownership, status, and window again server-side regardless —
    the token proves the eligibility check happened, it doesn't skip
    re-verification.
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id, status, placed_at
            FROM orders
            WHERE order_number = %s AND customer_id = %s
            """,
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"cancelled": False, "reason": "not_found", "message": f"No order {order_number} found for this customer."}

        cur.execute(
            """
            SELECT id, expires_at, used_at
            FROM confirmation_tokens
            WHERE action = 'cancel_order' AND target_id = %s AND conversation_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (order["id"], conversation_id),
        )
        tok = cur.fetchone()
        if not tok:
            return {"cancelled": False, "reason": "no_pending_confirmation", "message": "No cancellation was set up for this order in this conversation — call check_cancellation_eligibility first."}
        if tok["used_at"] is not None:
            return {"cancelled": False, "reason": "token_used", "message": "This order's cancellation was already processed."}
        if tok["expires_at"] < datetime.now(timezone.utc):
            return {"cancelled": False, "reason": "token_expired", "message": "Confirmation expired — re-check eligibility."}

        if order["status"] not in CANCELLABLE_FROM_STATUSES:
            return {"cancelled": False, "reason": "wrong_status", "message": f"Order is now {order['status']} and can no longer be cancelled."}

        placed_at = order["placed_at"]
        if placed_at.tzinfo is None:
            placed_at = placed_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - placed_at > timedelta(hours=CANCELLATION_WINDOW_HOURS):
            return {"cancelled": False, "reason": "window_expired", "message": "Cancellation window has since expired."}

        old_status = order["status"]
        cur.execute("UPDATE orders SET status = 'CANCELLED' WHERE id = %s", (order["id"],))
        cur.execute(
            """
            INSERT INTO order_status_history (order_id, from_status, to_status, reason)
            VALUES (%s, %s, 'CANCELLED', 'customer_requested')
            """,
            (order["id"], old_status),
        )
        cur.execute("UPDATE confirmation_tokens SET used_at = now() WHERE id = %s", (tok["id"],))
        conn.commit()

    return {"cancelled": True, "order_number": order_number, "message": "Order cancelled successfully."}
