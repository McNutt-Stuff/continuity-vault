import { Section, SectionHead, CTABand } from "../components/Layout";
import { useCases } from "../content";

export default function UseCases() {
  return (
    <>
      {/* Scenario-based protection — the prime story */}
      <Section>
        <SectionHead eyebrow="What Arkive protects you from" title={useCases.h1} lead={useCases.lead} />
        <div className="grid grid-2 scenario-grid">
          {useCases.scenarios.map((s) => (
            <div className="card scenario" key={s.h}>
              <div className="scenario-head">
                <div className="scenario-ico">{s.ico}</div>
                <div className="eyebrow" style={{ margin: 0 }}><span className="dot" /> {s.tag}</div>
              </div>
              <h3>{s.h}</h3>
              <p style={{ fontSize: 15 }}>{s.p}</p>
              <div className="scenario-points">
                {s.points.map((pt) => (
                  <div key={pt} className="scenario-point"><span className="tick">✓</span> {pt}</div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </Section>

      {/* Who it's for — audience segments */}
      <Section className="tight">
        <SectionHead eyebrow="Who it's for" title={useCases.whoTitle} lead={useCases.whoLead} />
        <div className="grid grid-2" style={{ marginTop: 32 }}>
          {useCases.cases.map((c) => (
            <div className="card" key={c.h}>
              <div className="ico">{c.ico}</div>
              <h3>{c.h}</h3>
              <p>{c.p}</p>
              <div style={{ display: "grid", gap: 9, marginTop: 14 }}>
                {c.points.map((pt) => (
                  <div key={pt} style={{ display: "flex", gap: 10, fontSize: 14, color: "var(--muted)" }}>
                    <span style={{ color: "var(--brand-2)" }}>✓</span> {pt}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </Section>
      <CTABand />
    </>
  );
}
