"""Proactive outreach: the store starting a conversation instead of only
answering one.

Every other tool in this codebase is reactive — a customer asks, the agent
answers. This module inverts that: it looks for reasons to reach out (a purchase
turning a year old, a coupon about to expire, a Gold SIP nearing maturity, a
good customer gone quiet, a gold-buying festival coming up) and drafts a message
for a human to approve.

Three rules make that safe enough to point at real customers.

**1. Signals are detected in SQL, never by the model.** Each detector below
returns hard facts — an order number, a date, a `_display` amount — pulled from
the database. The model is handed those facts and asked only to write the
sentence. It never decides whether an anniversary exists, and it never computes
an amount. This is the same division of labour as the `*_display` currency
convention, applied to a different failure: an outbound message asserting
something untrue is worse than an inbound one, because the customer didn't ask
and isn't in a position to correct it.

**2. Every draft is checked back against its facts.** `_is_grounded` rejects a
draft quoting a rupee figure, order number or SKU that wasn't in the facts it
was given. A rejected draft is dropped, not stored — the whole point is that
nothing reaches review unless it is traceable to data.

**3. Nothing sends itself.** Drafts land in `outreach_drafts` with status DRAFT
and wait for a staff member. Consent (`customers.marketing_opt_in`), a
frequency cap, and a per-run volume cap are enforced in the detectors
themselves rather than left to whoever writes the next caller.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.formatting import format_inr

# Don't nudge the same person twice inside this window, whatever the signal.
# A customer with an expiring coupon AND an anniversary this week gets one
# message, not two.
FREQUENCY_CAP_DAYS = 21

# Ceiling per signal type per run. A scheduled job that suddenly drafts 400
# messages is a bug, and the blast radius should be small enough that a human
# notices before it matters.
MAX_DRAFTS_PER_SIGNAL = 25

# Indian gold-buying occasions. Dates shift year to year with the lunar
# calendar; these are the 2026 dates, and the point is the *window*, not
# to-the-day precision.
FESTIVALS_2026 = [
    ("Akshaya Tritiya", date(2026, 4, 19), "the most auspicious day of the year to buy gold"),
    ("Dhanteras", date(2026, 11, 6), "when buying gold is considered to invite prosperity"),
    ("Diwali", date(2026, 11, 8), "the festival of lights"),
]
FESTIVAL_LEAD_DAYS = 21

SIGNAL_TYPES = (
    "purchase_anniversary",
    "coupon_expiring",
    "sip_maturing",
    "dormant_vip",
    "festival",
)

# A customer is "VIP" above this lifetime spend. Deliberately a constant here
# rather than a magic number inside a query, so it's tunable and visible.
VIP_LIFETIME_CENTS = 20_000_000  # ₹2,00,000
DORMANT_DAYS = 180

_RUPEE = re.compile(r"₹\s?[\d,]+(?:\.\d{1,2})?")
_ORDER_NO = re.compile(r"DPJ-(?:H?\d{5,6}|[A-Z0-9]{6})\b")
_SKU = re.compile(r"DPJ-[A-Z]{3}-[A-Z0-9]{4,5}\b")
_CODE = re.compile(r"DPJ-(?:CPN|SIP)-[A-Z0-9]+\b")


def _eligible_customer_sql(alias: str = "cu") -> str:
    """Consent + frequency cap, as a SQL fragment every detector includes.

    Written once and reused so that adding a sixth signal can't accidentally
    ship one that ignores an opt-out. The cap counts drafts in any status: a
    dismissed nudge still means we recently decided to contact this person.
    """
    return f"""
        {alias}.marketing_opt_in = true
        AND {alias}.role = 'customer'
        AND NOT EXISTS (
            SELECT 1 FROM outreach_drafts od
            WHERE od.customer_id = {alias}.id
              AND od.created_at > now() - interval '{FREQUENCY_CAP_DAYS} days'
        )
    """


# ---------------------------------------------------------------------------
# Signal detectors. Each returns a list of {customer_id, signal_key, facts}.
# Facts are what the model is allowed to know — nothing else reaches the prompt.
# ---------------------------------------------------------------------------


def _purchase_anniversary(cur, limit: int) -> list[dict]:
    """An order delivered roughly a year ago, from someone who hasn't bought
    since. The jewellery-specific bet: pieces are bought for occasions, and
    occasions recur annually."""
    cur.execute(
        f"""
        SELECT o.order_number, o.delivered_at::date AS delivered_on,
               cu.id AS customer_id, cu.name,
               p.name AS product_name, p.category,
               o.total_amount_cents
        FROM orders o
        JOIN customers cu ON cu.id = o.customer_id
        JOIN LATERAL (
            SELECT p.name, p.category FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = o.id ORDER BY oi.unit_price_cents DESC LIMIT 1
        ) p ON true
        WHERE o.status = 'DELIVERED'
          AND o.delivered_at BETWEEN now() - interval '13 months' AND now() - interval '11 months'
          AND NOT EXISTS (
              SELECT 1 FROM orders o2
              WHERE o2.customer_id = cu.id AND o2.placed_at > now() - interval '60 days'
          )
          AND {_eligible_customer_sql()}
        ORDER BY o.total_amount_cents DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [
        {
            "customer_id": str(r["customer_id"]),
            "signal_key": f"purchase_anniversary:{r['order_number']}",
            "facts": {
                "customer_name": r["name"],
                "occasion": "the anniversary of a past purchase",
                "order_number": r["order_number"],
                "purchased_on": r["delivered_on"].strftime("%d %B %Y"),
                "product_name": r["product_name"],
                "product_category": r["category"],
                "order_total_display": format_inr(r["total_amount_cents"]),
            },
        }
        for r in cur.fetchall()
    ]


def _coupon_expiring(cur, limit: int) -> list[dict]:
    """Unspent store credit with an expiry approaching. The most direct
    revenue signal here: the money is already committed, and breakage is a
    worse outcome for both sides than a reminder."""
    cur.execute(
        f"""
        SELECT c.code, c.remaining_cents, c.expires_at::date AS expires_on,
               cu.id AS customer_id, cu.name
        FROM coupons c
        JOIN customers cu ON cu.id = c.customer_id
        WHERE c.status = 'ACTIVE'
          AND c.remaining_cents > 0
          AND c.expires_at BETWEEN now() AND now() + interval '30 days'
          AND {_eligible_customer_sql()}
        ORDER BY c.remaining_cents DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [
        {
            "customer_id": str(r["customer_id"]),
            "signal_key": f"coupon_expiring:{r['code']}",
            "facts": {
                "customer_name": r["name"],
                "occasion": "store credit about to expire",
                "coupon_code": r["code"],
                "remaining_display": format_inr(r["remaining_cents"]),
                "expires_on": r["expires_on"].strftime("%d %B %Y"),
                "days_left": (r["expires_on"] - datetime.now(timezone.utc).date()).days,
            },
        }
        for r in cur.fetchall()
    ]


def _sip_maturing(cur, limit: int) -> list[dict]:
    """A Gold SIP one installment from maturity, or matured and unredeemed.
    Both are moments where a nudge is genuinely useful rather than noise."""
    cur.execute(
        f"""
        SELECT s.code, s.status, s.installments_paid, s.tenure_months_snapshot,
               s.monthly_amount_cents, s.redeemable_cents, s.remaining_cents,
               cu.id AS customer_id, cu.name
        FROM gold_sip_subscriptions s
        JOIN customers cu ON cu.id = s.customer_id
        WHERE (
                (s.status = 'ACTIVE' AND s.installments_paid >= s.tenure_months_snapshot - 1)
             OR (s.status = 'MATURED' AND COALESCE(s.remaining_cents, 0) > 0)
              )
          AND {_eligible_customer_sql()}
        LIMIT %s
        """,
        (limit,),
    )
    out = []
    for r in cur.fetchall():
        matured = r["status"] == "MATURED"
        facts = {
            "customer_name": r["name"],
            "occasion": "a matured Gold SIP ready to redeem" if matured else "a Gold SIP one payment from maturity",
            "subscription_code": r["code"],
            "installments_paid": r["installments_paid"],
            "tenure_months": r["tenure_months_snapshot"],
        }
        if matured:
            facts["redeemable_display"] = format_inr(r["remaining_cents"] or 0)
        else:
            facts["monthly_amount_display"] = format_inr(r["monthly_amount_cents"])
        out.append({
            "customer_id": str(r["customer_id"]),
            "signal_key": f"sip_{'matured' if matured else 'maturing'}:{r['code']}",
            "facts": facts,
        })
    return out


def _dormant_vip(cur, limit: int) -> list[dict]:
    """A high-lifetime-value customer who has gone quiet. Worth a personal note
    rather than a discount — the point is the relationship, not a promotion."""
    cur.execute(
        f"""
        SELECT cu.id AS customer_id, cu.name,
               SUM(o.total_amount_cents - o.discount_cents) AS lifetime,
               MAX(o.placed_at)::date AS last_order_on,
               COUNT(*) AS order_count
        FROM customers cu
        JOIN orders o ON o.customer_id = cu.id AND o.status NOT IN ('CANCELLED', 'RETURNED')
        WHERE {_eligible_customer_sql()}
        GROUP BY cu.id, cu.name
        HAVING SUM(o.total_amount_cents - o.discount_cents) >= %s
           -- make_interval, NOT a placeholder inside an interval string
           -- literal. psycopg counts placeholders lexically — quoting and
           -- comments don't hide one — so writing the day count that way
           -- substituted it INSIDE the quotes and produced a cutoff of two
           -- days rather than six months. Every recent customer then matched
           -- "dormant", with no error raised. Pinned by a test.
           AND MAX(o.placed_at) < now() - make_interval(days => %s)
        ORDER BY lifetime DESC
        LIMIT %s
        """,
        (VIP_LIFETIME_CENTS, DORMANT_DAYS, limit),
    )
    return [
        {
            "customer_id": str(r["customer_id"]),
            "signal_key": f"dormant_vip:{r['last_order_on']:%Y-%m}",
            "facts": {
                "customer_name": r["name"],
                "occasion": "a valued customer we haven't heard from in a while",
                "last_order_on": r["last_order_on"].strftime("%d %B %Y"),
                "order_count": r["order_count"],
                "lifetime_spend_display": format_inr(r["lifetime"]),
            },
        }
        for r in cur.fetchall()
    ]


def _upcoming_festival() -> tuple[str, date, str] | None:
    today = datetime.now(timezone.utc).date()
    for name, when, blurb in FESTIVALS_2026:
        if 0 <= (when - today).days <= FESTIVAL_LEAD_DAYS:
            return name, when, blurb
    return None


def _festival(cur, limit: int) -> list[dict]:
    """A gold-buying festival approaching, aimed at people who have bought
    before. Culturally specific and the highest-intent moment in the Indian
    retail calendar — Akshaya Tritiya and Dhanteras are when gold buying is
    considered auspicious, which is why the seed data's demand curve peaks
    there."""
    upcoming = _upcoming_festival()
    if not upcoming:
        return []
    name, when, blurb = upcoming

    cur.execute(
        f"""
        SELECT cu.id AS customer_id, cu.name, MAX(o.placed_at)::date AS last_order_on
        FROM customers cu
        JOIN orders o ON o.customer_id = cu.id AND o.status NOT IN ('CANCELLED', 'RETURNED')
        WHERE {_eligible_customer_sql()}
        GROUP BY cu.id, cu.name
        ORDER BY MAX(o.placed_at) DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [
        {
            "customer_id": str(r["customer_id"]),
            "signal_key": f"festival:{name}:{when:%Y}",
            "facts": {
                "customer_name": r["name"],
                "occasion": f"{name} is coming up",
                "festival_name": name,
                "festival_date": when.strftime("%d %B %Y"),
                "festival_note": blurb,
                "days_away": (when - datetime.now(timezone.utc).date()).days,
            },
        }
        for r in cur.fetchall()
    ]


_DETECTORS = {
    "purchase_anniversary": _purchase_anniversary,
    "coupon_expiring": _coupon_expiring,
    "sip_maturing": _sip_maturing,
    "dormant_vip": _dormant_vip,
    "festival": _festival,
}


def detect_signals(signal_types: tuple[str, ...] = SIGNAL_TYPES, limit: int = MAX_DRAFTS_PER_SIGNAL) -> list[dict]:
    """Run the detectors and drop anything already drafted.

    Read-only — writes nothing, so it's safe to run to see what a job *would*
    pick up.
    """
    found: list[dict] = []
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        for name in signal_types:
            detector = _DETECTORS.get(name)
            if not detector:
                continue
            for candidate in detector(cur, min(limit, MAX_DRAFTS_PER_SIGNAL)):
                cur.execute(
                    "SELECT 1 FROM outreach_drafts WHERE customer_id = %s AND signal_key = %s",
                    (candidate["customer_id"], candidate["signal_key"]),
                )
                if cur.fetchone():
                    continue
                candidate["signal_type"] = name
                found.append(candidate)

    # One nudge per person per run, even across signal types. The detectors'
    # frequency cap only sees drafts already in the table, not ones created
    # moments ago in this same run.
    seen: set[str] = set()
    deduped = []
    for candidate in found:
        if candidate["customer_id"] in seen:
            continue
        seen.add(candidate["customer_id"])
        deduped.append(candidate)
    return deduped


# ---------------------------------------------------------------------------
# Grounding — the draft must not assert anything it wasn't given.
# ---------------------------------------------------------------------------


def _is_grounded(message: str, facts: dict) -> tuple[bool, str | None]:
    """Reject a draft quoting a figure, order number, SKU or code absent from
    its facts.

    The same check `scripts/hallucination_audit.py` applies to live replies,
    applied here at generation time instead — an outbound message has no user
    in the loop to notice it's wrong.
    """
    # ensure_ascii=False matters: json.dumps escapes ₹ to ₹ by default, so a
    # message correctly quoting an amount from the facts would never match the
    # haystack and every faithful draft mentioning money would be rejected.
    haystack = json.dumps(facts, default=str, ensure_ascii=False)
    for pattern, label in (
        (_RUPEE, "amount"),
        (_ORDER_NO, "order number"),
        (_SKU, "SKU"),
        (_CODE, "code"),
    ):
        for token in set(pattern.findall(message)):
            normalised = token.replace(" ", "")
            if normalised in haystack.replace(" ", ""):
                continue
            return False, f"ungrounded {label}: {token!r}"
    return True, None


DRAFT_SYSTEM_PROMPT = """You write short, warm outreach messages for DP Jewellers, \
an Indian jewellery retailer, to send to an existing customer.

You are given a set of FACTS. Write 2-3 sentences based ONLY on those facts.

Hard rules:
- Never state a number, price, date, order number or code that is not in the FACTS. \
If you want to mention an amount, copy the *_display value exactly as given.
- Never invent a product, an offer, a discount, or a deadline that isn't in the FACTS.
- Never claim the customer did something the FACTS don't show.
- Address them by first name only.
- No subject line, no greeting block, no signature — just the message body.
- Warm and human, not salesy. This is a jewellery store where purchases are \
personal and often mark an occasion. One light call to action at most.
- Indian English. Rupees are written as given in the FACTS.
"""


def build_draft_prompt(facts: dict) -> str:
    return "FACTS:\n" + json.dumps(facts, indent=2, default=str) + "\n\nWrite the message."


def store_draft(customer_id: str, signal_type: str, signal_key: str, facts: dict, message: str) -> dict:
    """Persist a DRAFT row. Returns {stored: bool, ...}.

    The UNIQUE(customer_id, signal_key) constraint is the real idempotency
    guard — a nightly job that re-detects the same anniversary gets a conflict
    rather than a duplicate nudge.
    """
    grounded, problem = _is_grounded(message, facts)
    if not grounded:
        return {"stored": False, "reason": "ungrounded", "message": problem}

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO outreach_drafts (customer_id, signal_type, signal_key, facts, draft_message)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (customer_id, signal_key) DO NOTHING
            RETURNING id
            """,
            (customer_id, signal_type, signal_key, json.dumps(facts, default=str), message.strip()),
        )
        row = cur.fetchone()
        conn.commit()

    if not row:
        return {"stored": False, "reason": "already_drafted"}
    return {"stored": True, "draft_id": str(row["id"])}


# ---------------------------------------------------------------------------
# Customer-facing: consent.
# ---------------------------------------------------------------------------


def set_marketing_preference(customer_id: str, opt_in: bool) -> dict:
    """Let a customer turn proactive messages off (or back on) by asking.

    Deliberately a customer tool rather than staff-only: the person whose
    consent it is should be able to withdraw it in the same conversation where
    they realised they wanted to.
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            UPDATE customers
            SET marketing_opt_in = %s,
                marketing_opted_out_at = CASE WHEN %s THEN NULL ELSE now() END
            WHERE id = %s
            RETURNING name, marketing_opt_in
            """,
            (opt_in, opt_in, customer_id),
        )
        row = cur.fetchone()
        conn.commit()

    if not row:
        return {"error": "not_found", "message": "No such customer."}
    return {
        "updated": True,
        "marketing_opt_in": row["marketing_opt_in"],
        "message": (
            "You'll keep getting occasional notes about your orders, credit and offers."
            if row["marketing_opt_in"] else
            "Done — we won't send you proactive messages. You can still chat here any time, "
            "and we'll always reply about your own orders."
        ),
    }
