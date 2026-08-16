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
from src.tools.formatting import format_inr

CANCELLATION_WINDOW_HOURS = 24
CANCELLABLE_FROM_STATUSES = {"PLACED", "CONFIRMED"}
TOKEN_TTL_MINUTES = 10

# From the seeded "Return Policy" knowledge doc: 30 days from delivery. Keep the
# two in step — the assistant quotes that doc to customers, so a mismatch means
# it states one rule and enforces another.
RETURN_WINDOW_DAYS = 30
RETURNABLE_FROM_STATUSES = {"DELIVERED"}


def _restock_order_items(cur, order_id: str) -> list[dict]:
    """Put an order's items back into stock. Inverse of the decrement in
    purchase_tools.place_order.

    This did not exist before returns were built, which meant cancellation was
    a one-way ratchet on inventory: place_order took stock out, nothing ever put
    it back, and every cancelled order permanently shrank the catalogue's
    on-hand count. Called inside the same transaction as the status change, so
    the caller's token and status re-checks already make it exactly-once.

    Uses the same '_default' key convention as place_order for unsized products,
    and only touches keys that already exist — a size that has since been
    retired from stock_by_size shouldn't be resurrected by a return.
    """
    cur.execute(
        """
        SELECT oi.product_id, oi.quantity, COALESCE(oi.size, '_default') AS size_key
        FROM order_items oi WHERE oi.order_id = %s
        """,
        (order_id,),
    )
    restocked = []
    for item in cur.fetchall():
        cur.execute(
            """
            UPDATE products
            SET stock_by_size = jsonb_set(
                    stock_by_size, ARRAY[%s],
                    to_jsonb(COALESCE((stock_by_size->>%s)::int, 0) + %s)
                ),
                updated_at = now()
            WHERE id = %s AND stock_by_size ? %s
            RETURNING sku
            """,
            (item["size_key"], item["size_key"], item["quantity"], item["product_id"], item["size_key"]),
        )
        row = cur.fetchone()
        if row:
            restocked.append({"sku": row["sku"], "size": item["size_key"], "quantity": item["quantity"]})
    return restocked


def _row_to_order_summary(row: dict) -> dict:
    return {
        "order_number": row["order_number"],
        "status": row["status"],
        "placed_at": row["placed_at"].isoformat() if row["placed_at"] else None,
        "total_amount_cents": row["total_amount_cents"],
        "total_amount_display": format_inr(row["total_amount_cents"]),
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
        "total_amount_display": format_inr(order["total_amount_cents"]),
        "payment_status": order["payment_status"],
        "items": [
            {
                "name": i["name"],
                "sku": i["sku"],
                "quantity": i["quantity"],
                "unit_price_cents": i["unit_price_cents"],
                "unit_price_display": format_inr(i["unit_price_cents"]),
                "size": i["size"],
            }
            for i in items
        ],
    }


def list_resolution_reasons(kind: str = "cancellation", audience: str = "customer") -> dict:
    """The reasons a cancellation or return can be attributed to.

    Read from the DB rather than hardcoded so the team can retune the pick-list
    without a deploy — same principle as coupon_policy. `audience` filters out
    the options that would make no sense to the caller: a customer should never
    be offered "suspected fraudulent order".
    """
    audience = audience if audience in ("customer", "staff") else "customer"
    if kind not in ("cancellation", "return"):
        return {"error": "bad_kind", "message": "kind must be 'cancellation' or 'return'."}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT code, label, requires_note
            FROM resolution_reasons
            WHERE kind = %s AND active = true AND applies_to IN (%s, 'both')
            ORDER BY sort_order, label
            """,
            (kind, audience),
        )
        rows = cur.fetchall()

    return {
        "kind": kind,
        "audience": audience,
        "reasons": [
            {"code": r["code"], "label": r["label"], "requires_note": r["requires_note"]}
            for r in rows
        ],
        "note": (
            "Offer these as options in plain language. If none fit, use code 'other' and put "
            "what they actually said in reason_note — never invent a code that isn't listed here."
        ),
    }


def resolve_reason(cur, kind: str, reason_code: str | None, reason_note: str | None, audience: str) -> tuple[str | None, str | None, dict | None]:
    """Validate a (code, note) pair against the pick-list.

    Returns (code, note, error). Shared by the customer and staff cancellation
    paths so they can't drift on what counts as a valid reason — and so neither
    can quietly write a code that isn't in the table, which the FK would reject
    at commit time with a message no model could act on.
    """
    code = (reason_code or "").strip().lower()
    note = (reason_note or "").strip() or None
    if not code:
        return None, None, {
            "error": "reason_required",
            "message": (
                f"A {kind} reason is required. Call list_resolution_reasons, ask which one "
                "applies, and pass its code."
            ),
        }

    cur.execute(
        """
        SELECT code, label, requires_note FROM resolution_reasons
        WHERE code = %s AND kind = %s AND active = true AND applies_to IN (%s, 'both')
        """,
        (code, kind, audience),
    )
    row = cur.fetchone()
    if not row:
        cur.execute(
            "SELECT code FROM resolution_reasons WHERE kind = %s AND active = true AND applies_to IN (%s, 'both') ORDER BY sort_order",
            (kind, audience),
        )
        valid = [r["code"] for r in cur.fetchall()]
        return None, None, {
            "error": "bad_reason_code",
            "message": f"Unknown {kind} reason code '{reason_code}'. Valid codes: {', '.join(valid)}.",
        }

    if row["requires_note"] and not note:
        return None, None, {
            "error": "reason_note_required",
            "message": (
                f"Reason '{row['label']}' needs reason_note explaining what the actual reason was — "
                "recording it without one says no more than recording nothing."
            ),
        }
    return code, note, None


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


def cancel_order(
    customer_id: str,
    order_number: str,
    conversation_id: str,
    reason_code: str | None = None,
    reason_note: str | None = None,
) -> dict:
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
            SELECT id, status, placed_at, payment_status
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

        # Validated before anything is written, so a missing or bogus reason
        # costs nothing — the order stays PLACED and the token stays unused.
        code, note, err = resolve_reason(cur, "cancellation", reason_code, reason_note, "customer")
        if err:
            return {"cancelled": False, "reason": err["error"], "message": err["message"]}

        old_status = order["status"]
        cur.execute(
            """
            UPDATE orders
            SET status = 'CANCELLED', cancellation_reason_code = %s, cancellation_reason_note = %s
            WHERE id = %s
            """,
            (code, note, order["id"]),
        )
        restocked = _restock_order_items(cur, order["id"])
        cur.execute(
            """
            INSERT INTO order_status_history (order_id, from_status, to_status, reason)
            VALUES (%s, %s, 'CANCELLED', %s)
            """,
            (order["id"], old_status, f"customer_requested:{code}"),
        )
        cur.execute("UPDATE confirmation_tokens SET used_at = now() WHERE id = %s", (tok["id"],))
        conn.commit()

    return {
        "cancelled": True,
        "order_number": order_number,
        "reason_code": code,
        "reason_note": note,
        "restocked_items": restocked,
        "message": "Order cancelled successfully.",
        # Whether money was taken is a fact this function already has, and it
        # decides what happens next. Saying so here beats a prompt rule the
        # model can skip: live testing caught it cancelling an unpaid order and
        # then simply not mentioning settlement either way.
        "next_step": _settlement_next_step(order["payment_status"]),
    }


def _settlement_next_step(payment_status: str) -> str:
    if payment_status == "PENDING":
        return (
            "No payment was ever taken for this order. There is nothing to refund and no store "
            "credit to issue — do NOT call offer_settlement_options. Tell the customer plainly "
            "that they were never charged."
        )
    return (
        "Call offer_settlement_options NOW, in this same turn, and present both the cash refund "
        "and the coupon before asking which they'd prefer."
    )


def check_return_eligibility(customer_id: str, order_number: str, conversation_id: str) -> dict:
    """Determine whether a delivered order can be returned, and if so issue a
    confirmation token.

    Same two-step shape as cancellation, for the same reason: this MUST be
    called before request_return, which rejects any call without a valid,
    unexpired token scoped to (conversation, action, order).
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, status, delivered_at FROM orders WHERE order_number = %s AND customer_id = %s",
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"eligible": False, "reason": "not_found", "message": f"No order {order_number} found for this customer."}

        if order["status"] == "RETURNED":
            return {"eligible": False, "reason": "already_returned", "message": "This order has already been returned."}
        if order["status"] not in RETURNABLE_FROM_STATUSES:
            return {
                "eligible": False,
                "reason": "wrong_status",
                "message": (
                    f"Order is {order['status']}, not DELIVERED — only a delivered order can be "
                    "returned. An order that hasn't shipped yet may be cancellable instead."
                ),
            }

        delivered_at = order["delivered_at"]
        if delivered_at is None:
            # DELIVERED with no timestamp means the window can't be computed.
            # Refusing is the honest outcome; assuming "today" would silently
            # grant a return on an order delivered years ago.
            return {
                "eligible": False,
                "reason": "no_delivery_date",
                "message": "This order has no recorded delivery date, so the return window can't be checked. Staff can review it.",
            }
        if delivered_at.tzinfo is None:
            delivered_at = delivered_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - delivered_at
        if age > timedelta(days=RETURN_WINDOW_DAYS):
            return {
                "eligible": False,
                "reason": "window_expired",
                "message": (
                    f"Delivered {age.days} days ago, outside the {RETURN_WINDOW_DAYS}-day return window."
                ),
            }

        # Final sale, per the Return Policy doc. Checked against the order's
        # actual line items rather than trusting anything the conversation said.
        cur.execute(
            """
            SELECT p.name, p.sku FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = %s AND p.returnable = false
            """,
            (order["id"],),
        )
        final_sale = cur.fetchall()
        if final_sale:
            names = ", ".join(f"{p['name']} ({p['sku']})" for p in final_sale)
            return {
                "eligible": False,
                "reason": "final_sale",
                "message": (
                    f"This order contains final-sale items that can't be returned: {names}. "
                    "Custom-sized and engraved pieces are final sale unless they arrived damaged "
                    "or defective — if that's the case, staff can process it."
                ),
            }

        token = secrets.token_urlsafe(16)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)
        cur.execute(
            """
            INSERT INTO confirmation_tokens (conversation_id, action, target_id, token, expires_at)
            VALUES (%s, 'return_order', %s, %s, %s)
            """,
            (conversation_id, order["id"], token, expires_at),
        )
        conn.commit()

    return {
        "eligible": True,
        "days_since_delivery": age.days,
        "return_window_days": RETURN_WINDOW_DAYS,
        "expires_in_minutes": TOKEN_TTL_MINUTES,
        "message": (
            "Order is eligible for return. Ask the customer which reason applies "
            "(list_resolution_reasons with kind='return') and get their explicit confirmation "
            "before calling request_return."
        ),
    }


def request_return(
    customer_id: str,
    order_number: str,
    conversation_id: str,
    reason_code: str | None = None,
    reason_note: str | None = None,
) -> dict:
    """Return a delivered order. Requires check_return_eligibility to have run
    for this exact order in this exact conversation.

    Re-validates ownership, status, window and final-sale status from scratch —
    the token proves the eligibility check happened, it doesn't skip re-checking.
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, status, delivered_at FROM orders WHERE order_number = %s AND customer_id = %s",
            (order_number, customer_id),
        )
        order = cur.fetchone()
        if not order:
            return {"returned": False, "reason": "not_found", "message": f"No order {order_number} found for this customer."}

        cur.execute(
            """
            SELECT id, expires_at, used_at FROM confirmation_tokens
            WHERE action = 'return_order' AND target_id = %s AND conversation_id = %s
            ORDER BY created_at DESC LIMIT 1
            """,
            (order["id"], conversation_id),
        )
        tok = cur.fetchone()
        if not tok:
            return {"returned": False, "reason": "no_pending_confirmation", "message": "No return was set up for this order in this conversation — call check_return_eligibility first."}
        if tok["used_at"] is not None:
            return {"returned": False, "reason": "token_used", "message": "This order's return was already processed."}
        if tok["expires_at"] < datetime.now(timezone.utc):
            return {"returned": False, "reason": "token_expired", "message": "Confirmation expired — re-check return eligibility."}

        if order["status"] == "RETURNED":
            return {"returned": False, "reason": "already_returned", "message": "This order has already been returned."}
        if order["status"] not in RETURNABLE_FROM_STATUSES:
            return {"returned": False, "reason": "wrong_status", "message": f"Order is now {order['status']} and can no longer be returned."}

        delivered_at = order["delivered_at"]
        if delivered_at is None:
            return {"returned": False, "reason": "no_delivery_date", "message": "No recorded delivery date — the return window can't be checked."}
        if delivered_at.tzinfo is None:
            delivered_at = delivered_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - delivered_at > timedelta(days=RETURN_WINDOW_DAYS):
            return {"returned": False, "reason": "window_expired", "message": "The return window has since expired."}

        cur.execute(
            """
            SELECT 1 FROM order_items oi JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = %s AND p.returnable = false
            """,
            (order["id"],),
        )
        if cur.fetchone():
            return {"returned": False, "reason": "final_sale", "message": "This order contains final-sale items and can't be returned here."}

        code, note, err = resolve_reason(cur, "return", reason_code, reason_note, "customer")
        if err:
            return {"returned": False, "reason": err["error"], "message": err["message"]}

        old_status = order["status"]
        cur.execute(
            """
            UPDATE orders
            SET status = 'RETURNED', return_reason_code = %s, return_reason_note = %s, returned_at = now()
            WHERE id = %s
            """,
            (code, note, order["id"]),
        )
        restocked = _restock_order_items(cur, order["id"])
        cur.execute(
            """
            INSERT INTO order_status_history (order_id, from_status, to_status, reason)
            VALUES (%s, %s, 'RETURNED', %s)
            """,
            (order["id"], old_status, f"customer_requested:{code}"),
        )
        cur.execute("UPDATE confirmation_tokens SET used_at = now() WHERE id = %s", (tok["id"],))
        conn.commit()

    return {
        "returned": True,
        "order_number": order_number,
        "reason_code": code,
        "reason_note": note,
        "restocked_items": restocked,
        "message": (
            "Return recorded. Tell the customer a prepaid return label will be emailed, then call "
            "offer_settlement_options to present the refund-vs-coupon choice."
        ),
    }
