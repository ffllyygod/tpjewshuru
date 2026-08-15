"""Quick terminal chat client against the running API — no frontend needed.

Usage:
    python scripts/chat_cli.py arun@shurutech.com
    python scripts/chat_cli.py            # anonymous session (knowledge/product Q&A only)

Requires the API server running (uvicorn src.api.main:app --port 8000).
"""

import sys

import requests

API = "http://127.0.0.1:8000"


def main():
    email = sys.argv[1] if len(sys.argv) > 1 else None

    resp = requests.post(f"{API}/conversations", json={"email": email})
    resp.raise_for_status()
    session = resp.json()
    conv_id = session["conversation_id"]

    if session["customer_id"]:
        print(f"-- chatting as {session['customer_name']} ({email}) --")
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

        r = requests.post(f"{API}/chat", json={"conversation_id": conv_id, "message": msg})
        if r.status_code != 200:
            print(f"[error {r.status_code}] {r.text}")
            continue
        print(f"bot> {r.json()['reply']}\n")


if __name__ == "__main__":
    main()
