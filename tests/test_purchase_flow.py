"""Address, ordering and payment.

`place_order` used to write `payment_status = 'PAID'` unconditionally: an order
was born paid, with no payment step having happened. It now creates orders
PENDING and `confirm_payment` moves them, which makes "cancelled before paying"
a real state for the first time — and that state is where the money bug lives.
`test_settlement_refuses_on_an_order_that_was_never_paid` is the important test
in this file.

Real DB, no mocking, per the rest of the suite.
"""

from __future__ import annotations

import uuid

import pytest
from psycopg.rows import dict_row

from tests.conftest import give_default_address
from src.db.connection import get_conn
from src.tools import admin_tools, coupon_tools, purchase_tools
from src.tools.address_tools import get_my_addresses, save_address


@pytest.fixture(scope="module")
def sku() -> str:
    """A throwaway product with deep stock.

    Deliberately not the shared demo ring: these tests place a lot of orders,
    and draining a seeded product's stock would make them fail on the second run
    and quietly change what other tests see.
    """
    code = f"DPJ-PAY-{uuid.uuid4().hex[:6].upper()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO products (sku, name, category, price_cents, metal, stone,
                                  sizes_available, stock_by_size,
                                  purity_karat, making_charge_percent, stone_value_cents,
                                  net_weight_grams, occasion, style)
            VALUES (%s, %s, 'ring', 5000000, 'gold', 'diamond', '["6"]'::jsonb,
                    '{"6": 500}'::jsonb, 18, 12, 2000000, 3.5,
                    ARRAY['engagement'], 'classic')
            """,
            (code, f"Payment Test Ring {code[-6:]}"),
        )
        conn.commit()
    return code


@pytest.fixture
def customer() -> str:
    """A throwaway customer with one saved address."""
    suffix = uuid.uuid4().hex[:8]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (name, email) VALUES (%s, %s) RETURNING id",
            (f"Pay Test {suffix}", f"pay-test-{suffix}@example.invalid"),
        )
        cid = str(cur.fetchone()[0])
        conn.commit()
    give_default_address(cid)
    return cid


@pytest.fixture
def addressless_customer() -> str:
    suffix = uuid.uuid4().hex[:8]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (name, email) VALUES (%s, %s) RETURNING id",
            (f"No Addr {suffix}", f"no-addr-{suffix}@example.invalid"),
        )
        cid = str(cur.fetchone()[0])
        conn.commit()
    return cid


def _order_row(order_number: str) -> dict:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status::text AS status, payment_status, payment_method, payment_reference, "
            "paid_at, shipping_address FROM orders WHERE order_number = %s",
            (order_number,),
        )
        return cur.fetchone()


def _coupon_count(customer_id: str) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM coupons WHERE customer_id = %s", (customer_id,))
        return cur.fetchone()[0]


def _cancel(order_number: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET status = 'CANCELLED', cancellation_reason_code = 'other', "
            "cancellation_reason_note = 'test' WHERE order_number = %s",
            (order_number,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Placing no longer means paying.
# ---------------------------------------------------------------------------


def test_place_order_leaves_the_order_awaiting_payment(customer, sku):
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    assert placed["placed"] is True
    assert placed["payment_status"] == "PENDING"

    row = _order_row(placed["order_number"])
    assert row["payment_status"] == "PENDING"
    assert row["paid_at"] is None
    assert row["payment_reference"] is None


def test_place_order_snapshots_the_delivery_address(customer, sku):
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    snapshot = _order_row(placed["order_number"])["shipping_address"]
    assert snapshot["postal_code"] == "560001"
    assert snapshot["recipient_name"] == "Test Recipient"
    assert placed["shipping_address_display"]


def test_editing_a_saved_address_does_not_rewrite_a_placed_order(customer, sku):
    """The whole reason orders carry a snapshot rather than a foreign key."""
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    before = _order_row(placed["order_number"])["shipping_address"]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE customer_addresses SET line1 = 'Somewhere Else', postal_code = '600001' "
            "WHERE customer_id = %s",
            (customer,),
        )
        conn.commit()

    assert _order_row(placed["order_number"])["shipping_address"] == before


def test_place_order_echoes_back_what_it_actually_bought(customer, sku):
    """JOURNAL.md records the model describing the right product in prose while
    ordering a wrong-but-valid SKU. The tool naming what it did is how a
    mismatch becomes visible instead of silent."""
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    item = placed["items"][0]
    assert item["sku"] == sku
    assert item["name"].startswith("Payment Test Ring")
    assert item["size"] == "6"


def test_an_order_cannot_be_placed_with_nowhere_to_send_it(addressless_customer, sku):
    result = purchase_tools.place_order(addressless_customer, sku, 1, size="6")
    assert result["placed"] is False
    assert result["reason"] == "address_required"
    assert "PIN" in result["message"]


def test_another_customers_address_cannot_be_used_and_places_no_order(customer, addressless_customer, sku):
    """Cross-customer, and it must fail BEFORE any stock is taken."""
    theirs = get_my_addresses(customer)["addresses"][0]["address_id"]

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT stock_by_size FROM products WHERE sku = %s", (sku,))
        stock_before = cur.fetchone()[0]

    result = purchase_tools.place_order(addressless_customer, sku, 1, size="6", address_id=theirs)
    assert result["placed"] is False
    assert result["reason"] == "address_not_found"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT stock_by_size FROM products WHERE sku = %s", (sku,))
        assert cur.fetchone()[0] == stock_before, "stock was decremented for an order never created"
        cur.execute("SELECT COUNT(*) FROM orders WHERE customer_id = %s", (addressless_customer,))
        assert cur.fetchone()[0] == 0


def test_a_malformed_address_id_is_an_error_not_a_crash(customer, sku):
    result = purchase_tools.place_order(customer, sku, 1, size="6", address_id="not-a-uuid")
    assert result["placed"] is False
    assert result["reason"] == "address_not_found"


# ---------------------------------------------------------------------------
# Payment.
# ---------------------------------------------------------------------------


def test_confirm_payment_moves_pending_to_paid_with_a_reference(customer, sku):
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    paid = purchase_tools.confirm_payment(customer, placed["order_number"], "upi")

    assert paid["confirmed"] is True
    assert paid["payment_status"] == "PAID"
    assert paid["payment_reference"].startswith("DPJ-PAY-")
    assert paid["amount_paid_cents"] == placed["total_amount_cents"]

    row = _order_row(placed["order_number"])
    assert row["payment_status"] == "PAID"
    assert row["payment_method"] == "UPI"
    assert row["paid_at"] is not None


def test_paying_twice_reports_the_existing_payment_and_mints_no_second_reference(customer, sku):
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    first = purchase_tools.confirm_payment(customer, placed["order_number"], "upi")
    second = purchase_tools.confirm_payment(customer, placed["order_number"], "card")

    assert second["confirmed"] is False
    assert second["reason"] == "already_paid"
    assert second["payment_reference"] == first["payment_reference"]
    # The second call must not have overwritten the method either.
    assert _order_row(placed["order_number"])["payment_method"] == "UPI"


def test_cash_on_delivery_records_the_method_but_does_not_mark_it_paid(customer, sku):
    """COD means the money is collected at the door, by someone else, later.
    Writing PAID would put a fact in the database that nobody observed."""
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    result = purchase_tools.confirm_payment(customer, placed["order_number"], "COD")

    assert result["confirmed"] is True
    assert result["payment_status"] == "PENDING"
    assert result["collected_on_delivery"] is True

    row = _order_row(placed["order_number"])
    assert row["payment_status"] == "PENDING"
    assert row["payment_method"] == "COD"
    assert row["paid_at"] is None


def test_a_cancelled_order_cannot_be_paid(customer, sku):
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    _cancel(placed["order_number"])
    result = purchase_tools.confirm_payment(customer, placed["order_number"], "upi")
    assert result["confirmed"] is False
    assert result["reason"] == "wrong_status"


def test_another_customers_order_cannot_be_paid(customer, addressless_customer, sku):
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    result = purchase_tools.confirm_payment(addressless_customer, placed["order_number"], "upi")
    assert result["confirmed"] is False
    assert result["reason"] == "not_found"
    assert _order_row(placed["order_number"])["payment_status"] == "PENDING"


def test_an_unknown_payment_method_is_refused_with_the_valid_list(customer, sku):
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    result = purchase_tools.confirm_payment(customer, placed["order_number"], "bitcoin")
    assert result["confirmed"] is False
    assert result["reason"] == "bad_payment_method"
    assert "UPI" in result["message"]


# ---------------------------------------------------------------------------
# The money bug.
# ---------------------------------------------------------------------------


def test_settlement_refuses_on_an_order_that_was_never_paid(customer, sku):
    """Since orders start PENDING, cancelling before paying is routine. Without
    this guard the store would issue store credit worth 110% of a payment that
    never happened, and tell the customer they were being refunded something
    they were never charged."""
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    _cancel(placed["order_number"])

    offer = coupon_tools.offer_settlement_options(customer, placed["order_number"])
    assert offer["error"] == "nothing_to_settle"

    issued = coupon_tools.issue_coupon(customer, placed["order_number"], str(uuid.uuid4()))
    assert issued["error"] == "nothing_to_settle"

    refund = coupon_tools.request_cash_refund(customer, placed["order_number"])
    assert refund["error"] == "nothing_to_settle"

    assert _coupon_count(customer) == 0, "store credit was issued for money never collected"


def test_settlement_still_works_on_an_order_that_was_paid(customer, sku):
    """The guard must block only the unpaid case — a real refund must still be
    offered, or the fix would be worse than the bug."""
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    purchase_tools.confirm_payment(customer, placed["order_number"], "upi")
    _cancel(placed["order_number"])

    offer = coupon_tools.offer_settlement_options(customer, placed["order_number"])
    assert offer.get("error") is None
    assert offer["already_settled"] is False
    assert offer["cash_refund_cents"] == placed["total_amount_cents"]


def test_admin_revenue_is_unmoved_by_the_pending_default(customer, sku):
    """Revenue filters on order status, not payment status. Placing an unpaid
    order must move the reported figure by exactly its value — i.e. reporting
    behaves as it did before, which is the documented decision."""
    before = admin_tools.admin_sales_summary("00000000-0000-0000-0000-000000000000", period="today")
    placed = purchase_tools.place_order(customer, sku, 1, size="6")
    after = admin_tools.admin_sales_summary("00000000-0000-0000-0000-000000000000", period="today")

    assert after["net_revenue_cents"] - before["net_revenue_cents"] == placed["total_amount_cents"]


def test_seeded_history_orders_were_not_retroactively_unpaid():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM orders WHERE order_number LIKE 'DPJ-H%' AND payment_status = 'PENDING'"
        )
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Address validation — loud, and in the tool rather than the prompt.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("postal_code", "56002", "invalid_postal_code"),
        ("postal_code", "056002", "invalid_postal_code"),
        ("postal_code", "abcdef", "invalid_postal_code"),
        ("phone", "12345", "invalid_phone"),
        ("phone", "0801234567", "invalid_phone"),      # landline, not a mobile
        ("state", "Bavaria", "invalid_state"),
        ("line1", "x", "invalid_line1"),
        ("recipient_name", "  ", "missing_recipient"),
    ],
)
def test_bad_address_fields_error_with_something_actionable(customer, field, value, expected):
    args = dict(
        recipient_name="Arun", phone="9845012345", line1="12 MG Road",
        city="Bengaluru", state="Karnataka", postal_code="560001",
    )
    args[field] = value
    result = save_address(customer, **args, make_default=False)
    assert result["saved"] is False
    assert result["error"] == expected
    assert result["message"]


@pytest.mark.parametrize("raw", ["+91 98450 12345", "098450-12345", "98450 12345"])
def test_phone_formats_people_actually_type_are_normalised(customer, raw):
    result = save_address(
        customer, recipient_name="Arun", phone=raw, line1="12 MG Road",
        city="Bengaluru", state="Karnataka", postal_code="560001", make_default=False,
    )
    assert result["saved"] is True
    assert "9845012345" in result["formatted"]


def test_a_state_code_is_canonicalised_by_the_tool_not_the_model(customer, sku):
    result = save_address(
        customer, recipient_name="Arun", phone="9845012345", line1="12 MG Road",
        city="Bengaluru", state="ka", postal_code="560001", make_default=False,
    )
    assert result["saved"] is True
    assert "Karnataka" in result["formatted"]


def test_a_pin_that_belongs_to_another_state_is_flagged_and_not_saved(customer, sku):
    """The check a model would never make. Soft, because circle boundaries are
    fuzzy — but not saved until a human confirms."""
    before = get_my_addresses(customer)["count"]
    result = save_address(
        customer, recipient_name="Arun", phone="9845012345", line1="12 MG Road",
        city="Chennai", state="Tamil Nadu", postal_code="560001", make_default=False,
    )
    assert result["saved"] is False
    assert result["error"] == "pin_state_mismatch"
    assert "Karnataka" in result["message"]
    assert get_my_addresses(customer)["count"] == before


def test_a_flagged_mismatch_can_be_overridden_once_the_customer_confirms(customer, sku):
    result = save_address(
        customer, recipient_name="Arun", phone="9845012345", line1="12 MG Road",
        city="Chennai", state="Tamil Nadu", postal_code="560001",
        make_default=False, confirm_mismatch=True,
    )
    assert result["saved"] is True


def test_an_unknown_pin_prefix_is_no_opinion_rather_than_a_mismatch(customer, sku):
    """The prefix map is deliberately incomplete. A prefix it doesn't cover must
    pass silently — wrongly rejecting a real address is worse than not checking."""
    result = save_address(
        customer, recipient_name="Arun", phone="9845012345", line1="12 MG Road",
        city="Ranchi", state="Jharkhand", postal_code="834001", make_default=False,
    )
    assert result["saved"] is True


def test_only_one_default_address_can_exist(customer, sku):
    save_address(
        customer, recipient_name="Arun", phone="9845012345", line1="12 MG Road",
        city="Bengaluru", state="Karnataka", postal_code="560001", make_default=True,
    )
    save_address(
        customer, recipient_name="Arun", phone="9845012345", line1="99 Church Street",
        city="Bengaluru", state="Karnataka", postal_code="560002", make_default=True,
    )
    addresses = get_my_addresses(customer)["addresses"]
    assert sum(1 for a in addresses if a["is_default"]) == 1
    assert addresses[0]["line1"] == "99 Church Street"


def test_addresses_are_scoped_to_their_owner(customer, addressless_customer, sku):
    assert get_my_addresses(addressless_customer)["count"] == 0
    assert get_my_addresses(customer)["count"] >= 1


def test_cancelling_tells_the_model_whether_there_is_anything_to_settle(customer, sku):
    """Live testing caught the model cancelling an unpaid order and then simply
    not mentioning settlement either way. Whether money was taken is a fact
    cancel_order already holds, so it says so rather than the prompt hoping."""
    from src.tools import order_tools

    conv = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (id, customer_id) VALUES (%s, %s)", (conv, customer))
        conn.commit()

    unpaid = purchase_tools.place_order(customer, sku, 1, size="6")
    order_tools.check_cancellation_eligibility(customer, unpaid["order_number"], conv)
    result = order_tools.cancel_order(customer, unpaid["order_number"], conv, "found_better_price")
    assert result["cancelled"] is True
    assert "never charged" in result["next_step"]
    assert "do NOT call offer_settlement_options" in result["next_step"]

    conv2 = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (id, customer_id) VALUES (%s, %s)", (conv2, customer))
        conn.commit()

    paid = purchase_tools.place_order(customer, sku, 1, size="6")
    purchase_tools.confirm_payment(customer, paid["order_number"], "upi")
    order_tools.check_cancellation_eligibility(customer, paid["order_number"], conv2)
    result = order_tools.cancel_order(customer, paid["order_number"], conv2, "found_better_price")
    assert result["cancelled"] is True
    assert "offer_settlement_options NOW" in result["next_step"]
