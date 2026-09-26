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

// --- Live debug overlay: a rolling ring-buffer of recent API calls (method, path,
// status, timing, error detail). Always captured (cheap, capped) so the debug
// footer bar can show a behind-the-scenes history the moment it's enabled. ---
export interface DebugCall {
  id: number;
  ts: number;            // epoch ms when the call started
  method: string;
  path: string;
  status: number;        // 0 = network/abort error
  ms: number;            // round-trip time (browser → CP → back)
  ok: boolean;
  error?: string;        // detail message on failure
  route?: string;        // "cp" | "cp->node" | "node"
  node?: string;         // serving node host when proxied CP->node
  serverMs?: number;     // server processing time (X-Arkive-Server-Ms)
  upstreamMs?: number;   // CP->node hop time (X-Arkive-Upstream-Ms)
}
const DEBUG_MAX = 200;
const debugCalls: DebugCall[] = [];
let debugSeq = 0;
const debugSubs = new Set<(calls: DebugCall[]) => void>();

function recordDebugCall(c: Omit<DebugCall, "id">) {
  const entry: DebugCall = { id: ++debugSeq, ...c };
  debugCalls.push(entry);
  if (debugCalls.length > DEBUG_MAX) debugCalls.splice(0, debugCalls.length - DEBUG_MAX);
  debugSubs.forEach((fn) => { try { fn(debugCalls); } catch { /* ignore */ } });
}

export function getDebugCalls(): DebugCall[] {
  return debugCalls.slice();
}
export function subscribeDebugCalls(fn: (calls: DebugCall[]) => void): () => void {
  debugSubs.add(fn);
  return () => debugSubs.delete(fn);
}
export function clearDebugCalls() {
  debugCalls.length = 0;
  debugSubs.forEach((fn) => { try { fn(debugCalls); } catch { /* ignore */ } });
}

// Pull the request-chain breadcrumbs the server stamps on every response.
function debugMeta(res: Response): Pick<DebugCall, "route" | "node" | "serverMs" | "upstreamMs"> {
  const num = (v: string | null) => (v == null || v === "" ? undefined : Number(v));
  return {
    route: res.headers.get("X-Arkive-Route") || undefined,
    node: res.headers.get("X-Arkive-Node") || undefined,
    serverMs: num(res.headers.get("X-Arkive-Server-Ms")),
    upstreamMs: num(res.headers.get("X-Arkive-Upstream-Ms")),
  };
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const started = performance.now();
  const startedAt = Date.now();
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (netErr: any) {
    recordDebugCall({ ts: startedAt, method, path, status: 0,
      ms: Math.round(performance.now() - started), ok: false,
      error: netErr?.message || "network error" });
    throw netErr;
  }
  if (!res.ok) {
    let detail = res.statusText;
    let payload: any = null;
    try {
      payload = await res.json();
      detail = payload?.detail ?? detail;
    } catch {
      /* ignore */
    }
    recordDebugCall({ ts: startedAt, method, path, status: res.status,
      ms: Math.round(performance.now() - started), ok: false, error: detail,
      ...debugMeta(res) });
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
  recordDebugCall({ ts: startedAt, method, path, status: res.status,
    ms: Math.round(performance.now() - started), ok: true, ...debugMeta(res) });
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
