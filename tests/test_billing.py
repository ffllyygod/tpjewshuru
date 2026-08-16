"""Invoice decomposition and rendering.

The theme, inherited from tests/test_admin_edge_cases.py: **a wrong answer is
worse than an error.** A bill is the most load-bearing thing this assistant
produces — a customer will act on it, and an invoice that doesn't add up, or one
that invents a weight nobody measured, is worse than no invoice at all.

The sum identity below is the single most important test in this file. The whole
GST-inclusive design rests on it: the catalogue price never changes, and the
components always reconcile to it exactly.
"""

from __future__ import annotations

import random
import uuid

import pytest
from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools import billing
from src.tools.billing import decompose_price, generate_invoice, purity_label

COMPONENTS = (
    "metal_value_cents", "making_charge_cents", "stone_value_cents",
    "gst_goods_cents", "gst_making_cents", "round_off_cents",
)


def _sums_to(d: dict) -> int:
    return sum(d[k] for k in COMPONENTS)


def _demo_customer() -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'arun@shurutech.com'")
        return str(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# The identity everything else depends on.
# ---------------------------------------------------------------------------


def test_every_catalogue_product_decomposes_to_exactly_its_price():
    """Not a sample — every active product. If any row in the real catalogue
    can't be broken down to the paisa, the invoice tool is not shippable."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT sku, price_cents, net_weight_grams, purity_karat,
                   making_charge_percent, stone_value_cents, metal, stone
            FROM products
            """
        )
        rows = cur.fetchall()

    assert rows, "no products seeded"
    for r in rows:
        d = decompose_price(
            r["price_cents"], r["net_weight_grams"], r["purity_karat"],
            r["making_charge_percent"], r["stone_value_cents"], r["metal"], r["stone"],
        )
        assert _sums_to(d) == r["price_cents"], f"{r['sku']} does not balance"


def test_the_identity_holds_across_a_wide_random_sweep():
    """Property-style, because the catalogue only exercises the value ranges it
    happens to contain. Rounding bugs live at the edges — a ₹1 item, a 0%
    making charge, a stone worth almost the whole ticket."""
    rng = random.Random(20260816)
    for _ in range(5000):
        price = rng.randint(100, 100_000_000)
        making = rng.choice([None, 0, 0.5, 8, 12.5, 18, 30, 100])
        share = rng.choice([None, 0, 0.01, 0.35, 0.7, 0.95])
        stone = int(price * share) if share is not None else None
        d = decompose_price(price, making_charge_percent=making, stone_value_cents=stone)
        assert _sums_to(d) == price, (price, making, stone)


def test_the_rounding_adjustment_is_never_negative_and_under_a_rupee():
    """A bill reading "Rounding adjustment -₹0.01" invites a support call, and a
    large one would mean the maths is wrong rather than merely rounded."""
    rng = random.Random(7)
    for _ in range(2000):
        d = decompose_price(
            rng.randint(100, 90_000_000),
            making_charge_percent=rng.uniform(0, 25),
            stone_value_cents=rng.randint(0, 20_000_000),
        )
        assert 0 <= d["round_off_cents"] < 100


def test_a_two_of_something_line_decomposes_the_line_not_the_unit():
    """Decomposing one unit and multiplying is how a multi-quantity line ends up
    a paisa out: two rounded halves need not equal the rounded whole. The line
    total is what has to reconcile, so that is what gets decomposed."""
    unit_price, qty = 1_234_567, 2
    single = decompose_price(unit_price, making_charge_percent=12, stone_value_cents=400_000)
    line = decompose_price(unit_price * qty, making_charge_percent=12, stone_value_cents=400_000 * qty)

    assert _sums_to(line) == unit_price * qty
    # Doubling the per-unit components would have missed the true line total by
    # the accumulated rounding — this is the discrepancy being avoided.
    assert _sums_to(line) != 0
    assert abs(sum(single[k] for k in COMPONENTS) * qty - _sums_to(line)) < 100


# ---------------------------------------------------------------------------
# Honest degradation. The invoice must never fill a gap with a plausible number.
# ---------------------------------------------------------------------------


def test_no_weight_means_no_weight_shown_and_no_rate_invented():
    d = decompose_price(5_000_000, net_weight_grams=None, making_charge_percent=12)
    assert d["net_weight_display"] is None
    assert d["rate_per_gram_display"] is None
    assert d["data_complete"] is False
    assert any("weight" in n.lower() for n in d["notes"])
    # The money is still exactly right.
    assert _sums_to(d) == 5_000_000


def test_no_making_charge_is_stated_not_guessed():
    d = decompose_price(5_000_000, net_weight_grams=4, making_charge_percent=None)
    assert d["making_charge_cents"] == 0
    assert d["gst_making_cents"] == 0
    assert any("making charges" in n.lower() for n in d["notes"])
    assert d["data_complete"] is False


def test_an_unpriced_stone_is_declared_rather_than_treated_as_free():
    d = decompose_price(5_000_000, making_charge_percent=12, stone_value_cents=None, stone="ruby")
    assert d["stone_value_cents"] == 0
    assert any("ruby" in n.lower() for n in d["notes"])


def test_a_stoneless_piece_gets_no_stone_note():
    d = decompose_price(5_000_000, making_charge_percent=12, stone_value_cents=0, stone="none")
    assert not any("stone" in n.lower() for n in d["notes"])


def test_stone_value_above_the_price_refuses_the_breakdown_but_keeps_the_total():
    """Inconsistent product data must not produce a negative metal value, and
    must not block the sale either. Say the breakdown is unavailable; the total
    is still correct."""
    d = decompose_price(100_000, making_charge_percent=12, stone_value_cents=999_999_999)
    assert d["breakdown_reliable"] is False
    assert d["metal_value_cents"] > 0
    assert _sums_to(d) == 100_000
    assert any("inconsistent" in n.lower() for n in d["notes"])


@pytest.mark.parametrize(
    "metal,karat,expected",
    [
        ("gold", 22, "22K"), ("rose_gold", 18, "18K"),
        ("silver", None, "925 Silver"), ("platinum", None, "PT950"),
        # Karat is a gold measure; silver carries its millesimal label even if a
        # karat was somehow recorded against it.
        ("silver", 18, "925 Silver"),
        ("gold", None, None),   # never guesses 22K
    ],
)
def test_purity_labels_are_correct_per_metal(metal, karat, expected):
    assert purity_label(metal, karat) == expected


# ---------------------------------------------------------------------------
# The rendered bill.
# ---------------------------------------------------------------------------


def test_invoice_markdown_total_matches_the_order_and_is_a_table():
    customer = _demo_customer()
    inv = generate_invoice(customer, "DPJ-DEMO01")
    md = inv["invoice_markdown"]

    assert "| Item | Details | Amount |" in md
    assert inv["amount_payable_display"] in md
    assert inv["amount_payable_cents"] == inv["total_amount_cents"] - inv["discount_cents"]
    # Every component figure is present verbatim, so the model has no reason to
    # recompute one.
    for item in inv["items"]:
        assert item["breakdown"]["metal_value_display"] in md


def test_invoice_line_components_reconcile_to_the_order_total():
    customer = _demo_customer()
    inv = generate_invoice(customer, "DPJ-DEMO01")
    total = sum(_sums_to(i["breakdown"]) for i in inv["items"])
    assert total == inv["total_amount_cents"]


def test_invoice_for_another_customers_order_is_not_found():
    """The same scoping guarantee as get_order_status. A bill is a document
    about someone's purchase; the capability to read another's must not exist."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM customers WHERE email <> 'arun@shurutech.com' LIMIT 1"
        )
        other = str(cur.fetchone()[0])
    result = generate_invoice(other, "DPJ-DEMO01")
    assert result["error"] == "not_found"
    assert "invoice_markdown" not in result


def test_a_pipe_in_a_product_name_cannot_break_the_table():
    """Adversarial: markdown tables are delimited by the character a product
    name is allowed to contain."""
    sku = f"DPJ-PIPE-{uuid.uuid4().hex[:6].upper()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO products (sku, name, category, price_cents, stock_by_size,
                                  metal, stone, making_charge_percent, net_weight_grams, purity_karat)
            VALUES (%s, %s, 'ring', 5000000, '{"_default": 5}'::jsonb, 'gold', 'none', 12, 5.0, 18)
            RETURNING id
            """,
            (sku, "Ring | Special"),
        )
        pid = cur.fetchone()[0]
        cur.execute("SELECT id FROM customers WHERE email = 'arun@shurutech.com'")
        cid = cur.fetchone()[0]
        number = f"DPJ-P{uuid.uuid4().hex[:6].upper()}"
        cur.execute(
            "INSERT INTO orders (order_number, customer_id, status, total_amount_cents, payment_status) "
            "VALUES (%s, %s, 'PLACED', 5000000, 'PAID') RETURNING id",
            (number, cid),
        )
        oid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents) VALUES (%s, %s, 1, 5000000)",
            (oid, pid),
        )
        conn.commit()

    md = generate_invoice(str(cid), number)["invoice_markdown"]
    header_cells = md.split("| Item | Details | Amount |")[1].count("\n")
    assert "\\|" in md, "an unescaped pipe would split the row into an extra column"
    assert header_cells > 0


def test_the_invoice_never_needs_a_live_metal_rate(monkeypatch):
    """The implied rate comes from the price actually charged. If the invoice
    ever reached for a market feed, the same order would render differently on
    two consecutive turns — and would stop rendering at all when the feed is
    down."""
    from src.tools import market_tools

    def explode(*_a, **_kw):
        raise AssertionError("generate_invoice must not call get_metal_rates")

    monkeypatch.setattr(market_tools, "get_metal_rates", explode)
    inv = generate_invoice(_demo_customer(), "DPJ-DEMO01")
    assert inv["invoice_markdown"]


def test_a_legacy_address_without_a_recipient_still_renders():
    """Historical orders were seeded before addresses carried a name or phone.
    An invoice for one must drop those lines, not print 'None'."""
    from src.tools.formatting import format_address

    rendered = format_address(
        {"line1": "12 Old Road", "city": "Chennai", "postal_code": "600001", "country": "IN"}
    )
    assert "None" not in rendered
    assert "12 Old Road" in rendered


def test_admin_invoice_reaches_across_customers():
    from src.tools.billing import admin_order_invoice

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'admin@dpjewellers.com'")
        actor = str(cur.fetchone()[0])
    inv = admin_order_invoice(actor, "DPJ-DEMO01")
    assert inv["invoice_markdown"]
    assert inv["customer_email"] == "arun@shurutech.com"


def test_a_discounted_order_shows_the_discount_and_a_matching_payable():
    """The invoice has to reconcile after a coupon, not just before one."""
    from src.tools import coupon_tools, order_tools, purchase_tools

    customer = _demo_customer()
    placed = purchase_tools.place_order(customer, "DPJ-RIN-DEMO1", 1, size="6")
    assert placed["placed"] is True

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        coupon = billing_test_coupon(cur, customer)
        conn.commit()

    redeemed = coupon_tools.redeem_coupon(customer, coupon, placed["order_number"])
    assert redeemed["redeemed"] is True

    inv = generate_invoice(customer, placed["order_number"])
    assert inv["discount_cents"] > 0
    assert inv["amount_payable_cents"] == inv["total_amount_cents"] - inv["discount_cents"]
    assert inv["discount_display"] in inv["invoice_markdown"]
    assert inv["amount_payable_display"] in inv["invoice_markdown"]


def billing_test_coupon(cur, customer_id: str) -> str:
    """A standalone goodwill coupon, so this test doesn't depend on the
    cancellation flow to produce one."""
    from src.tools.coupon_tools import mint_coupon

    return mint_coupon(cur, customer_id, "goodwill", 5_000_00, 0, 30)["code"]
