"""Staff-only tools. These deliberately break the invariant every other tool
module holds.

Everywhere else in src/tools/, a function takes `customer_id` as its scope and
re-checks it in SQL — "look up someone else's order" is a capability that
doesn't exist. These functions are the exception: they read across all
customers, by design, for TP Jewellers staff.

Two rules keep that from leaking back into the customer path:

1. They live in this module, alone. No existing tool ever gains a nullable
   `customer_id` meaning "all customers" — that's the change that would turn
   every None-propagation bug elsewhere into a cross-customer data leak.
2. They take `actor_customer_id` (WHO is asking), never `customer_id` (WHOSE
   data). The orchestrator injects it server-side from the verified session and
   only for callers it has already confirmed are admins; the model cannot set
   it. Reads don't use it, but writes will record it, and keeping the parameter
   name distinct is what stops the two concepts blurring.

Access is enforced in src/agent/orchestrator.py's `_execute_tool` via
`_ADMIN_ONLY`, not here and not in the system prompt.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.coupon_tools import get_policy, mint_coupon
from src.tools.formatting import format_inr
from src.tools.order_tools import RETURN_WINDOW_DAYS, _restock_order_items, resolve_reason

# Revenue always excludes cancelled and returned orders, and is always net of
# discounts. Defined once and reused by every report below so two tools can
# never disagree about what "revenue" means — the kind of drift that makes an
# admin stop trusting the numbers.
_REVENUE_STATUS_FILTER = "o.status NOT IN ('CANCELLED', 'RETURNED')"
_NET_REVENUE = "SUM(o.total_amount_cents - o.discount_cents)"

_PERIODS = {
    "today": 1,
    "week": 7,
    "month": 30,
    "quarter": 90,
    "year": 365,
    "last_12_months": 365,
}

MAX_ROWS = 50

CATEGORIES = {"ring", "necklace", "earring", "bracelet", "bangle", "pendant"}
ORDER_STATUSES = {"PLACED", "CONFIRMED", "SHIPPED", "DELIVERED", "CANCELLED", "RETURNED"}


def _parse_date(value: str, field: str) -> datetime:
    """Parse a YYYY-MM-DD date, raising a message the model can act on. Bare
    ValueErrors surface to the model as an opaque 'tool_failed' with a Python
    traceback string, which it cannot correct from."""
    try:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise ValueError(f"{field} must be a date in YYYY-MM-DD format, got '{value}'.") from None


def _resolve_period(period: str, start_date: str | None, end_date: str | None) -> tuple[datetime, datetime, str]:
    """Return (start, end, human label). Dates are pre-formatted here so the
    model never formats an Indian date itself — same principle as the currency
    `_display` fields."""
    now = datetime.now(timezone.utc)

    if period == "custom":
        if not start_date or not end_date:
            raise ValueError("period='custom' requires both start_date and end_date (YYYY-MM-DD).")
        start = _parse_date(start_date, "start_date")
        end = _parse_date(end_date, "end_date") + timedelta(days=1)
        # A reversed range matches nothing and would return a confident zero.
        # Same failure class as an unrecognised filter value: fail loudly.
        if end <= start:
            raise ValueError(
                f"end_date ({end_date}) must be on or after start_date ({start_date})."
            )
    elif period == "last_month":
        first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = first_of_this_month
        start = (first_of_this_month - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period in _PERIODS:
        start = (now - timedelta(days=_PERIODS[period])).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    else:
        raise ValueError(
            f"Unknown period '{period}'. Use one of: today, week, month, quarter, year, "
            "last_month, last_12_months, custom."
        )

    label = f"{start.strftime('%d %b %Y')} – {(end - timedelta(days=1)).strftime('%d %b %Y')}"
    return start, end, label


def _money(cents: int | None) -> tuple[int, str]:
    cents = int(cents or 0)
    return cents, format_inr(cents)


def _pct(value: float | None) -> tuple[float, str]:
    """Percentages get a _display too. Model arithmetic on shares is the same
    failure class as rupee division — pre-format it rather than trusting it."""
    v = round(float(value or 0.0), 1)
    return v, f"{v}%"


def admin_sales_summary(
    actor_customer_id: str,
    period: str = "month",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Headline sales figures for a period, with a comparison against the
    immediately preceding period of the same length."""
    try:
        start, end, label = _resolve_period(period, start_date, end_date)
    except ValueError as e:
        return {"error": "bad_period", "message": str(e)}

    span = end - start
    prev_start, prev_end = start - span, start

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
              COUNT(*) FILTER (WHERE {_REVENUE_STATUS_FILTER})                AS order_count,
              COALESCE({_NET_REVENUE} FILTER (WHERE {_REVENUE_STATUS_FILTER}), 0) AS net_revenue,
              COALESCE(SUM(o.total_amount_cents) FILTER (WHERE {_REVENUE_STATUS_FILTER}), 0) AS gross_revenue,
              COALESCE(SUM(o.discount_cents) FILTER (WHERE {_REVENUE_STATUS_FILTER}), 0) AS discounts,
              COUNT(*) FILTER (WHERE o.status = 'CANCELLED')                  AS cancelled_count,
              COUNT(*) FILTER (WHERE o.status = 'RETURNED')                   AS returned_count,
              COALESCE(SUM(o.total_amount_cents) FILTER (WHERE o.status IN ('CANCELLED','RETURNED')), 0) AS refunded
            FROM orders o
            WHERE o.placed_at >= %s AND o.placed_at < %s
            """,
            (start, end),
        )
        cur_row = cur.fetchone()

        cur.execute(
            f"""
            SELECT COALESCE({_NET_REVENUE}, 0) AS net_revenue
            FROM orders o
            WHERE o.placed_at >= %s AND o.placed_at < %s AND {_REVENUE_STATUS_FILTER}
            """,
            (prev_start, prev_end),
        )
        prev_net = cur.fetchone()["net_revenue"] or 0

    net_cents, net_display = _money(cur_row["net_revenue"])
    gross_cents, gross_display = _money(cur_row["gross_revenue"])
    order_count = cur_row["order_count"] or 0
    aov_cents, aov_display = _money(net_cents // order_count if order_count else 0)
    change = ((net_cents - prev_net) / prev_net * 100) if prev_net else 0.0
    change_pct, change_display = _pct(change)
    prev_cents, prev_display = _money(prev_net)
    disc_cents, disc_display = _money(cur_row["discounts"])
    refund_cents, refund_display = _money(cur_row["refunded"])

    return {
        "period": period,
        "period_label": label,
        "order_count": order_count,
        "net_revenue_cents": net_cents,
        "net_revenue_display": net_display,
        "gross_revenue_cents": gross_cents,
        "gross_revenue_display": gross_display,
        "discount_cents": disc_cents,
        "discount_display": disc_display,
        "aov_cents": aov_cents,
        "aov_display": aov_display,
        "cancelled_count": cur_row["cancelled_count"] or 0,
        "returned_count": cur_row["returned_count"] or 0,
        "refunded_value_cents": refund_cents,
        "refunded_value_display": refund_display,
        "previous_period": {
            "net_revenue_cents": prev_cents,
            "net_revenue_display": prev_display,
            "change_percent": change_pct,
            "change_percent_display": change_display,
        },
        "note": "Revenue excludes cancelled and returned orders and is net of discounts.",
    }


_DIMENSIONS = {
    "month": ("to_char(o.placed_at, 'Mon YYYY')", "MIN(o.placed_at)"),
    "category": ("p.category", None),
    "metal": ("p.metal", None),
    "product": ("p.name", None),
    "customer": ("cu.name", None),
    "status": ("o.status::text", None),
}


def admin_sales_breakdown(
    actor_customer_id: str,
    dimension: str = "category",
    period: str = "month",
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 10,
) -> dict:
    """Revenue broken down by one dimension. Covers 'top sellers', 'revenue by
    category', 'monthly trend' and 'best customers' from one tool."""
    if dimension not in _DIMENSIONS:
        return {
            "error": "bad_dimension",
            "message": f"Unknown dimension '{dimension}'. Use one of: {', '.join(_DIMENSIONS)}.",
        }
    try:
        start, end, label = _resolve_period(period, start_date, end_date)
    except ValueError as e:
        return {"error": "bad_period", "message": str(e)}

    limit = max(1, min(int(limit or 10), MAX_ROWS))
    expr, order_expr = _DIMENSIONS[dimension]

    # 'status' counts whole orders; every other dimension is line-item based, so
    # revenue is summed from order_items to avoid multiplying an order's total by
    # its line count.
    if dimension == "status":
        sql = f"""
            SELECT o.status::text AS label,
                   COUNT(*) AS order_count,
                   COALESCE(SUM(o.total_amount_cents - o.discount_cents), 0) AS revenue,
                   0 AS units
            FROM orders o
            WHERE o.placed_at >= %s AND o.placed_at < %s
            GROUP BY 1 ORDER BY revenue DESC LIMIT %s
        """
    else:
        sql = f"""
            SELECT {expr} AS label,
                   COUNT(DISTINCT o.id) AS order_count,
                   COALESCE(SUM(oi.unit_price_cents * oi.quantity), 0) AS revenue,
                   COALESCE(SUM(oi.quantity), 0) AS units
            FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            JOIN products p ON p.id = oi.product_id
            JOIN customers cu ON cu.id = o.customer_id
            WHERE o.placed_at >= %s AND o.placed_at < %s AND {_REVENUE_STATUS_FILTER}
            GROUP BY 1
            ORDER BY {'MIN(o.placed_at)' if dimension == 'month' else 'revenue DESC'}
            LIMIT %s
        """

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (start, end, limit))
        rows = cur.fetchall()

        # Share is of the WHOLE period, not just the rows returned. Using the
        # returned rows as the denominator makes a top-3 look like it accounts
        # for 100% of the business, which is exactly the kind of confidently
        # wrong number an admin would act on.
        if dimension == "status":
            cur.execute(
                """
                SELECT COALESCE(SUM(o.total_amount_cents - o.discount_cents), 0) AS total
                FROM orders o WHERE o.placed_at >= %s AND o.placed_at < %s
                """,
                (start, end),
            )
        else:
            cur.execute(
                f"""
                SELECT COALESCE(SUM(oi.unit_price_cents * oi.quantity), 0) AS total
                FROM orders o JOIN order_items oi ON oi.order_id = o.id
                WHERE o.placed_at >= %s AND o.placed_at < %s AND {_REVENUE_STATUS_FILTER}
                """,
                (start, end),
            )
        period_total = cur.fetchone()["total"] or 0

    total = period_total or 1
    out = []
    for r in rows:
        cents, display = _money(r["revenue"])
        share, share_display = _pct(r["revenue"] / total * 100)
        out.append({
            "label": r["label"],
            "revenue_cents": cents,
            "revenue_display": display,
            "order_count": r["order_count"],
            "units": r["units"],
            "share_percent": share,
            "share_percent_display": share_display,
        })

    total_cents, total_display = _money(period_total)
    return {
        "dimension": dimension,
        "period": period,
        "period_label": label,
        "rows": out,
        "row_count": len(out),
        "truncated": len(out) >= limit,
        "period_total_cents": total_cents,
        "period_total_display": total_display,
        "note": "Share percentages are of total revenue for the whole period, not just the rows shown.",
    }


def admin_resolution_reasons(
    actor_customer_id: str,
    kind: str = "cancellation",
    period: str = "month",
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Why orders were cancelled or returned in a period, with counts and lost value.

    Deliberately NOT a dimension of admin_sales_breakdown: that tool's rows are
    revenue and its shares are of period revenue, whereas these are *lost*
    revenue. Folding them in would produce rows that look like income and a
    share denominator that means nothing.
    """
    if kind not in ("cancellation", "return"):
        return {"error": "bad_kind", "message": "kind must be 'cancellation' or 'return'."}
    try:
        start, end, label = _resolve_period(period, start_date, end_date)
    except ValueError as e:
        return {"error": "bad_period", "message": str(e)}

    # Column and status vary by kind; both are from a closed set validated
    # above, never interpolated from caller input.
    code_col = "cancellation_reason_code" if kind == "cancellation" else "return_reason_code"
    note_col = "cancellation_reason_note" if kind == "cancellation" else "return_reason_note"
    status = "CANCELLED" if kind == "cancellation" else "RETURNED"

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT o.{code_col} AS code,
                   COALESCE(cr.label, 'Not recorded') AS label,
                   cr.applies_to,
                   COUNT(*) AS n,
                   COALESCE(SUM(o.total_amount_cents - o.discount_cents), 0) AS lost
            FROM orders o
            LEFT JOIN resolution_reasons cr ON cr.code = o.{code_col}
            WHERE o.status = %s AND o.placed_at >= %s AND o.placed_at < %s
            GROUP BY 1, 2, 3
            ORDER BY n DESC
            """,
            (status, start, end),
        )
        rows = cur.fetchall()

        # Free-text notes are where the reasons the pick-list doesn't cover show
        # up, so a few recent ones are worth surfacing — capped, because this is
        # customer-written text and a report shouldn't become a transcript dump.
        cur.execute(
            f"""
            SELECT o.order_number, o.{note_col} AS note
            FROM orders o
            WHERE o.status = %s AND o.{note_col} IS NOT NULL
              AND o.placed_at >= %s AND o.placed_at < %s
            ORDER BY o.placed_at DESC LIMIT 5
            """,
            (status, start, end),
        )
        notes = cur.fetchall()

    total_n = sum(r["n"] for r in rows)
    total_lost = sum(r["lost"] for r in rows)
    denominator = total_n or 1

    # Per-row counts are safe to expose (they're not money), but per-row value is
    # display-only — same rule as every other list here.
    out = []
    for r in rows:
        share, share_display = _pct(r["n"] / denominator * 100)
        out.append({
            "reason_code": r["code"] or "unrecorded",
            "reason_label": r["label"],
            "raised_by": r["applies_to"] or "unknown",
            "count": r["n"],
            "share_percent": share,
            "share_percent_display": share_display,
            "lost_value_display": format_inr(r["lost"]),
        })

    lost_cents, lost_display = _money(total_lost)
    return {
        "kind": kind,
        "period": period,
        "period_label": label,
        "rows": out,
        "row_count": len(out),
        "total_resolutions": total_n,
        "total_lost_value_cents": lost_cents,
        "total_lost_value_display": lost_display,
        "recent_notes": [{"order_number": n["order_number"], "note": n["note"]} for n in notes],
        "note": (
            f"Shares are of {kind}s in this period, NOT of revenue. 'Not recorded' covers orders "
            "resolved before reasons were captured — it is not a reason anyone chose."
        ),
    }


def admin_inventory_status(
    actor_customer_id: str,
    filter: str = "low_stock",
    category: str | None = None,
    limit: int = 25,
) -> dict:
    """Stock levels. `filter`: low_stock | out_of_stock | all."""
    if filter not in ("low_stock", "out_of_stock", "all"):
        return {
            "error": "bad_filter",
            "message": "filter must be one of: low_stock, out_of_stock, all.",
        }
    limit = max(1, min(int(limit or 25), MAX_ROWS))

    clauses = ["p.active = true"]
    params: list = []
    # An unrecognised category must never silently match zero products. The
    # model has been observed passing category='all' (meaning "don't filter"),
    # which as a literal WHERE value returns an empty list with no error — and
    # the model then confidently reports "nothing is out of stock". Treat the
    # obvious no-filter words as no filter, and reject anything else loudly so
    # the model can correct itself rather than believing a false negative.
    if category and category.lower() not in ("all", "any", "*"):
        cat = category.lower()
        if cat not in CATEGORIES:
            return {
                "error": "bad_category",
                "message": (
                    f"Unknown category '{category}'. Use one of: {', '.join(sorted(CATEGORIES))} "
                    "— or omit it entirely to cover the whole catalogue."
                ),
            }
        clauses.append("p.category = %s")
        params.append(cat)

    having = {
        "low_stock": "HAVING COALESCE(SUM(s.value::int), 0) <= p.low_stock_threshold",
        "out_of_stock": "HAVING COALESCE(SUM(s.value::int), 0) = 0",
        "all": "",
    }[filter]
    params.append(limit)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT p.sku, p.name, p.category, p.price_cents, p.low_stock_threshold,
                   p.stock_by_size,
                   COALESCE(SUM(s.value::int), 0) AS total_stock
            FROM products p
            LEFT JOIN LATERAL jsonb_each_text(p.stock_by_size) s ON true
            WHERE {' AND '.join(clauses)}
            GROUP BY p.id, p.sku, p.name, p.category, p.price_cents, p.low_stock_threshold, p.stock_by_size
            {having}
            ORDER BY total_stock ASC, p.name ASC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    # Deliberately NO per-row price. Live testing caught the model repeatedly
    # summing a price column into an invented "total value" — and getting it
    # wrong every time. A stock report is about quantities; removing the
    # summable column removes the temptation structurally, which works where the
    # prompt rule alone did not. Aggregate value is provided below, computed here.
    out = []
    for r in rows:
        total = r["total_stock"]
        out.append({
            "sku": r["sku"],
            "name": r["name"],
            "category": r["category"],
            "total_stock": total,
            "by_size": r["stock_by_size"],
            "low_stock_threshold": r["low_stock_threshold"],
            "status": "OUT_OF_STOCK" if total == 0 else ("LOW" if total <= r["low_stock_threshold"] else "OK"),
        })

    # Totals are computed HERE, deliberately. Live testing caught the model
    # appending its own "total value" line to this list — and getting it wrong
    # (₹72,49,932 for a set actually worth ₹48,60,180). It wants a total, so give
    # it one it doesn't have to derive. Same principle as the *_display fields.
    by_sku = {r["sku"]: r["price_cents"] for r in rows}
    stock_value = sum(by_sku[r["sku"]] * r["total_stock"] for r in out)
    value_cents, value_display = _money(stock_value)
    return {
        "filter": filter,
        "category": category,
        "rows": out,
        "row_count": len(out),
        "truncated": len(out) >= limit,
        "out_of_stock_count": sum(1 for r in out if r["status"] == "OUT_OF_STOCK"),
        "low_stock_count": sum(1 for r in out if r["status"] == "LOW"),
        "total_units_on_hand": sum(r["total_stock"] for r in out),
        "remaining_stock_value_cents": value_cents,
        "remaining_stock_value_display": value_display,
    }


def admin_find_orders(
    actor_customer_id: str,
    status: str | None = None,
    customer_email: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 25,
) -> dict:
    """Search orders across ALL customers — the staff counterpart to
    list_customer_orders."""
    limit = max(1, min(int(limit or 25), MAX_ROWS))
    clauses, params = [], []
    if status:
        # Validated here rather than letting Postgres reject the enum cast — that
        # surfaces to the model as an opaque tool_failed it can't correct from.
        if status.upper() not in ORDER_STATUSES:
            return {
                "error": "bad_status",
                "message": f"Unknown status '{status}'. Use one of: {', '.join(sorted(ORDER_STATUSES))}.",
            }
        clauses.append("o.status = %s::order_status")
        params.append(status.upper())
    if customer_email:
        clauses.append("cu.email ILIKE %s")
        params.append(f"%{customer_email}%")
    try:
        if start_date:
            clauses.append("o.placed_at >= %s")
            params.append(_parse_date(start_date, "start_date"))
        if end_date:
            clauses.append("o.placed_at < %s")
            params.append(_parse_date(end_date, "end_date") + timedelta(days=1))
    except ValueError as e:
        return {"error": "bad_date", "message": str(e)}
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT o.order_number, o.status::text AS status, o.placed_at, o.payment_status,
                   o.total_amount_cents, o.discount_cents, cu.name AS customer_name, cu.email AS customer_email
            FROM orders o JOIN customers cu ON cu.id = o.customer_id
            {where}
            ORDER BY o.placed_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    # Per-row amounts are display-only, deliberately. A numeric *_cents column in
    # a list is an invitation to sum it — live testing caught the model doing
    # exactly that here, dividing the raw cents by 100 itself and rendering
    # ₹23,354,395.87 in Western grouping instead of the Indian ₹2,33,54,395.87.
    # The row total below is computed server-side for it instead.
    out = []
    for r in rows:
        out.append({
            "order_number": r["order_number"],
            "customer_name": r["customer_name"],
            "customer_email": r["customer_email"],
            "status": r["status"],
            "payment_status": r["payment_status"],
            "placed_at": r["placed_at"],
            "total_amount_display": format_inr(r["total_amount_cents"]),
        })
    # Pre-computed for the same reason as admin_inventory_status: the model will
    # otherwise sum the column itself and get it wrong.
    _, total_display = _money(sum(r["total_amount_cents"] for r in rows))
    return {
        "rows": out,
        "row_count": len(out),
        "truncated": len(out) >= limit,
        "rows_total_display": total_display,
        "note": "rows_total covers only the rows returned, which may be capped by `limit`.",
    }


def admin_order_detail(actor_customer_id: str, order_number: str) -> dict:
    """Full detail for any order, including who it belongs to and its status history."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT o.id, o.order_number, o.status::text AS status, o.placed_at, o.shipped_at,
                   o.delivered_at, o.total_amount_cents, o.discount_cents, o.payment_status,
                   o.shipping_address, cu.name AS customer_name, cu.email AS customer_email
            FROM orders o JOIN customers cu ON cu.id = o.customer_id
            WHERE o.order_number = %s
            """,
            (order_number,),
        )
        order = cur.fetchone()
        if not order:
            return {"error": "not_found", "message": f"No order {order_number}."}

        cur.execute(
            """
            SELECT p.name, p.sku, oi.quantity, oi.unit_price_cents, oi.size
            FROM order_items oi JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = %s
            """,
            (order["id"],),
        )
        items = cur.fetchall()

        cur.execute(
            """
            SELECT from_status::text AS from_status, to_status::text AS to_status, changed_at, reason
            FROM order_status_history WHERE order_id = %s ORDER BY changed_at ASC, id ASC
            """,
            (order["id"],),
        )
        history = cur.fetchall()

    total_cents, total_display = _money(order["total_amount_cents"])
    disc_cents, disc_display = _money(order["discount_cents"])
    return {
        "order_number": order["order_number"],
        "customer_name": order["customer_name"],
        "customer_email": order["customer_email"],
        "status": order["status"],
        "payment_status": order["payment_status"],
        "placed_at": order["placed_at"],
        "shipped_at": order["shipped_at"],
        "delivered_at": order["delivered_at"],
        "total_amount_cents": total_cents,
        "total_amount_display": total_display,
        "discount_cents": disc_cents,
        "discount_display": disc_display,
        "shipping_address": order["shipping_address"],
        "items": [
            {
                "name": i["name"], "sku": i["sku"], "quantity": i["quantity"], "size": i["size"],
                "unit_price_display": format_inr(i["unit_price_cents"]),
            }
            for i in items
        ],
        "status_history": [
            {"from": h["from_status"], "to": h["to_status"], "at": h["changed_at"], "reason": h["reason"]}
            for h in history
        ],
    }


def admin_find_customer(actor_customer_id: str, query: str, limit: int = 10) -> dict:
    """Find customers by name, email or phone. Phone is masked — a search should
    not be a bulk contact-details export; admin_customer_profile gives the full
    record for one named person."""
    # An empty query would ILIKE '%%' and return an arbitrary slice of the
    # customer base — a bulk PII dump triggered by the model passing "".
    if not query or not query.strip():
        return {
            "error": "empty_query",
            "message": "Provide a name, email, or phone fragment to search for.",
        }
    limit = max(1, min(int(limit or 10), 25))
    like = f"%{query.strip()}%"
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT cu.id, cu.name, cu.email, cu.phone, cu.created_at,
                   COUNT(o.id) FILTER (WHERE {_REVENUE_STATUS_FILTER}) AS order_count,
                   COALESCE({_NET_REVENUE} FILTER (WHERE {_REVENUE_STATUS_FILTER}), 0) AS lifetime_value,
                   MAX(o.placed_at) AS last_order_at
            FROM customers cu
            LEFT JOIN orders o ON o.customer_id = cu.id
            WHERE cu.name ILIKE %s OR cu.email ILIKE %s OR cu.phone ILIKE %s
            GROUP BY cu.id
            ORDER BY lifetime_value DESC
            LIMIT %s
            """,
            (like, like, like, limit),
        )
        rows = cur.fetchall()

    out = []
    for r in rows:
        cents, display = _money(r["lifetime_value"])
        phone = r["phone"] or ""
        out.append({
            "name": r["name"],
            "email": r["email"],
            "phone_masked": (f"…{phone[-4:]}" if len(phone) >= 4 else None),
            "order_count": r["order_count"],
            "lifetime_value_cents": cents,
            "lifetime_value_display": display,
            "last_order_at": r["last_order_at"],
        })
    return {"query": query, "rows": out, "row_count": len(out), "truncated": len(out) >= limit}


def admin_customer_profile(actor_customer_id: str, customer_email: str) -> dict:
    """Everything about one customer: orders, coupons, Gold SIPs. Deliberately
    keyed by email — the model handles emails reliably and mangles UUIDs, and we
    never surface internal customer ids to it."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, name, email, phone, created_at, role FROM customers WHERE email = %s",
            (customer_email,),
        )
        cu = cur.fetchone()
        if not cu:
            return {"error": "not_found", "message": f"No customer with email {customer_email}."}

        cur.execute(
            f"""
            SELECT COUNT(*) FILTER (WHERE {_REVENUE_STATUS_FILTER}) AS order_count,
                   COALESCE({_NET_REVENUE} FILTER (WHERE {_REVENUE_STATUS_FILTER}), 0) AS lifetime_value
            FROM orders o WHERE o.customer_id = %s
            """,
            (cu["id"],),
        )
        totals = cur.fetchone()

        cur.execute(
            """
            SELECT order_number, status::text AS status, placed_at, total_amount_cents, payment_status
            FROM orders WHERE customer_id = %s ORDER BY placed_at DESC LIMIT 10
            """,
            (cu["id"],),
        )
        orders = cur.fetchall()

        cur.execute(
            """
            SELECT code, status, total_cents, remaining_cents, expires_at
            FROM coupons WHERE customer_id = %s ORDER BY issued_at DESC
            """,
            (cu["id"],),
        )
        coupons = cur.fetchall()

        cur.execute(
            """
            SELECT code, status, installments_paid, tenure_months_snapshot AS tenure_months,
                   monthly_amount_cents, total_paid_cents
            FROM gold_sip_subscriptions WHERE customer_id = %s ORDER BY started_at DESC
            """,
            (cu["id"],),
        )
        sips = cur.fetchall()

    ltv_cents, ltv_display = _money(totals["lifetime_value"])
    return {
        "name": cu["name"],
        "email": cu["email"],
        "phone": cu["phone"],
        "role": cu["role"],
        "customer_since": cu["created_at"],
        "order_count": totals["order_count"],
        "lifetime_value_cents": ltv_cents,
        "lifetime_value_display": ltv_display,
        "recent_orders": [
            {
                "order_number": o["order_number"], "status": o["status"], "placed_at": o["placed_at"],
                "payment_status": o["payment_status"],
                "total_amount_display": format_inr(o["total_amount_cents"]),
            }
            for o in orders
        ],
        "coupons": [
            {
                "code": c["code"], "status": c["status"],
                "total_display": format_inr(c["total_cents"]),
                "remaining_display": format_inr(c["remaining_cents"]),
                "expires_at": c["expires_at"],
            }
            for c in coupons
        ],
        "gold_sips": [
            {
                "subscription_code": s["code"], "status": s["status"],
                "installments_paid": s["installments_paid"], "tenure_months": s["tenure_months"],
                "monthly_amount_display": format_inr(s["monthly_amount_cents"]),
                "total_paid_display": format_inr(s["total_paid_cents"]),
            }
            for s in sips
        ],
    }


def admin_bot_stats(actor_customer_id: str, period: str = "week") -> dict:
    """Aggregate chatbot usage. Deliberately returns NO message content — staff
    cannot read customers' conversations through this assistant, by design."""
    try:
        start, end, label = _resolve_period(period, None, None)
    except ValueError as e:
        return {"error": "bad_period", "message": str(e)}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE started_at >= %s AND started_at < %s",
            (start, end),
        )
        conversations = cur.fetchone()["n"]
        cur.execute(
            """
            SELECT tool_name, COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE result ? 'error') AS errors
            FROM tool_call_log WHERE created_at >= %s AND created_at < %s
            GROUP BY 1 ORDER BY calls DESC LIMIT 15
            """,
            (start, end),
        )
        tools = cur.fetchall()

    total_calls = sum(t["calls"] for t in tools)
    total_errors = sum(t["errors"] for t in tools)
    err_pct, err_display = _pct(total_errors / total_calls * 100 if total_calls else 0)
    return {
        "period": period,
        "period_label": label,
        "conversation_count": conversations,
        "tool_call_count": total_calls,
        "error_count": total_errors,
        "error_rate_percent": err_pct,
        "error_rate_percent_display": err_display,
        "top_tools": [
            {"tool_name": t["tool_name"], "calls": t["calls"], "errors": t["errors"]} for t in tools
        ],
        "note": "Aggregate only — conversation contents are not accessible.",
    }


# ===========================================================================
# WRITES
# ===========================================================================
# Everything above reads. Everything below changes data belonging to a customer
# who is not in the conversation and cannot object, so each one is a
# preview/apply pair sharing four properties:
#
#   1. The preview mints a single-use confirmation token scoped to
#      (conversation, action, target) — the same table and shape as customer
#      cancellation, so there is one confirmation mechanism in this codebase,
#      not two.
#   2. The token also carries the previewed PARAMS. The apply re-derives its
#      effect from those, and refuses if the arguments it was called with have
#      drifted. Without this, a token minted by previewing "₹500 to Priya" would
#      equally authorise "₹50,000 to Priya" — same conversation, same action,
#      same target id.
#   3. The apply re-validates every precondition from scratch. The token proves
#      a preview happened; it never substitutes for re-checking.
#   4. Every successful write inserts an admin_action_log row with before_state,
#      after_state, the actor, and a non-empty reason.
#
# The honest limitation, carried over from customer cancellation: the token
# proves the preview ran in this conversation, NOT that a human said yes in
# between. That step is prompt-enforced only. What the token does buy is that a
# single confused turn cannot both discover a target and mutate it.

ADMIN_TOKEN_TTL_MINUTES = 10

# Value caps. A cap is not a substitute for the confirmation flow — it's the
# backstop for when the flow is followed and the number is still wrong, which is
# the realistic failure here (a misplaced decimal in a model-supplied amount is
# far likelier than a forged token).
ADMIN_COUPON_MAX_CENTS = 5_000_000        # ₹50,000 per goodwill coupon
ADMIN_STOCK_MAX_QUANTITY = 500            # per size, per product

# Staff can cancel later in the lifecycle than a customer can (that is the whole
# point of an override) — but DELIVERED is a return, not a cancellation, and a
# terminal status is not re-cancellable.
ADMIN_CANCELLABLE_STATUSES = {"PLACED", "CONFIRMED", "SHIPPED"}

_MIN_REASON_CHARS = 3


def _clean_reason(reason: str | None) -> str:
    """admin_action_log.reason is NOT NULL for a reason: an audit row that
    doesn't say why is a row nobody can act on six months later."""
    text = (reason or "").strip()
    if len(text) < _MIN_REASON_CHARS:
        raise ValueError(
            "A reason is required and must be a real explanation (e.g. 'customer "
            "called, wrong size ordered') — it is written to the permanent audit log."
        )
    return text[:500]


def _mint_token(cur, conversation_id: str, action: str, target_id: str, params: dict) -> str:
    token = secrets.token_urlsafe(16)
    cur.execute(
        """
        INSERT INTO confirmation_tokens (conversation_id, action, target_id, params, token, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            conversation_id, action, target_id, json.dumps(params, default=str), token,
            datetime.now(timezone.utc) + timedelta(minutes=ADMIN_TOKEN_TTL_MINUTES),
        ),
    )
    return token


def _claim_token(cur, conversation_id: str, action: str, target_id: str, params: dict):
    """Find the pending confirmation for this exact (conversation, action,
    target) and check it still authorises exactly `params`.

    Returns (token_id, None) on success or (None, error_dict) on failure. The
    caller burns the token as part of its own transaction — deliberately not
    here, so a write that fails re-validation after this point leaves the
    confirmation intact rather than forcing the staff member to preview again.
    """
    cur.execute(
        """
        SELECT id, params, expires_at, used_at
        FROM confirmation_tokens
        WHERE conversation_id = %s AND action = %s AND target_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (conversation_id, action, target_id),
    )
    tok = cur.fetchone()
    if not tok:
        return None, {
            "applied": False,
            "reason": "no_pending_confirmation",
            "message": (
                f"No {action} was previewed for this target in this conversation. "
                "Call the matching preview tool first, and confirm with the staff member."
            ),
        }
    if tok["used_at"] is not None:
        return None, {
            "applied": False,
            "reason": "token_used",
            "message": "That confirmation was already used — this change has been applied once already.",
        }
    if tok["expires_at"] < datetime.now(timezone.utc):
        return None, {
            "applied": False,
            "reason": "token_expired",
            "message": f"Confirmation expired after {ADMIN_TOKEN_TTL_MINUTES} minutes — preview again.",
        }
    # Compared through JSON so the stored jsonb and the fresh dict are the same
    # shape (ints stay ints, and key order never matters).
    if json.loads(json.dumps(params, default=str)) != dict(tok["params"] or {}):
        return None, {
            "applied": False,
            "reason": "params_changed",
            "message": (
                "These arguments don't match what was previewed and confirmed. "
                "Preview again with the new values and get a fresh confirmation."
            ),
        }
    return tok["id"], None


def _log_admin_action(
    cur, actor_customer_id: str, conversation_id: str, action: str,
    target_table: str, target_id: str, before: dict | None, after: dict | None, reason: str,
) -> None:
    cur.execute(
        """
        INSERT INTO admin_action_log
          (actor_customer_id, conversation_id, action, target_table, target_id,
           before_state, after_state, reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            actor_customer_id, conversation_id, action, target_table, target_id,
            json.dumps(before, default=str) if before is not None else None,
            json.dumps(after, default=str) if after is not None else None,
            reason,
        ),
    )


# ---------------------------------------------------------------------------
# Write 1 — cancel any customer's order (staff override)
# ---------------------------------------------------------------------------


def _load_order_for_write(cur, order_number: str):
    cur.execute(
        """
        SELECT o.id, o.order_number, o.status::text AS status, o.placed_at, o.payment_status,
               o.total_amount_cents, o.discount_cents,
               cu.id AS customer_id, cu.name AS customer_name, cu.email AS customer_email
        FROM orders o JOIN customers cu ON cu.id = o.customer_id
        WHERE o.order_number = %s
        """,
        (order_number,),
    )
    return cur.fetchone()


def _order_cancellable(order) -> dict | None:
    """Shared by preview and apply so the two can't drift on what's allowed."""
    status = order["status"]
    if status in ("CANCELLED", "RETURNED"):
        return {
            "reason": "already_terminal",
            "message": f"Order {order['order_number']} is already {status} — nothing to cancel.",
        }
    if status == "DELIVERED":
        return {
            "reason": "delivered",
            "message": (
                f"Order {order['order_number']} has been DELIVERED. A delivered order is handled as "
                "a return, not a cancellation — this tool cannot cancel it."
            ),
        }
    if status not in ADMIN_CANCELLABLE_STATUSES:
        return {"reason": "wrong_status", "message": f"Order is {status} and cannot be cancelled."}
    return None


def admin_preview_order_cancellation(
    actor_customer_id: str, order_number: str, reason: str, conversation_id: str,
    reason_code: str | None = None, reason_note: str | None = None,
) -> dict:
    """Show exactly what cancelling this order would do, and mint the
    confirmation that admin_cancel_order requires. Changes nothing."""
    try:
        reason = _clean_reason(reason)
    except ValueError as e:
        return {"error": "reason_required", "message": str(e)}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        order = _load_order_for_write(cur, order_number)
        if not order:
            return {"error": "not_found", "message": f"No order {order_number}."}
        blocked = _order_cancellable(order)
        if blocked:
            return {"error": blocked["reason"], "message": blocked["message"]}

        # The structured code is what makes cancellations groupable in reports;
        # `reason` stays as the staff member's own words for the audit log. Both
        # go into the token, so neither can drift between preview and apply.
        code, note, err = resolve_reason(cur, "cancellation", reason_code, reason_note or reason, "staff")
        if err:
            return err

        _mint_token(
            cur, conversation_id, "admin_cancel_order", order["id"],
            {"reason": reason, "reason_code": code, "reason_note": note},
        )
        conn.commit()

    _, total_display = _money(order["total_amount_cents"] - order["discount_cents"])
    return {
        "preview": True,
        "order_number": order["order_number"],
        "customer_name": order["customer_name"],
        "customer_email": order["customer_email"],
        "current_status": order["status"],
        "payment_status": order["payment_status"],
        "order_total_display": total_display,
        "placed_at": order["placed_at"],
        "will_change": [
            f"Order status {order['status']} -> CANCELLED",
            "A status-history entry attributing the cancellation to staff",
            "An audit entry recording you as the staff member and this reason",
        ],
        "reason": reason,
        "reason_code": code,
        "reason_note": note,
        "staff_override_note": (
            "This bypasses the 24-hour window customers are held to — it is a staff override "
            "and is logged as one."
        ),
        "settlement_note": (
            "This does NOT refund anything. Once cancelled, the customer can settle it "
            "(cash refund or a coupon worth more) through their own chat, or you can issue "
            "a goodwill coupon separately."
        ),
        "confirmation_expires_in_minutes": ADMIN_TOKEN_TTL_MINUTES,
        "next_step": (
            "Read this back to the staff member — order number, customer name, and total — "
            "and ask them to confirm. Only call admin_cancel_order after they say yes."
        ),
    }


def admin_cancel_order(
    actor_customer_id: str, order_number: str, reason: str, conversation_id: str,
    reason_code: str | None = None, reason_note: str | None = None,
) -> dict:
    """Cancel any customer's order. Requires admin_preview_order_cancellation to
    have run in this conversation for this order with this same reason."""
    try:
        reason = _clean_reason(reason)
    except ValueError as e:
        return {"applied": False, "reason": "reason_required", "message": str(e)}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        order = _load_order_for_write(cur, order_number)
        if not order:
            return {"applied": False, "reason": "not_found", "message": f"No order {order_number}."}

        code, note, rerr = resolve_reason(cur, "cancellation", reason_code, reason_note or reason, "staff")
        if rerr:
            return {"applied": False, **rerr}

        token_id, err = _claim_token(
            cur, conversation_id, "admin_cancel_order", order["id"],
            {"reason": reason, "reason_code": code, "reason_note": note},
        )
        if err:
            return err

        # Re-validated from scratch: the order may have shipped or been cancelled
        # by the customer themselves between the preview and this call.
        blocked = _order_cancellable(order)
        if blocked:
            return {"applied": False, **blocked}

        before = {"status": order["status"], "payment_status": order["payment_status"]}
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
            (order["id"], order["status"], f"admin_cancelled:{code}"),
        )
        cur.execute("UPDATE confirmation_tokens SET used_at = now() WHERE id = %s", (token_id,))
        _log_admin_action(
            cur, actor_customer_id, conversation_id, "admin_cancel_order", "orders", order["id"],
            before,
            {"status": "CANCELLED", "payment_status": order["payment_status"],
             "cancellation_reason_code": code, "cancellation_reason_note": note},
            reason,
        )
        conn.commit()

    return {
        "applied": True,
        "order_number": order["order_number"],
        "customer_name": order["customer_name"],
        "previous_status": before["status"],
        "new_status": "CANCELLED",
        "reason": reason,
        "reason_code": code,
        "reason_note": note,
        "restocked_items": restocked,
        "message": (
            f"Order {order['order_number']} ({order['customer_name']}) is cancelled and the action "
            "is logged against your account. No money has moved — settlement is a separate step."
        ),
    }


# ---------------------------------------------------------------------------
# Write 2 — stock adjustment
# ---------------------------------------------------------------------------


def _resolve_size(stock: dict, size: str | None) -> tuple[str | None, dict | None]:
    """Pick which stock_by_size key is being adjusted.

    Unsized products carry a single '_default' key, so requiring the model to
    supply a size there would be asking it to know an internal convention. With
    exactly one key there is no ambiguity, so infer it; with several, refuse
    rather than guess — silently adjusting the wrong size is a real-inventory
    error nobody would notice.
    """
    keys = list(stock or {})
    if not keys:
        return None, {"error": "no_stock_record", "message": "This product has no stock records to adjust."}
    if size is None:
        if len(keys) == 1:
            return keys[0], None
        return None, {
            "error": "size_required",
            "message": f"This product is stocked by size. Specify which: {', '.join(sorted(keys))}.",
        }
    if size not in stock:
        return None, {
            "error": "bad_size",
            "message": f"No size '{size}' for this product. Valid sizes: {', '.join(sorted(keys))}.",
        }
    return size, None


def _validate_quantity(new_quantity) -> tuple[int | None, dict | None]:
    try:
        qty = int(new_quantity)
        if qty != float(new_quantity):
            raise ValueError
    except (TypeError, ValueError):
        return None, {"error": "bad_quantity", "message": f"new_quantity must be a whole number, got '{new_quantity}'."}
    if qty < 0:
        return None, {"error": "bad_quantity", "message": "new_quantity cannot be negative."}
    if qty > ADMIN_STOCK_MAX_QUANTITY:
        return None, {
            "error": "exceeds_limit",
            "message": (
                f"{qty} exceeds the {ADMIN_STOCK_MAX_QUANTITY}-unit per-size cap for a chat-issued "
                "stock adjustment. Nothing was changed. A larger correction needs to go through "
                "inventory management directly."
            ),
        }
    return qty, None


def _load_product_for_write(cur, sku: str):
    cur.execute(
        "SELECT id, sku, name, category, stock_by_size, active FROM products WHERE sku = %s",
        (sku,),
    )
    return cur.fetchone()


def admin_preview_stock_adjustment(
    actor_customer_id: str, sku: str, new_quantity: int, reason: str,
    conversation_id: str, size: str | None = None,
) -> dict:
    """Show the before/after of a stock correction and mint its confirmation.
    Changes nothing."""
    try:
        reason = _clean_reason(reason)
    except ValueError as e:
        return {"error": "reason_required", "message": str(e)}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        product = _load_product_for_write(cur, sku)
        if not product:
            return {
                "error": "not_found",
                "message": f"No product with SKU '{sku}'. SKUs look like TPJ-RIN-1010 — find it with admin_inventory_status.",
            }
        stock = dict(product["stock_by_size"] or {})
        key, err = _resolve_size(stock, size)
        if err:
            return err
        qty, err = _validate_quantity(new_quantity)
        if err:
            return err

        current = int(stock.get(key, 0))
        if current == qty:
            # A no-op write would still burn a confirmation and write a
            # meaningless audit row. Say so instead.
            return {
                "error": "no_change",
                "message": f"{product['name']} size {key} is already at {qty} units — nothing to change.",
            }

        _mint_token(
            cur, conversation_id, "admin_adjust_stock", product["id"],
            {"size": key, "new_quantity": qty, "reason": reason},
        )
        conn.commit()

    return {
        "preview": True,
        "sku": product["sku"],
        "product_name": product["name"],
        "category": product["category"],
        "size": key,
        "current_quantity": current,
        "new_quantity": qty,
        "change": qty - current,
        "current_stock_by_size": stock,
        "product_active": product["active"],
        "reason": reason,
        "will_change": [f"{product['name']} size {key}: {current} -> {qty} units"],
        "confirmation_expires_in_minutes": ADMIN_TOKEN_TTL_MINUTES,
        "next_step": (
            "Read back the product name, SKU, size, and the before/after quantity, and ask the "
            "staff member to confirm. Only then call admin_adjust_stock."
        ),
    }


def admin_adjust_stock(
    actor_customer_id: str, sku: str, new_quantity: int, reason: str,
    conversation_id: str, size: str | None = None,
) -> dict:
    """Set the on-hand quantity for one product/size. Requires a matching
    admin_preview_stock_adjustment in this conversation."""
    try:
        reason = _clean_reason(reason)
    except ValueError as e:
        return {"applied": False, "reason": "reason_required", "message": str(e)}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        product = _load_product_for_write(cur, sku)
        if not product:
            return {"applied": False, "reason": "not_found", "message": f"No product with SKU '{sku}'."}
        stock = dict(product["stock_by_size"] or {})
        key, err = _resolve_size(stock, size)
        if err:
            return {"applied": False, **err}
        qty, err = _validate_quantity(new_quantity)
        if err:
            return {"applied": False, **err}

        token_id, terr = _claim_token(
            cur, conversation_id, "admin_adjust_stock", product["id"],
            {"size": key, "new_quantity": qty, "reason": reason},
        )
        if terr:
            return terr

        before = dict(stock)
        cur.execute(
            """
            UPDATE products
            SET stock_by_size = jsonb_set(stock_by_size, ARRAY[%s], to_jsonb(%s::int)),
                updated_at = now()
            WHERE id = %s
            RETURNING stock_by_size
            """,
            (key, qty, product["id"]),
        )
        after = dict(cur.fetchone()["stock_by_size"])
        cur.execute("UPDATE confirmation_tokens SET used_at = now() WHERE id = %s", (token_id,))
        _log_admin_action(
            cur, actor_customer_id, conversation_id, "admin_adjust_stock", "products", product["id"],
            before, after, reason,
        )
        conn.commit()

    return {
        "applied": True,
        "sku": product["sku"],
        "product_name": product["name"],
        "size": key,
        "previous_quantity": int(before.get(key, 0)),
        "new_quantity": qty,
        "stock_by_size": after,
        "reason": reason,
        "message": f"{product['name']} size {key} set to {qty} units.",
    }


# ---------------------------------------------------------------------------
# Write 3 — goodwill coupon
# ---------------------------------------------------------------------------


def _validate_goodwill_amount(amount_rupees) -> tuple[int | None, dict | None]:
    """The one place in this codebase where a model-supplied number becomes
    money. Everywhere else an amount is derived server-side from an order total
    or a policy row; here a staff member genuinely chooses a figure, so it is
    validated hard and capped rather than trusted.

    Rupees, not paise: asking the model for a *_cents value is asking it to do
    the exact ×100 arithmetic the _display convention exists because it gets
    wrong.
    """
    try:
        rupees = float(amount_rupees)
    except (TypeError, ValueError):
        return None, {"error": "bad_amount", "message": f"amount_rupees must be a number, got '{amount_rupees}'."}
    if rupees != int(rupees):
        return None, {
            "error": "bad_amount",
            "message": f"amount_rupees must be a whole number of rupees (no paise), got {amount_rupees}.",
        }
    cents = int(rupees) * 100
    if cents <= 0:
        return None, {"error": "bad_amount", "message": "amount_rupees must be greater than zero."}
    if cents > ADMIN_COUPON_MAX_CENTS:
        return None, {
            "error": "exceeds_limit",
            "message": (
                f"{format_inr(cents)} exceeds the {format_inr(ADMIN_COUPON_MAX_CENTS)} cap on a "
                "chat-issued goodwill coupon. No coupon was created. Anything larger has to be "
                "authorised outside this assistant."
            ),
            # The rejected amount gets a _display too. The model will quote it
            # back when explaining the refusal, and every rupee figure it says
            # should come from a field rather than its own formatting.
            "requested_display": format_inr(cents),
            "cap_display": format_inr(ADMIN_COUPON_MAX_CENTS),
        }
    return cents, None


def _load_customer_for_write(cur, customer_email: str):
    cur.execute(
        "SELECT id, name, email, role FROM customers WHERE lower(email) = lower(%s)",
        ((customer_email or "").strip(),),
    )
    return cur.fetchone()


def admin_preview_goodwill_coupon(
    actor_customer_id: str, customer_email: str, amount_rupees: int, reason: str,
    conversation_id: str,
) -> dict:
    """Show the goodwill coupon that would be issued, and mint its confirmation.
    Creates nothing — no coupon row exists until admin_issue_goodwill_coupon."""
    try:
        reason = _clean_reason(reason)
    except ValueError as e:
        return {"error": "reason_required", "message": str(e)}

    cents, err = _validate_goodwill_amount(amount_rupees)
    if err:
        return err

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        customer = _load_customer_for_write(cur, customer_email)
        if not customer:
            return {
                "error": "not_found",
                "message": (
                    f"No customer with email '{customer_email}'. Use admin_find_customer to get "
                    "their exact address — a goodwill coupon must go to a real, confirmed person."
                ),
            }
        policy = get_policy(cur)
        expiry_days = policy["expiry_days"]
        _mint_token(
            cur, conversation_id, "admin_issue_goodwill_coupon", customer["id"],
            {"amount_cents": cents, "reason": reason},
        )
        conn.commit()

    return {
        "preview": True,
        "customer_name": customer["name"],
        "customer_email": customer["email"],
        "currency": "INR",
        "amount_display": format_inr(cents),
        "expiry_days": expiry_days,
        "reason": reason,
        "will_change": [
            f"A new store-credit coupon worth {format_inr(cents)} for {customer['name']}",
            f"Valid for {expiry_days} days, usable on any future purchase",
        ],
        "note": (
            "A goodwill coupon is store credit, not a refund — it is not tied to any order and "
            "cannot be exchanged for cash."
        ),
        "confirmation_expires_in_minutes": ADMIN_TOKEN_TTL_MINUTES,
        "next_step": (
            "Read back the customer's name and the amount, and ask the staff member to confirm. "
            "Only then call admin_issue_goodwill_coupon."
        ),
    }


def admin_issue_goodwill_coupon(
    actor_customer_id: str, customer_email: str, amount_rupees: int, reason: str,
    conversation_id: str,
) -> dict:
    """Issue a sourceless store-credit coupon to any customer. Requires a
    matching admin_preview_goodwill_coupon in this conversation."""
    try:
        reason = _clean_reason(reason)
    except ValueError as e:
        return {"applied": False, "reason": "reason_required", "message": str(e)}

    cents, err = _validate_goodwill_amount(amount_rupees)
    if err:
        # Note the ordering: the cap is checked BEFORE the token is claimed, so a
        # rejected amount neither creates a coupon nor consumes a confirmation.
        return {"applied": False, **err}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        customer = _load_customer_for_write(cur, customer_email)
        if not customer:
            return {"applied": False, "reason": "not_found", "message": f"No customer with email '{customer_email}'."}

        token_id, terr = _claim_token(
            cur, conversation_id, "admin_issue_goodwill_coupon", customer["id"],
            {"amount_cents": cents, "reason": reason},
        )
        if terr:
            return terr

        policy = get_policy(cur)
        # bonus_percent=0: the cancellation/return bonus buys retention against a
        # refund the customer was owed anyway. Goodwill is already a gift; there
        # is nothing to top up, and applying a bonus here would silently issue
        # more than the amount the staff member confirmed.
        coupon = mint_coupon(
            cur, customer["id"], "goodwill", cents, 0, policy["expiry_days"],
        )
        cur.execute("UPDATE confirmation_tokens SET used_at = now() WHERE id = %s", (token_id,))
        _log_admin_action(
            cur, actor_customer_id, conversation_id, "admin_issue_goodwill_coupon",
            "coupons", coupon["id"], None,
            {
                "code": coupon["code"],
                "customer_email": customer["email"],
                "total_cents": coupon["total_cents"],
            },
            reason,
        )
        conn.commit()

    return {
        "applied": True,
        "coupon_code": coupon["code"],
        "customer_name": customer["name"],
        "customer_email": customer["email"],
        "currency": "INR",
        "total_display": format_inr(coupon["total_cents"]),
        "expires_at": coupon["expires_at"],
        "reason": reason,
        "message": (
            f"Issued {format_inr(coupon['total_cents'])} of store credit to {customer['name']} "
            f"(code {coupon['code']}). Give them the code — it's already active."
        ),
    }


# ---------------------------------------------------------------------------
# Write 4 — process a return on any customer's order (staff override)
# ---------------------------------------------------------------------------


def _order_returnable(cur, order, allow_final_sale: bool) -> dict | None:
    """Shared by preview and apply so the two can't drift on what's allowed.

    Staff deliberately reach further than a customer: no 30-day window, because
    handling the out-of-policy case by hand is exactly why a staff override
    exists. Final sale is different — that's a property of the product, not a
    timing rule, so it still blocks unless the item actually arrived damaged.
    """
    status = order["status"]
    if status == "RETURNED":
        return {"reason": "already_returned", "message": f"Order {order['order_number']} is already RETURNED."}
    if status != "DELIVERED":
        return {
            "reason": "wrong_status",
            "message": (
                f"Order {order['order_number']} is {status}, not DELIVERED. Only a delivered order "
                "can be returned — cancel it instead if it hasn't shipped."
            ),
        }

    cur.execute(
        """
        SELECT p.name, p.sku FROM order_items oi JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = %s AND p.returnable = false
        """,
        (order["id"],),
    )
    final_sale = cur.fetchall()
    if final_sale and not allow_final_sale:
        names = ", ".join(f"{p['name']} ({p['sku']})" for p in final_sale)
        return {
            "reason": "final_sale",
            "message": (
                f"Contains final-sale items: {names}. These are returnable only when they arrived "
                "damaged or defective — use reason_code 'damaged_on_arrival' if that is the case."
            ),
        }
    return None


def _delivered_at_and_window(cur, order_id: str) -> tuple[datetime | None, bool]:
    cur.execute("SELECT delivered_at FROM orders WHERE id = %s", (order_id,))
    delivered_at = cur.fetchone()["delivered_at"]
    if not delivered_at:
        return None, False
    if delivered_at.tzinfo is None:
        delivered_at = delivered_at.replace(tzinfo=timezone.utc)
    return delivered_at, (datetime.now(timezone.utc) - delivered_at).days > RETURN_WINDOW_DAYS


def admin_preview_order_return(
    actor_customer_id: str, order_number: str, reason: str, conversation_id: str,
    reason_code: str | None = None, reason_note: str | None = None,
) -> dict:
    """Show what returning this order would do, and mint the confirmation that
    admin_return_order requires. Changes nothing."""
    try:
        reason = _clean_reason(reason)
    except ValueError as e:
        return {"error": "reason_required", "message": str(e)}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        order = _load_order_for_write(cur, order_number)
        if not order:
            return {"error": "not_found", "message": f"No order {order_number}."}

        code, note, err = resolve_reason(cur, "return", reason_code, reason_note or reason, "staff")
        if err:
            return err

        blocked = _order_returnable(cur, order, allow_final_sale=(code == "damaged_on_arrival"))
        if blocked:
            return {"error": blocked["reason"], "message": blocked["message"]}

        delivered_at, outside_window = _delivered_at_and_window(cur, order["id"])

        _mint_token(
            cur, conversation_id, "admin_return_order", order["id"],
            {"reason": reason, "reason_code": code, "reason_note": note},
        )
        conn.commit()

    _, total_display = _money(order["total_amount_cents"] - order["discount_cents"])
    return {
        "preview": True,
        "order_number": order["order_number"],
        "customer_name": order["customer_name"],
        "customer_email": order["customer_email"],
        "current_status": order["status"],
        "order_total_display": total_display,
        "delivered_at": delivered_at,
        "outside_return_window": outside_window,
        "reason": reason,
        "reason_code": code,
        "reason_note": note,
        "will_change": [
            f"Order status {order['status']} -> RETURNED",
            "The order's items go back into stock",
            "An audit entry recording you as the staff member and this reason",
        ],
        "staff_override_note": (
            f"Delivered more than {RETURN_WINDOW_DAYS} days ago — outside the window customers are "
            "held to. This is a staff override and is logged as one."
            if outside_window else
            f"Within the {RETURN_WINDOW_DAYS}-day return window."
        ),
        "settlement_note": (
            "This does NOT refund anything. Once returned, the customer can settle it (cash refund "
            "or a coupon worth more) through their own chat."
        ),
        "confirmation_expires_in_minutes": ADMIN_TOKEN_TTL_MINUTES,
        "next_step": (
            "Read this back — order number, customer name, total — and ask the staff member to "
            "confirm. Only call admin_return_order after they say yes."
        ),
    }


def admin_return_order(
    actor_customer_id: str, order_number: str, reason: str, conversation_id: str,
    reason_code: str | None = None, reason_note: str | None = None,
) -> dict:
    """Return any customer's order. Requires admin_preview_order_return to have
    run in this conversation for this order with these same values."""
    try:
        reason = _clean_reason(reason)
    except ValueError as e:
        return {"applied": False, "reason": "reason_required", "message": str(e)}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        order = _load_order_for_write(cur, order_number)
        if not order:
            return {"applied": False, "reason": "not_found", "message": f"No order {order_number}."}

        code, note, rerr = resolve_reason(cur, "return", reason_code, reason_note or reason, "staff")
        if rerr:
            return {"applied": False, **rerr}

        token_id, err = _claim_token(
            cur, conversation_id, "admin_return_order", order["id"],
            {"reason": reason, "reason_code": code, "reason_note": note},
        )
        if err:
            return err

        # Re-validated from scratch: the customer may have returned it themselves
        # between the preview and this call.
        blocked = _order_returnable(cur, order, allow_final_sale=(code == "damaged_on_arrival"))
        if blocked:
            return {"applied": False, **blocked}

        before = {"status": order["status"], "payment_status": order["payment_status"]}
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
            (order["id"], order["status"], f"admin_returned:{code}"),
        )
        cur.execute("UPDATE confirmation_tokens SET used_at = now() WHERE id = %s", (token_id,))
        _log_admin_action(
            cur, actor_customer_id, conversation_id, "admin_return_order", "orders", order["id"],
            before,
            {"status": "RETURNED", "payment_status": order["payment_status"],
             "return_reason_code": code, "return_reason_note": note},
            reason,
        )
        conn.commit()

    return {
        "applied": True,
        "order_number": order["order_number"],
        "customer_name": order["customer_name"],
        "previous_status": before["status"],
        "new_status": "RETURNED",
        "reason": reason,
        "reason_code": code,
        "reason_note": note,
        "restocked_items": restocked,
        "message": (
            f"Order {order['order_number']} ({order['customer_name']}) is returned, stock is back, "
            "and the action is logged against your account. No money has moved — settlement is separate."
        ),
    }
