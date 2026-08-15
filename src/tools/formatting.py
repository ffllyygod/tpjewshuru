"""Shared INR currency formatting — used everywhere a tool returns a rupee
amount, so the model is never the one doing paise-to-rupee arithmetic or
Indian digit grouping (3,25,000 not 325,000). This exists because relying
on the model to do that math correctly failed repeatedly in practice — see
JOURNAL.md ("off-by-10x display error", recurring). Every tool with a
"*_cents" field now also returns a matching "*_display" string; the system
prompt tells the model to use the display field verbatim.
"""

from __future__ import annotations


def format_inr(cents: int) -> str:
    """Format a paise amount (int) as an Indian-grouped rupee string,
    e.g. 32500000 -> "₹3,25,000.00", 3250000 -> "₹32,500.00"."""
    negative = cents < 0
    cents = abs(int(cents))
    rupees_int, paise = divmod(cents, 100)
    s = str(rupees_int)

    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        formatted = ",".join(groups + [last3])
    else:
        formatted = s

    result = f"₹{formatted}.{paise:02d}"
    return f"-{result}" if negative else result
