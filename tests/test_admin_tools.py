"""Functional tests for the staff analytics tools.

Two of these carry most of the weight:

- `test_sales_summary_matches_direct_sql` is the permanent guard against the
  bug class that broke this data before: a revenue figure that is confidently
  wrong because a filter silently dropped rows (the 'PAID' vs 'paid' split).
  It recomputes the number independently and asserts they agree.
- `test_every_cents_field_has_a_display_sibling` walks every admin tool's return
  value. JOURNAL.md records the model getting rupee arithmetic wrong in
  production more than once; the _display convention is the fix, and this test
  is what stops a new tool quietly omitting it.
"""

from __future__ import annotations

import pytest

from src.tools import admin_tools
from src.db.connection import get_conn

ACTOR = "00000000-0000-0000-0000-000000000000"  # reads don't use it; writes will


def _any_order_number(status: str = "CANCELLED") -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT order_number FROM orders WHERE status = %s LIMIT 1", (status,))
        row = cur.fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Correctness
# ---------------------------------------------------------------------------


def test_sales_summary_matches_direct_sql():
    result = admin_tools.admin_sales_summary(ACTOR, period="year")

    with get_conn() as conn, conn.cursor() as cur:
        # date_trunc, not a bare `now() - interval '365 days'`, because the tool
        # truncates its start boundary to midnight — a period that began
        # mid-afternoon would be a strange thing to report on. Comparing against
        # the untruncated boundary made this test pass or fail depending on the
        # time of day the database happened to be seeded, since any order landing
        # in the few-hour gap counts for one side and not the other.
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(total_amount_cents - discount_cents), 0)
            FROM orders
            WHERE placed_at >= date_trunc('day', now() - interval '365 days')
              AND status NOT IN ('CANCELLED', 'RETURNED')
            """
        )
        count, revenue = cur.fetchone()

    assert result["order_count"] == count
    assert result["net_revenue_cents"] == revenue


def test_revenue_excludes_cancelled_and_returned():
    """The definition of revenue must be consistent — a cancelled order's value
    must not appear in net revenue."""
    summary = admin_tools.admin_sales_summary(ACTOR, period="year")
    breakdown = admin_tools.admin_sales_breakdown(ACTOR, dimension="status", period="year", limit=10)
    statuses = {r["label"] for r in breakdown["rows"]}
    # Cancelled orders show up in the status breakdown...
    assert "CANCELLED" in statuses
    # ...but their value is excluded from headline revenue.
    cancelled_value = next(r["revenue_cents"] for r in breakdown["rows"] if r["label"] == "CANCELLED")
    assert cancelled_value > 0
    assert summary["net_revenue_cents"] < summary["gross_revenue_cents"] + cancelled_value


def test_breakdown_shares_are_of_the_whole_period_not_just_returned_rows():
    """A top-3 must not look like it accounts for 100% of the business."""
    result = admin_tools.admin_sales_breakdown(ACTOR, dimension="category", period="year", limit=2)
    assert sum(r["share_percent"] for r in result["rows"]) < 99.0
    assert result["period_total_cents"] > sum(r["revenue_cents"] for r in result["rows"])


def test_inventory_low_stock_respects_per_product_threshold():
    rows = admin_tools.admin_inventory_status(ACTOR, filter="low_stock", limit=50)["rows"]
    assert rows, "seed guarantees some low-stock products"
    for r in rows:
        assert r["total_stock"] <= r["low_stock_threshold"]
        assert r["status"] in ("LOW", "OUT_OF_STOCK")


def test_inventory_out_of_stock_is_strictly_zero():
    for r in admin_tools.admin_inventory_status(ACTOR, filter="out_of_stock", limit=50)["rows"]:
        assert r["total_stock"] == 0


def test_inventory_category_all_is_treated_as_no_filter():
    """Regression: the model was observed passing category='all', which as a
    literal WHERE value matched zero products and returned an empty list with no
    error — so the model reported "nothing is out of stock" when six things were.
    A silently-empty result is the worst failure mode an analytics tool has."""
    unfiltered = admin_tools.admin_inventory_status(ACTOR, filter="out_of_stock", limit=50)
    as_all = admin_tools.admin_inventory_status(ACTOR, filter="out_of_stock", category="all", limit=50)
    assert unfiltered["row_count"] > 0, "seed guarantees out-of-stock products"
    assert as_all["row_count"] == unfiltered["row_count"]


def test_inventory_unknown_category_errors_rather_than_returning_empty():
    result = admin_tools.admin_inventory_status(ACTOR, filter="all", category="tiara")
    assert result["error"] == "bad_category"
    assert "ring" in result["message"]  # tells the model what IS valid


def test_find_customer_masks_phone_numbers():
    """A search shouldn't be a bulk contact-details export."""
    for r in admin_tools.admin_find_customer(ACTOR, query="a", limit=5)["rows"]:
        assert "phone" not in r
        if r["phone_masked"]:
            assert r["phone_masked"].startswith("…")
            assert len(r["phone_masked"]) == 5


def test_customer_profile_reaches_across_customers():
    """The whole point of the persona: this is a cross-customer read that the
    customer-facing tools structurally cannot do."""
    profile = admin_tools.admin_customer_profile(ACTOR, customer_email="arun@shurutech.com")
    assert profile["email"] == "arun@shurutech.com"
    assert profile["order_count"] >= 0
    assert "recent_orders" in profile


def test_order_detail_includes_the_owning_customer():
    detail = admin_tools.admin_order_detail(ACTOR, _any_order_number())
    assert detail["customer_email"]
    assert detail["status_history"]


def test_bot_stats_never_returns_message_content():
    """Aggregate-only, by decision. If this ever starts returning transcripts,
    a customer-authored message becomes a prompt-injection vector into an admin
    session."""
    stats = admin_tools.admin_bot_stats(ACTOR, period="week")

    # Structural, not a substring scan: assert the shape is exactly the agreed
    # aggregate keys, so adding a transcript field later fails this test loudly.
    assert set(stats) == {
        "period", "period_label", "conversation_count", "tool_call_count",
        "error_count", "error_rate_percent", "error_rate_percent_display",
        "top_tools", "note",
    }
    for row in stats["top_tools"]:
        assert set(row) == {"tool_name", "calls", "errors"}


# ---------------------------------------------------------------------------
# Error shapes — tools return structured errors, they don't raise.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call,expected",
    [
        (lambda: admin_tools.admin_sales_summary(ACTOR, period="nonsense"), "bad_period"),
        (lambda: admin_tools.admin_sales_breakdown(ACTOR, dimension="nonsense", period="month"), "bad_dimension"),
        (lambda: admin_tools.admin_inventory_status(ACTOR, filter="nonsense"), "bad_filter"),
        (lambda: admin_tools.admin_order_detail(ACTOR, "DPJ-DOES-NOT-EXIST"), "not_found"),
        (lambda: admin_tools.admin_customer_profile(ACTOR, "nobody@example.invalid"), "not_found"),
    ],
)
def test_bad_input_returns_a_structured_error(call, expected):
    assert call()["error"] == expected


def test_custom_period_requires_both_dates():
    assert admin_tools.admin_sales_summary(ACTOR, period="custom")["error"] == "bad_period"


# ---------------------------------------------------------------------------
# The convention guard.
# ---------------------------------------------------------------------------


def _cents_fields_have_displays(node, path="") -> list[str]:
    """Recursively find any *_cents key lacking a sibling *_display."""
    missing = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.endswith("_cents"):
                sibling = key[: -len("_cents")] + "_display"
                if sibling not in node:
                    missing.append(f"{path}.{key}")
            missing += _cents_fields_have_displays(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            missing += _cents_fields_have_displays(item, f"{path}[{i}]")
    return missing


@pytest.mark.parametrize(
    "name,call",
    [
        ("sales_summary", lambda: admin_tools.admin_sales_summary(ACTOR, period="year")),
        ("sales_breakdown", lambda: admin_tools.admin_sales_breakdown(ACTOR, dimension="category", period="year")),
        ("inventory_status", lambda: admin_tools.admin_inventory_status(ACTOR, filter="all", limit=5)),
        ("find_orders", lambda: admin_tools.admin_find_orders(ACTOR, limit=5)),
        ("order_detail", lambda: admin_tools.admin_order_detail(ACTOR, _any_order_number())),
        ("find_customer", lambda: admin_tools.admin_find_customer(ACTOR, query="a", limit=5)),
        ("customer_profile", lambda: admin_tools.admin_customer_profile(ACTOR, "arun@shurutech.com")),
        ("bot_stats", lambda: admin_tools.admin_bot_stats(ACTOR, period="week")),
    ],
)
def test_every_cents_field_has_a_display_sibling(name, call):
    missing = _cents_fields_have_displays(call(), name)
    assert not missing, f"missing _display for: {missing}"
