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
      setError("Couldn't reach the API. Is it running on localhost:8000?");
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
      <div className="flex flex-1 items-center justify-center px-4">
        <p className="text-sm text-[var(--color-fg-muted)]">Connecting…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-1 items-center justify-center px-4">
        <p className="text-sm text-red-400">{error}</p>
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
