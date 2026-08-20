import { Section, SectionHead, CTABand } from "../components/Layout";
import { useCases } from "../content";

export default function UseCases() {
  return (
    <>
      <Section>
        <SectionHead eyebrow="Use cases" title={useCases.h1} lead={useCases.lead} />
        <div className="grid grid-2">
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
