"""Saved delivery addresses.

Every rule about what a valid Indian address looks like lives here, in code,
rather than in the system prompt. That is the whole point: "make sure the PIN
code is six digits" is an instruction a model can be talked out of, forget under
a long conversation, or silently apply to the wrong field. A regex cannot.

The same reasoning gives the tool the job of *normalising* — "+91 98450 12345"
to "9845012345", "KA" to "Karnataka". If the model were doing that, it would
occasionally do it wrong, and a wrong delivery address is not recoverable by
apologising.

Ownership follows the codebase invariant: `customer_id` is injected server-side
by the orchestrator, and every query is scoped by it. Reading or shipping to
another customer's address is not a thing these functions can be asked to do.
"""

from __future__ import annotations

import re

from psycopg.rows import dict_row

from src.db.connection import get_conn
from src.tools.formatting import format_address

# 28 states + 8 union territories, with the postal abbreviations people actually
# type. The model never decides that "KA" means Karnataka — this does.
_STATES = {
    "andhra pradesh": "Andhra Pradesh", "ap": "Andhra Pradesh",
    "arunachal pradesh": "Arunachal Pradesh", "ar": "Arunachal Pradesh",
    "assam": "Assam", "as": "Assam",
    "bihar": "Bihar", "br": "Bihar",
    "chhattisgarh": "Chhattisgarh", "cg": "Chhattisgarh", "ct": "Chhattisgarh",
    "goa": "Goa", "ga": "Goa",
    "gujarat": "Gujarat", "gj": "Gujarat",
    "haryana": "Haryana", "hr": "Haryana",
    "himachal pradesh": "Himachal Pradesh", "hp": "Himachal Pradesh",
    "jharkhand": "Jharkhand", "jh": "Jharkhand",
    "karnataka": "Karnataka", "ka": "Karnataka",
    "kerala": "Kerala", "kl": "Kerala",
    "madhya pradesh": "Madhya Pradesh", "mp": "Madhya Pradesh",
    "maharashtra": "Maharashtra", "mh": "Maharashtra",
    "manipur": "Manipur", "mn": "Manipur",
    "meghalaya": "Meghalaya", "ml": "Meghalaya",
    "mizoram": "Mizoram", "mz": "Mizoram",
    "nagaland": "Nagaland", "nl": "Nagaland",
    "odisha": "Odisha", "or": "Odisha", "orissa": "Odisha",
    "punjab": "Punjab", "pb": "Punjab",
    "rajasthan": "Rajasthan", "rj": "Rajasthan",
    "sikkim": "Sikkim", "sk": "Sikkim",
    "tamil nadu": "Tamil Nadu", "tn": "Tamil Nadu",
    "telangana": "Telangana", "tg": "Telangana", "ts": "Telangana",
    "tripura": "Tripura", "tr": "Tripura",
    "uttar pradesh": "Uttar Pradesh", "up": "Uttar Pradesh",
    "uttarakhand": "Uttarakhand", "uk": "Uttarakhand", "ua": "Uttarakhand",
    "west bengal": "West Bengal", "wb": "West Bengal",
    "andaman and nicobar islands": "Andaman and Nicobar Islands", "an": "Andaman and Nicobar Islands",
    "chandigarh": "Chandigarh", "ch": "Chandigarh",
    "dadra and nagar haveli and daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "dn": "Dadra and Nagar Haveli and Daman and Diu", "dd": "Dadra and Nagar Haveli and Daman and Diu",
    "delhi": "Delhi", "dl": "Delhi", "new delhi": "Delhi",
    "jammu and kashmir": "Jammu and Kashmir", "jk": "Jammu and Kashmir",
    "ladakh": "Ladakh", "la": "Ladakh",
    "lakshadweep": "Lakshadweep", "ld": "Lakshadweep",
    "puducherry": "Puducherry", "py": "Puducherry", "pondicherry": "Puducherry",
}

STATE_NAMES = sorted(set(_STATES.values()))

# PIN prefix -> state, for catching a transposed or mistyped PIN.
#
# DELIBERATELY INCOMPLETE. Only prefixes that map unambiguously to one state are
# here; the genuinely mixed ranges (16x spans Punjab and Chandigarh, 24x spans
# UP and Uttarakhand, 8xx spans Bihar and Jharkhand) are omitted entirely. An
# unknown prefix means "no opinion", never "mismatch" — a check that wrongly
# rejects a real customer's real address is far worse than one that stays quiet,
# and this exists to catch typos, not to be an authority on postal geography.
_PIN_PREFIX_STATE = {
    "11": "Delhi",
    "12": "Haryana", "13": "Haryana",
    "14": "Punjab", "15": "Punjab",
    "17": "Himachal Pradesh",
    "30": "Rajasthan", "31": "Rajasthan", "32": "Rajasthan", "33": "Rajasthan",
    "36": "Gujarat", "37": "Gujarat", "38": "Gujarat", "39": "Gujarat",
    "40": "Maharashtra", "41": "Maharashtra", "42": "Maharashtra",
    "43": "Maharashtra", "44": "Maharashtra",
    "45": "Madhya Pradesh", "46": "Madhya Pradesh",
    "47": "Madhya Pradesh", "48": "Madhya Pradesh",
    "49": "Chhattisgarh",
    "50": "Telangana",
    "51": "Andhra Pradesh", "52": "Andhra Pradesh", "53": "Andhra Pradesh",
    "56": "Karnataka", "57": "Karnataka", "58": "Karnataka", "59": "Karnataka",
    "60": "Tamil Nadu", "61": "Tamil Nadu", "62": "Tamil Nadu",
    "63": "Tamil Nadu", "64": "Tamil Nadu",
    "67": "Kerala", "68": "Kerala", "69": "Kerala",
    "70": "West Bengal", "71": "West Bengal", "72": "West Bengal",
    "73": "West Bengal", "74": "West Bengal",
    "75": "Odisha", "76": "Odisha", "77": "Odisha",
    "78": "Assam",
}

_PIN_RE = re.compile(r"^[1-9][0-9]{5}$")
_PHONE_RE = re.compile(r"^[6-9][0-9]{9}$")


def _err(code: str, message: str) -> dict:
    return {"saved": False, "error": code, "message": message}


def normalise_pin(raw: str) -> str | None:
    """'560 025' / '560-025' -> '560025'. None if it isn't an Indian PIN."""
    cleaned = re.sub(r"[\s-]", "", str(raw or ""))
    return cleaned if _PIN_RE.match(cleaned) else None


def normalise_phone(raw: str) -> str | None:
    """'+91 98450 12345' / '098450-12345' -> '9845012345'. None if not a valid
    Indian mobile. Landlines are rejected on purpose: couriers SMS this."""
    cleaned = re.sub(r"[\s\-()]", "", str(raw or ""))
    cleaned = re.sub(r"^(\+91|91|0)", "", cleaned)
    return cleaned if _PHONE_RE.match(cleaned) else None


def normalise_state(raw: str) -> str | None:
    return _STATES.get(str(raw or "").strip().lower())


def address_snapshot(row) -> dict:
    """The JSONB frozen onto `orders.shipping_address` at purchase.

    A snapshot, not a reference: editing a saved address later must never
    rewrite where an already-placed parcel was sent. Shared by place_order and
    the demo seeder so the two can't drift.
    """
    get = row.get if isinstance(row, dict) else (lambda k: row[k])
    return {
        "recipient_name": get("recipient_name"),
        "phone": get("phone"),
        "line1": get("line1"),
        "line2": get("line2"),
        "city": get("city"),
        "state": get("state"),
        "postal_code": get("postal_code"),
        "country": get("country") or "IN",
    }


def _row_to_address(row: dict) -> dict:
    snapshot = address_snapshot(row)
    return {
        "address_id": str(row["id"]),
        "label": row["label"],
        "is_default": row["is_default"],
        # Pre-formatted so the model repeats one string instead of reassembling
        # an address out of eight fields — the same reasoning as `_display`.
        "formatted": format_address(snapshot),
        **snapshot,
    }


_SELECT_ADDRESS = """
    SELECT id, label, recipient_name, phone, line1, line2, city, state,
           postal_code, country, is_default
    FROM customer_addresses
"""


def get_my_addresses(customer_id: str) -> dict:
    """The calling customer's saved addresses, default first."""
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _SELECT_ADDRESS + " WHERE customer_id = %s ORDER BY is_default DESC, created_at",
            (customer_id,),
        )
        rows = cur.fetchall()

    addresses = [_row_to_address(r) for r in rows]
    return {
        "addresses": addresses,
        "count": len(addresses),
        "default_address_id": next((a["address_id"] for a in addresses if a["is_default"]), None),
    }


def get_address_for_order(cur, customer_id: str, address_id: str | None):
    """Resolve which saved address an order should ship to, scoped to its owner.

    Returns (row, error_dict). A wrong-owner address_id is reported exactly like
    a nonexistent one — telling the caller apart would confirm that some other
    customer's address exists, which is the same reasoning redeem_coupon uses.
    """
    if address_id:
        try:
            cur.execute(
                _SELECT_ADDRESS + " WHERE id = %s AND customer_id = %s",
                (address_id, customer_id),
            )
        except Exception:
            # A malformed UUID is a bad argument, not a server error.
            return None, {"reason": "address_not_found", "message": f"No saved address {address_id}."}
        row = cur.fetchone()
        if not row:
            return None, {"reason": "address_not_found", "message": f"No saved address {address_id}."}
        return row, None

    cur.execute(
        _SELECT_ADDRESS + " WHERE customer_id = %s ORDER BY is_default DESC, created_at LIMIT 1",
        (customer_id,),
    )
    row = cur.fetchone()
    if not row:
        return None, {
            "reason": "address_required",
            "message": (
                "There's no delivery address on this account yet. Ask the customer for the "
                "house/street, city, state, 6-digit PIN code and a 10-digit mobile, then call "
                "save_address before placing the order."
            ),
        }
    return row, None


def save_address(
    customer_id: str,
    recipient_name: str,
    phone: str,
    line1: str,
    city: str,
    state: str,
    postal_code: str,
    line2: str | None = None,
    label: str | None = None,
    make_default: bool = True,
    confirm_mismatch: bool = False,
) -> dict:
    """Save a delivery address, validating and normalising every field.

    Errors are structured and say what a valid value looks like, so the model
    can go back to the customer with a real question instead of retrying a
    guess — the same shape as the `bad_status` errors in admin_tools.
    """
    if not str(recipient_name or "").strip():
        return _err("missing_recipient", "Who should the parcel be addressed to?")
    if len(str(line1 or "").strip()) < 5:
        return _err(
            "invalid_line1",
            "The first address line needs the house/flat number and street — I got "
            f"'{line1}', which is too short to deliver to.",
        )
    if len(str(city or "").strip()) < 2:
        return _err("invalid_city", f"'{city}' doesn't look like a city name.")

    pin = normalise_pin(postal_code)
    if not pin:
        return _err(
            "invalid_postal_code",
            f"An Indian PIN code is 6 digits and doesn't start with 0 — e.g. 560025. I got '{postal_code}'.",
        )

    mobile = normalise_phone(phone)
    if not mobile:
        return _err(
            "invalid_phone",
            f"I need a 10-digit Indian mobile number starting 6, 7, 8 or 9 for delivery. I got '{phone}'.",
        )

    canonical_state = normalise_state(state)
    if not canonical_state:
        return _err(
            "invalid_state",
            f"'{state}' isn't an Indian state or union territory. Valid values: {', '.join(STATE_NAMES)}.",
        )

    # Soft, overridable, and silent on prefixes the map has no opinion about.
    expected = _PIN_PREFIX_STATE.get(pin[:2])
    if expected and expected != canonical_state and not confirm_mismatch:
        return _err(
            "pin_state_mismatch",
            f"PIN {pin} is in {expected}, but the state given is {canonical_state}. "
            "Ask the customer which is right. If they confirm the address as given, "
            "call this again with confirm_mismatch=true.",
        )

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        if make_default:
            # Cleared in the same transaction as the insert, so the partial
            # unique index can never see two defaults even under a retry.
            cur.execute(
                "UPDATE customer_addresses SET is_default = false WHERE customer_id = %s AND is_default",
                (customer_id,),
            )
        cur.execute(
            """
            INSERT INTO customer_addresses
                (customer_id, label, recipient_name, phone, line1, line2, city,
                 state, postal_code, country, is_default)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'IN', %s)
            """ + " RETURNING " + _SELECT_ADDRESS.split("SELECT")[1].split("FROM")[0].strip(),
            (
                customer_id, label, str(recipient_name).strip(), mobile,
                str(line1).strip(), (str(line2).strip() if line2 else None),
                str(city).strip(), canonical_state, pin, bool(make_default),
            ),
        )
        row = cur.fetchone()
        conn.commit()

    address = _row_to_address(row)
    return {
        "saved": True,
        "address_id": address["address_id"],
        "formatted": address["formatted"],
        "is_default": address["is_default"],
        "message": "Delivery address saved.",
    }
