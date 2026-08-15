"""Tests the orchestrator's tool-loop mechanics with a fake model client —
no real NIM/network call. This validates the part that's easy to get subtly
wrong (identity injection, tool routing, stopping condition, audit logging)
independent of what any particular model decides to say.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from psycopg.rows import dict_row

from src.agent import orchestrator
from src.db.connection import get_conn


def _fake_call(text_content, name, arguments):
    """Fake a single chat.completions.create() response, matching the OpenAI/NIM
    protocol shape: response.choices[0].message, tool_calls carrying an id (used
    to link the follow-up tool-result message), arguments as a JSON string."""
    tool_calls = None
    if name:
        tool_calls = [
            SimpleNamespace(
                id=f"call_{name}",
                function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
            )
        ]
    message = SimpleNamespace(content=text_content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _new_conversation(customer_id: str | None) -> str:
    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (id, customer_id) VALUES (%s, %s)", (conv_id, customer_id))
        conn.commit()
    return conv_id


def _demo_customer_id() -> str:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'arun@shurutech.com'")
        return str(cur.fetchone()["id"])


def test_tool_call_then_final_answer():
    """Model calls list_customer_orders once, then answers in plain text."""
    customer_id = _demo_customer_id()
    conv_id = _new_conversation(customer_id)

    responses = [
        _fake_call("", "list_customer_orders", {}),
        _fake_call("You have 2 orders: TPJ-DEMO01 and TPJ-DEMO02.", None, None),
    ]

    with patch.object(orchestrator, "_call_model", side_effect=responses):
        reply = orchestrator.run_turn(conv_id, customer_id, "what are my orders?")

    assert "TPJ-DEMO01" in reply

    # Audit log should have exactly one entry for this conversation.
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT tool_name FROM tool_call_log WHERE conversation_id = %s", (conv_id,))
        logged = [r["tool_name"] for r in cur.fetchall()]
    assert logged == ["list_customer_orders"]


def test_customer_id_is_injected_not_agent_controlled():
    """Even if the (fake, misbehaving) model tries to pass its own customer_id, the
    orchestrator's injected value wins — the tool never sees an attacker-supplied one."""
    customer_id = _demo_customer_id()
    conv_id = _new_conversation(customer_id)

    responses = [
        _fake_call("", "get_order_status", {"order_number": "TPJ-DEMO01", "customer_id": "not-a-real-id"}),
        _fake_call("Your order is placed.", None, None),
    ]

    with patch.object(orchestrator, "_call_model", side_effect=responses):
        orchestrator.run_turn(conv_id, customer_id, "status of TPJ-DEMO01?")

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT result FROM tool_call_log WHERE conversation_id = %s", (conv_id,))
        result = cur.fetchone()["result"]
    # Real customer_id was injected server-side -> lookup succeeded, no not_found error.
    assert result.get("error") != "not_found"


def test_no_tool_call_returns_immediately():
    customer_id = _demo_customer_id()
    conv_id = _new_conversation(customer_id)

    with patch.object(orchestrator, "_call_model", return_value=_fake_call("Hi! How can I help?", None, None)):
        reply = orchestrator.run_turn(conv_id, customer_id, "hello")

    assert reply == "Hi! How can I help?"

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS c FROM tool_call_log WHERE conversation_id = %s", (conv_id,))
        assert cur.fetchone()["c"] == 0


def test_unauthenticated_session_cannot_use_order_tools():
    conv_id = _new_conversation(None)  # anonymous session

    responses = [
        _fake_call("", "list_customer_orders", {}),
        _fake_call("I couldn't find your orders — please log in.", None, None),
    ]

    with patch.object(orchestrator, "_call_model", side_effect=responses):
        orchestrator.run_turn(conv_id, None, "what are my orders?")

    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT result FROM tool_call_log WHERE conversation_id = %s", (conv_id,))
        result = cur.fetchone()["result"]
    assert result["error"] == "not_authenticated"
