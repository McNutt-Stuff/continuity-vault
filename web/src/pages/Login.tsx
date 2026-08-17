import { useState } from "react";
import { useAuth } from "../auth";
import { Icon } from "../components/Icon";

const DEMO_USERS = [
  { email: "owner@northwind.example", role: "Vault Owner" },
  { email: "security@northwind.example", role: "Security Admin" },
  { email: "admin@arkive.life", role: "Platform Admin" },
];

export default function Login() {
  const { login, enrollPasskey, unlock, me } = useAuth();
  const [email, setEmail] = useState("owner@northwind.example");
  const [stage, setStage] = useState<"login" | "passkey">("login");
  const [hasPasskey, setHasPasskey] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function doLogin() {
    setErr("");
    setBusy(true);
    try {
      const res = await login(email.trim().toLowerCase());
      setHasPasskey(res.has_passkey);
      setStage("passkey");
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function doEnroll() {
    setErr("");
    setBusy(true);
    try {
      await enrollPasskey("This device", "internal");
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  }

  async function doUnlock() {
    setErr("");
    setBusy(true);
    try {
      await unlock();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="auth-wrap">
      <div className="auth-card card">
        <div className="row" style={{ marginBottom: 18 }}>
          <div className="brand-logo">A</div>
          <div>
            <div className="brand-name">Arkive</div>
            <div className="faint" style={{ fontSize: 12 }}>Digital continuity & cyber-recovery</div>
          </div>
        </div>

        {stage === "login" && (
          <>
            <div className="field">
              <label>Work email</label>
              <input
                className="input"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && doLogin()}
                placeholder="you@company.com"
              />
            </div>
            <button className="btn primary" style={{ width: "100%" }} onClick={doLogin} disabled={busy}>
              Continue
            </button>
            <div className="divider" />
            <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>Demo identities</div>
            <div className="chips">
              {DEMO_USERS.map((u) => (
                <span
                  key={u.email}
                  className={`chip ${email === u.email ? "active" : ""}`}
                  onClick={() => setEmail(u.email)}
                >
                  {u.role}
                </span>
              ))}
            </div>
          </>
        )}

        {stage === "passkey" && (
          <div className="stack" style={{ gap: 16 }}>
            <div className="lock-banner">
              <Icon name="key" />
              <div>
                <div style={{ fontWeight: 600 }}>Hardware-backed unlock</div>
                <div className="faint" style={{ fontSize: 12 }}>
                  Your data-access interfaces are protected by a passkey / hardware token.
                </div>
              </div>
            </div>
            {hasPasskey ? (
              <button className="btn accent" onClick={doUnlock} disabled={busy}>
                <Icon name="lock" size={15} /> Unlock with passkey
              </button>
            ) : (
              <>
                <div className="muted" style={{ fontSize: 13 }}>
                  No passkey is enrolled on this device. Set one up to continue.
                </div>
                <button className="btn primary" onClick={doEnroll} disabled={busy}>
                  <Icon name="key" size={15} /> Set up a passkey
                </button>
              </>
            )}
            <button className="btn ghost sm" onClick={() => setStage("login")}>Back</button>
          </div>
        )}

        {err && (
          <div className="pill danger" style={{ marginTop: 14 }}>
            {err}
          </div>
        )}
      </div>
    </div>
  );
}
