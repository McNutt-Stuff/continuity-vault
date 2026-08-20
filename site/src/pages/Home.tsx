import { Link } from "react-router-dom";
import { Section, SectionHead, FeatureCard, CTABand } from "../components/Layout";
import { home } from "../content";

export default function Home() {
  return (
    <>
      {/* Hero */}
      <div className="wrap hero">
        <div className="reveal">
          <div className="eyebrow"><span className="dot" /> {home.eyebrow}</div>
          <h1 className="gradient-text">{home.h1}</h1>
          <p className="lead">{home.lead}</p>
          <div className="hero-cta">
            <Link className="btn primary lg" to={home.ctaPrimary.to}>{home.ctaPrimary.label}</Link>
            <Link className="btn ghost lg" to={home.ctaSecondary.to}>{home.ctaSecondary.label}</Link>
          </div>
          <div className="hero-badges">
            {home.badges.map((b) => <span key={b}><span className="tick">✓</span> {b}</span>)}
          </div>
        </div>

        {/* Product visual */}
        <div className="hero-visual reveal">
          <div className="bar"><i /><i /><i /></div>
          <div className="stage">
            <div className="card" style={{ margin: 0 }}>
              <div className="pill" style={{ marginBottom: 14 }}>● Protected & verified</div>
              <h3 style={{ marginBottom: 12 }}>Everything, protected</h3>
              {[
                ["📧", "Gmail — 48,204 messages", "Post-quantum · versioned"],
                ["🗂️", "OneDrive — 12,880 files", "Immutable · WORM"],
                ["🔐", "1Password — 214 items", "Zero-knowledge"],
                ["🖼️", "Photos — 31,507 items", "Originals preserved"],
              ].map(([i, t, s]) => (
                <div key={t} style={{ display: "flex", gap: 12, alignItems: "center", padding: "10px 0", borderTop: "1px solid var(--border)" }}>
                  <span style={{ fontSize: 18 }}>{i}</span>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 14, fontWeight: 600 }}>{t}</div>
                    <div style={{ fontSize: 12, color: "var(--faint)" }}>{s}</div>
                  </div>
                  <span style={{ color: "var(--brand-2)" }}>✓</span>
                </div>
              ))}
            </div>
            <div style={{ display: "grid", gap: 18 }}>
              <div className="card" style={{ margin: 0 }}>
                <div className="eyebrow" style={{ marginBottom: 10 }}><span className="dot" /> Integrity</div>
                <h3 style={{ marginBottom: 6 }}>Tamper-evident</h3>
                <p style={{ fontSize: 13.5 }}>Every snapshot is hash-chained and signed. Prove your archive is authentic and unaltered.</p>
              </div>
              <div className="card" style={{ margin: 0 }}>
                <div className="eyebrow" style={{ marginBottom: 10 }}><span className="dot" /> Recovery</div>
                <h3 style={{ marginBottom: 6 }}>Instant & controlled</h3>
                <p style={{ fontSize: 13.5 }}>Search everything, preview in a time-limited window, restore with approvals.</p>
              </div>
            </div>
          </div>
        </div>

        {/* Stat strip */}
        <div className="stat-strip" style={{ marginTop: 56 }}>
          {home.stats.map((s) => (
            <div key={s.l}><div className="n gradient-text">{s.n}</div><div className="l">{s.l}</div></div>
          ))}
        </div>
      </div>

      {/* Value props */}
      <Section>
        <SectionHead eyebrow="Why Arkive" title="Protection you can prove — privacy you can trust." />
        <div className="grid grid-3">
          {home.valueProps.map((v) => <FeatureCard key={v.h} {...v} />)}
        </div>
      </Section>

      {/* How it works */}
      <Section className="tight">
        <div className="grid grid-2" style={{ alignItems: "center", gap: 48 }}>
          <div>
            <SectionHead left eyebrow="How it works" title="From connect to recovery in three steps." />
          </div>
          <div className="steps">
            {home.steps.map((s) => (
              <div className="step" key={s.h}>
                <div className="num" />
                <div><h3 style={{ marginBottom: 4 }}>{s.h}</h3><p style={{ margin: 0, fontSize: 14.5 }}>{s.p}</p></div>
              </div>
            ))}
          </div>
        </div>
      </Section>

      <CTABand />
    </>
  );
}
