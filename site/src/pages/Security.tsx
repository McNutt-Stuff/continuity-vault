import { Section, SectionHead, FeatureCard, CTABand } from "../components/Layout";
import { security } from "../content";

export default function Security() {
  return (
    <>
      <Section>
        <SectionHead eyebrow="Security & privacy" title={security.h1} lead={security.lead} />
        <div className="grid grid-3">
          {security.pillars.map((p) => <FeatureCard key={p.h} {...p} />)}
        </div>
      </Section>

      <Section className="tight">
        <div className="grid grid-2" style={{ alignItems: "center", gap: 48 }}>
          <div>
            <div className="eyebrow"><span className="dot" /> Defense in depth</div>
            <h2>Isolation at every layer.</h2>
            <p className="lead">Every tenant is isolated across identity, storage prefix, policy, and encryption — enforced on every request. Least-privilege access means operators can run your protection without ever reading your data.</p>
          </div>
          <div className="card">
            {[
              ["Identity", "Passkeys & hardware tokens unlock access"],
              ["Encryption", "Hybrid post-quantum, per-object keys"],
              ["Storage", "Immutable, object-locked, tenant-prefixed"],
              ["Policy", "Retention, approvals, quorum you define"],
              ["Integrity", "Hash-chained, signed audit ledger"],
            ].map(([k, v]) => (
              <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: 16, padding: "12px 0", borderTop: "1px solid var(--border)" }}>
                <span style={{ fontWeight: 650 }}>{k}</span>
                <span style={{ color: "var(--muted)", fontSize: 14, textAlign: "right" }}>{v}</span>
              </div>
            ))}
          </div>
        </div>
      </Section>

      <CTABand />
    </>
  );
}
