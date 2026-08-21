import { useState } from "react";
import { useAuth } from "../auth";
import { Icon } from "../components/Icon";

type Stage = "email" | "signup" | "code";

export default function Login() {
  const { loginStart, loginWithPasskey, signup, requestEmailCode, verifyEmailCode } = useAuth();
  const [stage, setStage] = useState<Stage>("email");
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [orgName, setOrgName] = useState("");
  const [code, setCode] = useState("");
  const [codePurpose, setCodePurpose] = useState<"login" | "verify">("login");
  const [devCode, setDevCode] = useState<string | null>(null);
  const [delivery, setDelivery] = useState<string | null>(null);
  const [canPasskey, setCanPasskey] = useState(false);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  function fail(e: unknown) {
    setErr(e instanceof Error ? e.message : String(e));
    setBusy(false);
  }

  async function onContinue() {
    setErr("");
    setBusy(true);
    try {
      const addr = email.trim().toLowerCase();
      const start = await loginStart(addr);
      if (start.method === "passkey") {
        setCanPasskey(true);
        await loginWithPasskey(addr); // resolves into the portal
      } else if (start.method === "email") {
        const r = await requestEmailCode(addr, "login");
        setCodePurpose("login");
        setDevCode(r.dev_code ?? null);
        setDelivery(r.delivery ?? null);
        setStage("code");
        setBusy(false);
      } else {
        setStage("signup");
        setBusy(false);
      }
    } catch (e) {
      fail(e);
    }
  }

  async function onSignup() {
    setErr("");
    setBusy(true);
    try {
      const addr = email.trim().toLowerCase();
      const r = await signup(addr, displayName.trim(), orgName.trim());
      setCodePurpose("verify");
      setDevCode(r.dev_code ?? null);
      setDelivery(r.delivery ?? null);
      setStage("code");
      setBusy(false);
    } catch (e) {
      fail(e);
    }
  }

  async function onVerifyCode() {
    setErr("");
    setBusy(true);
    try {
      await verifyEmailCode(email.trim().toLowerCase(), code.trim(), codePurpose);
      // Session established -> the app renders the portal; passkey enrollment is
      // prompted there for accounts without one.
    } catch (e) {
      fail(e);
    }
  }

  async function onPasskey() {
    setErr("");
    setBusy(true);
    try {
      await loginWithPasskey(email.trim().toLowerCase());
    } catch (e) {
      fail(e);
    }
  }

  return (
    <div className="auth-wrap">
      <div className="auth-card card">
        <div className="auth-logo">
          <img src="/logos/Logo-Full.png" alt="Arkive — Your digital legacy, protected forever." />
        </div>

        {stage === "email" && (
          <>
            <div className="auth-sub">Sign in to your secure vault</div>
            <div className="field">
              <label>Your email</label>
              <input
                className="input"
                autoFocus
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && onContinue()}
                placeholder="you@email.com"
              />
            </div>
            <button className="btn primary" style={{ width: "100%" }} onClick={onContinue} disabled={busy || !email}>
              Continue
            </button>
            {canPasskey && (
              <button className="btn accent" style={{ width: "100%", marginTop: 10 }} onClick={onPasskey} disabled={busy}>
                <Icon name="key" size={15} /> Sign in with passkey
              </button>
            )}
            <div className="divider" />
            <div className="faint" style={{ fontSize: 12, textAlign: "center" }}>
              Passwordless — secured by passkeys (Touch ID, Windows Hello, or a security key).
            </div>
          </>
        )}

        {stage === "signup" && (
          <>
            <div className="lock-banner" style={{ marginBottom: 14 }}>
              <Icon name="user" />
              <div>
                <div style={{ fontWeight: 600 }}>Create your Arkive organization</div>
                <div className="faint" style={{ fontSize: 12 }}>We'll email a code to verify {email}.</div>
              </div>
            </div>
            <div className="field">
              <label>Your name</label>
              <input className="input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="Alex Rivera" />
            </div>
            <div className="field">
              <label>Organization name</label>
              <input className="input" value={orgName} onChange={(e) => setOrgName(e.target.value)} placeholder="Northwind Family Office" />
            </div>
            <button className="btn primary" style={{ width: "100%" }} onClick={onSignup} disabled={busy || !displayName || !orgName}>
              Create account & send code
            </button>
            <button className="btn ghost sm" style={{ marginTop: 10 }} onClick={() => setStage("email")}>Back</button>
          </>
        )}

        {stage === "code" && (
          <>
            <div className="field">
              <label>Enter the 6-digit code sent to {email}</label>
              <input
                className="input mono"
                autoFocus
                inputMode="numeric"
                maxLength={6}
                value={code}
                onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                onKeyDown={(e) => e.key === "Enter" && onVerifyCode()}
                placeholder="000000"
                style={{ fontSize: 22, letterSpacing: 6, textAlign: "center" }}
              />
            </div>
            {devCode && (
              <div className="pill info" style={{ marginBottom: 12 }}>
                Dev mode — code: <span className="mono" style={{ marginLeft: 6 }}>{devCode}</span>
              </div>
            )}
            {delivery === "log" && !devCode && (
              <div className="faint" style={{ fontSize: 12, marginBottom: 12 }}>
                No email provider configured — retrieve the code from the server log
                (<span className="mono">journalctl -u cv-cloud</span>).
              </div>
            )}
            <button className="btn primary" style={{ width: "100%" }} onClick={onVerifyCode} disabled={busy || code.length < 6}>
              Verify & continue
            </button>
            <button className="btn ghost sm" style={{ marginTop: 10 }} onClick={() => setStage("email")}>Back</button>
          </>
        )}

        {err && <div className="pill danger" style={{ marginTop: 14 }}>{err}</div>}
      </div>
    </div>
  );
}
