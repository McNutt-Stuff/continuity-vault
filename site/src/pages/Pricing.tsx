import { Link } from "react-router-dom";
import { Section, SectionHead } from "../components/Layout";
import { pricing } from "../content";

export default function Pricing() {
  return (
    <>
      <Section>
        <SectionHead eyebrow="Pricing" title={pricing.h1} lead={pricing.lead} />
        <div className="grid price-grid">
          {pricing.plans.map((p) => (
            <div key={p.name} className={`plan ${p.featured ? "featured" : ""}`}>
              <div className="pill" style={{ alignSelf: "flex-start" }}>{p.name}</div>
              <div className="price">{p.price}<small>{p.per}</small></div>
              <p style={{ fontSize: 14.5, minHeight: 42 }}>{p.blurb}</p>
              <ul>
                {p.features.map((f) => (
                  <li key={f}><span className="tick">✓</span> {f}</li>
                ))}
              </ul>
              <Link className={`btn ${p.featured ? "primary" : "ghost"}`} to={p.name === "Business" ? "/contact" : "/contact"}>
                {p.cta}
              </Link>
            </div>
          ))}
        </div>
        <p className="lead" style={{ textAlign: "center", margin: "36px auto 0", fontSize: 14 }}>{pricing.note}</p>
      </Section>

      <Section className="tight">
        <div className="cta-band">
          <h2>Questions about scale, compliance, or appliances?</h2>
          <p className="lead" style={{ margin: "0 auto 24px" }}>Our team will help you design the right protection and recovery posture.</p>
          <Link className="btn primary lg" to="/contact">Talk to us</Link>
        </div>
      </Section>
    </>
  );
}
