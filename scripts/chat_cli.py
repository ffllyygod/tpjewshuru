"""Terminal chat client for local development — no frontend, no OTP inbox.

    python scripts/chat_cli.py                          # anonymous
    python scripts/chat_cli.py arun@shurutech.com       # as that customer
    python scripts/chat_cli.py admin@dpjewellers.com --admin   # staff console

This talks to the orchestrator **directly**, not over HTTP. That is deliberate:
real login is passwordless OTP-over-email, so an HTTP client would need to read
a code out of a mailbox to authenticate. Since this is a local script against a
local database — the same way `tests/` work — it looks the customer up by email
and hands the orchestrator the identity the API layer would otherwise have
resolved from a verified session.

Consequences worth being clear about:

  - This BYPASSES authentication, so it proves nothing about the auth layer.
    `tests/test_auth.py` covers that; use the web app to exercise a real login.
  - It does NOT bypass any tool guardrail. Identity is still injected
    server-side, staff tools are still gated on the is_admin flag, confirmation
    tokens still apply. What you see here is what the deployed agent does.
  - `--admin` requires the account to actually have role='admin' in the
    database; it refuses otherwise rather than pretending, because a staff
    console you can enter without being staff would make this useless for
    checking the thing it's most useful for checking.

An earlier version posted to the HTTP API with {"email": ...}. That stopped
working when real auth landed — /conversations no longer accepts a
client-supplied email, so the field was silently ignored and every session came
back anonymous while the CLI claimed otherwise.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.orchestrator import run_turn  # noqa: E402
from src.db.connection import get_conn  # noqa: E402


def _lookup(email: str) -> tuple[str, str, str]:
    """(customer_id, name, role) for an email, or exit with a usable message."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, role FROM customers WHERE lower(email) = lower(%s)", (email,))
        row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"No customer with email '{email}'.\n"
            "Run scripts/seed_db.py --reset and scripts/add_demo_user.py first, "
            "or scripts/add_admin_users.py for a staff account."
        )
    return str(row[0]), row[1], row[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("email", nargs="?", help="customer email; omit for an anonymous session")
    parser.add_argument("--admin", action="store_true", help="open a staff-console conversation")
    args = parser.parse_args()

    customer_id = name = None
    mode = "customer"

    if args.email:
        customer_id, name, role = _lookup(args.email)
        if args.admin:
            if role != "admin":
                raise SystemExit(
                    f"{args.email} has role '{role}', not 'admin'. Promote them with:\n"
                    f"    python scripts/add_admin_users.py --email {args.email}"
                )
            mode = "admin"
    elif args.admin:
        raise SystemExit("--admin needs an email: staff access comes from an account, not a flag.")

    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, customer_id, mode) VALUES (%s, %s, %s)",
            (conv_id, customer_id, mode),
        )
        conn.commit()

    if mode == "admin":
        print(f"-- STAFF CONSOLE as {name} ({args.email}) --")
    elif customer_id:
        print(f"-- chatting as {name} ({args.email}) --")
    else:
        print("-- anonymous session (product/policy questions only) --")
    print("-- type 'exit' to quit --\n")

    while True:
        try:
            msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not msg or msg.lower() in ("exit", "quit"):
            break

        try:
            reply = run_turn(conv_id, customer_id, msg, is_admin=(mode == "admin"))
        except Exception as exc:  # noqa: BLE001 — a CLI shouldn't die on one bad turn
            print(f"[error] {type(exc).__name__}: {exc}\n")
            continue
        print(f"bot> {reply}\n")


if __name__ == "__main__":
    main()
