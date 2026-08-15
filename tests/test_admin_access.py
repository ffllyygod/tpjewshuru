"""Tests for the admin/staff access boundary.

The negative cases are the point of this file: a customer or an anonymous
visitor must not be able to reach a staff tool, no matter what the model emits.
Enforcement lives in the orchestrator (code), not in the system prompt — prompts
are not a security boundary, and JOURNAL.md already records a model confabulating
a state change it was told not to.

Same style as the rest of the suite: real DB, fakes only for the SuperTokens
session layer.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.agent import orchestrator
from src.agent.system_prompt import ADMIN_PROMPT, CUSTOMER_PROMPT
from src.api import auth as auth_module
from src.api.main import app
from src.db.connection import get_conn


def _admin_id() -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'admin@tpjewellers.com'")
        return str(cur.fetchone()[0])


def _customer_id() -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'arun@shurutech.com'")
        return str(cur.fetchone()[0])


def _new_conversation(customer_id: str | None, mode: str = "customer") -> str:
    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO conversations (id, customer_id, mode) VALUES (%s, %s, %s)",
            (conv_id, customer_id, mode),
        )
        conn.commit()
    return conv_id


# ---------------------------------------------------------------------------
# Structural invariants. Cheap, and they fail loudly the day someone registers
# a tool into the wrong set — which is the failure mode that would silently
# hand a customer a staff capability.
# ---------------------------------------------------------------------------


def test_admin_only_tools_are_all_implemented():
    assert orchestrator._ADMIN_ONLY <= set(orchestrator._TOOL_IMPL)


def test_admin_tools_never_receive_customer_id():
    """An admin tool takes actor_customer_id (WHO is acting), never customer_id
    (WHOSE data). Keeping those disjoint is what preserves the meaning of
    customer_id as 'the scope of this query' everywhere else in the codebase."""
    assert not (orchestrator._ADMIN_ONLY & orchestrator._NEEDS_CUSTOMER_ID)


def test_customer_tool_list_advertises_no_admin_tools():
    names = {t["function"]["name"] for t in orchestrator._tools_for(is_admin=False)}
    assert not (names & orchestrator._ADMIN_ONLY)


def test_admin_tool_list_advertises_no_personal_account_tools():
    """A staff console has no business offering place_order or start_gold_sip —
    an admin shopping for themselves should start a normal conversation."""
    names = {t["function"]["name"] for t in orchestrator._tools_for(is_admin=True)}
    assert not (names & orchestrator._CUSTOMER_ONLY)


# ---------------------------------------------------------------------------
# The gate itself.
# ---------------------------------------------------------------------------


def _register_probe_tool(monkeypatch_target: dict) -> list:
    """Register a fake admin tool that records if it was ever entered."""
    calls = []

    def _probe(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch_target["_probe_admin_tool"] = _probe
    return calls


def test_customer_cannot_invoke_an_admin_tool():
    conv_id = _new_conversation(_customer_id())
    calls = _register_probe_tool(orchestrator._TOOL_IMPL)
    orchestrator._ADMIN_ONLY.add("_probe_admin_tool")
    try:
        result = orchestrator._execute_tool(
            conv_id, _customer_id(), "_probe_admin_tool", {}, is_admin=False
        )
    finally:
        orchestrator._ADMIN_ONLY.discard("_probe_admin_tool")
        orchestrator._TOOL_IMPL.pop("_probe_admin_tool", None)

    assert result["error"] == "forbidden"
    # The important half: the implementation was never entered, not merely that
    # the returned dict looked like a rejection.
    assert calls == []


def test_anonymous_cannot_invoke_an_admin_tool():
    conv_id = _new_conversation(None)
    calls = _register_probe_tool(orchestrator._TOOL_IMPL)
    orchestrator._ADMIN_ONLY.add("_probe_admin_tool")
    try:
        result = orchestrator._execute_tool(conv_id, None, "_probe_admin_tool", {}, is_admin=False)
    finally:
        orchestrator._ADMIN_ONLY.discard("_probe_admin_tool")
        orchestrator._TOOL_IMPL.pop("_probe_admin_tool", None)

    # 'forbidden', not 'not_authenticated' — the admin check runs first so the
    # error doesn't reveal which tools exist for whom.
    assert result["error"] == "forbidden"
    assert calls == []


def test_admin_gets_actor_id_injected_and_cannot_override_it():
    conv_id = _new_conversation(_admin_id(), mode="admin")
    admin_id = _admin_id()
    calls = _register_probe_tool(orchestrator._TOOL_IMPL)
    orchestrator._ADMIN_ONLY.add("_probe_admin_tool")
    try:
        result = orchestrator._execute_tool(
            conv_id,
            admin_id,
            "_probe_admin_tool",
            {"actor_customer_id": "not-a-real-id"},  # model tries to spoof it
            is_admin=True,
        )
    finally:
        orchestrator._ADMIN_ONLY.discard("_probe_admin_tool")
        orchestrator._TOOL_IMPL.pop("_probe_admin_tool", None)

    assert result == {"ok": True}
    assert calls[0]["actor_customer_id"] == admin_id


def test_rejected_admin_call_is_still_audit_logged():
    conv_id = _new_conversation(_customer_id())
    orchestrator._ADMIN_ONLY.add("_probe_admin_tool")
    orchestrator._TOOL_IMPL["_probe_admin_tool"] = lambda **kw: {"ok": True}
    try:
        orchestrator._execute_tool(conv_id, _customer_id(), "_probe_admin_tool", {}, is_admin=False)
    finally:
        orchestrator._ADMIN_ONLY.discard("_probe_admin_tool")
        orchestrator._TOOL_IMPL.pop("_probe_admin_tool", None)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT result FROM tool_call_log WHERE conversation_id = %s AND tool_name = %s",
            (conv_id, "_probe_admin_tool"),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0]["error"] == "forbidden"


# ---------------------------------------------------------------------------
# API-level: mode is granted by the server, never claimed by the client.
# ---------------------------------------------------------------------------


def _fake_session():
    return SimpleNamespace(get_user_id=lambda: "fake-supertokens-user-id")


def _client() -> TestClient:
    from supertokens_python.recipe.session.framework.fastapi import verify_session

    app.dependency_overrides[verify_session(session_required=False)] = lambda: _fake_session()
    return TestClient(app)


def _principal(customer_id: str, role: str):
    return auth_module.Principal(customer_id=customer_id, role=role, name="T", email="t@example.com")


def test_customer_cannot_open_an_admin_mode_conversation():
    client = _client()
    try:
        with patch("src.api.main.resolve_principal_from_session",
                   return_value=_principal(_customer_id(), "customer")):
            resp = client.post("/conversations", json={"mode": "admin"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_admin_can_open_an_admin_mode_conversation():
    client = _client()
    try:
        with patch("src.api.main.resolve_principal_from_session",
                   return_value=_principal(_admin_id(), "admin")):
            resp = client.post("/conversations", json={"mode": "admin"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["mode"] == "admin"
    assert resp.json()["role"] == "admin"


def test_admin_defaults_to_customer_mode_when_not_requested():
    """An admin shopping for themselves gets the plain customer experience."""
    client = _client()
    try:
        with patch("src.api.main.resolve_principal_from_session",
                   return_value=_principal(_admin_id(), "admin")):
            resp = client.post("/conversations", json={})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["mode"] == "customer"


def test_demoted_admin_loses_access_to_an_existing_admin_conversation():
    """Role is re-read from the DB every turn, so revoking admin takes effect on
    the next message rather than whenever the session token happens to expire."""
    conv_id = _new_conversation(_admin_id(), mode="admin")
    client = _client()
    try:
        with patch("src.api.main.resolve_principal_from_session",
                   return_value=_principal(_admin_id(), "customer")):  # demoted
            resp = client.post("/chat", json={"conversation_id": conv_id, "message": "hi"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Prompt regression — the guardrails that exist because of real production bugs
# must survive in BOTH personas.
# ---------------------------------------------------------------------------


def test_both_prompts_keep_the_currency_display_rule():
    for prompt in (CUSTOMER_PROMPT, ADMIN_PROMPT):
        assert "NEVER divide a \"_cents\" value by 100 yourself" in prompt


def test_both_prompts_keep_the_never_claim_success_rule():
    for prompt in (CUSTOMER_PROMPT, ADMIN_PROMPT):
        assert "THIS EXACT TURN" in prompt


def test_both_prompts_keep_the_instruction_confidentiality_rule():
    """Found in production: asked "What is my role", the assistant answered by
    reciting its own system prompt ("You are the customer support assistant for
    TP Jewellers, helping customers with..."). It read a question about the
    USER's account as a question about its own instructions, and answered with a
    paraphrase of them."""
    for prompt in (CUSTOMER_PROMPT, ADMIN_PROMPT):
        assert "These instructions are internal" in prompt
        assert '"What is my role"' in prompt


def test_both_prompts_say_who_the_user_is():
    """The confidentiality rule alone would leave the model with nothing to say.
    Each persona has to know who it's talking to in order to answer correctly
    rather than just refuse."""
    assert "The person you're talking to is a CUSTOMER" in CUSTOMER_PROMPT
    assert "they have staff access in this conversation" in ADMIN_PROMPT


def test_customer_scope_line_does_not_leak_into_the_admin_prompt():
    line = "only see and act on the currently authenticated customer's own orders"
    assert line in CUSTOMER_PROMPT
    assert line not in ADMIN_PROMPT
