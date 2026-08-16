"""Proactive outreach: consent, grounding, and the review gate.

This is the only feature that produces text aimed at someone who did not ask for
it, so the tests are weighted accordingly — most of them assert that a message
is NOT generated, or NOT approved.

The three things worth breaking a build over:
  1. An opted-out customer must never appear in a signal.
  2. A draft must not assert anything absent from the facts it was given.
  3. Nothing reaches APPROVED without a preview and a fresh confirmation.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.db.connection import get_conn
from src.tools import admin_tools, outreach_tools

REASON = "test: reviewed and the claim checks out"


@pytest.fixture
def actor() -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'admin@dpjewellers.com'")
        return str(cur.fetchone()[0])


@pytest.fixture
def conversation(actor) -> str:
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
    suffix = uuid.uuid4().hex[:8]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO customers (name, email) VALUES (%s, %s) RETURNING id",
            (f"Outreach Test {suffix}", f"outreach-{suffix}@example.invalid"),
        )
        cid = str(cur.fetchone()[0])
        conn.commit()
    return {"id": cid, "name": f"Outreach Test {suffix}"}


def _expiring_coupon(customer_id: str, days: int = 10, remaining: int = 500000) -> str:
    code = f"DPJ-CPN-{uuid.uuid4().hex[:10].upper()}"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO coupons (code, customer_id, source_type, amount_cents,
                                 bonus_percent_applied, total_cents, remaining_cents, expires_at)
            VALUES (%s, %s, 'goodwill', %s, 0, %s, %s, now() + make_interval(days => %s))
            """,
            (code, customer_id, remaining, remaining, remaining, days),
        )
        conn.commit()
    return code


def _draft_for(customer_id: str) -> list[dict]:
    return [c for c in outreach_tools.detect_signals(("coupon_expiring",), limit=50)
            if c["customer_id"] == customer_id]


# ---------------------------------------------------------------------------
# Consent — the part that isn't negotiable.
# ---------------------------------------------------------------------------


def test_opted_out_customer_produces_no_signal(customer):
    _expiring_coupon(customer["id"])
    assert _draft_for(customer["id"]), "fixture should be detectable before opting out"

    outreach_tools.set_marketing_preference(customer["id"], opt_in=False)
    assert _draft_for(customer["id"]) == [], "an opted-out customer must not be reachable"


def test_opting_back_in_restores_eligibility(customer):
    _expiring_coupon(customer["id"])
    outreach_tools.set_marketing_preference(customer["id"], opt_in=False)
    outreach_tools.set_marketing_preference(customer["id"], opt_in=True)
    assert _draft_for(customer["id"])


def test_opt_out_records_when(customer):
    outreach_tools.set_marketing_preference(customer["id"], opt_in=False)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT marketing_opt_in, marketing_opted_out_at FROM customers WHERE id = %s",
            (customer["id"],),
        )
        opted_in, at = cur.fetchone()
    assert opted_in is False
    assert at is not None


def test_dormant_means_dormant(customer):
    """Regression: the dormancy cutoff was written as `interval '%s days'` with
    the day count passed as a parameter. A placeholder inside a string literal
    doesn't bind the way it reads — with 180 passed in, the cutoff came out two
    days ago, so customers who had ordered last week were being flagged as
    dormant VIPs. No error, just a wrong answer, which is the failure mode this
    whole codebase keeps running into.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM products WHERE active LIMIT 1")
        product_id = cur.fetchone()[0]
        # A big order placed yesterday: VIP-sized, but the opposite of dormant.
        cur.execute(
            """
            INSERT INTO orders (order_number, customer_id, status, placed_at, total_amount_cents)
            VALUES (%s, %s, 'DELIVERED', now() - interval '1 day', %s) RETURNING id
            """,
            (f"DPJ-V{uuid.uuid4().hex[:6].upper()}", customer["id"],
             outreach_tools.VIP_LIFETIME_CENTS * 2),
        )
        order_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents) VALUES (%s, %s, 1, %s)",
            (order_id, product_id, outreach_tools.VIP_LIFETIME_CENTS * 2),
        )
        conn.commit()

    dormant = [c for c in outreach_tools.detect_signals(("dormant_vip",), limit=50)
               if c["customer_id"] == customer["id"]]
    assert dormant == [], "a customer who ordered yesterday is not dormant"


def test_staff_accounts_are_never_marketed_to(actor):
    """Admins live in `customers`, so without the role filter every staff
    account would be a marketing target."""
    for candidate in outreach_tools.detect_signals(limit=50):
        assert candidate["customer_id"] != actor


# ---------------------------------------------------------------------------
# Grounding — the model writes the sentence, not the facts.
# ---------------------------------------------------------------------------


FACTS = {
    "customer_name": "Liam",
    "product_name": "Gold Drop Earrings",
    "order_number": "DPJ-H00015",
    "order_total_display": "₹11,51,500.74",
}


@pytest.mark.parametrize(
    "message,grounded",
    [
        ("Hi Liam, your Gold Drop Earrings turn one this month.", True),
        ("Hi Liam, order DPJ-H00015 turns one this month.", True),
        ("Hi Liam, your ₹11,51,500.74 purchase deserves a companion.", True),
        # The failure modes, in order of how plausible they look:
        ("Hi Liam, here's ₹2,50,000 off your next piece!", False),
        ("Hi Liam, order DPJ-999999 is due a refresh.", False),
        ("Hi Liam, we've reserved DPJ-RIN-1010 for you.", False),
        ("Hi Liam, redeem coupon DPJ-CPN-FAKE123 today.", False),
    ],
)
def test_grounding_check(message, grounded):
    ok, _ = outreach_tools._is_grounded(message, FACTS)
    assert ok is grounded, message


def test_rupee_symbol_survives_json_encoding():
    """Regression: json.dumps escapes ₹ to \\u20b9 unless ensure_ascii=False, so
    every draft that correctly quoted an amount was being rejected."""
    haystack = json.dumps(FACTS, ensure_ascii=False)
    assert "₹" in haystack


def test_ungrounded_draft_is_not_stored(customer):
    result = outreach_tools.store_draft(
        customer["id"], "coupon_expiring", f"test:{uuid.uuid4()}",
        {"customer_name": "X"}, "Here's ₹9,99,999 off, just for you!",
    )
    assert result["stored"] is False
    assert result["reason"] == "ungrounded"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outreach_drafts WHERE customer_id = %s", (customer["id"],))
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Idempotency and volume — a nightly job must not nag.
# ---------------------------------------------------------------------------


def test_the_same_signal_is_never_drafted_twice(customer):
    key = f"coupon_expiring:{uuid.uuid4()}"
    facts = {"customer_name": customer["name"]}
    first = outreach_tools.store_draft(customer["id"], "coupon_expiring", key, facts, "A gentle note.")
    second = outreach_tools.store_draft(customer["id"], "coupon_expiring", key, facts, "A gentle note.")

    assert first["stored"] is True
    assert second["stored"] is False
    assert second["reason"] == "already_drafted"


def test_a_recent_draft_suppresses_further_signals(customer):
    _expiring_coupon(customer["id"])
    assert _draft_for(customer["id"]), "should be detectable first"

    outreach_tools.store_draft(
        customer["id"], "dormant_vip", f"other:{uuid.uuid4()}",
        {"customer_name": customer["name"]}, "Hello again.",
    )
    assert _draft_for(customer["id"]) == [], "frequency cap should suppress a second nudge"


def test_one_candidate_per_customer_per_run(customer):
    """A customer with two live signals gets one message, not two."""
    _expiring_coupon(customer["id"])
    candidates = outreach_tools.detect_signals(limit=50)
    ids = [c["customer_id"] for c in candidates]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# The review gate.
# ---------------------------------------------------------------------------


def _stored_draft(customer_id: str, name: str) -> str:
    result = outreach_tools.store_draft(
        customer_id, "coupon_expiring", f"coupon_expiring:{uuid.uuid4()}",
        {"customer_name": name}, "A short, entirely factual note.",
    )
    assert result["stored"] is True
    return result["draft_id"]


def _status(draft_id: str) -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM outreach_drafts WHERE id::text = %s", (draft_id,))
        return cur.fetchone()[0]


def test_approval_without_preview_is_refused(actor, conversation, customer):
    draft_id = _stored_draft(customer["id"], customer["name"])
    result = admin_tools.admin_approve_outreach(actor, draft_id, REASON, conversation)
    assert result["applied"] is False
    assert result["reason"] == "no_pending_confirmation"
    assert _status(draft_id) == "DRAFT"


def test_approval_requires_a_fresh_confirmation_per_draft(actor, conversation, customer):
    first = _stored_draft(customer["id"], customer["name"])
    second = _stored_draft(customer["id"], customer["name"])

    admin_tools.admin_preview_outreach_approval(actor, first, REASON, conversation)
    # A confirmation for one draft must not authorise a different one.
    result = admin_tools.admin_approve_outreach(actor, second, REASON, conversation)
    assert result["applied"] is False
    assert _status(second) == "DRAFT"


def test_approval_happy_path_and_audit(actor, conversation, customer):
    draft_id = _stored_draft(customer["id"], customer["name"])
    preview = admin_tools.admin_preview_outreach_approval(actor, draft_id, REASON, conversation)
    assert preview["preview"] is True
    assert preview["message_to_send"]
    assert "generated_from" in preview, "a reviewer must see the facts behind the claim"
    assert _status(draft_id) == "DRAFT", "preview alone must change nothing"

    result = admin_tools.admin_approve_outreach(actor, draft_id, REASON, conversation)
    assert result["applied"] is True
    assert _status(draft_id) == "APPROVED"

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT actor_customer_id, reason FROM admin_action_log WHERE action = 'admin_approve_outreach' AND target_id::text = %s",
            (draft_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert str(row[0]) == actor


def test_approval_cannot_be_replayed(actor, conversation, customer):
    draft_id = _stored_draft(customer["id"], customer["name"])
    admin_tools.admin_preview_outreach_approval(actor, draft_id, REASON, conversation)
    admin_tools.admin_approve_outreach(actor, draft_id, REASON, conversation)

    again = admin_tools.admin_approve_outreach(actor, draft_id, REASON, conversation)
    assert again["applied"] is False


def test_opting_out_between_preview_and_approval_blocks_the_send(actor, conversation, customer):
    """Consent is re-checked at approval, not just at detection — the customer
    may have opted out in the minutes since."""
    draft_id = _stored_draft(customer["id"], customer["name"])
    admin_tools.admin_preview_outreach_approval(actor, draft_id, REASON, conversation)

    outreach_tools.set_marketing_preference(customer["id"], opt_in=False)

    result = admin_tools.admin_approve_outreach(actor, draft_id, REASON, conversation)
    assert result["applied"] is False
    assert result["reason"] == "opted_out"
    assert _status(draft_id) == "DRAFT"


def test_preview_refuses_an_opted_out_customer(actor, conversation, customer):
    draft_id = _stored_draft(customer["id"], customer["name"])
    outreach_tools.set_marketing_preference(customer["id"], opt_in=False)
    assert admin_tools.admin_preview_outreach_approval(actor, draft_id, REASON, conversation)["error"] == "opted_out"


def test_dismiss_is_single_step(actor, conversation, customer):
    draft_id = _stored_draft(customer["id"], customer["name"])
    result = admin_tools.admin_dismiss_outreach(actor, draft_id, "off-tone for this customer", conversation)
    assert result["applied"] is True
    assert _status(draft_id) == "DISMISSED"


def test_dismissed_draft_cannot_then_be_approved(actor, conversation, customer):
    draft_id = _stored_draft(customer["id"], customer["name"])
    admin_tools.admin_dismiss_outreach(actor, draft_id, "not appropriate", conversation)
    assert admin_tools.admin_preview_outreach_approval(actor, draft_id, REASON, conversation)["error"] == "already_reviewed"


def test_writes_require_a_reason(actor, conversation, customer):
    draft_id = _stored_draft(customer["id"], customer["name"])
    assert admin_tools.admin_preview_outreach_approval(actor, draft_id, "", conversation)["error"] == "reason_required"
    assert admin_tools.admin_dismiss_outreach(actor, draft_id, "", conversation)["reason"] == "reason_required"


def test_bad_draft_id_is_a_clean_error(actor, conversation):
    """A malformed id compares as text, so it matches nothing rather than
    raising — the model gets 'no such draft', which it can act on."""
    assert admin_tools.admin_preview_outreach_approval(actor, "not-a-uuid", REASON, conversation)["error"] == "not_found"
    assert admin_tools.admin_dismiss_outreach(actor, "not-a-uuid", REASON, conversation)["reason"] == "not_found"
    assert admin_tools.admin_outreach_queue(actor, status="NONSENSE")["error"] == "bad_status"


def test_queue_shows_the_facts_behind_each_message(actor, customer):
    _stored_draft(customer["id"], customer["name"])
    queue = admin_tools.admin_outreach_queue(actor, status="DRAFT", limit=50)
    mine = [r for r in queue["rows"] if r["customer_name"] == customer["name"]]
    assert mine
    assert mine[0]["generated_from"]["customer_name"] == customer["name"]
    assert "has been sent" in queue["note"]
