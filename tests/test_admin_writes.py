"""Tests for the staff write tools.

These are the only tools in the codebase that change data belonging to someone
who is not in the conversation, so the negative cases matter more than the happy
paths. Most of what follows asserts that a write did NOT happen.

Fixtures here are deliberately self-contained — each test creates its own
customer/product/order rather than mutating seeded rows. The existing
cancellation test does the opposite (it really cancels TPJ-10000), which is why
tests/conftest.py has to exist at all; not repeating that.
"""

from __future__ import annotations

import uuid

import pytest

from src.db.connection import get_conn
from src.tools import admin_tools


def _admin_id() -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'admin@tpjewellers.com'")
        return str(cur.fetchone()[0])


@pytest.fixture(scope="module")
def actor() -> str:
    return _admin_id()


@pytest.fixture
def conversation(actor) -> str:
    """A fresh admin-mode conversation. Confirmation tokens are scoped to one of
    these, so every test needs its own or they'd authorise each other."""
    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, customer_id, mode) VALUES (%s, %s, 'admin')",
            (conv_id, actor),
        )
        conn.commit()
    return conv_id


@pytest.fixture
def customer() -> dict:
    """A throwaway customer, so coupons issued in these tests can't disturb the
    counts other tests assert on for the seeded ones."""
    suffix = uuid.uuid4().hex[:8]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (name, email, phone) VALUES (%s, %s, %s) RETURNING id",
            (f"Write Test {suffix}", f"write-test-{suffix}@example.invalid", "+91 90000 00000"),
        )
        cid = str(cur.fetchone()[0])
        conn.commit()
    return {"id": cid, "email": f"write-test-{suffix}@example.invalid"}


@pytest.fixture
def order(customer) -> str:
    """A PLACED order for the throwaway customer. Returns its order_number."""
    number = f"TPJ-T{uuid.uuid4().hex[:6].upper()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO orders (order_number, customer_id, status, total_amount_cents, payment_status)
            VALUES (%s, %s, 'PLACED', 5000000, 'PAID')
            """,
            (number, customer["id"]),
        )
        conn.commit()
    return number


@pytest.fixture
def product() -> dict:
    """A throwaway product with two sizes, so size-resolution has something
    genuinely ambiguous to refuse."""
    sku = f"TPJ-TST-{uuid.uuid4().hex[:6].upper()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO products (sku, name, category, price_cents, stock_by_size)
            VALUES (%s, %s, 'ring', 1000000, '{"6": 4, "7": 9}'::jsonb)
            RETURNING id
            """,
            (sku, f"Write Test Ring {sku[-6:]}"),
        )
        pid = str(cur.fetchone()[0])
        conn.commit()
    return {"id": pid, "sku": sku}


def _order_status(order_number: str) -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status::text FROM orders WHERE order_number = %s", (order_number,))
        return cur.fetchone()[0]


def _stock(sku: str) -> dict:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT stock_by_size FROM products WHERE sku = %s", (sku,))
        return cur.fetchone()[0]


def _coupon_count(customer_id: str) -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM coupons WHERE customer_id = %s", (customer_id,))
        return cur.fetchone()[0]


def _audit_rows(action: str, target_id: str) -> list[dict]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT actor_customer_id, conversation_id, before_state, after_state, reason
            FROM admin_action_log WHERE action = %s AND target_id = %s
            """,
            (action, target_id),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


REASON = "test: customer called about a sizing mistake"


# ---------------------------------------------------------------------------
# The core guarantee: no write without a preview in THIS conversation.
# ---------------------------------------------------------------------------


def test_cancel_without_preview_is_refused(actor, conversation, order):
    result = admin_tools.admin_cancel_order(actor, order, REASON, conversation)
    assert result["applied"] is False
    assert result["reason"] == "no_pending_confirmation"
    assert _order_status(order) == "PLACED"


def test_adjust_stock_without_preview_is_refused(actor, conversation, product):
    before = _stock(product["sku"])
    result = admin_tools.admin_adjust_stock(actor, product["sku"], 99, REASON, conversation, size="6")
    assert result["applied"] is False
    assert result["reason"] == "no_pending_confirmation"
    assert _stock(product["sku"]) == before


def test_goodwill_coupon_without_preview_is_refused(actor, conversation, customer):
    result = admin_tools.admin_issue_goodwill_coupon(actor, customer["email"], 1000, REASON, conversation)
    assert result["applied"] is False
    assert result["reason"] == "no_pending_confirmation"
    assert _coupon_count(customer["id"]) == 0


def test_preview_alone_changes_nothing(actor, conversation, order, product, customer):
    """A preview that is never confirmed must leave the world untouched — it's
    the tool the model is expected to call speculatively."""
    stock_before = _stock(product["sku"])

    assert admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)["preview"] is True
    assert admin_tools.admin_preview_stock_adjustment(actor, product["sku"], 1, REASON, conversation, size="6")["preview"] is True
    assert admin_tools.admin_preview_goodwill_coupon(actor, customer["email"], 500, REASON, conversation)["preview"] is True

    assert _order_status(order) == "PLACED"
    assert _stock(product["sku"]) == stock_before
    assert _coupon_count(customer["id"]) == 0


# ---------------------------------------------------------------------------
# Token scope: conversation, single use, and the params it was minted for.
# ---------------------------------------------------------------------------


def test_token_from_another_conversation_is_not_accepted(actor, conversation, order):
    """The direct analogue of the customer-side replay test. A confirmation
    obtained in one session must not authorise a write in another."""
    admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)

    other = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, customer_id, mode) VALUES (%s, %s, 'admin')",
            (other, actor),
        )
        conn.commit()

    result = admin_tools.admin_cancel_order(actor, order, REASON, other)
    assert result["applied"] is False
    assert result["reason"] == "no_pending_confirmation"
    assert _order_status(order) == "PLACED"


def test_confirmation_cannot_be_replayed(actor, conversation, order):
    admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)
    assert admin_tools.admin_cancel_order(actor, order, REASON, conversation)["applied"] is True

    again = admin_tools.admin_cancel_order(actor, order, REASON, conversation)
    assert again["applied"] is False
    assert again["reason"] == "token_used"


@pytest.mark.parametrize(
    "preview_amount,apply_amount",
    [
        (500, 50_000),      # the decimal-slip case this column exists for
        (500, 501),
    ],
)
def test_coupon_amount_cannot_drift_from_what_was_previewed(
    actor, conversation, customer, preview_amount, apply_amount,
):
    """A token is scoped to (conversation, action, target) — and a customer id is
    a stable target, so without params in the token, previewing ₹500 for someone
    would equally authorise ₹50,000 for them."""
    admin_tools.admin_preview_goodwill_coupon(actor, customer["email"], preview_amount, REASON, conversation)

    result = admin_tools.admin_issue_goodwill_coupon(actor, customer["email"], apply_amount, REASON, conversation)
    assert result["applied"] is False
    assert result["reason"] == "params_changed"
    assert _coupon_count(customer["id"]) == 0


def test_stock_quantity_cannot_drift_from_what_was_previewed(actor, conversation, product):
    admin_tools.admin_preview_stock_adjustment(actor, product["sku"], 5, REASON, conversation, size="6")

    result = admin_tools.admin_adjust_stock(actor, product["sku"], 400, REASON, conversation, size="6")
    assert result["applied"] is False
    assert result["reason"] == "params_changed"
    assert _stock(product["sku"])["6"] == 4


def test_reason_cannot_drift_from_what_was_previewed(actor, conversation, order):
    """The reason is what the audit log records. If the apply could pass a
    different one, the log would describe an action nobody confirmed."""
    admin_tools.admin_preview_order_cancellation(actor, order, "stock was damaged in transit", conversation)

    result = admin_tools.admin_cancel_order(actor, order, "customer changed their mind", conversation)
    assert result["applied"] is False
    assert result["reason"] == "params_changed"
    assert _order_status(order) == "PLACED"


def test_a_failed_apply_does_not_burn_the_confirmation(actor, conversation, order):
    """A rejected write must leave the confirmation usable, or a single fumbled
    argument would force the staff member through the whole flow again."""
    assert admin_tools.admin_cancel_order(actor, order, "a different reason entirely", conversation)["applied"] is False
    admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)
    assert admin_tools.admin_cancel_order(actor, "TPJ-NOSUCHORDER", REASON, conversation)["applied"] is False

    assert admin_tools.admin_cancel_order(actor, order, REASON, conversation)["applied"] is True


# ---------------------------------------------------------------------------
# Value caps. The realistic failure is a wrong number, not a forged token.
# ---------------------------------------------------------------------------


def test_coupon_over_the_cap_creates_nothing(actor, conversation, customer):
    over = admin_tools.ADMIN_COUPON_MAX_CENTS // 100 + 1

    preview = admin_tools.admin_preview_goodwill_coupon(actor, customer["email"], over, REASON, conversation)
    assert preview["error"] == "exceeds_limit"

    # And directly, in case the model skips straight to the write.
    result = admin_tools.admin_issue_goodwill_coupon(actor, customer["email"], over, REASON, conversation)
    assert result["error"] == "exceeds_limit"
    assert result["applied"] is False
    assert _coupon_count(customer["id"]) == 0


def test_cap_is_checked_before_the_token_so_a_bad_amount_costs_nothing(actor, conversation, customer):
    """Ordering matters: if the token were claimed first, an over-cap typo would
    consume the confirmation and the staff member would have to start over."""
    admin_tools.admin_preview_goodwill_coupon(actor, customer["email"], 1000, REASON, conversation)
    over = admin_tools.ADMIN_COUPON_MAX_CENTS // 100 + 1

    assert admin_tools.admin_issue_goodwill_coupon(actor, customer["email"], over, REASON, conversation)["error"] == "exceeds_limit"

    # The original confirmation still works.
    assert admin_tools.admin_issue_goodwill_coupon(actor, customer["email"], 1000, REASON, conversation)["applied"] is True


@pytest.mark.parametrize("amount", [0, -100, 12.5, "lots", None])
def test_bad_coupon_amounts_are_refused(actor, conversation, customer, amount):
    result = admin_tools.admin_preview_goodwill_coupon(actor, customer["email"], amount, REASON, conversation)
    assert result["error"] in ("bad_amount", "exceeds_limit")
    assert _coupon_count(customer["id"]) == 0


def test_stock_over_the_cap_is_refused(actor, conversation, product):
    result = admin_tools.admin_preview_stock_adjustment(
        actor, product["sku"], admin_tools.ADMIN_STOCK_MAX_QUANTITY + 1, REASON, conversation, size="6")
    assert result["error"] == "exceeds_limit"
    assert _stock(product["sku"])["6"] == 4


@pytest.mark.parametrize("quantity", [-1, 2.5, "many"])
def test_bad_stock_quantities_are_refused(actor, conversation, product, quantity):
    result = admin_tools.admin_preview_stock_adjustment(actor, product["sku"], quantity, REASON, conversation, size="6")
    assert result["error"] == "bad_quantity"


# ---------------------------------------------------------------------------
# A reason is mandatory — admin_action_log.reason is NOT NULL by design.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [None, "", "   ", "x"])
def test_writes_require_a_real_reason(actor, conversation, order, reason):
    preview = admin_tools.admin_preview_order_cancellation(actor, order, reason, conversation)
    assert preview["error"] == "reason_required"

    result = admin_tools.admin_cancel_order(actor, order, reason, conversation)
    assert result["applied"] is False
    assert result["reason"] == "reason_required"
    assert _order_status(order) == "PLACED"


# ---------------------------------------------------------------------------
# Happy paths, and the audit trail they must leave behind.
# ---------------------------------------------------------------------------


def test_cancel_writes_a_complete_audit_row(actor, conversation, order):
    admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)
    result = admin_tools.admin_cancel_order(actor, order, REASON, conversation)

    assert result["applied"] is True
    assert result["previous_status"] == "PLACED"
    assert _order_status(order) == "CANCELLED"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM orders WHERE order_number = %s", (order,))
        order_id = str(cur.fetchone()[0])

    rows = _audit_rows("admin_cancel_order", order_id)
    assert len(rows) == 1
    row = rows[0]
    # The actor is the whole point of this table: tool_call_log records only the
    # model-supplied arguments, which never include who was acting.
    assert str(row["actor_customer_id"]) == actor
    assert str(row["conversation_id"]) == conversation
    assert row["before_state"]["status"] == "PLACED"
    assert row["after_state"]["status"] == "CANCELLED"
    assert row["reason"] == REASON


def test_cancelled_order_gets_a_status_history_entry(actor, conversation, order):
    admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)
    admin_tools.admin_cancel_order(actor, order, REASON, conversation)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.from_status::text, h.to_status::text, h.reason
            FROM order_status_history h JOIN orders o ON o.id = h.order_id
            WHERE o.order_number = %s
            """,
            (order,),
        )
        history = cur.fetchall()
    assert ("PLACED", "CANCELLED", "admin_cancelled") in history


def test_stock_adjustment_applies_and_leaves_other_sizes_alone(actor, conversation, product):
    admin_tools.admin_preview_stock_adjustment(actor, product["sku"], 12, REASON, conversation, size="6")
    result = admin_tools.admin_adjust_stock(actor, product["sku"], 12, REASON, conversation, size="6")

    assert result["applied"] is True
    assert result["previous_quantity"] == 4
    stock = _stock(product["sku"])
    assert stock["6"] == 12
    assert stock["7"] == 9, "adjusting one size must not disturb another"

    rows = _audit_rows("admin_adjust_stock", product["id"])
    assert len(rows) == 1
    assert rows[0]["before_state"]["6"] == 4
    assert rows[0]["after_state"]["6"] == 12


def test_goodwill_coupon_is_issued_with_no_source_and_no_bonus(actor, conversation, customer):
    """The constraint relaxation this feature needed: a coupon with neither a
    source order nor a source subscription was previously impossible to insert."""
    admin_tools.admin_preview_goodwill_coupon(actor, customer["email"], 2500, REASON, conversation)
    result = admin_tools.admin_issue_goodwill_coupon(actor, customer["email"], 2500, REASON, conversation)

    assert result["applied"] is True
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_type, source_order_id, source_subscription_id, amount_cents,
                   total_cents, remaining_cents, bonus_percent_applied, status
            FROM coupons WHERE code = %s
            """,
            (result["coupon_code"],),
        )
        cols = [c.name for c in cur.description]
        coupon = dict(zip(cols, cur.fetchone()))

    assert coupon["source_type"] == "goodwill"
    assert coupon["source_order_id"] is None
    assert coupon["source_subscription_id"] is None
    # No bonus: the cancellation/return bonus buys retention against a refund the
    # customer was already owed. Goodwill is the gift itself — a bonus here would
    # quietly issue more than the staff member confirmed.
    assert coupon["amount_cents"] == 250_000
    assert coupon["total_cents"] == 250_000
    assert coupon["remaining_cents"] == 250_000
    assert float(coupon["bonus_percent_applied"]) == 0.0
    assert coupon["status"] == "ACTIVE"
    assert result["total_display"] == "₹2,500.00"


def test_goodwill_coupon_is_spendable_by_its_owner(actor, conversation, customer):
    """A coupon nobody can redeem would be a convincing-looking no-op. Prove the
    sourceless one behaves like any other."""
    from src.tools import coupon_tools

    admin_tools.admin_preview_goodwill_coupon(actor, customer["email"], 1500, REASON, conversation)
    issued = admin_tools.admin_issue_goodwill_coupon(actor, customer["email"], 1500, REASON, conversation)

    listed = coupon_tools.get_my_coupons(customer["id"])["coupons"]
    assert [c["code"] for c in listed] == [issued["coupon_code"]]
    assert listed[0]["remaining_display"] == "₹1,500.00"


def test_goodwill_coupon_belongs_to_the_named_customer_only(actor, conversation, customer):
    """Cross-customer reach is the point of these tools, so pin that the credit
    lands on the person named and nobody else."""
    from src.tools import coupon_tools

    admin_tools.admin_preview_goodwill_coupon(actor, customer["email"], 1000, REASON, conversation)
    issued = admin_tools.admin_issue_goodwill_coupon(actor, customer["email"], 1000, REASON, conversation)

    demo = coupon_tools.get_my_coupons(_demo_customer_id())["coupons"]
    assert issued["coupon_code"] not in [c["code"] for c in demo]


def _demo_customer_id() -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'arun@shurutech.com'")
        return str(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Lifecycle rules for the cancellation override.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [("DELIVERED", "delivered"), ("CANCELLED", "already_terminal"), ("RETURNED", "already_terminal")],
)
def test_orders_past_cancellation_are_refused(actor, conversation, order, status, expected):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE orders SET status = %s WHERE order_number = %s", (status, order))
        conn.commit()

    assert admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)["error"] == expected
    assert _order_status(order) == status


def test_shipped_orders_can_be_cancelled_by_staff(actor, conversation, order):
    """The staff override exists precisely to reach further than a customer can —
    a customer cannot cancel a SHIPPED order, or one older than 24 hours."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE orders SET status = 'SHIPPED', placed_at = now() - interval '30 days' WHERE order_number = %s",
            (order,),
        )
        conn.commit()

    admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)
    assert admin_tools.admin_cancel_order(actor, order, REASON, conversation)["applied"] is True
    assert _order_status(order) == "CANCELLED"


def test_status_change_between_preview_and_apply_is_caught(actor, conversation, order):
    """The token proves a preview happened; it never substitutes for re-checking.
    The customer may have cancelled it themselves in the meantime."""
    admin_tools.admin_preview_order_cancellation(actor, order, REASON, conversation)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE orders SET status = 'DELIVERED' WHERE order_number = %s", (order,))
        conn.commit()

    result = admin_tools.admin_cancel_order(actor, order, REASON, conversation)
    assert result["applied"] is False
    assert result["reason"] == "delivered"
    assert _order_status(order) == "DELIVERED"


# ---------------------------------------------------------------------------
# Target resolution: never guess which thing is being changed.
# ---------------------------------------------------------------------------


def test_ambiguous_size_is_refused_rather_than_guessed(actor, conversation, product):
    result = admin_tools.admin_preview_stock_adjustment(actor, product["sku"], 3, REASON, conversation)
    assert result["error"] == "size_required"
    assert "6" in result["message"] and "7" in result["message"]


def test_unknown_size_lists_the_valid_ones(actor, conversation, product):
    result = admin_tools.admin_preview_stock_adjustment(actor, product["sku"], 3, REASON, conversation, size="99")
    assert result["error"] == "bad_size"
    assert "6" in result["message"]


def test_single_size_product_needs_no_size_argument(actor, conversation):
    """Unsized products carry one '_default' key — an internal convention the
    model shouldn't have to know."""
    sku = f"TPJ-TST-{uuid.uuid4().hex[:6].upper()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO products (sku, name, category, price_cents, stock_by_size)
            VALUES (%s, 'Write Test Pendant', 'pendant', 500000, '{"_default": 7}'::jsonb)
            """,
            (sku,),
        )
        conn.commit()

    preview = admin_tools.admin_preview_stock_adjustment(actor, sku, 15, REASON, conversation)
    assert preview["size"] == "_default"
    assert preview["current_quantity"] == 7
    assert admin_tools.admin_adjust_stock(actor, sku, 15, REASON, conversation)["applied"] is True
    assert _stock(sku)["_default"] == 15


def test_no_op_adjustment_is_refused(actor, conversation, product):
    """A write that changes nothing would still burn a confirmation and leave a
    meaningless audit row."""
    result = admin_tools.admin_preview_stock_adjustment(actor, product["sku"], 4, REASON, conversation, size="6")
    assert result["error"] == "no_change"


def test_unknown_targets_are_refused(actor, conversation):
    assert admin_tools.admin_preview_order_cancellation(actor, "TPJ-000000", REASON, conversation)["error"] == "not_found"
    assert admin_tools.admin_preview_stock_adjustment(actor, "TPJ-NOPE-9999", 1, REASON, conversation)["error"] == "not_found"
    assert admin_tools.admin_preview_goodwill_coupon(
        actor, "nobody@nowhere.invalid", 100, REASON, conversation)["error"] == "not_found"


def test_customer_email_lookup_is_case_insensitive(actor, conversation, customer):
    result = admin_tools.admin_preview_goodwill_coupon(
        actor, customer["email"].upper(), 100, REASON, conversation)
    assert result["preview"] is True
    assert result["customer_email"] == customer["email"]
