"""The scheduled half of proactive outreach: find reasons to reach out, draft a
message for each, and leave them for a human to approve.

    python scripts/generate_outreach.py --dry-run    # what WOULD be drafted
    python scripts/generate_outreach.py              # draft and store
    python scripts/generate_outreach.py --signal coupon_expiring --limit 5

Intended to run nightly (cron, Railway scheduled job). Nothing it produces is
sent — drafts land in `outreach_drafts` with status DRAFT and appear in the
staff console via `admin_outreach_queue`.

The division of labour is the point:

    SQL decides WHO and WHY.        (src/tools/outreach_tools.py detectors)
    The model decides only WORDING. (given nothing but the verified facts)
    A grounding check then verifies the wording didn't add anything.
    A human approves before anyone hears from us.

--dry-run costs nothing and calls no model, so it's the safe way to see what a
change to the detectors would pick up before pointing it at real customers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent.orchestrator import _CLIENTS, LLM_MODEL, _active_key  # noqa: E402
from src.tools import outreach_tools  # noqa: E402


def _draft_message(facts: dict) -> str:
    """One model call, no tools. Deliberately not run_turn(): this isn't a
    conversation, and giving a copywriting task access to the customer toolset
    would be handing it capabilities it has no reason to hold."""
    response = _CLIENTS[_active_key].chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": outreach_tools.DRAFT_SYSTEM_PROMPT},
            {"role": "user", "content": outreach_tools.build_draft_prompt(facts)},
        ],
        max_tokens=300,
    )
    return (response.choices[0].message.content or "").strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="detect only; call no model, write nothing")
    parser.add_argument("--signal", action="append", choices=list(outreach_tools.SIGNAL_TYPES),
                        help="restrict to one signal type (repeatable)")
    parser.add_argument("--limit", type=int, default=outreach_tools.MAX_DRAFTS_PER_SIGNAL,
                        help="max candidates per signal type")
    args = parser.parse_args()

    signals = tuple(args.signal) if args.signal else outreach_tools.SIGNAL_TYPES
    candidates = outreach_tools.detect_signals(signals, limit=args.limit)

    if not candidates:
        print("No outreach signals found. Nobody is due a message right now.")
        return

    by_type: dict[str, int] = {}
    for c in candidates:
        by_type[c["signal_type"]] = by_type.get(c["signal_type"], 0) + 1
    print(f"{len(candidates)} candidate(s): " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())) + "\n")

    if args.dry_run:
        for c in candidates:
            print(f"  [{c['signal_type']}] {c['facts'].get('customer_name')} — {c['signal_key']}")
            for k, v in c["facts"].items():
                if k not in ("customer_name", "occasion"):
                    print(f"        {k}: {v}")
        print("\nDry run — no model called, nothing stored.")
        return

    stored = skipped = rejected = 0
    for c in candidates:
        try:
            message = _draft_message(c["facts"])
        except Exception as exc:  # noqa: BLE001 — one bad generation shouldn't kill the run
            print(f"  [error] {c['signal_key']}: {type(exc).__name__}: {exc}")
            continue

        result = outreach_tools.store_draft(
            c["customer_id"], c["signal_type"], c["signal_key"], c["facts"], message,
        )
        if result["stored"]:
            stored += 1
            print(f"  [draft] {c['facts'].get('customer_name')} ({c['signal_type']})")
            print(f"          {message.splitlines()[0][:110]}")
        elif result["reason"] == "ungrounded":
            # The interesting failure. The model asserted something it wasn't
            # given — exactly what this check exists to catch, and worth being
            # loud about rather than silently dropping.
            rejected += 1
            print(f"  [REJECTED] {c['signal_key']}: {result['message']}")
            print(f"             {message[:160]}")
        else:
            skipped += 1

    print(f"\n{stored} drafted, {rejected} rejected as ungrounded, {skipped} already existed.")
    if stored:
        print("Review them in the staff console: \"show me the outreach queue\".")


if __name__ == "__main__":
    main()
