"""Live gold/silver spot rates, converted to INR/gram for common jewellery
purities. Uses free, keyless public APIs (gold-api.com for spot XAU/XAG,
exchangerate-api.com's open endpoint for USD->INR) — no account needed, but
also no SLA, so this fails soft (returns an error dict, never raises) and is
cached briefly to avoid hammering them on every chat turn.

Important honesty note (also stated in the tool's own response and echoed by
the system prompt): these are spot-market-derived estimates, not this
store's showroom price. Real Indian retail gold/silver pricing includes
import duty, GST, and making charges on top of spot — meaningfully higher
than what this tool reports. Don't let the agent present this as "the price
you'll pay in-store."
"""

from __future__ import annotations

import time

import requests

TROY_OUNCE_GRAMS = 31.1034768
CACHE_TTL_SECONDS = 300

_cache: dict | None = None
_cache_at: float = 0.0


def _fetch_live() -> dict:
    gold = requests.get("https://api.gold-api.com/price/XAU", timeout=8).json()
    silver = requests.get("https://api.gold-api.com/price/XAG", timeout=8).json()
    fx = requests.get("https://open.er-api.com/v6/latest/USD", timeout=8).json()

    usd_inr = fx["rates"]["INR"]
    gold_usd_per_gram = gold["price"] / TROY_OUNCE_GRAMS
    silver_usd_per_gram = silver["price"] / TROY_OUNCE_GRAMS
    gold_inr_per_gram_24k = gold_usd_per_gram * usd_inr
    silver_inr_per_gram = silver_usd_per_gram * usd_inr

    return {
        "as_of": gold.get("updatedAt"),
        "usd_inr_rate": round(usd_inr, 2),
        "gold_inr_per_gram": {
            "24k_999": round(gold_inr_per_gram_24k, 2),
            "22k_916": round(gold_inr_per_gram_24k * 0.916, 2),
            "18k_750": round(gold_inr_per_gram_24k * 0.750, 2),
        },
        "silver_inr_per_gram_999": round(silver_inr_per_gram, 2),
        "note": (
            "These are global spot-market rates converted to INR, not a showroom "
            "quote. Actual retail price for jewellery is meaningfully higher — it "
            "includes import duty, GST, and making/design charges on top of the "
            "metal value."
        ),
    }


def get_metal_rates() -> dict:
    """Get current gold (24K/22K/18K) and silver spot rates in INR per gram."""
    global _cache, _cache_at
    now = time.time()
    if _cache is not None and (now - _cache_at) < CACHE_TTL_SECONDS:
        return _cache

    try:
        result = _fetch_live()
    except (requests.RequestException, KeyError, ValueError, TypeError) as e:
        return {
            "error": "rates_unavailable",
            "message": f"Couldn't fetch live metal rates right now ({e.__class__.__name__}). Try again shortly.",
        }

    _cache = result
    _cache_at = now
    return result
