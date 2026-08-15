"use client";

import { useState } from "react";
import LoginScreen from "@/components/LoginScreen";
import ChatWindow from "@/components/ChatWindow";
import { ApiError, startConversation } from "@/lib/api";

type Session = {
  conversationId: string;
  customerName: string | null;
};

export default function Home() {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleStart(email: string | null) {
    setLoading(true);
    setError(null);
    try {
      const res = await startConversation(email);
      setSession({ conversationId: res.conversation_id, customerName: res.customer_name });
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setError("No customer found with that email — try arun@shurutech.com, or leave it blank.");
      } else {
        setError("Couldn't reach the API. Is it running on localhost:8000?");
      }
    } finally {
      setLoading(false);
    }
  }

  if (!session) {
    return <LoginScreen onStart={handleStart} loading={loading} error={error} />;
  }

  return <ChatWindow conversationId={session.conversationId} customerName={session.customerName} />;
}
