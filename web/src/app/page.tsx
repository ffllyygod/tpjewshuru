"use client";

import { useState } from "react";
import LoginScreen from "@/components/LoginScreen";
import ChatWindow from "@/components/ChatWindow";
import { startConversation } from "@/lib/api";

type Session = {
  conversationId: string;
  customerName: string | null;
};

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
      setSession({ conversationId: res.conversation_id, customerName: res.customer_name });
    } catch {
      setError("Couldn't reach the API. Is it running on localhost:8000?");
    } finally {
      setLoading(false);
    }
  }

  if (!session) {
    return (
      <LoginScreen onLoggedIn={startWithCurrentSession} onBrowseAnonymously={startWithCurrentSession} />
    );
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

  return <ChatWindow conversationId={session.conversationId} customerName={session.customerName} />;
}
