import { createContext, useContext, useEffect, useState, ReactNode } from "react";
import { api, setToken, Me, LoginResponse } from "./api";

interface AuthState {
  me: Me | null;
  loading: boolean;
  login: (email: string) => Promise<LoginResponse>;
  enrollPasskey: (label: string, transport: string) => Promise<void>;
  unlock: () => Promise<void>;
  logout: () => void;
  refresh: () => Promise<void>;
}

const Ctx = createContext<AuthState>(null as unknown as AuthState);

export function useAuth() {
  return useContext(Ctx);
}

// The credential id of the enrolled simulated passkey for this browser session.
function credKey(userId: string) {
  return `cv_cred_${userId}`;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  async function refresh() {
    try {
      const m = await api.get<Me>("/auth/me");
      setMe(m);
    } catch {
      setMe(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  async function login(email: string) {
    const res = await api.post<LoginResponse>("/auth/login", { email });
    setToken(res.token);
    await refresh();
    return res;
  }

  async function enrollPasskey(label: string, transport: string) {
    const res = await api.post<{ credential_id: string }>(
      "/auth/passkey/register-simulated",
      { label, transport }
    );
    if (me) localStorage.setItem(credKey(me.user_id), res.credential_id);
    await unlock();
  }

  // Passkey step-up: get challenge -> simulated authenticator signs -> verify.
  async function unlock() {
    const m = me ?? (await api.get<Me>("/auth/me"));
    let credentialId = localStorage.getItem(credKey(m.user_id));
    if (!credentialId && m.passkeys.length) credentialId = m.passkeys[0].id;
    if (!credentialId) throw new Error("No passkey enrolled on this device");

    const { challenge } = await api.post<{ challenge: string }>(
      "/auth/passkey/challenge"
    );
    const { signature } = await api.post<{ signature: string }>(
      "/auth/passkey/sign-simulated",
      { credential_id: credentialId, challenge }
    );
    const res = await api.post<LoginResponse>("/auth/passkey/verify", {
      credential_id: credentialId,
      challenge,
      signature,
    });
    setToken(res.token);
    await refresh();
  }

  function logout() {
    setToken(null);
    setMe(null);
  }

  return (
    <Ctx.Provider value={{ me, loading, login, enrollPasskey, unlock, logout, refresh }}>
      {children}
    </Ctx.Provider>
  );
}
