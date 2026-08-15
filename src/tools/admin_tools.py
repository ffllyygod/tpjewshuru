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

from datetime import datetime, timedelta, timezone

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.formatting import format_inr

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


def _resolve_period(period: str, start_date: str | None, end_date: str | None) -> tuple[datetime, datetime, str]:
    """Return (start, end, human label). Dates are pre-formatted here so the
    model never formats an Indian date itself — same principle as the currency
    `_display` fields."""
    now = datetime.now(timezone.utc)

    if period == "custom":
        if not start_date or not end_date:
            raise ValueError("period='custom' requires both start_date and end_date (YYYY-MM-DD).")
        start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1)
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

    out = []
    for r in rows:
        cents, display = _money(r["price_cents"])
        total = r["total_stock"]
        out.append({
            "sku": r["sku"],
            "name": r["name"],
            "category": r["category"],
            "price_cents": cents,
            "price_display": display,
            "total_stock": total,
            "by_size": r["stock_by_size"],
            "low_stock_threshold": r["low_stock_threshold"],
            "status": "OUT_OF_STOCK" if total == 0 else ("LOW" if total <= r["low_stock_threshold"] else "OK"),
        })

    return {"filter": filter, "category": category, "rows": out, "row_count": len(out),
            "truncated": len(out) >= limit}


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
        clauses.append("o.status = %s::order_status")
        params.append(status.upper())
    if customer_email:
        clauses.append("cu.email ILIKE %s")
        params.append(f"%{customer_email}%")
    if start_date:
        clauses.append("o.placed_at >= %s")
        params.append(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc))
    if end_date:
        clauses.append("o.placed_at < %s")
        params.append(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc) + timedelta(days=1))
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

    out = []
    for r in rows:
        cents, display = _money(r["total_amount_cents"])
        out.append({
            "order_number": r["order_number"],
            "customer_name": r["customer_name"],
            "customer_email": r["customer_email"],
            "status": r["status"],
            "payment_status": r["payment_status"],
            "placed_at": r["placed_at"],
            "total_amount_cents": cents,
            "total_amount_display": display,
        })
    return {"rows": out, "row_count": len(out), "truncated": len(out) >= limit}


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
                "unit_price_cents": i["unit_price_cents"],
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
    limit = max(1, min(int(limit or 10), 25))
    like = f"%{query}%"
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
                "total_amount_cents": o["total_amount_cents"],
                "total_amount_display": format_inr(o["total_amount_cents"]),
            }
            for o in orders
        ],
        "coupons": [
            {
                "code": c["code"], "status": c["status"],
                "total_cents": c["total_cents"], "total_display": format_inr(c["total_cents"]),
                "remaining_cents": c["remaining_cents"], "remaining_display": format_inr(c["remaining_cents"]),
                "expires_at": c["expires_at"],
            }
            for c in coupons
        ],
        "gold_sips": [
            {
                "subscription_code": s["code"], "status": s["status"],
                "installments_paid": s["installments_paid"], "tenure_months": s["tenure_months"],
                "monthly_amount_cents": s["monthly_amount_cents"],
                "monthly_amount_display": format_inr(s["monthly_amount_cents"]),
                "total_paid_cents": s["total_paid_cents"],
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
