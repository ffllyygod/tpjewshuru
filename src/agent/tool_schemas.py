"""Anthropic tool-use schemas for the agent's callable tools.

`customer_id` is deliberately NOT a parameter in any schema — the agent
never gets to choose whose orders it's looking at. The orchestrator injects
the session's authenticated customer_id when it actually calls the
underlying Python function. This is the single most important guardrail in
this codebase: it makes "look up someone else's order" not a prompt you can
talk the model out of, but a capability that doesn't exist.
"""

TOOLS = [
    {
        "name": "list_customer_orders",
        "description": "List the current customer's recent orders (most recent first). Use this when the customer asks about 'my orders' generally, or you need an order_number to work with.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max orders to return.", "default": 10},
            },
        },
    },
    {
        "name": "get_order_status",
        "description": "Get full status and item details for one order belonging to the current customer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string", "description": "e.g. TPJ-10001"},
            },
            "required": ["order_number"],
        },
    },
    {
        "name": "check_cancellation_eligibility",
        "description": (
            "Check whether an order can be cancelled and, if so, obtain a confirmation_token. "
            "ALWAYS call this before cancel_order — never call cancel_order without a token from "
            "this call in the same conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string"},
            },
            "required": ["order_number"],
        },
    },
    {
        "name": "cancel_order",
        "description": (
            "Cancel an order. You must have already called check_cancellation_eligibility for this "
            "exact order earlier in this conversation, AND the customer must have explicitly said "
            "yes/confirm to cancelling in their own words before you call this. Never call this "
            "speculatively or to 'check' something."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string"},
            },
            "required": ["order_number"],
        },
    },
    {
        "name": "search_products",
        "description": "Browse/recommend products from the catalog by category, metal, stone, and/or price range. Use this when the customer wants suggestions or is browsing (e.g. 'show me rings under $2000', 'gold necklaces with diamonds').",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["ring", "necklace", "earring", "bracelet", "bangle", "pendant"]},
                "metal": {"type": "string", "enum": ["gold", "silver", "platinum", "rose_gold"]},
                "stone": {"type": "string", "enum": ["diamond", "ruby", "emerald", "sapphire", "none"]},
                "min_price": {"type": "number", "description": "Minimum price in dollars."},
                "max_price": {"type": "number", "description": "Maximum price in dollars."},
            },
        },
    },
    {
        "name": "get_product_details",
        "description": "Get full details (including description) for a single product by SKU.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
            },
            "required": ["sku"],
        },
    },
    {
        "name": "get_metal_rates",
        "description": "Get current gold (24K/22K/18K) and silver spot rates in INR per gram. Use when the customer asks about gold/silver price, rate, or 'what's gold worth today'. Takes no arguments.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "search_knowledge",
        "description": "Search store policies and guides (cancellation/return policy, ring sizing, care instructions, shipping, engraving). Use for any general policy/how-to question that isn't about a specific order.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
]
