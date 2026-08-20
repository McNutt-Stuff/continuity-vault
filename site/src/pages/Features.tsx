import { Section, SectionHead, FeatureCard, CTABand } from "../components/Layout";
import { features } from "../content";

export default function Features() {
  return (
    <>
      <Section>
        <SectionHead eyebrow="Features" title={features.h1} lead={features.lead} />
        {features.groups.map((g) => (
          <div key={g.title} style={{ marginBottom: 48 }}>
            <h3 style={{ fontSize: 15, textTransform: "uppercase", letterSpacing: ".12em", color: "var(--faint)", margin: "0 0 18px" }}>{g.title}</h3>
            <div className="grid grid-3">
              {g.items.map((it) => <FeatureCard key={it.h} {...it} />)}
            </div>
          </div>
        ))}
      </Section>
      <CTABand />
    </>
  );
}
