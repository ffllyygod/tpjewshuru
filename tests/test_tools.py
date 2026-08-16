"""Direct tests of the tool functions against the live seeded DB.

Doesn't touch the LLM — these are the guardrail-critical functions
(eligibility, cancellation, ownership scoping) and they need to be right
independent of prompt behavior. Run with:  pytest tests/test_tools.py -v
"""

from __future__ import annotations

import uuid

from psycopg.rows import dict_row

import pytest

from src.db.connection import get_conn
from src.tools import knowledge_tools, market_tools, order_tools, product_tools


def _customer_and_order(order_number: str) -> tuple[str, str]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT customer_id FROM orders WHERE order_number = %s", (order_number,)
        )
        row = cur.fetchone()
    return str(row["customer_id"]), order_number


def _new_conversation(customer_id: str) -> str:
    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (id, customer_id) VALUES (%s, %s)", (conv_id, customer_id))
        conn.commit()
    return conv_id


def test_placed_recent_is_cancellable():
    customer_id, order_number = _customer_and_order("DPJ-10000")
    conv_id = _new_conversation(customer_id)
    result = order_tools.check_cancellation_eligibility(customer_id, order_number, conv_id)
    assert result["eligible"] is True
    assert "confirmation_token" in result


def test_confirmed_old_is_outside_window():
    customer_id, order_number = _customer_and_order("DPJ-10001")
    conv_id = _new_conversation(customer_id)
    result = order_tools.check_cancellation_eligibility(customer_id, order_number, conv_id)
    assert result["eligible"] is False
    assert result["reason"] == "window_expired"


def test_shipped_is_wrong_status():
    customer_id, order_number = _customer_and_order("DPJ-10002")
    conv_id = _new_conversation(customer_id)
    result = order_tools.check_cancellation_eligibility(customer_id, order_number, conv_id)
    assert result["eligible"] is False
    assert result["reason"] == "wrong_status"


def test_delivered_is_wrong_status():
    customer_id, order_number = _customer_and_order("DPJ-10003")
    conv_id = _new_conversation(customer_id)
    result = order_tools.check_cancellation_eligibility(customer_id, order_number, conv_id)
    assert result["eligible"] is False
    assert result["reason"] == "wrong_status"


def test_already_cancelled_is_wrong_status():
    customer_id, order_number = _customer_and_order("DPJ-10004")
    conv_id = _new_conversation(customer_id)
    result = order_tools.check_cancellation_eligibility(customer_id, order_number, conv_id)
    assert result["eligible"] is False
    assert result["reason"] == "wrong_status"


def test_wrong_customer_cannot_see_order():
    # Any customer_id that does NOT own DPJ-10000 must get not_found, never the order.
    real_customer_id, order_number = _customer_and_order("DPJ-10000")
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id FROM customers WHERE id != %s LIMIT 1", (real_customer_id,)
        )
        other_customer_id = str(cur.fetchone()["id"])

    result = order_tools.get_order_status(other_customer_id, order_number)
    assert result.get("error") == "not_found"

    conv_id = _new_conversation(other_customer_id)
    elig = order_tools.check_cancellation_eligibility(other_customer_id, order_number, conv_id)
    assert elig["eligible"] is False
    assert elig["reason"] == "not_found"


def test_cancel_without_prior_eligibility_check_rejected():
    customer_id, order_number = _customer_and_order("DPJ-10000")
    conv_id = _new_conversation(customer_id)
    result = order_tools.cancel_order(customer_id, order_number, conversation_id=conv_id, reason_code="changed_mind")
    assert result["cancelled"] is False
    assert result["reason"] == "no_pending_confirmation"


def test_eligibility_from_different_conversation_does_not_authorize_cancel():
    customer_id, order_number = _customer_and_order("DPJ-10000")
    conv_a = _new_conversation(customer_id)
    conv_b = _new_conversation(customer_id)
    elig = order_tools.check_cancellation_eligibility(customer_id, order_number, conv_a)
    assert elig["eligible"] is True

    # conv_b never called check_cancellation_eligibility — it shouldn't be able to
    # cancel just because conv_a did.
    result = order_tools.cancel_order(customer_id, order_number, conversation_id=conv_b, reason_code="changed_mind")
    assert result["cancelled"] is False
    assert result["reason"] == "no_pending_confirmation"


def test_full_cancellation_happy_path():
    # Mutates DPJ-10000 to CANCELLED — must run after the read-only checks above
    # if run in file order (pytest runs top-to-bottom by default, which is fine here).
    customer_id, order_number = _customer_and_order("DPJ-10000")
    conv_id = _new_conversation(customer_id)
    elig = order_tools.check_cancellation_eligibility(customer_id, order_number, conv_id)
    assert elig["eligible"] is True

    result = order_tools.cancel_order(customer_id, order_number, conversation_id=conv_id, reason_code="changed_mind")
    assert result["cancelled"] is True

    # The confirmation is single-use — calling again must fail even though the
    # order was cancellable, since it's no longer PLACED/CONFIRMED anyway.
    replay = order_tools.cancel_order(customer_id, order_number, conversation_id=conv_id, reason_code="changed_mind")
    assert replay["cancelled"] is False
    assert replay["reason"] in ("token_used", "wrong_status")


def test_knowledge_search_finds_cancellation_policy():
    result = knowledge_tools.search_knowledge("can I cancel my order")
    titles = [r["title"] for r in result["results"]]
    assert any("Cancellation" in t for t in titles)


def test_knowledge_search_finds_ring_sizing():
    result = knowledge_tools.search_knowledge("how do I know my ring size")
    titles = [r["title"] for r in result["results"]]
    assert any("Sizing" in t for t in titles)


def test_product_search_by_category_and_price():
    # max_price set to the top of seed_db.py's price range (₹15,000-₹8,00,000)
    # so this is guaranteed to match at least one ring regardless of the
    # random prices rolled this seed run.
    result = product_tools.search_products(category="ring", max_price=800_000)
    assert result["count"] >= 1
    assert all(r["category"] == "ring" for r in result["results"])
    assert all(r["price"] <= 800_000 for r in result["results"])


def test_product_search_demo_ring_findable():
    result = product_tools.search_products(category="ring", stone="diamond", metal="rose_gold")
    skus = [r["sku"] for r in result["results"]]
    assert "DPJ-RIN-DEMO1" in skus


def test_product_details_not_found():
    result = product_tools.get_product_details("NOPE-404")
    assert result["error"] == "not_found"


def test_metal_rates_shape():
    # Hits real free public APIs (no key). If they're down, we still want a
    # clean error dict rather than an exception — check for one or the other,
    # not a hard-fail on network flakiness in CI.
    result = market_tools.get_metal_rates()
    if "error" in result:
        assert result["error"] == "rates_unavailable"
        return
    assert result["gold_inr_per_gram"]["24k_999"] > 0
    assert result["gold_inr_per_gram"]["24k_999"] > result["gold_inr_per_gram"]["22k_916"] > result["gold_inr_per_gram"]["18k_750"]
    assert result["silver_inr_per_gram_999"] > 0
    assert "not a showroom" in result["note"] or "spot" in result["note"].lower()


# ---------------------------------------------------------------------------
# Occasion / style search, and filters that fail loudly.
# ---------------------------------------------------------------------------


def test_search_by_occasion_returns_only_pieces_made_for_it():
    result = product_tools.search_products(occasion="wedding", limit=10)
    assert result["count"] > 0
    for product in result["results"]:
        assert "wedding" in product["occasion"]


@pytest.mark.parametrize(
    "occasion",
    ["wedding", "engagement", "anniversary", "birthday", "festive", "daily", "gifting"],
)
def test_every_occasion_has_something_to_show(occasion):
    """An occasion that returns nothing reads as a broken assistant rather than
    a catalogue that happens not to stock one — seed_occasion_coverage exists
    to prevent it, and this is what proves it worked."""
    assert product_tools.search_products(occasion=occasion, limit=5)["count"] > 0


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"occasion": "valentines"}, "bad_occasion"),
        ({"style": "baroque"}, "bad_style"),
        ({"category": "tiara"}, "bad_category"),
        ({"metal": "titanium"}, "bad_metal"),
        ({"stone": "opal"}, "bad_stone"),
    ],
)
def test_an_unknown_filter_is_an_error_not_a_confident_empty_list(kwargs, expected):
    """VALID_CATEGORIES and friends were declared and never used: an unknown
    category went into the WHERE clause and came back as "we don't stock any".
    That is the same failure the admin tools were hardened against, and it was
    still live here."""
    result = product_tools.search_products(**kwargs)
    assert result["error"] == expected
    assert "Valid values" in result["message"]
    assert "results" not in result


def test_product_results_carry_what_the_advisor_needs_to_explain_a_choice():
    result = product_tools.get_product_details("DPJ-RIN-DEMO1")
    assert result["purity"] == "18K"
    assert result["occasion"]
    assert result["style"]
    assert "includes GST" in result["price_note"]


def test_an_over_narrow_search_relaxes_the_soft_filters_instead_of_dead_ending():
    """Found in live testing: asked for something minimal, for an anniversary,
    under ₹50,000, the assistant reported "no options" and stopped. A shop
    assistant would show the nearest thing. Style and occasion are OUR reading
    of a mood, so they're what gets dropped."""
    result = product_tools.search_products(occasion="anniversary", style="minimal", max_price=50000)
    assert result["count"] > 0
    assert result["relaxed_filters"] == ["style"]
    # A ready-made opening line, not an instruction to compose one: telling the
    # model to flag the mismatch didn't take, and it went on calling the
    # near-misses "minimalistic options" — the very filter that found nothing.
    assert result["say_first"].startswith("I haven't got anything minimal")
    assert "NOT what was asked for" in result["note"]


def test_relaxing_never_widens_the_budget_the_customer_named():
    """Showing someone a ₹2 lakh piece after they said ₹50,000 is worse than
    showing them nothing. Price is a hard filter, always."""
    result = product_tools.search_products(occasion="wedding", style="minimal", max_price=200)
    assert result["count"] == 0
    assert "widening the budget" in result["note"]
    assert "don't invent products" in result["note"]


def test_a_search_that_matches_exactly_reports_no_relaxation():
    result = product_tools.search_products(category="ring", limit=3)
    assert result["count"] > 0
    assert "relaxed_filters" not in result
