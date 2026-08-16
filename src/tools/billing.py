"""Invoice generation — decomposes a catalogue price into the lines a real
Indian jewellery bill carries, and renders the bill itself.

The load-bearing decision here: **`price_cents` is the amount payable, GST
inclusive, and never changes.** The invoice works *backwards* from it rather
than adding tax on top. Two reasons:

  1. Nothing else moves. `orders.total_amount_cents`, every admin revenue
     figure, coupon settlement (which deducts `min(balance, total)`), and the
     whole existing test suite are untouched by adding invoicing.
  2. A price computed forwards from a live gold rate would give the same order
     a different invoice every day. An invoice is a record of what was charged,
     not a re-quote.

The maths. With M = metal value, K = making charges, S = stone value, and
Indian jewellery GST of 3% on goods (metal + stones) and 5% on making charges:

    P = 1.03·(M + S) + 1.05·K,     K = M · r/100
      = M·(1.03 + 1.05·r/100) + 1.03·S

    ⇒  M = (P − 1.03·S) / (1.03 + 1.05·r/100)

So M is solved for. Every component is then floored to whole paise and the few
paise left over become an explicit **rounding adjustment** row — the same row a
real GST invoice carries. The alternative, folding the remainder into the metal
line, would mean the printed making charge is no longer exactly r% of the
printed metal value and the printed GST no longer exactly 3% of the printed
base: two small lies on the two lines an auditor checks first. One visible
₹0.02 row is the honest version, and `_assert_balances` verifies the column sums
to P before anything is returned. A bill that doesn't add up must never reach a
customer.

Exact rational arithmetic (`fractions.Fraction`) throughout, not float — float
drift is precisely the class of error this module exists to eliminate.

The implied rate per gram (M / net_weight_grams) is reported rather than taken
from a market feed, so the stated rate and the stated total are always
consistent with each other.

Missing data degrades, it never gets invented — same doctrine as the rest of
src/tools/. No weight on record means no per-gram line and a footnote saying so,
not a plausible 6.2 g.
"""

from __future__ import annotations

from fractions import Fraction

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.formatting import format_address, format_inr

# Indian jewellery GST. HSN 7113 is 3% for gold, silver AND platinum alike, so
# there is deliberately no per-metal branching here — only the labels differ.
GST_GOODS_PERCENT = 3     # metal and stones
GST_MAKING_PERCENT = 5    # making / design charges

_GOODS_MULT = Fraction(100 + GST_GOODS_PERCENT, 100)     # 1.03
_MAKING_MULT = Fraction(100 + GST_MAKING_PERCENT, 100)   # 1.05

# Karat is a gold measure. Silver is millesimal (925 sterling) and platinum is
# PT950 — printing "18K" on a silver bangle, or nothing at all, would both be
# wrong. Deterministic map, because purity is a property of the metal and not
# something the model should be inferring.
_NON_KARAT_PURITY = {"silver": "925 Silver", "platinum": "PT950"}


def _money(cents: int) -> tuple[int, str]:
    cents = int(cents)
    return cents, format_inr(cents)


def _assert_balances(components: list[int], total: int) -> None:
    """The invariant this whole module exists to hold. Raised, not returned:
    a caller has no sensible way to recover from a bill that doesn't add up,
    and shipping one is worse than erroring."""
    if sum(components) != total:
        raise AssertionError(
            f"Invoice does not balance: components {components} sum to "
            f"{sum(components)}, expected {total}."
        )


def purity_label(metal: str | None, purity_karat: int | None) -> str | None:
    """"18K", "925 Silver", "PT950", or None when it genuinely isn't recorded.

    Never guesses. A gold piece with no recorded karat prints no purity rather
    than a plausible 22K — the same rule as every other missing figure here.
    """
    if metal in _NON_KARAT_PURITY:
        return _NON_KARAT_PURITY[metal]
    return f"{int(purity_karat)}K" if purity_karat else None


def decompose_price(
    price_cents: int,
    net_weight_grams=None,
    purity_karat: int | None = None,
    making_charge_percent=None,
    stone_value_cents: int | None = None,
    metal: str | None = None,
    stone: str | None = None,
) -> dict:
    """Break a GST-inclusive price into metal / making / stone / GST lines that
    sum to exactly `price_cents`.

    Every argument except the price may be None — the normal case for a product
    nobody has measured. The result then carries `data_complete: False` and a
    `notes` list naming exactly what was left out, so the invoice can say so
    instead of quietly filling the gap.
    """
    P = int(price_cents)
    notes: list[str] = []

    if making_charge_percent is None:
        notes.append("Making charges are not itemised separately for this piece.")
        r = Fraction(0)
    else:
        r = Fraction(str(making_charge_percent))
        if r < 0:
            r = Fraction(0)

    if stone_value_cents is None:
        S = 0
        if stone and stone != "none":
            notes.append(
                f"{stone.title()} value is included in the piece's value and not itemised."
            )
    else:
        S = max(0, int(stone_value_cents))

    # A recorded stone value at or above the ticket price makes the split
    # unsolvable (metal would be zero or negative). That's a data error, not a
    # reason to refuse a legitimate sale — so the breakdown is dropped, the
    # total stands, and the invoice says which one happened.
    breakdown_reliable = _GOODS_MULT * S < P
    if not breakdown_reliable:
        notes.append(
            "The recorded stone value is inconsistent with this piece's price, so the "
            "component breakdown is unavailable. The total is correct."
        )
        S = 0

    denominator = _GOODS_MULT + _MAKING_MULT * r / 100
    M = (Fraction(P) - _GOODS_MULT * S) / denominator

    # Floor every component, then let the remainder be a visible row. Flooring
    # (rather than rounding) is what guarantees the adjustment is never
    # negative — "Rounding adjustment -₹0.01" on a bill invites a support call.
    metal_cents = int(M)                                     # floor, M > 0 here
    making_cents = int(Fraction(metal_cents) * r / 100)
    gst_goods_cents = (metal_cents + S) * GST_GOODS_PERCENT // 100
    gst_making_cents = making_cents * GST_MAKING_PERCENT // 100
    round_off_cents = P - (metal_cents + making_cents + S + gst_goods_cents + gst_making_cents)

    # Each floor loses under a paisa and M's floor propagates by at most
    # (1.03 + 0.0105·r) of one. A remainder of a rupee or more means the maths
    # above is wrong, and per doctrine that fails loudly rather than shipping.
    if not 0 <= round_off_cents < 100:
        raise AssertionError(
            f"Rounding adjustment {round_off_cents} out of range for price {P} "
            f"(making {r}%, stone {S}) — decomposition is wrong."
        )
    _assert_balances(
        [metal_cents, making_cents, S, gst_goods_cents, gst_making_cents, round_off_cents], P
    )

    weight = Fraction(str(net_weight_grams)) if net_weight_grams is not None else None
    if weight is not None and weight <= 0:
        weight = None
    if weight is None:
        notes.append("Metal weight is not on record for this piece.")

    result: dict = {"breakdown_reliable": breakdown_reliable}
    result["price_cents"], result["price_display"] = _money(P)
    result["metal_value_cents"], result["metal_value_display"] = _money(metal_cents)
    result["making_charge_cents"], result["making_charge_display"] = _money(making_cents)
    result["making_charge_percent"] = float(r)
    result["making_charge_percent_display"] = f"{float(r):g}%"
    result["stone_value_cents"], result["stone_value_display"] = _money(S)
    result["gst_goods_cents"], result["gst_goods_display"] = _money(gst_goods_cents)
    result["gst_making_cents"], result["gst_making_display"] = _money(gst_making_cents)
    result["gst_total_cents"], result["gst_total_display"] = _money(gst_goods_cents + gst_making_cents)
    result["round_off_cents"], result["round_off_display"] = _money(round_off_cents)
    result["taxable_value_cents"], result["taxable_value_display"] = _money(metal_cents + making_cents + S)

    result["net_weight_grams"] = float(weight) if weight is not None else None
    result["net_weight_display"] = f"{float(weight):.3f} g" if weight is not None else None
    result["purity_label"] = purity_label(metal, purity_karat)

    if weight is not None:
        # The IMPLIED rate — metal value divided by weight. Always consistent
        # with the printed total, which a live market rate could never be.
        rate = int(Fraction(metal_cents) / weight)
        result["rate_per_gram_cents"], result["rate_per_gram_display"] = _money(rate)
    else:
        result["rate_per_gram_cents"] = None
        result["rate_per_gram_display"] = None

    # "Complete" means the bill can show every line a real one would. GST and
    # totals are right regardless; this flags how much detail sits behind them.
    result["data_complete"] = (
        weight is not None and making_charge_percent is not None and breakdown_reliable
    )
    result["notes"] = notes
    return result


# ---------------------------------------------------------------------------
# Invoice rendering
# ---------------------------------------------------------------------------
#
# The markdown is built here, not by the model. Exactly the same reasoning as
# the `_display` convention: the model has demonstrably got rupee arithmetic and
# column totals wrong, so it is handed a finished string to repeat rather than a
# table to assemble. GFM tables render in the chat UI already
# (web/src/components/Markdown.tsx), and h1/h2 are downgraded to <p> there —
# hence bold text for the heading rather than a markdown heading.


def _item_rows(item: dict) -> list[str]:
    """The component lines for one order line. Lines with no value are omitted
    rather than printed as ₹0.00 — a zero making charge on a machine-made chain
    is noise, and an itemised zero invites the question of whether it's a bug."""
    d = item["breakdown"]
    rows = []

    header = f"SKU {item['sku']}"
    if item.get("size"):
        header += f" · Size {item['size']}"
    header += f" · Qty {item['quantity']}"
    rows.append(f"| **{item['name']}** | {header} | |")

    metal_detail = " · ".join(
        p for p in (
            d["purity_label"],
            d["net_weight_display"],
            f"@ {d['rate_per_gram_display']}/g" if d["rate_per_gram_display"] else None,
        ) if p
    ) or "weight not on record"
    rows.append(f"| — {item['metal_label']} value | {metal_detail} | {d['metal_value_display']} |")

    if d["making_charge_cents"]:
        rows.append(f"| — Making charges | {d['making_charge_percent_display']} | {d['making_charge_display']} |")
    if d["stone_value_cents"]:
        rows.append(f"| — {item['stone_label']} | | {d['stone_value_display']} |")

    rows.append(f"| — GST @ {GST_GOODS_PERCENT}% | on metal + stones | {d['gst_goods_display']} |")
    if d["gst_making_cents"]:
        rows.append(f"| — GST @ {GST_MAKING_PERCENT}% | on making charges | {d['gst_making_display']} |")
    if d["round_off_cents"]:
        rows.append(f"| — Rounding adjustment | | {d['round_off_display']} |")
    return rows


def _render_invoice_markdown(inv: dict) -> str:
    lines = [
        f"**TAX INVOICE — {inv['order_number']}**  ·  {inv['placed_on_display']}",
        "",
        "| Item | Details | Amount |",
        "|---|---|---|",
    ]
    for item in inv["items"]:
        lines.extend(_item_rows(item))

    if inv["discount_cents"]:
        # Subtotal only appears when something is deducted from it. With no
        # discount it would just restate the total on the next line.
        lines.append(f"| Subtotal | | {inv['subtotal_display']} |")
        lines.append(f"| {inv['discount_label']} | | -{inv['discount_display']} |")
    lines.append(f"| **Total payable** | | **{inv['amount_payable_display']}** |")

    lines.append("")
    if inv["shipping_address_display"]:
        lines.append("**Ship to**")
        lines.extend(inv["shipping_address_display"].split("\n"))
        lines.append("")

    payment = f"**Payment:** {inv['payment_status']}"
    if inv["payment_method"]:
        payment += f" · {inv['payment_method']}"
    if inv["payment_reference"]:
        payment += f" · ref {inv['payment_reference']}"
    lines.append(payment)

    notes = inv["notes"]
    if notes:
        lines.append("")
        lines.extend(f"_{n}_" for n in notes)

    return "\n".join(lines)


_INVOICE_SQL = """
    SELECT o.id, o.order_number, o.status::text AS status, o.placed_at,
           o.total_amount_cents, o.discount_cents, o.payment_status,
           o.payment_method, o.payment_reference, o.shipping_address,
           o.coupon_id, o.gold_sip_subscription_id,
           cu.name AS customer_name, cu.email AS customer_email
    FROM orders o
    JOIN customers cu ON cu.id = o.customer_id
    WHERE o.order_number = %s
"""


def _build_invoice(cur, order: dict) -> dict:
    cur.execute(
        """
        SELECT oi.quantity, oi.unit_price_cents, oi.size,
               p.sku, p.name, p.metal, p.stone,
               p.net_weight_grams, p.purity_karat,
               p.making_charge_percent, p.stone_value_cents
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = %s
        """,
        (order["id"],),
    )
    rows = cur.fetchall()

    items, notes, subtotal = [], [], 0
    for r in rows:
        qty = int(r["quantity"] or 1)
        line_total = int(r["unit_price_cents"]) * qty
        subtotal += line_total

        # Decompose the whole LINE, scaling weight and stone value by quantity,
        # rather than decomposing one unit and multiplying. Multiplying a rounded
        # per-unit figure is how a two-of-something invoice ends up a paise out.
        breakdown = decompose_price(
            line_total,
            net_weight_grams=(r["net_weight_grams"] * qty) if r["net_weight_grams"] is not None else None,
            purity_karat=r["purity_karat"],
            making_charge_percent=r["making_charge_percent"],
            stone_value_cents=(r["stone_value_cents"] * qty) if r["stone_value_cents"] is not None else None,
            metal=r["metal"],
            stone=r["stone"],
        )
        metal = (r["metal"] or "metal").replace("_", " ").title()
        stone = (r["stone"] or "none").title()
        items.append({
            "sku": r["sku"],
            # A product name containing a pipe would break the markdown table it
            # is interpolated into — escape at the boundary, not by trusting the
            # catalogue.
            "name": str(r["name"]).replace("|", "\\|"),
            "size": r["size"],
            "quantity": qty,
            "unit_price_cents": int(r["unit_price_cents"]),
            "unit_price_display": format_inr(r["unit_price_cents"]),
            "line_total_cents": line_total,
            "line_total_display": format_inr(line_total),
            "metal_label": metal,
            "stone_label": stone if stone != "None" else "Stones",
            "breakdown": breakdown,
        })
        for note in breakdown["notes"]:
            if note not in notes:
                notes.append(note)

    discount = int(order["discount_cents"] or 0)
    total = int(order["total_amount_cents"])
    payable = total - discount

    # The line totals are derived from order_items; the order carries its own
    # total. They must agree — if a past migration or a manual edit made them
    # disagree, a bill quoting one and an order charging the other is the worst
    # possible outcome, so say so rather than picking a side.
    if subtotal != total:
        return {
            "error": "invoice_mismatch",
            "message": (
                f"Order {order['order_number']}'s line items sum to {format_inr(subtotal)} "
                f"but the order total is {format_inr(total)}. Staff need to look at this "
                "before an invoice can be issued."
            ),
        }

    inv: dict = {
        "order_number": order["order_number"],
        "status": order["status"],
        "placed_at": order["placed_at"].isoformat(),
        "placed_on_display": order["placed_at"].strftime("%d %B %Y"),
        "customer_name": order["customer_name"],
        "items": items,
        "payment_status": order["payment_status"],
        "payment_method": order["payment_method"],
        "payment_reference": order["payment_reference"],
        "shipping_address_display": format_address(order["shipping_address"]),
        "notes": notes,
        # Which discount source it was, so the bill names it rather than showing
        # an unexplained deduction.
        "discount_label": "Gold SIP redemption" if order["gold_sip_subscription_id"] else "Coupon discount",
    }
    inv["subtotal_cents"], inv["subtotal_display"] = _money(subtotal)
    inv["total_amount_cents"], inv["total_amount_display"] = _money(total)
    inv["discount_cents"], inv["discount_display"] = _money(discount)
    inv["amount_payable_cents"], inv["amount_payable_display"] = _money(payable)
    inv["gst_total_cents"], inv["gst_total_display"] = _money(
        sum(i["breakdown"]["gst_total_cents"] for i in items)
    )
    inv["round_off_cents"], inv["round_off_display"] = _money(
        sum(i["breakdown"]["round_off_cents"] for i in items)
    )
    inv["note"] = (
        "Listed prices already include GST — the breakdown shows how each price is "
        "composed, it does not add anything on top."
    )
    inv["invoice_markdown"] = _render_invoice_markdown(inv)
    return inv


def generate_invoice(customer_id: str, order_number: str) -> dict:
    """Full itemised tax invoice for one of the calling customer's own orders.

    Scoped exactly like get_order_status: `customer_id` is injected by the
    orchestrator from the verified session and re-checked in SQL here, so an
    invoice for someone else's order is not a thing this function can produce.
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_INVOICE_SQL + " AND o.customer_id = %s", (order_number, customer_id))
        order = cur.fetchone()
        if not order:
            return {
                "error": "not_found",
                "message": f"No order {order_number} on your account.",
            }
        return _build_invoice(cur, order)


def admin_order_invoice(actor_customer_id: str, order_number: str) -> dict:
    """The same invoice, for any customer's order. Staff-only — reached only
    through the admin gate in src/agent/orchestrator.py, same as every other
    function taking `actor_customer_id` rather than `customer_id`."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_INVOICE_SQL, (order_number,))
        order = cur.fetchone()
        if not order:
            return {"error": "not_found", "message": f"No order {order_number}."}
        invoice = _build_invoice(cur, order)
        invoice["customer_email"] = order["customer_email"]
        return invoice
