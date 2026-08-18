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
  del: <T,>(p: string) => request<T>("DELETE", p),
};

// --- Types ---
export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  role: string;
  tenant_id: string;
  is_platform_admin: boolean;
  email_verified: boolean;
  passkey_verified: boolean;
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
