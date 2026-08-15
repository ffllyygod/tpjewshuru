"""The agent's tool-use loop.

Backend: any OpenAI-compatible chat-completions provider, configured via
LLM_BASE_URL/LLM_API_KEY/LLM_MODEL. Currently OpenRouter hosting
gpt-4o-mini — see JOURNAL.md for why (tried NVIDIA NIM first; its endpoint
stalled for minutes on this network for reasons unrelated to NIM itself,
then DeepSeek, which confabulated a payment success without calling the
tool). LLM_API_KEY(S) accepts a comma-separated list and _call_model()
fails over between them on auth/credit/rate-limit/outage errors. Given a
conversation_id (already persisted), a customer_id (from the authenticated
session — never from the LLM), and the latest user message, runs the
tool-use loop to completion and returns the final assistant text. Every
tool call and its result is written to tool_call_log for audit.

This is the third backend this orchestrator has run against (Anthropic ->
Ollama -> NIM -> OpenRouter/DeepSeek) without the loop itself changing —
swapping only ever means changing _call_model() and the response-parsing
branch in run_turn(). The guardrail logic (identity injection, audit
logging) lives here once and doesn't care which model is answering.
"""

from __future__ import annotations

import json
import logging
import os

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    RateLimitError,
)
from psycopg.rows import dict_row

from src.agent.system_prompt import SYSTEM_PROMPT
from src.agent.tool_schemas import TOOLS
from src.db.connection import get_conn
from src.tools import coupon_tools, gold_sip_tools, knowledge_tools, market_tools, order_tools, product_tools, purchase_tools

LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-4o-mini")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
MAX_TOOL_ITERATIONS = 8

logger = logging.getLogger(__name__)


def _load_api_keys() -> list[str]:
    """Keys to rotate through, in preference order.

    Accepts a comma-separated list in either LLM_API_KEYS or LLM_API_KEY, so
    a single-key setup (the old shape) keeps working untouched. Duplicates
    are dropped — order-preserving, because a repeated key would otherwise
    make failover retry an already-failed credential.
    """
    raw = os.environ.get("LLM_API_KEYS") or os.environ.get("LLM_API_KEY") or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return list(dict.fromkeys(keys))


# The OpenAI SDK raises at construction time on an empty api_key —
# placeholder here so import/tests work before a real key is set; an actual
# model call will still (correctly) fail auth until a key is filled in.
_API_KEYS = _load_api_keys() or ["not-set"]
_CLIENTS = [OpenAI(base_url=LLM_BASE_URL, api_key=key) for key in _API_KEYS]

# Which key to try first. Sticky: once a key fails over, later turns start
# from the one that worked instead of re-testing the dead key every call.
# Plain int, no lock — FastAPI runs these sync handlers in a threadpool, but
# an int rebind is atomic under the GIL and the worst case of a torn read is
# one redundant retry, which the failover loop already handles.
_active_key = 0

# Errors where trying a different key is the right move: exhausted credits
# (402), revoked/invalid key (401), rate limit (429), and provider-side
# outages. Deliberately NOT 400-class schema errors — those are our bug, and
# rotating through every key would just bury the real message.
_ROTATE_ON_STATUS = frozenset({401, 402, 403, 408, 409, 429})

# Anthropic-style schema -> OpenAI-style function schema (also what NIM expects).
_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in TOOLS
]

# Maps tool name -> callable
_TOOL_IMPL = {
    "list_customer_orders": order_tools.list_customer_orders,
    "get_order_status": order_tools.get_order_status,
    "check_cancellation_eligibility": order_tools.check_cancellation_eligibility,
    "cancel_order": order_tools.cancel_order,
    "search_knowledge": knowledge_tools.search_knowledge,
    "search_products": product_tools.search_products,
    "get_product_details": product_tools.get_product_details,
    "get_metal_rates": market_tools.get_metal_rates,
    "offer_settlement_options": coupon_tools.offer_settlement_options,
    "issue_coupon": coupon_tools.issue_coupon,
    "request_cash_refund": coupon_tools.request_cash_refund,
    "get_my_coupons": coupon_tools.get_my_coupons,
    "redeem_coupon": coupon_tools.redeem_coupon,
    "place_order": purchase_tools.place_order,
    "list_gold_sip_plans": gold_sip_tools.list_gold_sip_plans,
    "start_gold_sip": gold_sip_tools.start_gold_sip,
    "pay_sip_installment": gold_sip_tools.pay_sip_installment,
    "get_my_gold_sips": gold_sip_tools.get_my_gold_sips,
    "cancel_gold_sip": gold_sip_tools.cancel_gold_sip,
    "redeem_gold_sip": gold_sip_tools.redeem_gold_sip,
}
_NEEDS_CUSTOMER_ID = {
    "list_customer_orders", "get_order_status", "check_cancellation_eligibility", "cancel_order",
    "offer_settlement_options", "issue_coupon", "request_cash_refund", "get_my_coupons",
    "redeem_coupon", "place_order",
    "start_gold_sip", "pay_sip_installment", "get_my_gold_sips", "cancel_gold_sip", "redeem_gold_sip",
}
_NEEDS_CONVERSATION_ID = {"check_cancellation_eligibility", "cancel_order", "issue_coupon"}


def _log_tool_call(conversation_id: str, tool_name: str, arguments: dict, result: dict) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tool_call_log (conversation_id, tool_name, arguments, result)
            VALUES (%s, %s, %s, %s)
            """,
            (conversation_id, tool_name, json.dumps(arguments), json.dumps(result, default=str)),
        )
        conn.commit()


def _execute_tool(conversation_id: str, customer_id: str | None, tool_name: str, tool_input: dict) -> dict:
    fn = _TOOL_IMPL.get(tool_name)
    if fn is None:
        result = {"error": "unknown_tool", "message": f"No such tool: {tool_name}"}
        _log_tool_call(conversation_id, tool_name, tool_input, result)
        return result

    kwargs = dict(tool_input)
    if tool_name in _NEEDS_CUSTOMER_ID:
        if not customer_id:
            result = {"error": "not_authenticated", "message": "No authenticated customer for this session."}
            _log_tool_call(conversation_id, tool_name, tool_input, result)
            return result
        kwargs["customer_id"] = customer_id  # injected server-side, agent cannot override this
    if tool_name in _NEEDS_CONVERSATION_ID:
        kwargs["conversation_id"] = conversation_id  # scopes the confirmation token to this conversation

    try:
        result = fn(**kwargs)
    except TypeError as e:
        result = {"error": "bad_arguments", "message": str(e)}
    except Exception as e:  # noqa: BLE001 — surface as a tool error, not a crash
        result = {"error": "tool_failed", "message": str(e)}

    _log_tool_call(conversation_id, tool_name, tool_input, result)
    return result


def _load_history(conversation_id: str) -> list[dict]:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT role, content FROM messages
            WHERE conversation_id = %s
            ORDER BY created_at ASC
            """,
            (conversation_id,),
        )
        rows = cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows if r["role"] in ("user", "assistant")]


def _save_message(conversation_id: str, role: str, content: str) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
            (conversation_id, role, content),
        )
        conn.commit()


def _should_rotate(exc: Exception) -> bool:
    """True if a *different* API key might succeed where this one failed."""
    if isinstance(exc, (AuthenticationError, RateLimitError)):
        return True
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        # Could be our network rather than this key, but the next key is a
        # different connection attempt and costs one round-trip to find out.
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in _ROTATE_ON_STATUS or exc.status_code >= 500
    return False


def _call_model(messages: list[dict]):
    """One model call, failing over across API keys.

    Tries the currently-active key first, then every other key in order.
    Only the last error is raised if they all fail — by then the useful
    signal is "every key is down", which the log lines below spell out.
    """
    global _active_key

    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
        "tools": _TOOLS_OPENAI,
        "tool_choice": "auto",
    }

    last_exc: Exception | None = None
    for offset in range(len(_CLIENTS)):
        index = (_active_key + offset) % len(_CLIENTS)
        try:
            response = _CLIENTS[index].chat.completions.create(**payload)
        except Exception as exc:  # noqa: BLE001 — re-raised below if unrotatable
            if not _should_rotate(exc):
                raise
            last_exc = exc
            # Never log the key itself — position only.
            logger.warning(
                "LLM key #%d/%d failed (%s: %s); trying next key",
                index + 1, len(_CLIENTS), type(exc).__name__, exc,
            )
            continue

        if index != _active_key:
            logger.info("LLM failover: now using key #%d/%d", index + 1, len(_CLIENTS))
            _active_key = index
        return response

    logger.error("All %d LLM API key(s) failed; giving up on this turn", len(_CLIENTS))
    raise last_exc  # type: ignore[misc]  # unreachable with an empty key list — _CLIENTS is never empty


def run_turn(conversation_id: str, customer_id: str | None, user_message: str) -> str:
    """Run one full user turn (including any tool round-trips) and return the reply text."""
    _save_message(conversation_id, "user", user_message)

    messages = _load_history(conversation_id)

    for _ in range(MAX_TOOL_ITERATIONS):
        response = _call_model(messages)
        message = response.choices[0].message

        if not message.tool_calls:
            final_text = message.content or ""
            _save_message(conversation_id, "assistant", final_text)
            return final_text

        # Record the assistant's tool-call turn, then run each tool and feed
        # results back linked by tool_call_id (OpenAI-protocol requirement —
        # unlike Ollama's client, NIM requires this id round-trip or the tool
        # result won't be associated with the right call).
        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments},
                    }
                    for call in message.tool_calls
                ],
            }
        )
        for call in message.tool_calls:
            tool_name = call.function.name
            # OpenAI-protocol arguments arrive as a JSON string, not a dict
            # (Ollama's client parsed this for us — NIM doesn't).
            try:
                tool_input = json.loads(call.function.arguments) if call.function.arguments else {}
            except json.JSONDecodeError:
                tool_input = {}
            result = _execute_tool(conversation_id, customer_id, tool_name, tool_input)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str),
                }
            )

    fallback = "I'm having trouble completing that request right now — could you try rephrasing, or ask something more specific?"
    _save_message(conversation_id, "assistant", fallback)
    return fallback
