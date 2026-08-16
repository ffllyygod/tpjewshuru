"""
Seed the DP Jewellers dev database with realistic fake data.

Usage:
    python scripts/seed_db.py           # seed (fails if schema not applied)
    python scripts/seed_db.py --reset   # drop + recreate schema, then seed

Requires DATABASE_URL in .env (see .env.example) and a running Postgres
(docker compose up -d).
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from faker import Faker
from psycopg.types.json import Json

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "src" / "db" / "schema.sql"

# backfill_invoice_attributes builds its figures with the same decompose_price
# the invoice tool uses, so seeded data is self-consistent with the bill by
# construction rather than by a comment asking someone to keep them in step.
sys.path.insert(0, str(ROOT))

fake = Faker()
Faker.seed(42)
random.seed(42)

METALS = ["gold", "silver", "platinum", "rose_gold"]
STONES = ["diamond", "ruby", "emerald", "sapphire", "none"]
RING_SIZES = ["5", "6", "7", "8", "9", "10"]

PRODUCT_TEMPLATES = [
    ("ring", "Solitaire Ring", True),
    ("ring", "Eternity Band", True),
    ("ring", "Halo Engagement Ring", True),
    ("necklace", "Tennis Necklace", False),
    ("necklace", "Pendant Chain", False),
    ("earring", "Stud Earrings", False),
    ("earring", "Drop Earrings", False),
    ("bracelet", "Tennis Bracelet", False),
    ("bracelet", "Charm Bracelet", False),
    ("bangle", "Classic Bangle", False),
    ("pendant", "Solitaire Pendant", False),
]


def apply_schema(conn: psycopg.Connection, reset: bool) -> None:
    with conn.cursor() as cur:
        if reset:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    print(f"Schema applied{' (reset)' if reset else ''}.")


def seed_products(conn: psycopg.Connection, count: int = 24) -> list[dict]:
    products = []
    with conn.cursor() as cur:
        for i in range(count):
            category, base_name, has_sizes = random.choice(PRODUCT_TEMPLATES)
            metal = random.choice(METALS)
            stone = random.choice(STONES)
            name = f"{metal.replace('_', ' ').title()} {base_name}" + (
                f" with {stone.title()}" if stone != "none" else ""
            )
            sku = f"DPJ-{category[:3].upper()}-{1000 + i}"
            # price_cents stores the smallest currency unit — paise, since this
            # store prices in INR. Derived from the piece's weight, purity,
            # making charge and stones rather than drawn from a flat range, so
            # the price and the invoice breakdown are the same arithmetic.
            spec = _jewellery_spec(category, metal, stone)
            price_cents = spec["price_cents"]
            occasions, style = _occasion_and_style(name)

            sizes_available = RING_SIZES if has_sizes else None
            if has_sizes:
                stock_by_size = {s: random.randint(0, 8) for s in RING_SIZES}
            else:
                stock_by_size = {"_default": random.randint(0, 20)}

            cur.execute(
                """
                INSERT INTO products
                    (sku, name, category, description, price_cents, metal, stone,
                     sizes_available, stock_by_size,
                     net_weight_grams, purity_karat, making_charge_percent,
                     stone_value_cents, occasion, style)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, sku, name, category, price_cents
                """,
                (
                    sku,
                    name,
                    category,
                    f"{name} — handcrafted, {stone if stone != 'none' else 'no stone'}, {metal.replace('_', ' ')}.",
                    price_cents,
                    metal,
                    stone,
                    Json(sizes_available) if sizes_available is not None else None,
                    Json(stock_by_size),
                    spec["net_weight_grams"],
                    spec["purity_karat"],
                    spec["making_charge_percent"],
                    spec["stone_value_cents"],
                    occasions,
                    style,
                ),
            )
            row = cur.fetchone()
            products.append(
                {
                    "id": row[0],
                    "sku": row[1],
                    "name": row[2],
                    "category": row[3],
                    "price_cents": row[4],
                    "sizes_available": sizes_available,
                }
            )
    conn.commit()
    print(f"Seeded {len(products)} products.")
    return products


# (city, state, PIN prefix). Hand-written rather than Faker's postcode(),
# because Faker will not make the PIN agree with the city it just gave you — and
# a seeder that violates the PIN/state cross-check the address tool enforces
# would be embarrassing the first time anyone looked.
_INDIAN_CITIES = [
    ("Bengaluru", "Karnataka", "5600"),
    ("Mumbai", "Maharashtra", "4000"),
    ("Chennai", "Tamil Nadu", "6000"),
    ("Hyderabad", "Telangana", "5000"),
    ("Pune", "Maharashtra", "4110"),
    ("Kolkata", "West Bengal", "7000"),
    ("Jaipur", "Rajasthan", "3020"),
    ("Kochi", "Kerala", "6820"),
    ("Ahmedabad", "Gujarat", "3800"),
    ("New Delhi", "Delhi", "1100"),
]

_STREETS = [
    "MG Road", "Residency Road", "Brigade Road", "Nehru Nagar", "Gandhi Marg",
    "Church Street", "Lake View Road", "Temple Street", "Station Road", "Park Avenue",
]


def _indian_phone() -> str:
    """10 digits starting 6-9 — the shape the address tool validates against."""
    return f"{random.choice('6789')}{random.randint(0, 999999999):09d}"


def _indian_address(recipient_name: str | None = None) -> dict:
    city, state, prefix = random.choice(_INDIAN_CITIES)
    address = {
        "line1": f"{random.randint(1, 400)} {random.choice(_STREETS)}",
        "line2": None,
        "city": city,
        "state": state,
        "postal_code": f"{prefix}{random.randint(1, 99):02d}",
        "country": "IN",
        "phone": _indian_phone(),
    }
    if recipient_name:
        address["recipient_name"] = recipient_name
    return address


def seed_customer_addresses(conn: psycopg.Connection, customers: list[dict]) -> None:
    """One saved default address per customer.

    Load-bearing for the test suite, not just the demo: place_order now requires
    a delivery address, and every existing purchase test uses a seeded customer.
    Without this they would all fail on address_required.
    """
    with conn.cursor() as cur:
        for customer in customers:
            cur.execute(
                "SELECT 1 FROM customer_addresses WHERE customer_id = %s AND is_default",
                (customer["id"],),
            )
            if cur.fetchone():
                continue
            addr = _indian_address(customer["name"])
            cur.execute(
                """
                INSERT INTO customer_addresses
                    (customer_id, label, recipient_name, phone, line1, city, state,
                     postal_code, country, is_default)
                VALUES (%s, 'Home', %s, %s, %s, %s, %s, %s, 'IN', true)
                """,
                (
                    customer["id"], customer["name"], addr["phone"], addr["line1"],
                    addr["city"], addr["state"], addr["postal_code"],
                ),
            )
    conn.commit()
    print(f"Seeded default delivery addresses for {len(customers)} customers.")


def seed_customers(conn: psycopg.Connection, count: int = 10) -> list[dict]:
    customers = []
    with conn.cursor() as cur:
        for _ in range(count):
            name = fake.name()
            email = fake.unique.email()
            phone = fake.phone_number()
            cur.execute(
                "INSERT INTO customers (name, email, phone) VALUES (%s, %s, %s) RETURNING id, name, email",
                (name, email, phone),
            )
            row = cur.fetchone()
            customers.append({"id": row[0], "name": row[1], "email": row[2]})
    conn.commit()
    print(f"Seeded {len(customers)} customers.")
    return customers


STATUS_FLOW = ["PLACED", "CONFIRMED", "SHIPPED", "DELIVERED"]


def seed_orders(
    conn: psycopg.Connection,
    customers: list[dict],
    products: list[dict],
    count: int = 20,
) -> None:
    """
    Deliberately spans every cancellation-eligibility case:
    - recent PLACED order (should be cancellable)
    - CONFIRMED order older than the cancellation window (should be rejected on time)
    - SHIPPED / DELIVERED orders (should be rejected on status)
    - an already-CANCELLED order (idempotency check target)
    """
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        for i in range(count):
            customer = random.choice(customers)
            order_number = f"DPJ-{10000 + i}"

            # Force a spread of scenarios for the first few orders (deterministic
            # for manual testing); randomize the rest for volume.
            if i == 0:
                status, placed_offset_hours = "PLACED", 2          # cancellable
            elif i == 1:
                status, placed_offset_hours = "CONFIRMED", 24 * 10  # too old to cancel
            elif i == 2:
                status, placed_offset_hours = "SHIPPED", 24 * 3     # wrong status
            elif i == 3:
                status, placed_offset_hours = "DELIVERED", 24 * 20  # wrong status
            elif i == 4:
                status, placed_offset_hours = "CANCELLED", 24 * 5   # already cancelled
            else:
                status = random.choice(STATUS_FLOW + ["CANCELLED"])
                placed_offset_hours = random.randint(1, 24 * 30)

            placed_at = now - timedelta(hours=placed_offset_hours)
            shipped_at = placed_at + timedelta(days=1) if status in (
                "SHIPPED", "DELIVERED"
            ) else None
            delivered_at = placed_at + timedelta(days=4) if status == "DELIVERED" else None

            n_items = random.randint(1, 3)
            chosen_products = random.sample(products, n_items)
            total_cents = 0
            items = []
            for p in chosen_products:
                qty = random.randint(1, 2)
                size = random.choice(p["sizes_available"]) if p["sizes_available"] else None
                total_cents += p["price_cents"] * qty
                items.append((p["id"], qty, p["price_cents"], size))

            cur.execute(
                """
                INSERT INTO orders
                    (order_number, customer_id, status, placed_at, shipped_at,
                     delivered_at, total_amount_cents, shipping_address, payment_status,
                     cancellation_reason_code, cancellation_reason_note,
                     return_reason_code, return_reason_note, returned_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    order_number,
                    customer["id"],
                    status,
                    placed_at,
                    shipped_at,
                    delivered_at,
                    total_cents,
                    # Indian addresses, not Faker's US default. This is an Indian
                    # store pricing in rupees; a shipping address in Ohio with a
                    # 5-digit ZIP reads as obviously synthetic on an invoice, and
                    # fails the PIN validation the address tools enforce.
                    psycopg.types.json.Json(_indian_address()),
                    "REFUNDED" if status == "CANCELLED" else "PAID",
                    *(_seed_resolution_reason("cancellation") if status == "CANCELLED" else (None, None)),
                    None, None, None,   # return reason/note/returned_at — no RETURNED fixtures
                ),
            )
            order_id = cur.fetchone()[0]

            for product_id, qty, unit_price_cents, size in items:
                cur.execute(
                    """
                    INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents, size)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (order_id, product_id, qty, unit_price_cents, size),
                )

            # status history: PLACED -> ... -> current status
            history = STATUS_FLOW[: STATUS_FLOW.index(status) + 1] if status in STATUS_FLOW else ["PLACED", "CANCELLED"]
            prev = None
            for step_status in history:
                cur.execute(
                    """
                    INSERT INTO order_status_history (order_id, from_status, to_status, reason)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        order_id,
                        prev,
                        step_status,
                        "Customer requested cancellation" if step_status == "CANCELLED" else None,
                    ),
                )
                prev = step_status

    conn.commit()
    print(f"Seeded {count} orders (order #DPJ-10000..DPJ-{10000 + count - 1}).")
    print("  DPJ-10000: PLACED, 2h ago      -> cancellable")
    print("  DPJ-10001: CONFIRMED, 10d ago  -> outside cancellation window")
    print("  DPJ-10002: SHIPPED             -> not cancellable (status)")
    print("  DPJ-10003: DELIVERED           -> not cancellable (status)")
    print("  DPJ-10004: CANCELLED           -> already cancelled")


KNOWLEDGE_DOCS = [
    (
        "Cancellation Policy",
        "policy",
        """Orders can be cancelled free of charge within 24 hours of being placed, as
long as the order has not yet been marked CONFIRMED for production or shipping.
Custom and engraved pieces cannot be cancelled once production has started —
production begins as soon as an order is CONFIRMED. Once an order ships, it can
no longer be cancelled; you may instead request a return within our 30-day
return window (see Return Policy). Refunds for cancelled orders are issued to
the original payment method within 5-7 business days.""",
    ),
    (
        "Return Policy",
        "policy",
        """We accept returns within 30 days of delivery for unworn items in their
original packaging with all tags and certificates of authenticity attached.
Custom-sized rings and engraved items are final sale and cannot be returned
unless defective. To start a return, contact support with your order number;
we'll email a prepaid return label. Refunds are processed within 5-7 business
days of us receiving the returned item.""",
    ),
    (
        "Ring Sizing Guide",
        "sizing_guide",
        """Not sure of your ring size? The most accurate method is visiting any
jeweller for a free sizing, but you can also measure an existing ring that
fits the intended finger: measure its inner diameter in millimetres and match
it against our size chart (available on the product page). If you're between
sizes, we recommend sizing up. We offer one free resizing within 60 days of
delivery for standard rings (not applicable to eternity bands, which cannot be
resized due to the continuous stone setting).""",
    ),
    (
        "Jewellery Care Guide",
        "care_guide",
        """Store pieces separately in soft pouches to prevent scratching. Avoid
contact with perfume, lotion, and chlorinated water — apply these before
putting on jewellery, not after. Clean gold and platinum pieces with warm
water, mild soap, and a soft brush; avoid ultrasonic cleaners on pieces with
emeralds or opals, as the vibration can damage these softer stones. We
recommend a professional cleaning and prong check once a year for
daily-worn rings.""",
    ),
    (
        "Shipping FAQ",
        "faq",
        """Standard shipping takes 3-5 business days within India; expedited
options are available at checkout. All orders over ₹40,000 ship fully insured
and require a signature on delivery. International shipping is available to
select countries and typically takes 7-14 business days, with customs duties
the responsibility of the recipient. You'll receive a tracking link by email
as soon as your order ships.""",
    ),
    (
        "Engraving FAQ",
        "faq",
        """Most rings and pendants can be engraved with up to 20 characters at
checkout for a flat ₹2,000 fee. Engraved items take an additional 2-3 business
days to produce and, because they're made to order, cannot be cancelled or
returned unless defective. Preview your engraving text carefully at
checkout — we are not able to modify it once production has started.""",
    ),
]


def seed_knowledge_docs(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for title, category, content in KNOWLEDGE_DOCS:
            cur.execute(
                """
                INSERT INTO knowledge_docs (title, content, category)
                VALUES (%s, %s, %s)
                """,
                (title, content.strip(), category),
            )
    conn.commit()
    print(f"Seeded {len(KNOWLEDGE_DOCS)} knowledge docs (embeddings NOT generated —"
          " run scripts/index_knowledge.py separately once VOYAGE_API_KEY is set).")


def seed_coupon_policy(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO coupon_policy (name, cancellation_bonus_percent, return_bonus_percent, expiry_days)
            VALUES ('default', 10, 15, 180)
            ON CONFLICT (name) DO NOTHING
            """
        )
    conn.commit()
    print("Seeded default coupon policy (10% cancellation / 15% return bonus, 180-day expiry).")


GOLD_SIP_PLANS = [
    # (name, tenure_months, bonus_percent, early_exit_penalty_percent)
    # `name` is the ONLY identifier — deliberately already human-readable
    # (not a separate slug + display-label pair) so there's no ambiguity
    # between what the agent shows the customer and what it passes back to
    # start_gold_sip. A slug/display-name split caused the model to invent
    # its own display string and then pass that (not the real key) as the
    # tool argument — see JOURNAL.md.
    ("Classic 6-Month", 6, 5, 5),
    ("Standard 11-Month", 11, 9, 8),
    ("Premium 12-Month", 12, 10, 10),
]


# (code, kind, label, applies_to, requires_note, sort_order). Team-editable in
# the DB afterwards — this is just the starting set. Ordered so the likeliest
# reason is offered first, since a pick-list nobody scrolls is a pick-list where
# everyone picks the top item.
#
# Codes are unique across BOTH kinds (the table is keyed on code alone), so the
# few that could apply to either are suffixed — 'changed_mind' vs
# 'changed_mind_return'. Keeping them distinct means a report never has to
# explain why one code appears under two headings.
RESOLUTION_REASONS = [
    # --- cancellation: before dispatch -------------------------------------
    ("changed_mind",        "cancellation", "Changed my mind",                         "customer", False, 10),
    ("wrong_item",          "cancellation", "Ordered the wrong item or size",          "customer", False, 20),
    ("found_better_price",  "cancellation", "Found a better price elsewhere",          "customer", False, 30),
    ("delivery_too_slow",   "cancellation", "Delivery is taking too long",             "customer", False, 40),
    ("ordered_by_mistake",  "cancellation", "Ordered by mistake or duplicate order",   "customer", False, 50),
    ("no_longer_needed",    "cancellation", "No longer needed — occasion passed",      "customer", False, 60),
    ("out_of_stock",        "cancellation", "Item unavailable or out of stock",        "staff",    False, 70),
    ("damaged_stock",       "cancellation", "Item damaged before dispatch",            "staff",    False, 80),
    ("pricing_error",       "cancellation", "Pricing or listing error",                "staff",    False, 90),
    ("payment_issue",       "cancellation", "Payment failed or could not be verified", "staff",    False, 100),
    ("customer_offline",    "cancellation", "Customer requested by phone or in store", "staff",    False, 110),
    ("suspected_fraud",     "cancellation", "Suspected fraudulent order",              "staff",    False, 120),
    ("other",               "cancellation", "Something else",                          "both",     True,  900),

    # --- return: after delivery --------------------------------------------
    ("doesnt_fit",          "return", "Doesn't fit",                                "customer", False, 10),
    ("not_as_described",    "return", "Doesn't match the description or photos",    "customer", False, 20),
    ("damaged_on_arrival",  "return", "Arrived damaged or defective",               "both",     False, 30),
    ("quality_issue",       "return", "Quality not as expected",                    "customer", False, 40),
    ("changed_mind_return", "return", "Changed my mind",                            "customer", False, 50),
    ("gift_returned",       "return", "Unwanted gift",                              "customer", False, 60),
    ("wrong_item_sent",     "return", "We sent the wrong item",                     "staff",    False, 70),
    ("other_return",        "return", "Something else",                             "both",     True,  900),
]

# Not offered to anyone (active = false). These exist only as FK targets for
# orders resolved before reasons were recorded, so those rows can satisfy the
# CHECK constraints without a reason being invented for them.
LEGACY_REASONS = [
    ("unspecified",        "cancellation", "Not recorded", "both", False, 999),
    ("unspecified_return", "return",       "Not recorded", "both", False, 999),
]


def seed_resolution_reasons(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for code, kind, label, applies_to, requires_note, sort_order in RESOLUTION_REASONS:
            cur.execute(
                """
                INSERT INTO resolution_reasons (code, kind, label, applies_to, requires_note, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE
                  SET kind = EXCLUDED.kind, label = EXCLUDED.label,
                      applies_to = EXCLUDED.applies_to,
                      requires_note = EXCLUDED.requires_note, sort_order = EXCLUDED.sort_order
                """,
                (code, kind, label, applies_to, requires_note, sort_order),
            )
        for code, kind, label, applies_to, requires_note, sort_order in LEGACY_REASONS:
            cur.execute(
                """
                INSERT INTO resolution_reasons (code, kind, label, applies_to, requires_note, active, sort_order)
                VALUES (%s, %s, %s, %s, %s, false, %s)
                ON CONFLICT (code) DO NOTHING
                """,
                (code, kind, label, applies_to, requires_note, sort_order),
            )
    conn.commit()
    n_cancel = sum(1 for r in RESOLUTION_REASONS if r[1] == "cancellation")
    print(
        f"Seeded {len(RESOLUTION_REASONS)} resolution reasons "
        f"({n_cancel} cancellation / {len(RESOLUTION_REASONS) - n_cancel} return, +2 inactive legacy)."
    )


def _seed_resolution_reason(kind: str = "cancellation", customer_facing: bool = True) -> tuple[str, str | None]:
    """Pick a plausible reason for a seeded cancelled/returned order, so the
    'why' reports show a realistic distribution rather than one value repeated a
    thousand times."""
    audience = ("customer", "both") if customer_facing else ("staff", "both")
    pool = [r for r in RESOLUTION_REASONS if r[1] == kind and r[3] in audience]
    # 'other' is deliberately rare: a report where the top reason is "something
    # else" tells nobody anything, and that isn't what real data looks like.
    weights = [0.3 if r[0].startswith("other") else 1.0 for r in pool]
    code = random.choices([r[0] for r in pool], weights=weights)[0]
    note = "Customer explained over chat." if code.startswith("other") else None
    return code, note


def seed_gold_sip_plans(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for name, tenure, bonus, penalty in GOLD_SIP_PLANS:
            cur.execute(
                """
                INSERT INTO gold_sip_plans (name, tenure_months, bonus_percent, early_exit_penalty_percent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name) DO NOTHING
                """,
                (name, tenure, bonus, penalty),
            )
    conn.commit()
    print(f"Seeded {len(GOLD_SIP_PLANS)} Gold SIP plan tiers.")


# ============================================================
# Admin-persona seed data — 12 months of history for analytics
# ============================================================
# Everything below runs AFTER the generators above and re-seeds the RNG at its
# own start. That ordering is load-bearing: tests/test_tools.py and
# tests/test_coupon_tools.py pin DPJ-10000..DPJ-10004 to exact statuses, and
# those come out of seed_orders' RNG stream. Appending here (rather than
# rewriting seed_orders) means the volume of history generated below can be
# tuned freely without ever shifting the stream that produces those fixtures.

HISTORY_SEED = 4242

# Order numbers MUST NOT collide with runtime orders. purchase_tools mints
# DPJ-{100000..999999} (6 digits) and the scenario orders are DPJ-1000x, so a
# numeric history range would eventually collide. The H prefix is provably
# disjoint from both, and instantly recognisable as seed data when debugging.
HISTORY_ORDER_PREFIX = "DPJ-H"

# Indian retail jewellery calendar. Volume multiplier by month (1 = January).
# This is the whole reason the admin reports are worth demoing: a flat random
# spread makes "revenue by month" a straight line, which tells you nothing.
MONTH_DEMAND = {
    1: 1.6,   # wedding season continues
    2: 1.6,   # wedding season
    3: 1.0,
    4: 2.2,   # Akshaya Tritiya — gold-buying is auspicious
    5: 2.0,   # Akshaya Tritiya tail + summer weddings
    6: 0.9,
    7: 0.8,   # monsoon lull
    8: 0.8,   # monsoon lull
    9: 0.5,   # Pitru Paksha — inauspicious for purchases, a real trough
    10: 3.0,  # Dhanteras / Diwali — the year's peak
    11: 2.4,  # Diwali tail into wedding season
    12: 1.6,  # wedding season
}

# Category mix shifts with the season, not just volume. Without this, a
# breakdown-by-category-by-month report looks synthetic the moment you read it.
WEDDING_MONTHS = {11, 12, 1, 2}
GOLD_FESTIVAL_MONTHS = {4, 5, 10}

INDIAN_CITIES = [
    ("Mumbai", "400001", "Maharashtra"), ("Delhi", "110001", "Delhi"),
    ("Bengaluru", "560001", "Karnataka"), ("Chennai", "600001", "Tamil Nadu"),
    ("Hyderabad", "500001", "Telangana"), ("Pune", "411001", "Maharashtra"),
    ("Kolkata", "700001", "West Bengal"), ("Ahmedabad", "380001", "Gujarat"),
    ("Jaipur", "302001", "Rajasthan"), ("Surat", "395003", "Gujarat"),
]

ADMIN_USERS = [
    ("Priya Nair", "admin@dpjewellers.com", "admin"),
    ("Store Manager", "manager@dpjewellers.com", "admin"),
]


def seed_admin_users(conn: psycopg.Connection) -> None:
    """Staff accounts. Deliberately NOT flipping arun@shurutech.com to admin —
    every orchestrator/auth test resolves through that email, and making it an
    admin would silently change which prompt and toolset those tests exercise."""
    with conn.cursor() as cur:
        for name, email, role in ADMIN_USERS:
            cur.execute(
                """
                INSERT INTO customers (name, email, role) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role
                """,
                (name, email, role),
            )
    conn.commit()
    print(f"Seeded {len(ADMIN_USERS)} admin users ({', '.join(e for _, e, _ in ADMIN_USERS)}).")


def seed_extra_products(conn: psycopg.Connection, count: int = 26) -> list[dict]:
    """Widen the catalogue so category/product breakdowns aren't 6 rows wide."""
    products = []
    with conn.cursor() as cur:
        for i in range(count):
            category, base_name, has_sizes = random.choice(PRODUCT_TEMPLATES)
            metal = random.choice(METALS)
            stone = random.choice(STONES)
            name = f"{metal.replace('_', ' ').title()} {base_name}" + (
                f" with {stone.title()}" if stone != "none" else ""
            )
            sku = f"DPJ-{category[:3].upper()}-{2000 + i}"
            spec = _jewellery_spec(category, metal, stone)
            price_cents = spec["price_cents"]
            occasions, style = _occasion_and_style(name)
            # Margin varies by category so the margin report differentiates
            # rather than showing one flat percentage everywhere.
            cost_ratio = random.uniform(0.55, 0.75)
            sizes_available = RING_SIZES if has_sizes else None
            stock_by_size = (
                {s: random.randint(0, 8) for s in RING_SIZES}
                if has_sizes
                else {"_default": random.randint(0, 20)}
            )
            cur.execute(
                """
                INSERT INTO products
                    (sku, name, category, description, price_cents, cost_price_cents,
                     metal, stone, sizes_available, stock_by_size, low_stock_threshold,
                     net_weight_grams, purity_karat, making_charge_percent,
                     stone_value_cents, occasion, style)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, sku, name, category, price_cents
                """,
                (
                    sku, name, category,
                    f"{name} — handcrafted, {stone if stone != 'none' else 'no stone'}, {metal.replace('_', ' ')}.",
                    price_cents, int(price_cents * cost_ratio), metal, stone,
                    Json(sizes_available) if sizes_available is not None else None,
                    Json(stock_by_size), random.choice([2, 2, 2, 5]),
                    spec["net_weight_grams"], spec["purity_karat"],
                    spec["making_charge_percent"], spec["stone_value_cents"],
                    occasions, style,
                ),
            )
            row = cur.fetchone()
            products.append({
                "id": row[0], "sku": row[1], "name": row[2],
                "category": row[3], "price_cents": row[4],
                "sizes_available": sizes_available,
            })
    conn.commit()
    print(f"Seeded {len(products)} additional products (DPJ-*-2000+).")
    return products


def seed_extra_customers(conn: psycopg.Connection, count: int = 140) -> list[dict]:
    # Indian locale: this is an Indian store pricing in rupees, and a "top
    # customers" report full of Faker's default US names reads as obviously
    # synthetic the moment anyone looks at it.
    fake_in = Faker("en_IN")
    customers = []
    with conn.cursor() as cur:
        for _ in range(count):
            # Spread signup dates across ~2 years so "new customers this month"
            # is answerable rather than everyone joining on seed day.
            created_at = datetime.now(timezone.utc) - timedelta(days=random.randint(1, 730))
            cur.execute(
                "INSERT INTO customers (name, email, phone, created_at) VALUES (%s, %s, %s, %s) RETURNING id, name, email",
                (fake_in.name(), fake.unique.email(), fake_in.phone_number(), created_at),
            )
            row = cur.fetchone()
            customers.append({"id": row[0], "name": row[1], "email": row[2]})
    conn.commit()
    print(f"Seeded {len(customers)} additional customers.")
    return customers


def backfill_cost_prices(conn: psycopg.Connection) -> None:
    """Give the original 24 products a cost price too, so margin reports don't
    report most of the catalogue as 'cost data incomplete'."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, price_cents FROM products WHERE cost_price_cents IS NULL")
        rows = cur.fetchall()
        for pid, price_cents in rows:
            cur.execute(
                "UPDATE products SET cost_price_cents = %s WHERE id = %s",
                (int(price_cents * random.uniform(0.55, 0.75)), pid),
            )
    conn.commit()
    print(f"Backfilled cost prices for {len(rows)} products.")


# Fixed seed-time metal rates, paise per gram. Deliberately NOT
# market_tools.get_metal_rates: a network call in the seeder makes the fixture
# non-reproducible, and a rate that moves would give the same product a
# different weight on every reseed.
_RATE_PER_GRAM = {
    ("gold", 24): 1_085_000, ("gold", 22): 995_000, ("gold", 18): 814_000,
    ("rose_gold", 18): 814_000, ("rose_gold", 14): 635_000,
    ("silver", None): 12_800, ("platinum", None): 320_000,
}

# Plausible finished weights, grams, by category.
#
# These drive the PRICE, not the other way round — which is the opposite of how
# this seeder used to work and the only ordering that produces a coherent
# catalogue. Picking a price from one flat ₹15,000-₹8,00,000 range regardless of
# metal gave ₹6.8 lakh silver bangles, and back-solving their weight at ₹128/g
# gave 972 g. Both are nonsense, and the second is only a symptom of the first.
#
# Generating weight first and deriving the price through the same GST maths the
# invoice uses means the catalogue, the bill and the implied per-gram rate are
# all consistent by construction: silver comes out cheap because silver is
# cheap, and a platinum ring costs what a platinum ring costs.
_WEIGHT_RANGE_GRAMS = {
    "ring": (2, 15), "pendant": (1, 8), "earring": (1.5, 10),
    "bracelet": (5, 35), "bangle": (8, 60), "necklace": (8, 45),
}

# Stone value as a MULTIPLE of the metal value — not a fraction of the price,
# which would be circular (the price is what we're deriving).
_STONE_MULTIPLE = {
    "diamond": (0.8, 3.0),
    "ruby": (0.4, 1.5), "emerald": (0.4, 1.5), "sapphire": (0.4, 1.5),
    "none": (0.0, 0.0),
}

# Making charges vary by how much handwork a category takes.
_MAKING_RANGE = {
    "ring": (10, 16), "pendant": (10, 16), "necklace": (8, 14),
    "bangle": (8, 14), "earring": (10, 18), "bracelet": (10, 18),
}

# What each piece is actually FOR. Without this, "something for our anniversary"
# is answered by the model guessing from product names, which is exactly the
# kind of judgement the catalogue should be making instead.
_OCCASION_BY_TEMPLATE = {
    "Solitaire Ring": (["engagement", "wedding", "anniversary"], "classic"),
    "Eternity Band": (["wedding", "anniversary"], "classic"),
    "Halo Engagement Ring": (["engagement", "wedding"], "statement"),
    "Tennis Necklace": (["wedding", "festive", "gifting"], "statement"),
    "Pendant Chain": (["daily", "gifting", "birthday"], "minimal"),
    "Stud Earrings": (["daily", "gifting", "birthday"], "minimal"),
    "Drop Earrings": (["festive", "wedding", "gifting"], "contemporary"),
    "Tennis Bracelet": (["anniversary", "gifting", "festive"], "contemporary"),
    "Charm Bracelet": (["birthday", "gifting", "daily"], "contemporary"),
    "Classic Bangle": (["festive", "wedding", "gifting"], "traditional"),
    "Solitaire Pendant": (["anniversary", "gifting", "birthday"], "classic"),
}

ALL_OCCASIONS = ["wedding", "engagement", "anniversary", "birthday", "festive", "daily", "gifting"]


def _jewellery_spec(category: str, metal: str, stone: str) -> dict:
    """Generate one internally-consistent piece: weight, purity, making charge,
    stone value, and the GST-inclusive price they add up to.

    The price is computed FORWARD through exactly the identity
    src/tools/billing.py solves backwards —

        P = 1.03·(M + S) + 1.05·K,   K = M·r/100

    — so decomposing the resulting price with these same attributes returns
    this same metal value. The catalogue can't drift from the invoice because
    they are the same arithmetic run in opposite directions.
    """
    if metal == "gold":
        karat = random.choice([22, 18])
    elif metal == "rose_gold":
        karat = random.choice([18, 14])
    else:
        karat = None

    lo, hi = _WEIGHT_RANGE_GRAMS.get(category, (2, 20))
    weight = round(random.uniform(lo, hi), 3)
    making = round(random.uniform(*_MAKING_RANGE.get(category, (10, 15))), 2)

    metal_value = weight * _RATE_PER_GRAM[(metal, karat)]
    stone_value = round(metal_value * random.uniform(*_STONE_MULTIPLE[stone]))
    making_value = metal_value * making / 100
    price_cents = round(1.03 * (metal_value + stone_value) + 1.05 * making_value)

    return {
        "price_cents": price_cents,
        "net_weight_grams": weight,
        "purity_karat": karat,
        "making_charge_percent": making,
        "stone_value_cents": stone_value,
    }


def _occasion_and_style(name: str) -> tuple[list[str], str]:
    base = next((b for b in _OCCASION_BY_TEMPLATE if b in name), None)
    return _OCCASION_BY_TEMPLATE.get(base, (["gifting"], "classic"))


def backfill_invoice_attributes(conn: psycopg.Connection) -> None:
    """Give invoice data to products whose price was fixed by hand.

    The generated catalogue derives its price FROM these attributes
    (`_jewellery_spec`), so it never lands here. This is for products created
    the other way round — the hand-priced demo pieces in add_demo_user.py, and
    anything added through the admin tools — where the price is already fixed
    and the attributes have to be solved backwards out of it with the same
    `billing.decompose_price` the invoice uses.
    """
    from src.tools.billing import decompose_price

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, category, metal, stone, price_cents
            FROM products WHERE making_charge_percent IS NULL
            """
        )
        rows = cur.fetchall()
        no_weight = 0

        for pid, name, category, metal, stone, price_cents in rows:
            making = round(random.uniform(*_MAKING_RANGE.get(category, (10, 15))), 2)

            if stone == "none":
                stone_value = 0
            else:
                share = random.uniform(0.15, 0.35) if stone == "diamond" else random.uniform(0.10, 0.22)
                stone_value = int(price_cents * share)

            if metal in ("gold",):
                karat = random.choice([22, 18])
            elif metal == "rose_gold":
                karat = random.choice([18, 14])
            else:
                karat = None

            metal_value = decompose_price(
                price_cents,
                making_charge_percent=making,
                stone_value_cents=stone_value,
            )["metal_value_cents"]
            weight = round(metal_value / _RATE_PER_GRAM[(metal, karat)], 3)
            # A price fixed by hand may imply an impossible weight (a ₹1.8L
            # silver bangle would be 972 g). Record no weight rather than an
            # absurd one — the invoice says "weight not on record", which is
            # true, and every money line on it is still exactly right.
            if weight > _WEIGHT_RANGE_GRAMS.get(category, (0, 100))[1] * 1.5:
                weight = None

            base = next((b for b in _OCCASION_BY_TEMPLATE if b in name), None)
            occasions, style = _OCCASION_BY_TEMPLATE.get(base, (["gifting"], "classic"))

            cur.execute(
                """
                UPDATE products
                SET making_charge_percent = %s, stone_value_cents = %s,
                    purity_karat = %s, net_weight_grams = %s,
                    occasion = %s, style = %s
                WHERE id = %s
                """,
                (making, stone_value, karat, weight, occasions, style, pid),
            )
            if weight is None:
                no_weight += 1
    conn.commit()
    print(
        f"Backfilled invoice + occasion attributes for {len(rows)} products "
        f"({no_weight} left without a weight — price implies an implausible one)."
    )


def seed_occasion_coverage(conn: psycopg.Connection) -> None:
    """Guarantee every occasion returns something.

    Same defensive move as seed_inventory_edge_cases: on an unlucky RNG draw
    "something for a birthday" could come back empty, which reads as a broken
    assistant rather than a catalogue that happens not to stock one.
    """
    with conn.cursor() as cur:
        for occasion in ALL_OCCASIONS:
            cur.execute(
                "SELECT count(*) FROM products WHERE active AND %s = ANY(occasion)", (occasion,)
            )
            if cur.fetchone()[0] >= 3:
                continue
            cur.execute(
                """
                UPDATE products SET occasion = array_append(occasion, %s)
                WHERE id IN (
                    SELECT id FROM products
                    WHERE active AND NOT (%s = ANY(occasion))
                    ORDER BY price_cents LIMIT 3
                )
                """,
                (occasion, occasion),
            )
    conn.commit()
    print(f"Ensured every occasion ({', '.join(ALL_OCCASIONS)}) has stock.")


def seed_final_sale_products(conn: psycopg.Connection) -> None:
    """Mark the products the Return Policy doc calls final sale.

    That doc says custom-sized rings and engraved pieces can't be returned
    unless defective. Eternity bands are the canonical custom-sized item — they
    are cut to the finger, not resized — so they stand in for the category here.
    Without at least one such product seeded, the final-sale branch of
    check_return_eligibility is never exercised by anyone testing by hand.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE products SET returnable = false WHERE name ILIKE %s RETURNING sku",
            ("%Eternity Band%",),
        )
        skus = [r[0] for r in cur.fetchall()]
    conn.commit()
    print(f"Marked {len(skus)} products final-sale (non-returnable).")


def seed_inventory_edge_cases(conn: psycopg.Connection) -> None:
    """Guarantee the low-stock and out-of-stock reports return something. Without
    this they can come back empty on a lucky RNG draw, which reads as a broken
    tool rather than a healthy warehouse."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, stock_by_size FROM products ORDER BY sku LIMIT 12")
        rows = cur.fetchall()
        for idx, (pid, stock) in enumerate(rows):
            if idx < 4:                      # fully out of stock
                new_stock = {k: 0 for k in stock}
            elif idx < 10:                   # at or below the low-stock threshold
                new_stock = {k: random.choice([1, 2]) for k in stock}
            else:
                continue
            cur.execute(
                "UPDATE products SET stock_by_size = %s, updated_at = now() WHERE id = %s",
                (Json(new_stock), pid),
            )
    conn.commit()
    print("Forced 4 out-of-stock and 6 low-stock products for inventory reporting.")


def _history_status(age_days: int) -> str:
    """Status is derived from order age, never drawn at random per row — an
    order placed 8 months ago that is still 'PLACED' would make every ageing
    or fulfilment report nonsense."""
    if age_days > 30:
        return random.choices(["DELIVERED", "RETURNED", "CANCELLED"], weights=[88, 4, 8])[0]
    if age_days > 5:
        return random.choices(["SHIPPED", "DELIVERED"], weights=[45, 55])[0]
    return random.choices(["PLACED", "CONFIRMED", "SHIPPED"], weights=[40, 35, 25])[0]


def _seasonal_products(products: list[dict], month: int) -> list[dict]:
    """Weight the catalogue toward what actually sells in that season."""
    if month in WEDDING_MONTHS:
        preferred = {"bangle", "necklace"}
    elif month in GOLD_FESTIVAL_MONTHS:
        preferred = {"pendant", "bangle", "earring"}
    else:
        return products
    # Triple the weight of in-season categories by repeating them in the pool.
    return products + [p for p in products if p["category"] in preferred] * 2


def seed_history_orders(
    conn: psycopg.Connection,
    customers: list[dict],
    products: list[dict],
    days: int = 365,
) -> None:
    random.seed(HISTORY_SEED)
    Faker.seed(HISTORY_SEED)

    # Pareto-ish weighting: a small set of repeat buyers should dominate the
    # "top customers" ranking, otherwise that report is a flat, meaningless list.
    vip_count = max(1, len(customers) // 10)
    weights = [8.0] * vip_count + [1.0] * (len(customers) - vip_count)

    now = datetime.now(timezone.utc)
    seq = 0
    per_month: dict[str, int] = {}

    with conn.cursor() as cur:
        for day_offset in range(days, 0, -1):
            day = now - timedelta(days=day_offset)
            mult = MONTH_DEMAND[day.month] * (1.4 if day.weekday() >= 5 else 1.0)
            expected = 2.0 * mult
            n_orders = max(0, int(random.gauss(expected, expected * 0.35)))

            season_pool = _seasonal_products(products, day.month)
            # Bigger baskets during festival/wedding peaks lifts AOV, not just
            # order count — the two move together in real retail.
            peak = day.month in GOLD_FESTIVAL_MONTHS or day.month in WEDDING_MONTHS

            for _ in range(n_orders):
                seq += 1
                order_number = f"{HISTORY_ORDER_PREFIX}{seq:05d}"
                customer = random.choices(customers, weights=weights)[0]
                placed_at = day.replace(
                    hour=random.randint(9, 21), minute=random.randint(0, 59)
                )
                age_days = (now - placed_at).days
                status = _history_status(age_days)

                n_items = random.randint(1, 3) if peak else random.randint(1, 2)
                chosen = random.sample(season_pool, min(n_items, len(season_pool)))
                # random.sample over a weighted pool can repeat a product; dedupe
                # so one order never carries the same product twice.
                seen, items, total_cents = set(), [], 0
                for p in chosen:
                    if p["id"] in seen:
                        continue
                    seen.add(p["id"])
                    qty = random.randint(1, 2)
                    size = random.choice(p["sizes_available"]) if p["sizes_available"] else None
                    total_cents += p["price_cents"] * qty
                    items.append((p["id"], qty, p["price_cents"], size))

                shipped_at = placed_at + timedelta(days=1) if status in ("SHIPPED", "DELIVERED", "RETURNED") else None
                delivered_at = placed_at + timedelta(days=4) if status in ("DELIVERED", "RETURNED") else None
                city, postcode, state = random.choice(INDIAN_CITIES)

                cur.execute(
                    """
                    INSERT INTO orders
                        (order_number, customer_id, status, placed_at, shipped_at,
                         delivered_at, total_amount_cents, shipping_address, payment_status,
                         cancellation_reason_code, cancellation_reason_note,
                         return_reason_code, return_reason_note, returned_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        order_number, customer["id"], status, placed_at, shipped_at,
                        delivered_at, total_cents,
                        Json({
                            "line1": fake.street_address(), "city": city,
                            "state": state, "postal_code": postcode, "country": "IN",
                        }),
                        "REFUNDED" if status in ("CANCELLED", "RETURNED") else "PAID",
                        # ~1 in 5 historical resolutions came from staff rather
                        # than the customer, so the reason mix isn't uniformly
                        # customer-side and the reports have something to show.
                        *(_seed_resolution_reason("cancellation", random.random() > 0.2)
                          if status == "CANCELLED" else (None, None)),
                        *((*_seed_resolution_reason("return", random.random() > 0.2),
                           delivered_at + timedelta(days=random.randint(1, 20)))
                          if status == "RETURNED" else (None, None, None)),
                    ),
                )
                order_id = cur.fetchone()[0]

                for product_id, qty, unit_price_cents, size in items:
                    cur.execute(
                        """
                        INSERT INTO order_items (order_id, product_id, quantity, unit_price_cents, size)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (order_id, product_id, qty, unit_price_cents, size),
                    )

                if status == "CANCELLED":
                    flow = ["PLACED", "CANCELLED"]
                elif status == "RETURNED":
                    flow = ["PLACED", "CONFIRMED", "SHIPPED", "DELIVERED", "RETURNED"]
                else:
                    flow = STATUS_FLOW[: STATUS_FLOW.index(status) + 1]
                prev = None
                for step in flow:
                    cur.execute(
                        """
                        INSERT INTO order_status_history (order_id, from_status, to_status, changed_at, reason)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (order_id, prev, step, placed_at,
                         "Customer requested cancellation" if step == "CANCELLED" else None),
                    )
                    prev = step

                key = placed_at.strftime("%Y-%m")
                per_month[key] = per_month.get(key, 0) + 1

    conn.commit()
    print(f"Seeded {seq} history orders ({HISTORY_ORDER_PREFIX}00001..{HISTORY_ORDER_PREFIX}{seq:05d}) over {days} days.")
    print("  monthly volume (seasonality check):")
    for month in sorted(per_month):
        print(f"    {month}: {per_month[month]:4d} {'#' * (per_month[month] // 5)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema first")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set. Copy .env.example to .env and fill it in.", file=sys.stderr)
        sys.exit(1)

    with psycopg.connect(database_url) as conn:
        apply_schema(conn, reset=args.reset)
        products = seed_products(conn)
        customers = seed_customers(conn)
        # Before seed_orders for the same reason resolution_reasons is: an order
        # now needs somewhere to ship to.
        seed_customer_addresses(conn, customers)
        # Before seed_orders, not after: a cancelled order carries an FK to this
        # table, so the pick-list has to exist before any order is written.
        seed_resolution_reasons(conn)
        seed_orders(conn, customers, products)
        seed_knowledge_docs(conn)
        seed_coupon_policy(conn)
        seed_gold_sip_plans(conn)

        # --- admin-persona data. Must stay LAST: these re-seed the RNG, so
        # nothing above them can be shifted by tuning anything below. ---
        seed_admin_users(conn)
        extra_products = seed_extra_products(conn)
        extra_customers = seed_extra_customers(conn)
        seed_customer_addresses(conn, extra_customers)
        seed_history_orders(conn, customers + extra_customers, products + extra_products)
        backfill_cost_prices(conn)
        # After every product exists, so nothing is left without invoice data.
        backfill_invoice_attributes(conn)
        seed_occasion_coverage(conn)
        seed_final_sale_products(conn)
        seed_inventory_edge_cases(conn)

    print("\nDone.")


if __name__ == "__main__":
    main()
