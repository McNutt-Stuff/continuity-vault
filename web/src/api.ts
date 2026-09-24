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

// Global maintenance hook: a 503 {maintenance:true} means the tenant is briefly
// unavailable during an HA switchover. The app shows a friendly dialog and retries.
let onMaintenance: ((detail: string, retryAfter: number) => void) | null = null;
export function setOnMaintenance(cb: ((detail: string, retryAfter: number) => void) | null) {
  onMaintenance = cb;
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
    let payload: any = null;
    try {
      payload = await res.json();
      detail = payload?.detail ?? detail;
    } catch {
      /* ignore */
    }
    // A 401 on an authenticated (non-auth) request means the session expired —
    // fire the global sign-out so the app redirects to Login with a notice.
    if (res.status === 401 && token && !path.startsWith("/auth/")) {
      onUnauthorized?.();
    }
    // A 503 maintenance signal = the tenant is mid-HA-switchover; show a dialog.
    if (res.status === 503 && (payload?.maintenance || payload?.node_not_ready)) {
      onMaintenance?.(detail, Number(payload?.retry_after) || 15);
      throw new ApiError(res.status, detail, true);
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public maintenance = false) {
    super(message);
  }
}

async function uploadRequest<T>(path: string, form: FormData): Promise<T> {
  // Multipart upload — let the browser set the Content-Type (with boundary).
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: form,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json())?.detail ?? detail; } catch { /* ignore */ }
    if (res.status === 401 && token && !path.startsWith("/auth/")) onUnauthorized?.();
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

async function blobRequest(path: string): Promise<Blob> {
  const res = await fetch(BASE + path, {
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!res.ok) {
    if (res.status === 401 && token) onUnauthorized?.();
    throw new ApiError(res.status, res.statusText);
  }
  return res.blob();
}

export const api = {
  get: <T,>(p: string) => request<T>("GET", p),
  post: <T,>(p: string, b?: unknown) => request<T>("POST", p, b),
  put: <T,>(p: string, b?: unknown) => request<T>("PUT", p, b),
  patch: <T,>(p: string, b?: unknown) => request<T>("PATCH", p, b),
  del: <T,>(p: string) => request<T>("DELETE", p),
  upload: <T,>(p: string, form: FormData) => uploadRequest<T>(p, form),
  blob: (p: string) => blobRequest(p),
};

// --- Types ---
export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  full_name?: string;
  first_name?: string;
  last_name?: string;
  phone?: string;
  timezone?: string;
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
  recovery_key?: {
    eligible: boolean;
    eligible_vaults: number;
    created: boolean;
    created_at?: string | null;
    rotated_at?: string | null;
    last_used_at?: string | null;
    hint?: string;
    covered_vaults?: number;
    needs_prompt?: boolean;
    stale?: boolean;
  } | null;
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
