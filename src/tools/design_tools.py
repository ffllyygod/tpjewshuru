"""Custom design requests.

A customer who likes a piece but wants it changed — other metal, other stone, a
different weight, an engraving, a size that isn't stocked — previously had
nowhere to go but the catalogue. This captures the brief, puts an *indicative*
number on it, and hands it to a human jeweller.

Two deliberate limits, both structural rather than instructional:

  1. **There is no path from a design request to an order or a payment.** Not a
     discouraged one — an absent one. An estimate is not a quote, and the only
     reliable way to stop it being treated as one is for the capability not to
     exist.

  2. **The estimate never covers what it cannot know.** Change the stone and its
     value becomes genuinely unknown, so the range covers metal and making only
     and says so in `estimate_basis`. Returning one confident number there would
     be the same failure this codebase designs against everywhere else.

The pricing basis is the catalogue's own implied per-gram rates, computed from
real product prices via billing.decompose_price — not a market feed and not a
hardcoded table. It is therefore always consistent with what the store actually
charges, and it moves when the catalogue does.
"""

from __future__ import annotations

import secrets
from statistics import median

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.db.connection import get_conn
from src.tools.billing import (
    GST_GOODS_PERCENT,
    GST_MAKING_PERCENT,
    decompose_price,
    purity_label,
)
from src.tools.formatting import format_inr

# A custom piece is quoted after a jeweller has seen the brief. This is the
# honest width of "before anyone has looked at it".
ESTIMATE_SPREAD_PERCENT = 12

VALID_METALS = {"gold", "silver", "platinum", "rose_gold"}
VALID_STONES = {"diamond", "ruby", "emerald", "sapphire", "none"}
VALID_KARATS = {24, 22, 18, 14}
MAX_ENGRAVING_CHARS = 30

# Gold and rose gold are the same metal at the same karat — a rose 18K rate is a
# perfectly good basis for a yellow 18K piece. Silver and platinum are not
# interchangeable with either, or each other.
_RATE_EQUIVALENT = {"gold": ("gold", "rose_gold"), "rose_gold": ("gold", "rose_gold")}

_STATUSES = {"NEW", "REVIEWED", "QUOTED", "CLOSED"}


def _new_request_number(cur) -> str:
    for _ in range(5):
        candidate = f"DR-{secrets.randbelow(9000) + 1000}"
        cur.execute("SELECT 1 FROM design_requests WHERE request_number = %s", (candidate,))
        if not cur.fetchone():
            return candidate
    raise RuntimeError("Could not generate a unique design request number after 5 attempts.")


def _catalogue_rate(cur, metal: str, karat: int | None) -> int | None:
    """The median implied rate per gram for a metal/purity, from our own prices.

    Every active product with a weight has an implied rate (its decomposed metal
    value over its weight). Taking the median of those makes the estimate
    grounded in what this store actually charges, and immune to one mispriced
    row. Returns None when the catalogue has nothing comparable — in which case
    the honest answer is that we can't estimate, not that we'll guess.
    """
    metals = _RATE_EQUIVALENT.get(metal, (metal,))
    cur.execute(
        """
        SELECT price_cents, net_weight_grams, purity_karat, making_charge_percent, stone_value_cents
        FROM products
        WHERE active AND net_weight_grams IS NOT NULL AND metal = ANY(%s)
          AND (%s::smallint IS NULL OR purity_karat IS NOT DISTINCT FROM %s::smallint)
        """,
        (list(metals), karat, karat),
    )
    rates = []
    for r in cur.fetchall():
        d = decompose_price(
            r["price_cents"], r["net_weight_grams"], r["purity_karat"],
            r["making_charge_percent"], r["stone_value_cents"],
        )
        if d["rate_per_gram_cents"]:
            rates.append(d["rate_per_gram_cents"])
    return int(median(rates)) if rates else None


def _not_estimable(message: str, **extra) -> dict:
    """No number is better than an invented one. The design conversation
    continues; the jeweller supplies the price."""
    return {
        "estimable": False,
        "message": message,
        "next_step": (
            "Carry on collecting the brief and submit it — a jeweller will price it. "
            "Do not offer the customer a figure of your own."
        ),
        **extra,
    }


def estimate_custom_design(
    reference_sku: str | None = None,
    metal: str | None = None,
    purity_karat: int | None = None,
    stone: str | None = None,
    net_weight_grams=None,
    size: str | None = None,
    engraving_text: str | None = None,
) -> dict:
    """An indicative price range for a custom piece, based on a catalogue piece.

    Explicitly a range, explicitly not a quote, and explicit about what it
    excludes. Never turns into an order.
    """
    if metal and metal.lower() not in VALID_METALS:
        return {"error": "bad_metal", "message": f"Unknown metal '{metal}'. Valid: {', '.join(sorted(VALID_METALS))}."}
    if stone and stone.lower() not in VALID_STONES:
        return {"error": "bad_stone", "message": f"Unknown stone '{stone}'. Valid: {', '.join(sorted(VALID_STONES))}."}
    if purity_karat and int(purity_karat) not in VALID_KARATS:
        return {
            "error": "bad_purity",
            "message": f"Gold purity must be one of {sorted(VALID_KARATS, reverse=True)} karat.",
        }
    if engraving_text and len(str(engraving_text)) > MAX_ENGRAVING_CHARS:
        return {
            "error": "engraving_too_long",
            "message": f"Engraving is limited to {MAX_ENGRAVING_CHARS} characters; that one is {len(engraving_text)}.",
        }

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        reference = None
        if reference_sku:
            cur.execute(
                """
                SELECT sku, name, metal, stone, price_cents, net_weight_grams,
                       purity_karat, making_charge_percent, stone_value_cents, category
                FROM products WHERE sku = %s
                """,
                (reference_sku,),
            )
            reference = cur.fetchone()
            if not reference:
                return {"error": "not_found", "message": f"No product with SKU {reference_sku}."}

        target_metal = (metal or (reference["metal"] if reference else None) or "").lower()
        if not target_metal:
            return _not_estimable(
                "Without a reference piece or a metal, there's nothing to base a figure on."
            )

        target_karat = int(purity_karat) if purity_karat else (reference["purity_karat"] if reference else None)
        if target_metal in ("silver", "platinum"):
            target_karat = None   # karat is a gold measure

        weight = net_weight_grams if net_weight_grams is not None else (
            reference["net_weight_grams"] if reference else None
        )
        if not weight:
            return _not_estimable(
                "I don't have a weight to work from for this piece, so I can't put a figure on it. "
                "Ask roughly how heavy they'd like it, or let the jeweller price the brief."
            )
        weight = float(weight)

        rate = _catalogue_rate(cur, target_metal, target_karat)
        if not rate:
            return _not_estimable(
                f"We don't have comparable {target_metal.replace('_', ' ')} pieces priced by weight, "
                "so a jeweller will need to quote this one."
            )

        making_percent = float(reference["making_charge_percent"]) if reference and reference["making_charge_percent"] else 12.0

        # The stone is the honest hard edge. Its value only carries across when
        # the customer is keeping the reference's stone unchanged; the moment
        # they ask for a different one, its cost depends on sourcing a specific
        # stone and nobody here knows it.
        stone_changed = bool(stone) and reference is not None and stone.lower() != (reference["stone"] or "none")
        no_reference_stone = reference is None or reference["stone_value_cents"] is None
        include_stone = not stone_changed and not no_reference_stone and (stone or "").lower() != "none"
        stone_value = int(reference["stone_value_cents"]) if include_stone else 0

    metal_value = int(round(weight * rate))
    making_value = int(round(metal_value * making_percent / 100))
    goods_gst = int(round((metal_value + stone_value) * GST_GOODS_PERCENT / 100))
    making_gst = int(round(making_value * GST_MAKING_PERCENT / 100))
    midpoint = metal_value + making_value + stone_value + goods_gst + making_gst

    low = int(round(midpoint * (100 - ESTIMATE_SPREAD_PERCENT) / 100))
    high = int(round(midpoint * (100 + ESTIMATE_SPREAD_PERCENT) / 100))

    covers = "metal, making charges and GST"
    if include_stone:
        covers += f", and the {reference['stone']} at the reference piece's value"
    basis = (
        f"Indicative only — not a quote. Covers {covers}, at roughly "
        f"{format_inr(rate)}/g for {purity_label(target_metal, target_karat) or target_metal.replace('_', ' ')} "
        f"and about {weight:g} g."
    )
    if not include_stone and (stone or (reference and reference["stone"])) not in (None, "none"):
        basis += " The stone is NOT included — it is quoted separately once sourced."

    result = {
        "estimable": True,
        "is_quote": False,
        "reference_sku": reference["sku"] if reference else None,
        "reference_name": reference["name"] if reference else None,
        "estimate_low_cents": low,
        "estimate_low_display": format_inr(low),
        "estimate_high_cents": high,
        "estimate_high_display": format_inr(high),
        "estimate_range_display": f"{format_inr(low)} – {format_inr(high)}",
        "estimate_basis": basis,
        "assumptions": {
            "metal": target_metal,
            "purity": purity_label(target_metal, target_karat),
            "net_weight_grams": round(weight, 3),
            "rate_per_gram_display": format_inr(rate),
            "making_charge_percent": making_percent,
            "stone_included": include_stone,
        },
        "note": (
            "Give the customer the RANGE and say plainly that it is indicative, not a final "
            "price — a jeweller confirms it after reviewing the design."
        ),
    }
    if engraving_text or size:
        # The store's own Return Policy makes these final sale. Saying so after
        # the customer has committed would be setting up a refusal our own
        # quoted policy guarantees.
        result["final_sale_warning"] = (
            "Engraved and custom-sized pieces are final sale — they can't be returned unless "
            "faulty. Tell the customer this BEFORE they confirm the design."
        )
    return result


def submit_design_request(
    customer_id: str,
    reference_sku: str | None = None,
    metal: str | None = None,
    purity_karat: int | None = None,
    stone: str | None = None,
    net_weight_grams=None,
    size: str | None = None,
    engraving_text: str | None = None,
    occasion: str | None = None,
    budget_max_rupees=None,
    notes: str | None = None,
) -> dict:
    """File the brief for a jeweller to price and call back on.

    The estimate is recomputed here and SNAPSHOTTED onto the row rather than
    taken from the caller: a range quoted today must not silently re-price when
    the catalogue moves, and the model must not be able to file a number it made
    up.
    """
    estimate = estimate_custom_design(
        reference_sku=reference_sku, metal=metal, purity_karat=purity_karat,
        stone=stone, net_weight_grams=net_weight_grams, size=size,
        engraving_text=engraving_text,
    )
    if estimate.get("error"):
        return estimate

    spec = {
        "metal": metal, "purity_karat": purity_karat, "stone": stone,
        "approx_weight_grams": float(net_weight_grams) if net_weight_grams else None,
        "size": size, "engraving_text": engraving_text, "occasion": occasion,
        "budget_max_cents": int(float(budget_max_rupees) * 100) if budget_max_rupees else None,
        "notes": notes,
    }

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        reference_id = None
        if reference_sku:
            cur.execute("SELECT id FROM products WHERE sku = %s", (reference_sku,))
            row = cur.fetchone()
            if not row:
                return {"error": "not_found", "message": f"No product with SKU {reference_sku}."}
            reference_id = row["id"]

        request_number = _new_request_number(cur)
        cur.execute(
            """
            INSERT INTO design_requests
                (request_number, customer_id, reference_product_id, spec,
                 estimate_low_cents, estimate_high_cents, estimate_basis)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING created_at
            """,
            (
                request_number, customer_id, reference_id, Json(spec),
                estimate.get("estimate_low_cents"), estimate.get("estimate_high_cents"),
                estimate.get("estimate_basis") or estimate.get("message"),
            ),
        )
        created = cur.fetchone()["created_at"]
        conn.commit()

    return {
        "submitted": True,
        "request_number": request_number,
        "created_at": created.isoformat(),
        "estimate_range_display": estimate.get("estimate_range_display"),
        "estimate_basis": estimate.get("estimate_basis") or estimate.get("message"),
        "callback_note": "A jeweller will review the design and get in touch within 2 working days.",
        "message": (
            f"Design request {request_number} filed. Nothing has been ordered or charged — "
            "this is a brief, not a purchase."
        ),
    }


def get_my_design_requests(customer_id: str) -> dict:
    """The calling customer's own design requests, newest first."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT d.request_number, d.spec, d.estimate_low_cents, d.estimate_high_cents,
                   d.estimate_basis, d.status, d.created_at, p.sku AS reference_sku, p.name AS reference_name
            FROM design_requests d
            LEFT JOIN products p ON p.id = d.reference_product_id
            WHERE d.customer_id = %s
            ORDER BY d.created_at DESC
            """,
            (customer_id,),
        )
        rows = cur.fetchall()

    requests = []
    for r in rows:
        entry = {
            "request_number": r["request_number"],
            "status": r["status"],
            "created_at": r["created_at"].isoformat(),
            "reference_sku": r["reference_sku"],
            "reference_name": r["reference_name"],
            "spec": r["spec"],
            "estimate_basis": r["estimate_basis"],
        }
        if r["estimate_low_cents"] is not None:
            entry["estimate_range_display"] = (
                f"{format_inr(r['estimate_low_cents'])} – {format_inr(r['estimate_high_cents'])}"
            )
        requests.append(entry)
    return {"design_requests": requests, "count": len(requests)}


def admin_list_design_requests(
    actor_customer_id: str,
    status: str | None = None,
    limit: int = 25,
) -> dict:
    """The staff queue of custom design briefs. Staff-only, gated in the
    orchestrator like every other `actor_customer_id` function."""
    limit = max(1, min(int(limit or 25), 50))
    clauses, params = [], []
    if status:
        if status.upper() not in _STATUSES:
            return {
                "error": "bad_status",
                "message": f"Unknown status '{status}'. Use one of: {', '.join(sorted(_STATUSES))}.",
            }
        clauses.append("d.status = %s")
        params.append(status.upper())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT d.request_number, d.spec, d.estimate_low_cents, d.estimate_high_cents,
                   d.status, d.created_at, cu.name AS customer_name, cu.email AS customer_email,
                   p.sku AS reference_sku, p.name AS reference_name
            FROM design_requests d
            JOIN customers cu ON cu.id = d.customer_id
            LEFT JOIN products p ON p.id = d.reference_product_id
            {where}
            ORDER BY d.created_at DESC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    out = []
    for r in rows:
        entry = {
            "request_number": r["request_number"],
            "customer_name": r["customer_name"],
            "customer_email": r["customer_email"],
            "status": r["status"],
            "created_at": r["created_at"].isoformat(),
            "reference_sku": r["reference_sku"],
            "reference_name": r["reference_name"],
            "spec": r["spec"],
        }
        if r["estimate_low_cents"] is not None:
            entry["estimate_range_display"] = (
                f"{format_inr(r['estimate_low_cents'])} – {format_inr(r['estimate_high_cents'])}"
            )
        out.append(entry)
    return {"rows": out, "row_count": len(out), "truncated": len(out) >= limit}
