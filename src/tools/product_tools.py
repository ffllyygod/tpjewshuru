"""Product catalog browsing / recommendation tool.

Not customer-scoped — anyone (even anonymous, pre-login) can browse the
catalog, so this doesn't take customer_id like the order tools do.
"""

from __future__ import annotations

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.billing import purity_label
from src.tools.formatting import format_inr

VALID_CATEGORIES = {"ring", "necklace", "earring", "bracelet", "bangle", "pendant"}
VALID_METALS = {"gold", "silver", "platinum", "rose_gold"}
VALID_STONES = {"diamond", "ruby", "emerald", "sapphire", "none"}
VALID_OCCASIONS = {"wedding", "engagement", "anniversary", "birthday", "festive", "daily", "gifting"}
VALID_STYLES = {"classic", "contemporary", "minimal", "statement", "traditional"}

_PRODUCT_COLUMNS = """
    sku, name, category, price_cents, metal, stone, sizes_available, stock_by_size,
    occasion, style, net_weight_grams, purity_karat
"""


def _row_to_product(row: dict) -> dict:
    return {
        "sku": row["sku"],
        "name": row["name"],
        "category": row["category"],
        "price": row["price_cents"] / 100,
        "price_display": format_inr(row["price_cents"]),
        "metal": row["metal"],
        "stone": row["stone"],
        # What the piece is FOR, so the assistant can say why it suits the
        # occasion the customer named instead of inferring it from the name.
        "occasion": row.get("occasion") or [],
        "style": row.get("style"),
        "purity": purity_label(row["metal"], row.get("purity_karat")),
        "net_weight_grams": float(row["net_weight_grams"]) if row.get("net_weight_grams") else None,
        "sizes_available": row["sizes_available"],
        "in_stock": any(v > 0 for v in row["stock_by_size"].values()) if row["stock_by_size"] else False,
        "price_note": "Price includes GST. The full breakdown is on the invoice after ordering.",
    }


def _bad_filter(field: str, value: str, valid: set[str]) -> dict:
    """A filter value the catalogue doesn't know must be an error, never an
    empty result list.

    These constants were declared and then never used: an unknown category went
    straight into the WHERE clause and came back as a confident "we don't stock
    any". That is the exact failure the admin tools were hardened against — the
    model reported six out-of-stock products as none — and it was still live
    here.
    """
    return {
        "error": f"bad_{field}",
        "message": f"Unknown {field} '{value}'. Valid values: {', '.join(sorted(valid))}.",
    }


def search_products(
    category: str | None = None,
    metal: str | None = None,
    stone: str | None = None,
    max_price: float | None = None,
    min_price: float | None = None,
    occasion: str | None = None,
    style: str | None = None,
    limit: int = 3,
) -> dict:
    """Search/recommend products by category, metal, stone, occasion, style
    and/or price range (in rupees).

    Discontinued products (active = false) are never returned — an admin can
    soft-delete a product without breaking the order_items rows that FK it.
    """
    for field, value, valid in (
        ("category", category, VALID_CATEGORIES),
        ("metal", metal, VALID_METALS),
        ("stone", stone, VALID_STONES),
        ("occasion", occasion, VALID_OCCASIONS),
        ("style", style, VALID_STYLES),
    ):
        if value and value.lower() not in valid:
            return _bad_filter(field, value, valid)

    # (field, sql, param). Kept as triples rather than parallel lists so a
    # filter can be dropped below without the params drifting out of step.
    filters: list[tuple[str, str, object]] = []
    if category:
        filters.append(("category", "category = %s", category.lower()))
    if metal:
        filters.append(("metal", "metal = %s", metal.lower()))
    if stone:
        filters.append(("stone", "stone = %s", stone.lower()))
    if occasion:
        # Overlap, not equality — a piece is legitimately right for several.
        filters.append(("occasion", "occasion && ARRAY[%s]", occasion.lower()))
    if style:
        filters.append(("style", "style = %s", style.lower()))
    if min_price is not None:
        filters.append(("min_price", "price_cents >= %s", int(min_price * 100)))
    if max_price is not None:
        filters.append(("max_price", "price_cents <= %s", int(max_price * 100)))

    rows = _run_search(filters, limit)

    # Nothing matched. Rather than dead-ending — which is exactly what live
    # testing produced ("no minimal pieces under ₹50,000 for an anniversary",
    # full stop) — drop the SOFT descriptors and show what we do have.
    #
    # Style goes first, then occasion: those are an interpretation of a mood.
    # Category, metal, stone and above all the BUDGET are things the customer
    # actually said, and must never be quietly widened — showing someone a
    # ₹2 lakh piece after they said ₹50,000 is worse than showing them nothing.
    relaxed: list[str] = []
    if not rows:
        for field in ("style", "occasion"):
            if not any(f[0] == field for f in filters):
                continue
            relaxed.append(field)
            filters = [f for f in filters if f[0] != field]
            rows = _run_search(filters, limit)
            if rows:
                break

    result = {"results": [_row_to_product(r) for r in rows], "count": len(rows)}
    if relaxed and rows:
        result["relaxed_filters"] = relaxed
        # A pre-written sentence rather than an instruction to write one.
        #
        # Telling the model "say these aren't an exact match" did not work in
        # live testing — it went straight on calling relaxed results
        # "minimalistic options", which is what the customer asked for and not
        # what they were shown. Handing it the actual line is the same move as
        # invoice_markdown and the `_display` fields: the model repeats a string
        # instead of composing one, and there is nothing left to get wrong.
        result["say_first"] = _relaxed_sentence(relaxed, style, occasion)
        result["note"] = (
            "These are NOT what was asked for — the "
            f"{' and '.join(relaxed)} filter had no matches. Open with the exact sentence in "
            "`say_first`, then show the pieces. Everything else the customer specified, "
            "including the budget, still holds."
        )
    elif relaxed:
        result["note"] = (
            "Nothing matched even after relaxing style and occasion. Suggest widening the budget "
            "or a different category — don't invent products."
        )
    return result


def _relaxed_sentence(relaxed: list[str], style: str | None, occasion: str | None) -> str:
    """The line the assistant opens with when it's showing near-misses."""
    if relaxed == ["style"]:
        return f"I haven't got anything {style} in that range, but these come closest —"
    if relaxed == ["occasion"]:
        return f"Nothing in the catalogue is made specifically for {occasion} at that budget, but these would work beautifully —"
    return (
        f"Nothing matched {style} for {occasion} at that budget, so here are the closest "
        "pieces I do have —"
    )


def _run_search(filters: list[tuple[str, str, object]], limit: int) -> list[dict]:
    # `active = true` is always applied and is never model-controllable.
    clauses = ["active = true", *(sql for _, sql, _ in filters)]
    params = [param for _, _, param in filters]

    # When the customer names a budget, show what's NEAREST it, not the cheapest
    # thing that squeaks under it. Someone who says "around ₹50,000" is telling
    # you what they mean to spend — answering with a ₹1,038 ring reads as being
    # steered to the bargain bin. Without a budget, cheapest-first stands.
    has_budget = any(field == "max_price" for field, _, _ in filters)
    order = "price_cents DESC" if has_budget else "price_cents ASC"

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_PRODUCT_COLUMNS}
            FROM products
            WHERE {' AND '.join(clauses)}
            ORDER BY {order}
            LIMIT %s
            """,
            [*params, limit],
        )
        return cur.fetchall()


def get_product_details(sku: str) -> dict:
    """Get full details for a single product by SKU."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT {_PRODUCT_COLUMNS}, description
            FROM products
            WHERE sku = %s
            """,
            (sku,),
        )
        row = cur.fetchone()

    if not row:
        return {"error": "not_found", "message": f"No product with SKU {sku}."}

    product = _row_to_product(row)
    product["description"] = row["description"]
    return product
