"""Custom design briefs and their indicative estimates.

The estimate is the risky part. A number a customer reads as a price, on a piece
nobody has made yet, is exactly the kind of thing that has to be either
defensible or absent. So these tests pin three properties: it is grounded in
real catalogue prices, it declines rather than guesses when it has no basis, and
it never silently includes a cost nobody can know (a stone that hasn't been
sourced).

And one structural property: there is no path from a design request to an order.
"""

from __future__ import annotations

import uuid

import pytest

from src.db.connection import get_conn
from src.tools import design_tools
from src.tools.design_tools import (
    estimate_custom_design,
    get_my_design_requests,
    submit_design_request,
)

REFERENCE = "DPJ-RIN-DEMO1"   # ₹3,25,000, 18K rose gold, 1.2ct diamond


@pytest.fixture
def customer() -> str:
    suffix = uuid.uuid4().hex[:8]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (name, email) VALUES (%s, %s) RETURNING id",
            (f"Design Test {suffix}", f"design-{suffix}@example.invalid"),
        )
        cid = str(cur.fetchone()[0])
        conn.commit()
    return cid


def _reference_price() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT price_cents FROM products WHERE sku = %s", (REFERENCE,))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Is the pricing basis sound?
# ---------------------------------------------------------------------------


def test_an_unchanged_piece_estimates_around_its_own_price():
    """The sanity check on the whole approach: asked to remake a piece exactly
    as it is, the range must bracket what the store already charges for it. If
    it doesn't, the rate basis is wrong and every other estimate is too."""
    estimate = estimate_custom_design(REFERENCE)
    assert estimate["estimable"] is True

    price = _reference_price()
    assert estimate["estimate_low_cents"] <= price <= estimate["estimate_high_cents"]


def test_the_estimate_is_a_range_and_says_it_is_not_a_quote():
    estimate = estimate_custom_design(REFERENCE)
    assert estimate["is_quote"] is False
    assert estimate["estimate_low_cents"] < estimate["estimate_high_cents"]
    assert "indicative" in estimate["estimate_basis"].lower()
    assert "not a quote" in estimate["estimate_basis"].lower()


def test_higher_purity_costs_more_per_gram():
    """22K is more fine gold than 18K, so the same weight must estimate higher.
    A basis that got this backwards would be worse than none."""
    at_18 = estimate_custom_design(REFERENCE, metal="gold", purity_karat=18, net_weight_grams=5)
    at_22 = estimate_custom_design(REFERENCE, metal="gold", purity_karat=22, net_weight_grams=5)
    assert at_22["estimate_low_cents"] > at_18["estimate_low_cents"]


def test_a_heavier_piece_costs_more():
    light = estimate_custom_design(REFERENCE, net_weight_grams=3)
    heavy = estimate_custom_design(REFERENCE, net_weight_grams=9)
    assert heavy["estimate_low_cents"] > light["estimate_low_cents"]


# ---------------------------------------------------------------------------
# What it refuses to price.
# ---------------------------------------------------------------------------


def test_changing_the_stone_excludes_it_and_says_so():
    """The honest hard edge. A different stone's cost depends on sourcing a
    specific stone, which nobody here knows — so the range covers metal and
    making only, and the exclusion is stated rather than buried."""
    estimate = estimate_custom_design(REFERENCE, stone="emerald")
    assert estimate["estimable"] is True
    assert estimate["assumptions"]["stone_included"] is False
    assert "NOT included" in estimate["estimate_basis"]
    # And the number is visibly smaller than the stone-bearing reference, rather
    # than quietly carrying a diamond's value on an emerald piece.
    assert estimate["estimate_high_cents"] < _reference_price()


def test_with_no_reference_and_no_weight_it_declines_to_quote_at_all():
    estimate = estimate_custom_design(metal="gold", purity_karat=22)
    assert estimate["estimable"] is False
    assert "estimate_low_cents" not in estimate
    assert "estimate_range_display" not in estimate
    # And it tells the model what to do instead of guessing.
    assert "do not offer" in estimate["next_step"].lower()


def test_a_metal_the_catalogue_cannot_price_declines_rather_than_guessing():
    """With no comparable priced-by-weight pieces there is no rate to work
    from. Saying so beats inventing one."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM products WHERE active AND metal = 'platinum' AND net_weight_grams IS NOT NULL"
        )
        has_platinum = cur.fetchone()[0] > 0
    if has_platinum:
        pytest.skip("catalogue can price platinum, so there is nothing to decline here")

    estimate = estimate_custom_design(REFERENCE, metal="platinum", net_weight_grams=5)
    assert estimate["estimable"] is False


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"stone": "opal"}, "bad_stone"),
        ({"metal": "titanium"}, "bad_metal"),
        ({"purity_karat": 9}, "bad_purity"),
        ({"engraving_text": "x" * 60}, "engraving_too_long"),
        ({"reference_sku": "NOPE-404"}, "not_found"),
    ],
)
def test_bad_inputs_error_with_the_valid_values(kwargs, expected):
    base = {"reference_sku": REFERENCE}
    base.update(kwargs)
    result = estimate_custom_design(**base)
    assert result["error"] == expected
    assert result["message"]


def test_engraving_and_sizing_trigger_the_final_sale_warning():
    """The store's own Return Policy makes these non-returnable. Saying so after
    the customer commits would be setting up a refusal we already guaranteed."""
    plain = estimate_custom_design(REFERENCE)
    assert "final_sale_warning" not in plain

    engraved = estimate_custom_design(REFERENCE, engraving_text="A&M 2026")
    assert "final sale" in engraved["final_sale_warning"].lower()
    assert "BEFORE" in engraved["final_sale_warning"]


# ---------------------------------------------------------------------------
# Filing the brief.
# ---------------------------------------------------------------------------


def test_submitting_files_the_brief_and_says_nothing_was_charged(customer):
    result = submit_design_request(
        customer, REFERENCE, metal="gold", purity_karat=22, stone="emerald",
        size="16", engraving_text="A&M 2026", occasion="anniversary",
        notes="wants a softer green stone",
    )
    assert result["submitted"] is True
    assert result["request_number"].startswith("DR-")
    assert "not a purchase" in result["message"]
    assert result["callback_note"]


def test_a_filed_estimate_is_snapshotted_and_does_not_re_price(customer):
    """A range quoted today must not silently move when the catalogue does."""
    filed = submit_design_request(customer, REFERENCE, net_weight_grams=5)
    before = get_my_design_requests(customer)["design_requests"][0]["estimate_range_display"]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE products SET price_cents = price_cents * 2 WHERE sku = %s", (REFERENCE,)
        )
        conn.commit()
    try:
        after = get_my_design_requests(customer)["design_requests"][0]["estimate_range_display"]
        assert after == before
    finally:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE products SET price_cents = price_cents / 2 WHERE sku = %s", (REFERENCE,)
            )
            conn.commit()
    assert filed["request_number"]


def test_the_spec_records_what_the_customer_actually_asked_for(customer):
    submit_design_request(
        customer, REFERENCE, metal="gold", purity_karat=22, size="16",
        engraving_text="A&M 2026", occasion="anniversary", budget_max_rupees=250000,
        notes="softer green stone",
    )
    spec = get_my_design_requests(customer)["design_requests"][0]["spec"]
    assert spec["purity_karat"] == 22
    assert spec["engraving_text"] == "A&M 2026"
    assert spec["budget_max_cents"] == 25_000_000
    assert spec["notes"] == "softer green stone"


def test_a_customer_only_sees_their_own_design_requests(customer):
    submit_design_request(customer, REFERENCE, net_weight_grams=5)

    suffix = uuid.uuid4().hex[:8]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (name, email) VALUES (%s, %s) RETURNING id",
            (f"Other {suffix}", f"other-{suffix}@example.invalid"),
        )
        other = str(cur.fetchone()[0])
        conn.commit()

    assert get_my_design_requests(customer)["count"] >= 1
    assert get_my_design_requests(other)["count"] == 0


def test_a_bad_reference_sku_files_nothing(customer):
    result = submit_design_request(customer, "NOPE-404", net_weight_grams=5)
    assert result["error"] == "not_found"
    assert get_my_design_requests(customer)["count"] == 0


# ---------------------------------------------------------------------------
# The structural limit.
# ---------------------------------------------------------------------------


def test_there_is_no_tool_that_turns_a_design_request_into_an_order():
    """An estimate is not a quote, and the only reliable way to keep it that way
    is for the capability to be absent rather than discouraged. If someone adds
    one, this test is where they should have to think about it."""
    from src.agent.orchestrator import _TOOL_IMPL

    design_tool_names = {
        name for name, fn in _TOOL_IMPL.items()
        if getattr(fn, "__module__", "") == design_tools.__name__
    }
    assert design_tool_names == {
        "estimate_custom_design", "submit_design_request",
        "get_my_design_requests", "admin_list_design_requests",
    }


def test_staff_can_see_the_queue_and_a_bad_status_is_refused(customer):
    submit_design_request(customer, REFERENCE, net_weight_grams=5)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'admin@dpjewellers.com'")
        actor = str(cur.fetchone()[0])

    queue = design_tools.admin_list_design_requests(actor, status="NEW")
    assert queue["row_count"] >= 1
    assert queue["rows"][0]["customer_email"]

    bad = design_tools.admin_list_design_requests(actor, status="PENDING")
    assert bad["error"] == "bad_status"
    assert "NEW" in bad["message"]
