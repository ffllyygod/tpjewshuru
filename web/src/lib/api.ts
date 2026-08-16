const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

// SuperTokens session, header-based (not cookies — frontend/backend are on
// different domains). Stored in sessionStorage: cleared when the tab closes,
// which is the right lifetime for a demo concierge chat, not a "remember me"
// product. See JOURNAL.md / src/api/auth.py for the full rationale.
const ACCESS_TOKEN_KEY = "dpj_access_token";

export function getStoredAccessToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.sessionStorage.getItem(ACCESS_TOKEN_KEY);
}

function storeAccessToken(token: string) {
  window.sessionStorage.setItem(ACCESS_TOKEN_KEY, token);
}

export function clearSession() {
  window.sessionStorage.removeItem(ACCESS_TOKEN_KEY);
}

function authHeaders(): Record<string, string> {
  const token = getStoredAccessToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// `role` is what the server resolved this session to be — never what the client
// claimed. `mode` is what the conversation actually opened as, which can only be
// "admin" if role is too. The client shows or hides staff affordances based on
// these, but that is presentation only: the backend re-verifies the role against
// the session on every single turn, so a tampered client gets 403s, not data.
export type Role = "anonymous" | "customer" | "admin";
export type Mode = "customer" | "admin";

export type StartConversationResponse = {
  conversation_id: string;
  customer_id: string | null;
  customer_name: string | null;
  role: Role;
  mode: Mode;
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

// Requesting mode="admin" is a request, not an assertion: the server 403s unless
// the verified session actually resolves to a staff account.
export function startConversation(mode: Mode = "customer"): Promise<StartConversationResponse> {
  return fetch(`${API_BASE}/conversations`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ mode }),
  }).then((r) => handle<StartConversationResponse>(r));
}

export function sendChatMessage(conversationId: string, message: string): Promise<{ reply: string }> {
  return fetch(`${API_BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ conversation_id: conversationId, message }),
  }).then((r) => handle<{ reply: string }>(r));
}

// --- OTP login (SuperTokens Passwordless REST API, called directly rather
// than the supertokens-auth-react widget, to keep our own dark/gold design). ---

export type RequestOtpResult = { deviceId: string; preAuthSessionId: string };

export async function requestOtp(email: string): Promise<RequestOtpResult> {
  const res = await fetch(`${API_BASE}/auth/signinup/code`, {
    method: "POST",
    headers: { "Content-Type": "application/json", rid: "passwordless" },
    body: JSON.stringify({ email }),
  });
  const data = await handle<{ deviceId: string; preAuthSessionId: string }>(res);
  return { deviceId: data.deviceId, preAuthSessionId: data.preAuthSessionId };
}

export async function verifyOtp(params: {
  deviceId: string;
  preAuthSessionId: string;
  code: string;
}): Promise<boolean> {
  const res = await fetch(`${API_BASE}/auth/signinup/code/consume`, {
    method: "POST",
    headers: { "Content-Type": "application/json", rid: "passwordless" },
    body: JSON.stringify({
      deviceId: params.deviceId,
      preAuthSessionId: params.preAuthSessionId,
      userInputCode: params.code,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new ApiError(res.status, text || res.statusText);
  }
  const accessToken = res.headers.get("st-access-token");
  const data = (await res.json()) as { status: string };
  // RESTART_FLOW_ERROR / GENERAL_ERROR: wrong or expired code — caller
  // should let the customer retry, not treat this as a network failure.
  if (data.status !== "OK" || !accessToken) {
    return false;
  }
  storeAccessToken(accessToken);
  return true;
}
