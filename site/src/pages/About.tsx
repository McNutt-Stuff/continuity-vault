import { Section, SectionHead, CTABand } from "../components/Layout";
import { about } from "../content";

export default function About() {
  return (
    <>
      <Section>
        <SectionHead eyebrow="About us" title={about.h1} lead={about.lead} />
        <div className="grid grid-3" style={{ marginTop: 40 }}>
          {about.values.map((v) => (
            <div className="card" key={v.h}>
              <h3>{v.h}</h3>
              <p style={{ margin: 0 }}>{v.p}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section className="tight">
        <div className="prose" style={{ margin: "0 auto" }}>
          {about.body.map((b) => (
            <div key={b.h}>
              <h2>{b.h}</h2>
              <p>{b.p}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section>
        <SectionHead eyebrow="Built to endure" title={about.resilience.h} lead={about.resilience.lead} />
        <div className="grid grid-3" style={{ marginTop: 40 }}>
          {about.resilience.layers.map((l) => (
            <div className="card" key={l.h}>
              <div style={{ fontSize: 26, marginBottom: 10 }}>{l.ico}</div>
              <h3>{l.h}</h3>
              <p style={{ margin: 0 }}>{l.p}</p>
            </div>
          ))}
        </div>
      </Section>

      <Section className="tight">
        <SectionHead eyebrow="Our pedigree" title={about.pedigree.h} lead={about.pedigree.lead} />
        <div className="grid grid-3" style={{ marginTop: 40 }}>
          {about.pedigree.points.map((p) => (
            <div className="card" key={p.h}>
              <div style={{ fontSize: 26, marginBottom: 10 }}>{p.ico}</div>
              <h3>{p.h}</h3>
              <p style={{ margin: 0 }}>{p.p}</p>
            </div>
          ))}
        </div>
      </Section>

      <CTABand />
    </>
  );
}
