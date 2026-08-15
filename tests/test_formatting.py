"""Regression tests for src/tools/formatting.py — this exists because the
model got paise->rupee conversion wrong in production more than once (see
JOURNAL.md), most recently reporting a coupon's ₹32,500 remaining balance
as ₹3,25,000 (10x too high). These lock in the exact real-world cases that
were wrong, not just abstract examples.
"""

from __future__ import annotations

from src.tools.formatting import format_inr


def test_the_actual_production_bug_case():
    # This is the real coupon balance that was misreported as ₹3,25,000.
    assert format_inr(3_250_000) == "₹32,500.00"


def test_lakh_grouping():
    assert format_inr(32_500_000) == "₹3,25,000.00"


def test_crore_grouping():
    assert format_inr(150_000_000_00) == "₹15,00,00,000.00"


def test_small_amount_no_grouping_needed():
    assert format_inr(50_000) == "₹500.00"


def test_zero():
    assert format_inr(0) == "₹0.00"


def test_with_paise_remainder():
    # A real seeded product price with a fractional-rupee paise component.
    assert format_inr(12_209_497) == "₹1,22,094.97"


def test_negative():
    assert format_inr(-32_500_000) == "-₹3,25,000.00"
