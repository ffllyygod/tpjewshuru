"use client";

import { useState } from "react";
import LoginScreen from "@/components/LoginScreen";
import ChatWindow from "@/components/ChatWindow";
import ModeChooser from "@/components/ModeChooser";
import { startConversation, type Mode, type StartConversationResponse } from "@/lib/api";

type Session = {
  conversationId: string;
  customerName: string | null;
  mode: Mode;
};

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [staffChoice, setStaffChoice] = useState<StartConversationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function adopt(res: StartConversationResponse) {
    setSession({
      conversationId: res.conversation_id,
      customerName: res.customer_name,
      mode: res.mode,
    });
  }

  // Called once we already have a verified SuperTokens session (token is in
  // sessionStorage — see src/lib/api.ts) or the customer chose to browse
  // anonymously. Either way, /conversations resolves identity itself from
  // whatever session header is attached — never from anything the client
  // asserts directly.
  async function startWithCurrentSession() {
    setLoading(true);
    setError(null);
    try {
      const res = await startConversation();
      // Role is only knowable from a real response, so a staff member always
      // opens a customer conversation first and is then offered the choice. The
      // unused row if they pick the staff console is deliberate: the alternative
      // is a "who am I" endpoint that exists purely to shape the UI, and the
      // server would still have to re-verify on every turn regardless.
      if (res.role === "admin") {
        setStaffChoice(res);
      } else {
        adopt(res);
      }
    } catch {
      // Customer-facing copy: a shopper has no idea what port 8000 is, and
      // showing them our infrastructure is neither useful nor reassuring.
      setError("We couldn't open the concierge just now. Please try again.");
    } finally {
      setLoading(false);
    }
  }

  async function chooseMode(mode: Mode) {
    if (!staffChoice) return;
    if (mode === "customer") {
      adopt(staffChoice);
      setStaffChoice(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      adopt(await startConversation("admin"));
      setStaffChoice(null);
    } catch {
      // A 403 here would mean the role changed between the two calls — rare, but
      // it is the server's answer and the UI must not paper over it.
      setError("Couldn't open the staff console — your account may no longer have staff access.");
    } finally {
      setLoading(false);
    }
  }

  if (loading) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 px-4">
        <span
          aria-hidden
          className="font-display grid h-12 w-12 place-items-center rounded-full border border-[var(--color-gold)]/40 bg-[var(--color-gold-wash)] text-[var(--color-gold)]"
        >
          DP
        </span>
        <p className="text-sm text-[var(--color-fg-muted)]" role="status">
          Opening the counter…
        </p>
      </div>
    );
  }

  // A dead end with no way out was the old behaviour here: the message appeared
  // and the only recovery was reloading the page.
  if (error) {
    return (
      <div className="flex flex-1 items-center justify-center px-4">
        <div className="w-full max-w-sm rounded-[18px] border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-6 text-center shadow-[var(--shadow-lift)]">
          <p role="alert" className="text-sm text-[var(--color-danger)]">
            {error}
          </p>
          <button
            onClick={() => {
              setError(null);
              void startWithCurrentSession();
            }}
            className="mt-4 w-full rounded-xl bg-[var(--color-gold)] py-2.5 text-sm font-medium text-[var(--color-user-bubble-fg)] transition hover:bg-[var(--color-gold-soft)]"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (staffChoice) {
    return <ModeChooser name={staffChoice.customer_name} onChoose={chooseMode} />;
  }

  if (!session) {
    return (
      <LoginScreen onLoggedIn={startWithCurrentSession} onBrowseAnonymously={startWithCurrentSession} />
    );
  }

  return (
    <ChatWindow
      conversationId={session.conversationId}
      customerName={session.customerName}
      mode={session.mode}
    />
  );
}
