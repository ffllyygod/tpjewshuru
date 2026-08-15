const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

export type StartConversationResponse = {
  conversation_id: string;
  customer_id: string | null;
  customer_name: string | null;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, text || res.statusText);
  }
  return res.json() as Promise<T>;
}

export function startConversation(email: string | null): Promise<StartConversationResponse> {
  return fetch(`${API_BASE}/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  }).then((r) => handle<StartConversationResponse>(r));
}

export function sendChatMessage(conversationId: string, message: string): Promise<{ reply: string }> {
  return fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ conversation_id: conversationId, message }),
  }).then((r) => handle<{ reply: string }>(r));
}
