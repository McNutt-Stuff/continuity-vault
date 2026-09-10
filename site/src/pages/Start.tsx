import { Section } from "../components/Layout";
import { site } from "../content";

const SIGNUP_URL = `${site.appUrl}/signup`;

const BENEFITS = [
  { ico: "🛡️", h: "Quantum-safe from day one", p: "Every backup is encrypted with post-quantum (ML-KEM / ML-DSA) hybrid cryptography. Only you hold the keys." },
  { ico: "⚡", h: "Continuous capture, instant recovery", p: "Email, files, photos, passwords and more are protected automatically — and recoverable in seconds." },
  { ico: "☁️", h: "Cloud, your cloud, or an appliance", p: "Keep copies in Arkive Cloud, your own bucket, or an offline appliance you control." },
  { ico: "🔑", h: "No passwords, ever", p: "Sign in with passkeys (Touch ID, Windows Hello, or a security key) — plus a last-resort recovery key." },
];

const STEPS = [
  { n: 1, h: "Pick a plan", p: "Personal, family, or business — start free for 7 days." },
  { n: 2, h: "Set up protection", p: "Choose where your encrypted backups are kept." },
  { n: 3, h: "Connect your sources", p: "A guided setup wizard gets your first backup running in minutes." },
];

export default function Start() {
  return (
    <>
      <Section>
        <div style={{ textAlign: "center", maxWidth: 760, margin: "0 auto" }}>
          <div className="eyebrow" style={{ justifyContent: "center" }}><span className="dot" /> 7-day free trial · no charge today</div>
          <h1 className="gradient-text" style={{ marginTop: 14 }}>Try Arkive free for 7 days</h1>
          <p className="lead" style={{ margin: "16px auto 0" }}>
            Start protecting everything that matters with quantum-safe backup and instant recovery.
            Set up in minutes — cancel anytime before your trial ends and you won't be charged.
          </p>
          <div style={{ display: "flex", gap: 14, justifyContent: "center", flexWrap: "wrap", marginTop: 28 }}>
            <a className="btn primary lg" href={SIGNUP_URL}>Start your free trial</a>
            <a className="btn ghost lg" href={site.appUrl}>Sign in</a>
          </div>
          <div className="footer-bottom" style={{ justifyContent: "center", marginTop: 18, borderTop: "none" }}>
            <span>Post-quantum · Private by design · Cancel anytime</span>
          </div>
        </div>
      </Section>

      <Section className="tight">
        <div className="grid grid-3">
          {STEPS.map((s) => (
            <div className="card" key={s.n}>
              <div className="brand-mark" style={{ marginBottom: 12 }}>{s.n}</div>
              <h3>{s.h}</h3>
              <p style={{ margin: 0 }}>{s.p}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section>
        <div className="grid grid-2">
          {BENEFITS.map((b) => (
            <div className="card" key={b.h}>
              <div style={{ fontSize: 26, marginBottom: 10 }}>{b.ico}</div>
              <h3>{b.h}</h3>
              <p style={{ margin: 0 }}>{b.p}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section>
        <div className="cta-band">
          <div className="eyebrow" style={{ justifyContent: "center" }}><span className="dot" /> Ready when you are</div>
          <h2 className="gradient-text">Your data deserves to be permanent.</h2>
          <p className="lead" style={{ margin: "0 auto 26px" }}>Start your 7-day free trial today — no charge until it ends.</p>
          <div style={{ display: "flex", gap: 14, justifyContent: "center", flexWrap: "wrap" }}>
            <a className="btn primary lg" href={SIGNUP_URL}>Start your free trial</a>
            <a className="btn ghost lg" href="/pricing">See pricing</a>
          </div>
          <p className="lead" style={{ margin: "18px auto 0", fontSize: 14 }}>
            Need enterprise controls, SSO, or volume pricing? <a href="/contact">Contact sales</a>.
          </p>
        </div>
      </Section>
    </>
  );
}
