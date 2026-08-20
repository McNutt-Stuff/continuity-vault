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

      <CTABand />
    </>
  );
}
