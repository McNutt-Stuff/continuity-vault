import { Link } from "react-router-dom";
import { useState } from "react";
import { Section, SectionHead } from "../components/Layout";
import { pricing, storageOptions, hardware } from "../content";
import { platformPricing, planById, startingMonthly, money } from "../pricing";

export default function Pricing() {
  const cloud = platformPricing.cloud_price_per_tb_month;
  const appMonthlyMin = platformPricing.appliance_tiers.length
    ? Math.min(...platformPricing.appliance_tiers.map((t) => t.monthly)) : 0;
  const byoRates = [platformPricing.azure_price_per_tb_month, platformPricing.s3_price_per_tb_month];
  const byoLow = Math.min(...byoRates);
  const byoHigh = Math.max(...byoRates);

  function optionPrice(id: string) {
    if (id === "cloud") return { top: `${money(cloud)}`, unit: "/TB · month", sub: "Starting price · Arkive Cloud storage" };
    if (id === "appliance") return { top: `from ${money(appMonthlyMin)}`, unit: "/month", sub: "Leased hardware + one-time setup" };
    if (id === "byo") return { top: `est. ${money(byoLow)}–${money(byoHigh)}`, unit: "/TB · month", sub: "Estimated — paid directly to your provider" };
    return null;
  }

  return (
    <>
      <Section>
        <SectionHead eyebrow="Pricing" title={pricing.h1} lead={pricing.lead} />
        <div className="grid price-grid">
          {pricing.plans.map((p: any) => {
            const pl = planById(p.planId);
            const start = startingMonthly(p.planId);
            return (
              <div key={p.name} className={`plan ${p.featured ? "featured" : ""}`}>
                <div className="pill" style={{ alignSelf: "flex-start" }}>{p.name}</div>
                {start != null && pl ? (
                  <>
                    <div className="price"><small style={{ display: "block", fontSize: 12, opacity: 0.7 }}>Starting at</small>{money(start)}<small>/mo</small></div>
                    <div className="faint" style={{ fontSize: 12.5, marginTop: -6, marginBottom: 4 }}>
                      includes {pl.min_tb} TB · then {money(pl.price_per_tb_month)}/TB · month
                    </div>
                  </>
                ) : (
                  <div className="price">{p.price}<small>{p.per}</small></div>
                )}
                <p style={{ fontSize: 14.5, minHeight: 42 }}>{p.blurb}</p>
                <ul>
                  {p.features.map((f: string) => (
                    <li key={f}><span className="tick">✓</span> {f}</li>
                  ))}
                </ul>
                <Link className={`btn ${p.featured ? "primary" : "ghost"}`} to="/contact">{p.cta}</Link>
              </div>
            );
          })}
        </div>

        <p className="lead" style={{ textAlign: "center", margin: "28px auto 0", fontSize: 14 }}>{pricing.note}</p>
      </Section>

      {/* Where your data lives — 3 storage options, priced from admin config */}
      <Section className="tight">
        <SectionHead eyebrow="Storage options" title={storageOptions.h1} lead={storageOptions.lead} />
        <div className="grid grid-3" style={{ marginTop: 36 }}>
          {storageOptions.options.map((o) => {
            const price = optionPrice(o.id);
            return (
              <div className="card" key={o.h}>
                <div className="ico">{o.ico}</div>
                <div className="eyebrow" style={{ marginBottom: 6 }}><span className="dot" /> {o.tag}</div>
                <h3>{o.h}</h3>
                {price && (
                  <div style={{ margin: "8px 0 10px", paddingBottom: 12, borderBottom: "1px solid var(--border)" }}>
                    <div style={{ fontSize: 22, fontWeight: 800 }}>{price.top}<small className="faint" style={{ fontSize: 12.5, fontWeight: 500 }}> {price.unit}</small></div>
                    <div className="faint" style={{ fontSize: 12 }}>{price.sub}</div>
                  </div>
                )}
                <p style={{ fontSize: 14 }}>{o.p}</p>
                <ul style={{ listStyle: "none", padding: 0, margin: "10px 0 0" }}>
                  {o.points.map((pt) => (
                    <li key={pt} style={{ fontSize: 13.5, padding: "3px 0" }}><span className="tick">✓</span> {pt}</li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      </Section>

      {/* Hardware */}
      <Section className="tight">
        <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 40, alignItems: "center" }}>
          <ApplianceImage />
          <div>
            <div className="eyebrow"><span className="dot" /> Secure hardware</div>
            <h2 style={{ marginTop: 8 }}>{hardware.h1}</h2>
            <p className="lead" style={{ margin: "0 0 18px" }}>{hardware.lead}</p>
            <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {hardware.points.map((pt) => (
                <li key={pt} style={{ fontSize: 14, padding: "5px 0" }}><span className="tick">✓</span> {pt}</li>
              ))}
            </ul>
          </div>
        </div>

        <div className="grid grid-3" style={{ marginTop: 32 }}>
          {platformPricing.appliance_tiers.map((t) => (
            <div className="card" key={t.model} style={{ textAlign: "center" }}>
              <div className="pill" style={{ margin: "0 auto 10px" }}>{t.model}</div>
              <div style={{ fontSize: 26, fontWeight: 800 }}>{t.capacity_tb} TB</div>
              <div className="faint" style={{ fontSize: 12.5, marginBottom: 12 }}>usable capacity</div>
              <div style={{ fontSize: 20, fontWeight: 700 }}>{money(t.monthly)}<small className="faint" style={{ fontSize: 12, fontWeight: 500 }}>/mo</small></div>
              <div className="faint" style={{ fontSize: 12.5 }}>+ {money(t.setup)} one-time setup</div>
            </div>
          ))}
        </div>
        <p className="lead" style={{ textAlign: "center", margin: "24px auto 0", fontSize: 14 }}>{hardware.note}</p>
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

// Appliance product shot, with a graceful gradient fallback if the image asset
// isn't present in the site's public folder yet.
function ApplianceImage() {
  const [ok, setOk] = useState(true);
  if (!ok) {
    return (
      <div className="card" style={{ display: "flex", alignItems: "center", justifyContent: "center",
        minHeight: 300, background: "radial-gradient(circle at 50% 40%, rgba(79,124,255,.25), transparent 70%)" }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: 64 }}>🔒</div>
          <div className="faint" style={{ fontSize: 13, marginTop: 8 }}>Arkive Secure Appliance</div>
        </div>
      </div>
    );
  }
  return (
    <img src={hardware.image} alt="Arkive Secure Appliance" onError={() => setOk(false)}
         style={{ width: "100%", borderRadius: 16, display: "block" }} />
  );
}

