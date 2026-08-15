"""Tests for the auth boundary (src/api/auth.py, src/api/main.py's session
handling) — the customer-existence gate that stops SuperTokens sending a real
OTP to a non-customer email (anti-enumeration), the session->customer_id
bridge, and the ownership check that stops one customer's session from
reading another customer's conversation.

Uses fakes for the SuperTokens session/user layer (no real OTP round-trip —
that's covered by live testing, see JOURNAL.md) but hits the real DB, same
pattern as every other test in this suite.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.api import auth as auth_module
from src.api.main import app
from src.db.connection import get_conn


def _demo_customer_email_and_id() -> tuple[str, str]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM customers WHERE email = 'arun@shurutech.com'")
        row = cur.fetchone()
    return "arun@shurutech.com", str(row[0])


def _new_conversation(customer_id: str | None) -> str:
    conv_id = str(uuid.uuid4())
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO conversations (id, customer_id) VALUES (%s, %s)", (conv_id, customer_id))
        conn.commit()
    return conv_id


def _fake_session():
    """A stand-in for supertokens_python's SessionContainer — only
    get_user_id() is ever called on it by our code."""
    return SimpleNamespace(get_user_id=lambda: "fake-supertokens-user-id")


# ---------------------------------------------------------------------------
# _customer_exists — the anti-enumeration gate
# ---------------------------------------------------------------------------


def test_customer_exists_true_for_known_customer():
    email, _ = _demo_customer_email_and_id()
    assert auth_module._customer_exists(email) is True


def test_customer_exists_false_for_unknown_email():
    assert auth_module._customer_exists("definitely-not-a-customer@example.com") is False


# ---------------------------------------------------------------------------
# resolve_customer_id_from_session — the session -> customers.id bridge
# ---------------------------------------------------------------------------


def test_resolve_customer_id_from_session_returns_none_when_no_session():
    assert auth_module.resolve_customer_id_from_session(None) is None


def test_resolve_customer_id_bridges_verified_email_to_customer_id():
    email, expected_customer_id = _demo_customer_email_and_id()
    fake_user = SimpleNamespace(emails=[email])

    with patch("supertokens_python.syncio.get_user", return_value=fake_user):
        resolved = auth_module.resolve_customer_id_from_session(_fake_session())

    assert resolved == expected_customer_id


def test_resolve_customer_id_returns_none_for_verified_but_unknown_email():
    """A real SuperTokens session for an email that isn't (or is no longer) in
    our customers table resolves to anonymous, not an error — same
    fail-closed principle as everywhere else identity is resolved."""
    fake_user = SimpleNamespace(emails=["not-in-our-customers-table@example.com"])

    with patch("supertokens_python.syncio.get_user", return_value=fake_user):
        resolved = auth_module.resolve_customer_id_from_session(_fake_session())

    assert resolved is None


def test_resolve_customer_id_returns_none_when_supertokens_user_missing():
    with patch("supertokens_python.syncio.get_user", return_value=None):
        resolved = auth_module.resolve_customer_id_from_session(_fake_session())

    assert resolved is None


# ---------------------------------------------------------------------------
# /chat ownership check — a session must resolve to the SAME customer as the
# conversation it's trying to use.
# ---------------------------------------------------------------------------


def _client_with_session(customer_id: str | None) -> TestClient:
    """Override the verify_session dependency so we don't need a real OTP
    round-trip to exercise the endpoint-level ownership check."""
    from supertokens_python.recipe.session.framework.fastapi import verify_session

    session = _fake_session() if customer_id is not None else None

    def _override():
        return session

    app.dependency_overrides[verify_session(session_required=False)] = _override
    client = TestClient(app)
    client._resolve_customer_id = customer_id  # stash for the patch target below
    return client


def test_chat_rejects_session_belonging_to_a_different_customer():
    _, owner_customer_id = _demo_customer_email_and_id()
    conv_id = _new_conversation(owner_customer_id)

    other_customer_id = str(uuid.uuid4())  # a different, unrelated customer
    client = _client_with_session(other_customer_id)
    try:
        with patch("src.api.main.resolve_customer_id_from_session", return_value=other_customer_id):
            resp = client.post("/chat", json={"conversation_id": conv_id, "message": "hi"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 403


def test_chat_allows_session_matching_the_conversation_owner():
    email, owner_customer_id = _demo_customer_email_and_id()
    conv_id = _new_conversation(owner_customer_id)

    client = _client_with_session(owner_customer_id)
    try:
        with patch("src.api.main.resolve_customer_id_from_session", return_value=owner_customer_id), \
             patch("src.api.main.run_turn", return_value="ok"):
            resp = client.post("/chat", json={"conversation_id": conv_id, "message": "hi"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200


def test_chat_allows_anonymous_access_to_anonymous_conversation():
    conv_id = _new_conversation(None)

    client = _client_with_session(None)
    try:
        with patch("src.api.main.resolve_customer_id_from_session", return_value=None), \
             patch("src.api.main.run_turn", return_value="ok"):
            resp = client.post("/chat", json={"conversation_id": conv_id, "message": "hi"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200


def test_chat_unknown_conversation_id_is_404():
    client = _client_with_session(None)
    try:
        with patch("src.api.main.resolve_customer_id_from_session", return_value=None):
            resp = client.post("/chat", json={"conversation_id": str(uuid.uuid4()), "message": "hi"})
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 404
