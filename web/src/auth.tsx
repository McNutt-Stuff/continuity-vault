import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { startRegistration, startAuthentication } from "@simplewebauthn/browser";
import { api, setToken, getToken, setOnUnauthorized, setOnMaintenance, ApiError, Me, LoginResponse } from "./api";
import { setUserTimezone } from "./components/ui";

interface StartResult {
  exists: boolean;
  has_passkey?: boolean;
  method: "passkey" | "email" | "signup";
}

interface CodeResult {
  sent: boolean;
  delivery?: string;
  dev_code?: string;
  throttled?: boolean;
}

interface AuthState {
  me: Me | null;
  loading: boolean;
  sessionExpired: boolean;
  loginStart: (email: string) => Promise<StartResult>;
  loginWithPasskey: (email: string) => Promise<void>;
  signup: (email: string, displayName: string, orgName: string) => Promise<CodeResult>;
  requestEmailCode: (email: string, purpose?: string) => Promise<CodeResult>;
  verifyEmailCode: (email: string, code: string, purpose?: string) => Promise<void>;
  redeemRecoveryKey: (email: string, code: string) => Promise<void>;
  enrollPasskey: (label?: string) => Promise<void>;
  stepUp: () => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthState>(null as unknown as AuthState);

export function useAuth() {
  return useContext(Ctx);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [maintenance, setMaintenance] = useState<string | null>(null);

  async function refresh() {
    try {
      const m = await api.get<Me>("/auth/me");
      setUserTimezone(m.timezone);
      setMe(m);
      setSessionExpired(false);
    } catch (e) {
      // A 401 while a token is stored means the session expired (vs. never
      // signed in) — surface the timeout notice on the login screen.
      if ((e as ApiError)?.status === 401) {
        if (getToken()) setSessionExpired(true);
        setToken(null);
      }
      setMe(null);
    } finally {
      setLoading(false);
    }
  }

  // Any authenticated request that 401s (token expired) signs the user out and
  // flips the app to the login screen with the timeout notice.
  useEffect(() => {
    setOnUnauthorized(() => {
      setToken(null);
      setMe(null);
      setSessionExpired(true);
    });
    return () => setOnUnauthorized(null);
  }, []);

  // A 503 maintenance signal (tenant mid-HA-switchover) shows a brief dialog that
  // auto-dismisses after the retry window so the customer can resume seamlessly.
  useEffect(() => {
    setOnMaintenance((detail, retryAfter) => {
      setMaintenance(detail);
      window.setTimeout(() => setMaintenance(null), Math.max(3, retryAfter) * 1000);
    });
    return () => setOnMaintenance(null);
  }, []);

  useEffect(() => {
    void refresh();
  }, []);

  async function applySession(res: LoginResponse) {
    setToken(res.token);
    setSessionExpired(false);
    await refresh();
  }

  const loginStart = (email: string) =>
    api.post<StartResult>("/auth/login/start", { email });

  // Passwordless primary factor: authenticate with an enrolled passkey.
  async function loginWithPasskey(email: string) {
    const options = await api.post<any>("/auth/login/passkey/options", { email });
    const asseResp = await startAuthentication(options);
    const res = await api.post<LoginResponse>("/auth/login/passkey/verify", {
      email,
      credential: asseResp,
    });
    await applySession(res);
  }

  const signup = (email: string, displayName: string, orgName: string) =>
    api.post<CodeResult>("/auth/signup", {
      email,
      display_name: displayName,
      org_name: orgName,
    });

  const requestEmailCode = (email: string, purpose = "login") =>
    api.post<CodeResult>("/auth/email/request", { email, purpose });

  // Bootstrap / recovery factor: proves email ownership; yields an identity
  // session that can enroll a passkey but is not hardware-verified.
  async function verifyEmailCode(email: string, code: string, purpose = "login") {
    const res = await api.post<LoginResponse>("/auth/email/verify", {
      email,
      code,
      purpose,
    });
    await applySession(res);
  }

  // Last-resort access: redeem a Vault Recovery Key. Restores the vault key and
  // yields an identity session (not hardware-verified) so the user can enrol a
  // fresh passkey immediately.
  async function redeemRecoveryKey(email: string, code: string) {
    const res = await api.post<LoginResponse>("/auth/recovery/redeem", { email, code });
    await applySession(res);
  }

  // Enroll a real passkey (platform authenticator / security key). A successful
  // registration also produces a hardware-verified session.
  async function enrollPasskey(label = "This device") {
    const options = await api.post<any>("/auth/webauthn/register/options");
    const attResp = await startRegistration(options);
    const res = await api.post<LoginResponse>("/auth/webauthn/register/verify", {
      credential: attResp,
      label,
    });
    await applySession(res);
  }

  // In-session step-up: verify with an existing passkey, or enroll one if the
  // account has none yet.
  async function stepUp() {
    const current = me ?? (await api.get<Me>("/auth/me"));
    if (current.passkeys.length === 0) {
      await enrollPasskey();
      return;
    }
    const options = await api.post<any>("/auth/webauthn/authenticate/options");
    const asseResp = await startAuthentication(options);
    const res = await api.post<LoginResponse>("/auth/webauthn/authenticate/verify", {
      credential: asseResp,
    });
    await applySession(res);
  }

  function logout() {
    setToken(null);
    setMe(null);
    setSessionExpired(false);
  }

  return (
    <Ctx.Provider
      value={{
        me,
        loading,
        sessionExpired,
        loginStart,
        loginWithPasskey,
        signup,
        requestEmailCode,
        verifyEmailCode,
        redeemRecoveryKey,
        enrollPasskey,
        stepUp,
        logout,
        refresh,
      }}
    >
      {children}
      {maintenance && (
        <div className="modal-backdrop" style={{ zIndex: 9999 }} role="alertdialog" aria-modal>
          <div className="modal" style={{ textAlign: "center", maxWidth: 420 }}>
            <div className="spinner" style={{ margin: "0 auto 14px" }} aria-hidden />
            <h3 style={{ marginTop: 0 }}>Brief maintenance</h3>
            <p className="muted" style={{ margin: 0 }}>{maintenance}</p>
          </div>
        </div>
      )}
    </Ctx.Provider>
  );
}
