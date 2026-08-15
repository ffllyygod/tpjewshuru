"""Adversarial / edge-case tests for the staff tools.

The theme: **a wrong answer is worse than an error.** An analytics tool that
returns an empty list because it didn't understand an argument will be reported
by the model as a confident fact ("nothing is out of stock"), and nobody will
know it was wrong. Every test here pins a case where the tool must fail loudly
rather than return a plausible-looking empty or partial result.

Found live, not theorised: the model really did pass category='all' and really
did report six out-of-stock products as none.
"""

from __future__ import annotations

import pytest

from src.tools import admin_tools
from src.db.connection import get_conn

ACTOR = "00000000-0000-0000-0000-000000000000"


def _any_order_number(status: str = "CANCELLED") -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT order_number FROM orders WHERE status = %s LIMIT 1", (status,))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Inputs that must produce a structured error, never a silent empty result.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call,expected_error",
    [
        (lambda: admin_tools.admin_find_orders(ACTOR, status="NONSENSE"), "bad_status"),
        (lambda: admin_tools.admin_find_orders(ACTOR, status="delivered!"), "bad_status"),
        (lambda: admin_tools.admin_find_orders(ACTOR, start_date="last tuesday"), "bad_date"),
        (lambda: admin_tools.admin_find_orders(ACTOR, end_date="31/07/2026"), "bad_date"),
        (lambda: admin_tools.admin_find_customer(ACTOR, query=""), "empty_query"),
        (lambda: admin_tools.admin_find_customer(ACTOR, query="   "), "empty_query"),
        (lambda: admin_tools.admin_inventory_status(ACTOR, filter="all", category="tiara"), "bad_category"),
        (lambda: admin_tools.admin_sales_summary(
            ACTOR, period="custom", start_date="2026-08-01", end_date="2026-01-01"), "bad_period"),
        (lambda: admin_tools.admin_sales_summary(
            ACTOR, period="custom", start_date="nope", end_date="2026-01-01"), "bad_period"),
        (lambda: admin_tools.admin_sales_summary(ACTOR, period="custom", start_date="2026-01-01"), "bad_period"),
    ],
)
def test_bad_input_errors_instead_of_returning_a_plausible_empty_result(call, expected_error):
    result = call()
    assert result.get("error") == expected_error, result
    # The message must tell the model how to fix it, or it can't self-correct.
    assert result.get("message")


def test_status_filter_is_case_insensitive_for_valid_values():
    """The model writes 'cancelled' as often as 'CANCELLED'; that shouldn't be
    the difference between data and an error."""
    lower = admin_tools.admin_find_orders(ACTOR, status="cancelled", limit=5)
    upper = admin_tools.admin_find_orders(ACTOR, status="CANCELLED", limit=5)
    assert "error" not in lower
    assert lower["row_count"] == upper["row_count"]


@pytest.mark.parametrize("word", ["all", "any", "*", "ALL"])
def test_category_no_filter_synonyms_are_honoured(word):
    baseline = admin_tools.admin_inventory_status(ACTOR, filter="all", limit=50)["row_count"]
    assert admin_tools.admin_inventory_status(ACTOR, filter="all", category=word, limit=50)["row_count"] == baseline


# ---------------------------------------------------------------------------
# Injection / hostile strings. Everything is parameterized; prove it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "'; DROP TABLE orders;--",
        "' OR '1'='1",
        "%",           # a bare LIKE wildcard must not become "everyone"
        "_",
        "\\",
        "Ω≈ç√∫˜µ",
        "<script>alert(1)</script>",
    ],
)
def test_hostile_search_strings_are_inert(payload):
    result = admin_tools.admin_find_customer(ACTOR, query=payload, limit=5)
    assert "error" not in result or result["error"] == "empty_query"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM orders")
        assert cur.fetchone()[0] > 0, "orders table must still exist and be populated"


def test_bare_wildcard_query_does_not_dump_the_customer_base():
    """'%' is a LIKE wildcard, so an unescaped one would match every customer.
    Even if it matches, the row cap must hold."""
    result = admin_tools.admin_find_customer(ACTOR, query="%", limit=5)
    assert result["row_count"] <= 5


# ---------------------------------------------------------------------------
# Numeric and boundary behaviour.
# ---------------------------------------------------------------------------


def test_limits_are_clamped_not_trusted():
    assert admin_tools.admin_sales_breakdown(
        ACTOR, dimension="product", period="year", limit=999_999)["row_count"] <= admin_tools.MAX_ROWS
    assert admin_tools.admin_inventory_status(ACTOR, filter="all", limit=-5)["row_count"] >= 1


def test_empty_period_returns_zeroes_not_a_crash():
    """A period with no orders must not divide by zero computing AOV or the
    period-over-period change."""
    result = admin_tools.admin_sales_summary(
        ACTOR, period="custom", start_date="2019-01-01", end_date="2019-01-02")
    assert result["order_count"] == 0
    assert result["net_revenue_cents"] == 0
    assert result["aov_cents"] == 0
    assert result["previous_period"]["change_percent"] == 0
    assert result["net_revenue_display"].startswith("₹")


def test_breakdown_on_empty_period_does_not_divide_by_zero():
    result = admin_tools.admin_sales_breakdown(
        ACTOR, dimension="category", period="custom",
        start_date="2019-01-01", end_date="2019-01-02")
    assert result["rows"] == []
    assert result["period_total_cents"] == 0


def test_currency_display_never_loses_precision_against_cents():
    """Cross-check the formatter against the raw integer for a real figure —
    the off-by-100x bug this convention exists to prevent."""
    result = admin_tools.admin_sales_summary(ACTOR, period="year")
    cents = result["net_revenue_cents"]
    digits = result["net_revenue_display"].replace("₹", "").replace(",", "").replace(".", "")
    assert digits == str(cents), (result["net_revenue_display"], cents)


# ---------------------------------------------------------------------------
# Cross-tool consistency. Two tools disagreeing about revenue is how an admin
# stops trusting all of them.
# ---------------------------------------------------------------------------


def test_summary_and_breakdown_agree_on_period_total():
    summary = admin_tools.admin_sales_summary(ACTOR, period="year")
    breakdown = admin_tools.admin_sales_breakdown(ACTOR, dimension="category", period="year", limit=50)
    # Breakdown sums line items; summary sums order totals net of discount. They
    # should be within a discount's distance of each other, not wildly apart.
    assert breakdown["period_total_cents"] >= summary["net_revenue_cents"]
    drift = abs(breakdown["period_total_cents"] - summary["net_revenue_cents"])
    assert drift <= summary["discount_cent" "s"] + summary["net_revenue_cents"] * 0.02


def test_all_breakdown_dimensions_return_rows_for_a_year():
    """A dimension that silently returns nothing is indistinguishable from 'no
    sales' to the model."""
    for dim in ("month", "category", "metal", "product", "customer", "status"):
        result = admin_tools.admin_sales_breakdown(ACTOR, dimension=dim, period="year", limit=5)
        assert "error" not in result, (dim, result)
        assert result["row_count"] > 0, f"dimension '{dim}' returned no rows"


@pytest.mark.parametrize(
    "name,call",
    [
        ("find_orders", lambda: admin_tools.admin_find_orders(ACTOR, limit=5)),
        ("inventory_status", lambda: admin_tools.admin_inventory_status(ACTOR, filter="all", limit=5)),
        ("customer_profile", lambda: admin_tools.admin_customer_profile(ACTOR, "arun@shurutech.com")),
        ("order_detail", lambda: admin_tools.admin_order_detail(ACTOR, _any_order_number())),
    ],
)
def test_list_rows_expose_no_summable_cents_column(name, call):
    """Rows in a list must carry display strings only, never raw *_cents ints.

    Found by live audit, twice: given a numeric column in a list, the model sums
    it and reports the total as fact. Once it produced ₹72,49,932 for a set worth
    ₹48,60,180; once it divided raw cents by 100 itself and rendered
    ₹23,354,395.87 in Western grouping instead of ₹2,33,54,395.87. Prompt rules
    did not stop it; removing the column did. Scalar top-level totals may keep
    their _cents (tests assert against them) — it's the per-row columns that get
    summed.
    """
    result = call()

    def _row_lists(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    yield key, value
                else:
                    yield from _row_lists(value)

    for key, rows in _row_lists(result):
        for row in rows:
            offenders = [k for k in row if k.endswith("_cents")]
            assert not offenders, f"{name}.{key} rows expose summable columns: {offenders}"


def test_shares_never_exceed_one_hundred_percent():
    for dim in ("category", "metal", "status"):
        rows = admin_tools.admin_sales_breakdown(ACTOR, dimension=dim, period="year", limit=50)["rows"]
        assert sum(r["share_percent"] for r in rows) <= 100.5, dim
