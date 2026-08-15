"""Product catalog browsing / recommendation tool.

Not customer-scoped — anyone (even anonymous, pre-login) can browse the
catalog, so this doesn't take customer_id like the order tools do.
"""

from __future__ import annotations

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.formatting import format_inr

VALID_CATEGORIES = {"ring", "necklace", "earring", "bracelet", "bangle", "pendant"}
VALID_METALS = {"gold", "silver", "platinum", "rose_gold"}
VALID_STONES = {"diamond", "ruby", "emerald", "sapphire", "none"}


def _row_to_product(row: dict) -> dict:
    return {
        "sku": row["sku"],
        "name": row["name"],
        "category": row["category"],
        "price": row["price_cents"] / 100,
        "price_display": format_inr(row["price_cents"]),
        "metal": row["metal"],
        "stone": row["stone"],
        "sizes_available": row["sizes_available"],
        "in_stock": any(v > 0 for v in row["stock_by_size"].values()) if row["stock_by_size"] else False,
    }


def search_products(
    category: str | None = None,
    metal: str | None = None,
    stone: str | None = None,
    max_price: float | None = None,
    min_price: float | None = None,
    limit: int = 5,
) -> dict:
    """Search/recommend products by category, metal, stone, and/or price range (in rupees).

    Discontinued products (active = false) are never returned — an admin can
    soft-delete a product without breaking the order_items rows that FK it.
    """
    # Always applied, never model-controllable.
    clauses = ["active = true"]
    params: list = []

    if category:
        clauses.append("category = %s")
        params.append(category.lower())
    if metal:
        clauses.append("metal = %s")
        params.append(metal.lower())
    if stone:
        clauses.append("stone = %s")
        params.append(stone.lower())
    if min_price is not None:
        clauses.append("price_cents >= %s")
        params.append(int(min_price * 100))
    if max_price is not None:
        clauses.append("price_cents <= %s")
        params.append(int(max_price * 100))

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT sku, name, category, price_cents, metal, stone, sizes_available, stock_by_size
            FROM products
            {where}
            ORDER BY price_cents ASC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    return {"results": [_row_to_product(r) for r in rows], "count": len(rows)}


def get_product_details(sku: str) -> dict:
    """Get full details for a single product by SKU."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT sku, name, category, description, price_cents, metal, stone, sizes_available, stock_by_size
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
