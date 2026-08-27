// Typed API client for the Arkive control plane.

const BASE = "/api";

let token: string | null = localStorage.getItem("cv_token");

export function setToken(t: string | null) {
  token = t;
  if (t) localStorage.setItem("cv_token", t);
  else localStorage.removeItem("cv_token");
}

export function getToken() {
  return token;
}

// Global session-expiry hook: the AuthProvider registers a callback so any 401
// on an authenticated request signs the user out and shows the timeout notice.
let onUnauthorized: (() => void) | null = null;
export function setOnUnauthorized(cb: (() => void) | null) {
  onUnauthorized = cb;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(BASE + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    // A 401 on an authenticated (non-auth) request means the session expired —
    // fire the global sign-out so the app redirects to Login with a notice.
    if (res.status === 401 && token && !path.startsWith("/auth/")) {
      onUnauthorized?.();
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export const api = {
  get: <T,>(p: string) => request<T>("GET", p),
  post: <T,>(p: string, b?: unknown) => request<T>("POST", p, b),
  put: <T,>(p: string, b?: unknown) => request<T>("PUT", p, b),
  del: <T,>(p: string) => request<T>("DELETE", p),
};

// --- Types ---
export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
  tenant_id: string;
  tenant_type?: string;
  is_platform_admin: boolean;
  can_admin?: boolean;
  is_owner?: boolean;
  email_verified: boolean;
  passkey_verified: boolean;
  needs_setup?: boolean;
  features?: Record<string, boolean>;
  passkeys: { id: string; label: string; transport: string }[];
}

export interface LoginResponse {
  token: string;
  user_id: string;
  tenant_id: string;
  role: string;
  is_platform_admin: boolean;
  passkey_verified: boolean;
  has_passkey: boolean;
}
