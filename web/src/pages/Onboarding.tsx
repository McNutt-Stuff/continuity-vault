import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Card, Pill, bytes } from "../components/ui";
import { Icon } from "../components/Icon";

interface Tier {
  id: string; title: string; tagline: string; icon: string; color: string;
  billing: "per-tb" | "device" | "byo"; benefits: string[];
}
interface ApplianceTier { capacity_tb: number; monthly: number; setup: number; model: string; }
interface Pricing {
  currency: string;
  protection_price_per_tb_month: number;
  cloud_price_per_tb_month: number;
  s3_price_per_tb_month: number;
  azure_price_per_tb_month: number;
  appliance_tiers: ApplianceTier[];
  data_value_per_type: Record<string, number>;
  tiers: Tier[];
}
interface ValueRow { key: string; label: string; icon: string; color: string; count: number; value_each: number; value_total: number; }
interface Plan {
  options: string[]; licensed_tb: number; used_bytes: number; used_tb: number;
  objects_total: number; value_breakdown: ValueRow[]; data_value_total: number;
  appliance_plan: { capacity_tb: number; qty: number }[];
}

const iconName = (n: string) => (["cloud", "server", "key", "shield", "check", "database", "file"].includes(n) ? n : "database") as never;
function money(n: number): string {
  return "$" + Math.round(n).toLocaleString();
}
function money2(n: number): string {
  return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export default function Onboarding() {
  const [pricing, setPricing] = useState<Pricing | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [options, setOptions] = useState<Set<string>>(new Set());
  const [licensedTb, setLicensedTb] = useState(1);
  const [qty, setQty] = useState<Record<number, number>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    api.get<Pricing>("/billing/pricing").then(setPricing).catch(() => {});
    api.get<Plan>("/billing/plan").then((p) => {
      setPlan(p);
      setOptions(new Set(p.options));
      setLicensedTb(Math.max(p.licensed_tb || 0, Math.ceil((p.used_tb || 0) * 10) / 10, 1));
      const q: Record<number, number> = {};
      for (const a of p.appliance_plan || []) q[a.capacity_tb] = a.qty;
      setQty(q);
    }).catch(() => {});
  }, []);

  const usedTb = plan?.used_tb || 0;
  const maxTb = Math.max(20, Math.ceil(usedTb * 3));

  const costs = useMemo(() => {
    if (!pricing) return null;
    const protection = licensedTb * pricing.protection_price_per_tb_month;
    const cloud = options.has("cv-cloud") ? usedTb * pricing.cloud_price_per_tb_month : 0;
    const thirdParty = options.has("customer-cloud") ? usedTb * pricing.s3_price_per_tb_month : 0;
    const applianceMonthly = (pricing.appliance_tiers || []).reduce((s, t) => s + (qty[t.capacity_tb] || 0) * t.monthly, 0);
    const applianceSetup = (pricing.appliance_tiers || []).reduce((s, t) => s + (qty[t.capacity_tb] || 0) * t.setup, 0);
    const totalMonthly = protection + cloud + applianceMonthly;
    const annual = totalMonthly * 12;
    const dataValue = plan?.data_value_total || 0;
    const ratio = annual > 0 ? dataValue / annual : null;
    return { protection, cloud, thirdParty, applianceMonthly, applianceSetup, totalMonthly, annual, dataValue, ratio };
  }, [pricing, options, licensedTb, qty, usedTb, plan]);

  function toggle(id: string) {
    setSaved(false);
    setOptions((cur) => { const n = new Set(cur); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }
  function bump(cap: number, d: number) {
    setSaved(false);
    setQty((cur) => ({ ...cur, [cap]: Math.max(0, (cur[cap] || 0) + d) }));
  }

  async function save() {
    setSaving(true); setSaved(false);
    try {
      const appliance_plan = Object.entries(qty).filter(([, q]) => q > 0)
        .map(([cap, q]) => ({ capacity_tb: Number(cap), qty: q }));
      const updated = await api.put<Plan>("/billing/plan", {
        options: [...options], licensed_tb: licensedTb, appliance_plan,
      });
      setPlan(updated); setSaved(true);
    } catch { /* surfaced via disabled state */ }
    setSaving(false);
  }

  if (!pricing || !plan || !costs) return <Card><div className="muted">Loading plan…</div></Card>;

  const overLicensed = usedTb > licensedTb;

  return (
    <>
      <div className="stack" style={{ marginBottom: 18 }}>
        <h2 style={{ margin: 0 }}>Protection Setup</h2>
        <div className="muted" style={{ fontSize: 13 }}>
          Choose how your data is protected, how much you protect, and see your monthly cost.
          Your selections control which storage destinations are available across the platform.
        </div>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 340px", gap: 16, alignItems: "start" }}>
        {/* -------- Left: choices -------- */}
        <div className="stack" style={{ gap: 16 }}>
          <Card>
            <div className="spread" style={{ marginBottom: 4 }}>
              <h3 style={{ margin: 0 }}>Storage protection</h3>
              <span className="faint" style={{ fontSize: 12 }}>Choose one or more</span>
            </div>
            <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
              Keep your data in the Arkive cloud, on hardware you own, in your own cloud account — or any combination.
            </div>
            <div className="stack" style={{ gap: 12 }}>
              {pricing.tiers.map((t) => {
                const on = options.has(t.id);
                const priceLabel = t.billing === "per-tb"
                  ? `${money2(pricing.cloud_price_per_tb_month)} / TB · month`
                  : t.billing === "device"
                    ? `Leased hardware · from ${money(Math.min(...pricing.appliance_tiers.map((a) => a.monthly)))}/mo + setup`
                    : `You pay your provider · ~${money2(pricing.s3_price_per_tb_month)}/TB (S3) · ~${money2(pricing.azure_price_per_tb_month)}/TB (Azure)`;
                return (
                  <div key={t.id} onClick={() => toggle(t.id)}
                       style={{ cursor: "pointer", border: `1.5px solid ${on ? t.color : "var(--border-soft)"}`,
                                borderRadius: 12, padding: 14, background: on ? `${t.color}12` : "transparent", transition: "all .12s" }}>
                    <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
                      <div className="result-icon" style={{ width: 40, height: 40, background: `${t.color}22`, color: t.color }}>
                        <Icon name={iconName(t.icon)} size={20} />
                      </div>
                      <div className="flex1">
                        <div className="spread">
                          <div style={{ fontWeight: 700, fontSize: 15 }}>{t.title}</div>
                          <div style={{ width: 20, height: 20, borderRadius: 6, border: `1.5px solid ${on ? t.color : "var(--border-soft)"}`,
                                        background: on ? t.color : "transparent", display: "flex", alignItems: "center", justifyContent: "center" }}>
                            {on && <Icon name="check" size={13} />}
                          </div>
                        </div>
                        <div className="faint" style={{ fontSize: 12.5 }}>{t.tagline}</div>
                        <div className="grid grid-2" style={{ gap: 4, marginTop: 10 }}>
                          {t.benefits.map((b) => (
                            <div key={b} className="row" style={{ gap: 6, fontSize: 12 }}>
                              <span style={{ color: t.color }}><Icon name="check" size={12} /></span>{b}
                            </div>
                          ))}
                        </div>
                        <div style={{ marginTop: 10, fontSize: 12.5, fontWeight: 600, color: t.color }}>{priceLabel}</div>
                      </div>
                    </div>

                    {/* Appliance tier picker */}
                    {t.id === "appliance" && on && (
                      <div onClick={(e) => e.stopPropagation()} style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
                        <div className="faint" style={{ fontSize: 11.5, marginBottom: 8 }}>Lease your appliances (you can run several)</div>
                        <div className="stack" style={{ gap: 6 }}>
                          {pricing.appliance_tiers.map((a) => (
                            <div key={a.capacity_tb} className="row" style={{ gap: 8, alignItems: "center" }}>
                              <div className="flex1" style={{ fontSize: 12.5 }}>
                                <span style={{ fontWeight: 600 }}>{a.model}</span> <span className="faint">· {a.capacity_tb} TB · {money(a.monthly)}/mo + {money(a.setup)} setup</span>
                              </div>
                              <button className="btn ghost sm" onClick={() => bump(a.capacity_tb, -1)}>−</button>
                              <span style={{ minWidth: 20, textAlign: "center", fontWeight: 600 }}>{qty[a.capacity_tb] || 0}</span>
                              <button className="btn ghost sm" onClick={() => bump(a.capacity_tb, 1)}>+</button>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </Card>

          {/* Protection level slider */}
          <Card>
            <div className="spread" style={{ marginBottom: 4 }}>
              <h3 style={{ margin: 0 }}>Protection level</h3>
              <span className="faint" style={{ fontSize: 12 }}>{money2(pricing.protection_price_per_tb_month)} / TB · month</span>
            </div>
            <div className="muted" style={{ fontSize: 12.5, marginBottom: 16 }}>
              How much data protection you license. You can raise it any time as you protect more.
            </div>
            <div className="spread" style={{ alignItems: "flex-end", marginBottom: 8 }}>
              <div>
                <div style={{ fontSize: 30, fontWeight: 700, lineHeight: 1 }}>{licensedTb.toFixed(licensedTb < 10 ? 1 : 0)} TB</div>
                <div className="faint" style={{ fontSize: 12 }}>licensed</div>
              </div>
              <div style={{ textAlign: "right" }}>
                <div style={{ fontSize: 15, fontWeight: 600 }}>{bytes(plan.used_bytes)} in use</div>
                <div className="faint" style={{ fontSize: 12 }}>{plan.objects_total.toLocaleString()} objects</div>
              </div>
            </div>
            <input type="range" min={0.5} max={maxTb} step={0.5} value={Math.min(licensedTb, maxTb)}
                   onChange={(e) => { setSaved(false); setLicensedTb(Number(e.target.value)); }}
                   style={{ width: "100%", accentColor: overLicensed ? "#f2545b" : "#4f7cff" }} />
            <div className="spread faint" style={{ fontSize: 11 }}>
              <span>0.5 TB</span><span>{maxTb} TB</span>
            </div>
            {overLicensed && (
              <div style={{ marginTop: 8 }}>
                <Pill tone="warn">You're using more than you've licensed — raise the slider</Pill>
              </div>
            )}
          </Card>
        </div>

        {/* -------- Right: cost + value summary -------- */}
        <div className="stack" style={{ gap: 16, position: "sticky", top: 16 }}>
          <Card>
            <h3 style={{ margin: "0 0 12px" }}>Your monthly cost</h3>
            <CostRow label="Data protection" detail={`${licensedTb.toFixed(licensedTb < 10 ? 1 : 0)} TB × ${money2(pricing.protection_price_per_tb_month)}`} value={money2(costs.protection)} />
            {options.has("cv-cloud") && (
              <CostRow label="Arkive Cloud storage" detail={`${usedTb.toFixed(2)} TB × ${money2(pricing.cloud_price_per_tb_month)}`} value={money2(costs.cloud)} />
            )}
            {options.has("customer-cloud") && (
              <CostRow label="Your cloud (est.)" detail="billed by your provider" value={money2(costs.thirdParty)} muted />
            )}
            {costs.applianceMonthly > 0 && (
              <CostRow label="Appliance lease" detail="leased hardware" value={money2(costs.applianceMonthly)} />
            )}
            <div className="spread" style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
              <div style={{ fontWeight: 700 }}>Total</div>
              <div style={{ fontSize: 24, fontWeight: 800 }}>{money2(costs.totalMonthly)}<span className="faint" style={{ fontSize: 13, fontWeight: 500 }}> /mo</span></div>
            </div>
            {costs.applianceSetup > 0 && (
              <div className="spread faint" style={{ fontSize: 12, marginTop: 6 }}>
                <span>+ Setup (one-time)</span><span style={{ fontWeight: 600 }}>{money(costs.applianceSetup)}</span>
              </div>
            )}
            <button className="btn primary" style={{ width: "100%", marginTop: 14 }} onClick={save} disabled={saving || options.size === 0}>
              {saving ? "Saving…" : saved ? "Saved ✓" : "Save protection plan"}
            </button>
            {options.size === 0 && <div className="faint" style={{ fontSize: 11.5, marginTop: 6, textAlign: "center" }}>Select at least one storage option</div>}
          </Card>

          <Card>
            <div className="spread" style={{ marginBottom: 10 }}>
              <h3 style={{ margin: 0 }}>Your data's value</h3>
              <Icon name="shield" size={16} />
            </div>
            <div style={{ fontSize: 26, fontWeight: 800 }}>{money(costs.dataValue)}</div>
            <div className="faint" style={{ fontSize: 12, marginBottom: 12 }}>estimated worth of what you're protecting</div>
            <div className="stack" style={{ gap: 6 }}>
              {plan.value_breakdown.map((v) => (
                <div key={v.key} className="spread" style={{ fontSize: 12 }}>
                  <span className="row" style={{ gap: 6 }}>
                    <span style={{ width: 8, height: 8, borderRadius: 2, background: v.color }} /> {v.label}
                  </span>
                  <span className="faint">{v.count.toLocaleString()} · {money(v.value_total)}</span>
                </div>
              ))}
              {plan.value_breakdown.length === 0 && <div className="faint" style={{ fontSize: 12 }}>Protect data to see its value.</div>}
            </div>
            {costs.ratio != null && costs.ratio > 0 && (
              <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
                <div className="row" style={{ gap: 8, alignItems: "center" }}>
                  <div className="result-icon" style={{ width: 32, height: 32, background: "#0e1524", color: "#35d0a5" }}>
                    <Icon name="check" size={16} />
                  </div>
                  <div style={{ fontSize: 12.5 }}>
                    Every <b>{money2(costs.annual / 12)}</b>/mo protects about <b style={{ color: "#35d0a5" }}>{money(costs.dataValue)}</b> — a <b>{costs.ratio.toLocaleString(undefined, { maximumFractionDigits: 0 })}×</b> return.
                  </div>
                </div>
              </div>
            )}
          </Card>
        </div>
      </div>
    </>
  );
}

function CostRow({ label, detail, value, muted }: { label: string; detail: string; value: string; muted?: boolean }) {
  return (
    <div className="spread" style={{ padding: "5px 0", opacity: muted ? 0.7 : 1 }}>
      <div>
        <div style={{ fontSize: 13, fontWeight: 600 }}>{label}</div>
        <div className="faint" style={{ fontSize: 11.5 }}>{detail}</div>
      </div>
      <div style={{ fontWeight: 600 }}>{value}{muted ? <span className="faint" style={{ fontSize: 11 }}> /mo</span> : ""}</div>
    </div>
  );
}
