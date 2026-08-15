"""SuperTokens (self-hosted, open-source) auth setup — Passwordless/OTP-over-
email recipe. See JOURNAL.md and docs/ARCHITECTURE.md for the full rationale.

Design:
  - Login only, for EXISTING TP Jewellers customers (email already in our
    `customers` table). This chatbot doesn't handle new-customer signup.
  - `create_code_post` is overridden to check our `customers` table BEFORE
    letting SuperTokens create/send a real code. If the email isn't a known
    customer, we still return a normal-looking CreateCodePostOkResult (with
    placeholder IDs that will simply fail to consume later) rather than an
    error — so the API never reveals whether a given email is a real
    customer (avoids account enumeration), and we don't waste a real SMTP
    send on an unknown address.
  - We never store a SuperTokens user ID in our own schema. On every
    authenticated request, `resolve_customer_id_from_session` looks up
    `customers WHERE email = <verified email>` fresh — keeps our schema
    fully decoupled from SuperTokens' internal user model.
  - Session transport is header-based, not cookies — the frontend
    (vercel.app) and backend (railway.app) are different domains, and
    third-party-cookie browser restrictions make cross-domain cookie
    sessions unreliable. Header mode sidesteps that entirely.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, Optional, Union

import httpx
from psycopg.rows import dict_row
from supertokens_python import InputAppInfo, SupertokensConfig
from supertokens_python import init as supertokens_init
from supertokens_python.recipe import passwordless, session
from supertokens_python.recipe.passwordless import ContactEmailOnlyConfig
from supertokens_python.recipe.passwordless.emaildelivery.services import SMTPService
from supertokens_python.recipe.passwordless.emaildelivery.services.smtp.pless_login import (
    pless_email_content,
)
from supertokens_python.recipe.passwordless.interfaces import (
    APIInterface as PasswordlessAPIInterface,
    APIOptions,
    CreateCodePostOkResult,
    PasswordlessLoginEmailTemplateVars,
)
from supertokens_python.recipe.passwordless.utils import PasswordlessOverrideConfig
from supertokens_python.recipe.session.interfaces import SessionContainer
from supertokens_python.ingredients.emaildelivery.types import (
    EmailDeliveryConfig,
    EmailDeliveryInterface,
    SMTPSettings,
    SMTPSettingsFrom,
)

from src.db.connection import get_conn

API_DOMAIN = os.environ.get("API_DOMAIN", "http://localhost:8000")
WEBSITE_DOMAIN = os.environ.get("WEBSITE_DOMAIN", "http://localhost:3000")
SUPERTOKENS_CONNECTION_URI = os.environ.get("SUPERTOKENS_CONNECTION_URI", "http://localhost:3567")


class ResendEmailService(EmailDeliveryInterface[PasswordlessLoginEmailTemplateVars]):
    """Production email transport — Resend's HTTP API (port 443), used
    instead of SMTPService because Railway blocks outbound SMTP (25/465/587)
    at the network level — confirmed live via SMTPConnectTimeoutError, not
    guessed (see JOURNAL.md). Reuses SuperTokens' own default OTP email
    template (`pless_email_content`) so the email is byte-identical to the
    local SMTP path; only the transport differs."""

    def __init__(self, api_key: str, from_address: str) -> None:
        self._api_key = api_key
        self._from_address = from_address

    async def send_email(self, template_vars: PasswordlessLoginEmailTemplateVars, user_context: Dict[str, Any]) -> None:
        content = pless_email_content(template_vars)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "from": self._from_address,
                    "to": [content.to_email],
                    "subject": content.subject,
                    "html": content.body,
                },
            )
            resp.raise_for_status()


def _customer_exists(email: str) -> bool:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM customers WHERE email = %s", (email,))
        return cur.fetchone() is not None


def _override_passwordless_apis(original_implementation: PasswordlessAPIInterface) -> PasswordlessAPIInterface:
    original_create_code_post = original_implementation.create_code_post

    async def create_code_post(
        email: Optional[str],
        phone_number: Optional[str],
        session: Optional[SessionContainer],  # SDK calls this by keyword — name must match exactly
        should_try_linking_with_session_user: Optional[bool],
        tenant_id: str,
        api_options: APIOptions,
        user_context: Dict[str, Any],
    ):
        if email and not _customer_exists(email):
            # Don't create a real code / send a real email for an unknown
            # address — but return the same success shape so the API
            # doesn't leak whether the email belongs to a customer.
            # These placeholder IDs won't match anything at consume time,
            # so a follow-up verify attempt just fails as "invalid code".
            return CreateCodePostOkResult(
                device_id=str(uuid.uuid4()),
                pre_auth_session_id=str(uuid.uuid4()),
                flow_type="USER_INPUT_CODE",
            )
        return await original_create_code_post(
            email, phone_number, session, should_try_linking_with_session_user,
            tenant_id, api_options, user_context,
        )

    original_implementation.create_code_post = create_code_post
    return original_implementation


def _build_email_service() -> Union[SMTPService, "ResendEmailService", None]:
    # Resend (HTTP API, port 443) takes priority when configured — this is
    # the production path, since Railway blocks outbound SMTP entirely.
    # Gmail SMTP is the local-dev fallback, where no such block exists.
    resend_api_key = os.environ.get("RESEND_API_KEY", "")
    resend_from = os.environ.get("RESEND_FROM", "")
    if resend_api_key and resend_from:
        return ResendEmailService(api_key=resend_api_key, from_address=resend_from)

    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_password = os.environ.get("SMTP_PASSWORD", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)
    if smtp_user and smtp_password:
        return SMTPService(
            smtp_settings=SMTPSettings(
                host=smtp_host,
                port=smtp_port,
                username=smtp_user,
                password=smtp_password,
                secure=False,  # STARTTLS on 587, not implicit TLS
                from_=SMTPSettingsFrom(name="TP Jewellers", email=smtp_from),
            )
        )
    return None


def init_auth() -> None:
    email_service = _build_email_service()

    supertokens_init(
        app_info=InputAppInfo(
            app_name="TP Jewellers",
            api_domain=API_DOMAIN,
            website_domain=WEBSITE_DOMAIN,
            api_base_path="/auth",
            website_base_path="/auth",
        ),
        supertokens_config=SupertokensConfig(connection_uri=SUPERTOKENS_CONNECTION_URI),
        framework="fastapi",
        recipe_list=[
            session.init(
                # Header-based sessions (not cookies) — see module docstring.
                get_token_transfer_method=lambda *_args, **_kwargs: "header",
                cookie_secure=True,
            ),
            passwordless.init(
                contact_config=ContactEmailOnlyConfig(),
                flow_type="USER_INPUT_CODE",
                override=PasswordlessOverrideConfig(apis=_override_passwordless_apis),
                email_delivery=EmailDeliveryConfig(service=email_service) if email_service else None,
            ),
        ],
    )


def resolve_customer_id_from_session(session_container: Optional[SessionContainer]) -> Optional[str]:
    """Bridge a verified SuperTokens session to our own customers.id. Returns
    None if there's no session (anonymous) — never guesses, never trusts
    anything other than the session's own verified user record. Sync (uses
    supertokens_python.syncio, not asyncio) to match this API's existing
    sync endpoint style — src/api/main.py's handlers are plain `def`, not
    `async def`, same as everywhere else DB calls happen in this codebase."""
    if session_container is None:
        return None

    from supertokens_python.syncio import get_user

    user = get_user(session_container.get_user_id())
    if not user or not user.emails:
        return None

    email = user.emails[0]
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id FROM customers WHERE email = %s", (email,))
        row = cur.fetchone()
    return str(row["id"]) if row else None
