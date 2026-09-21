import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, bytes, groupScope } from "../components/ui";
import { Icon } from "../components/Icon";
import { confirmDialog } from "../components/dialog";

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
interface LicensePlan { id: string; name: string; price_per_tb_month: number; min_tb: number; }
interface Plan {
  options: string[]; licensed_tb: number; used_bytes: number; used_tb: number;
  objects_total: number; value_breakdown: ValueRow[]; data_value_total: number;
  appliance_plan: { capacity_tb: number; qty: number }[];
  license_plan?: LicensePlan; min_tb?: number;
}
interface EstimateLine {
  key: string; label: string; quantity: number; unit_price_cents: number; amount_cents: number;
  included_qty: number; licensed_qty: number; kind: string; source: string;
}
interface Estimate {
  plan: string; currency: string; price_version: number;
  recurring_cents: number; one_time_cents: number; recurring_display: string;
  lines: EstimateLine[];
}
interface AddOnView {
  code: string; name: string; description: string; price_cents: number;
  pricing_model: string; billing_interval: string;
}
interface AddonsResp {
  plan: string;
  active: { code: string; quantity: number; name: string; price_cents: number }[];
  available: AddOnView[];
}

const iconName = (n: string) => (["cloud", "server", "key", "shield", "check", "database", "file"].includes(n) ? n : "database") as never;
function money(n: number): string {
  return "$" + Math.round(n).toLocaleString();
}
function money2(n: number): string {
  return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function addonPrice(a: AddOnView): string {
  const p = money2((a.price_cents || 0) / 100);
  switch (a.pricing_model) {
    case "per_user": return `${p} / user · mo`;
    case "per_member": return `${p} / member · mo`;
    case "per_cloud_tb":
    case "per_tb": return `${p} / TB · mo`;
    case "per_appliance": return `${p} / appliance · mo`;
    case "metered": return `${p} / unit`;
    default: return `${p} / mo`;
  }
}

export default function Onboarding() {
  const [pricing, setPricing] = useState<Pricing | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [bill, setBill] = useState<Estimate | null>(null);
  const [addons, setAddons] = useState<AddonsResp | null>(null);
  const [org, setOrg] = useState<{ name?: string; plan?: string; can_admin?: boolean } | null>(null);
  const [options, setOptions] = useState<Set<string>>(new Set());
  const [licensedTb, setLicensedTb] = useState(1);
  const [qty, setQty] = useState<Record<number, number>>({});
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [params] = useSearchParams();
  const orderingAppliance = params.get("order") === "appliance";
  const [orderApplied, setOrderApplied] = useState(false);

  useEffect(() => {
    api.get<Pricing>("/billing/pricing").then(setPricing).catch(() => {});
    api.get<{ name: string; plan: string; can_admin: boolean }>("/tenant").then(setOrg).catch(() => {});
    api.get<Plan>("/billing/plan").then((p) => {
      setPlan(p);
      setOptions(new Set(p.options));
      const min = p.license_plan?.min_tb || 0;
      setLicensedTb(Math.max(p.licensed_tb || 0, Math.ceil((p.used_tb || 0) * 10) / 10, min, 1));
      const q: Record<number, number> = {};
      for (const a of p.appliance_plan || []) q[a.capacity_tb] = a.qty;
      setQty(q);
    }).catch(() => {});
    api.get<AddonsResp>("/billing/addons").then(setAddons).catch(() => {});
  }, []);

  // Server-authoritative itemized bill (includes active add-ons) — recomputed for the
  // pending licensed amount + the pending Arkive Cloud selection (the cv-cloud tier
  // maps to the arkive_cloud consumption add-on, which is only persisted on Save, so
  // it's injected here as an override to preview live). A request token guards against
  // a stale slower response overwriting a newer one.
  const billReq = useRef(0);
  function previewBody(tb: number) {
    // The cv-cloud tier and each selected appliance map to add-ons that only persist
    // on Save — inject them here so the live bill reflects the pending selection.
    const applianceAddons = (pricing?.appliance_tiers || []).map((t) => ({
      code: `appliance_${t.capacity_tb}tb`, quantity: qty[t.capacity_tb] || 0,
    }));
    return {
      licensed_tb: tb,
      addons: [{ code: "arkive_cloud", quantity: options.has("cv-cloud") ? 1 : 0 }, ...applianceAddons],
    };
  }
  async function recomputeBill(tb: number) {
    const token = ++billReq.current;
    try {
      const r = await api.post<{ proposed: Estimate }>("/billing/estimate/preview", previewBody(tb));
      if (token === billReq.current) setBill(r.proposed);
    } catch { /* ignore */ }
  }
  async function reloadAddons() {
    try { setAddons(await api.get<AddonsResp>("/billing/addons")); } catch { /* ignore */ }
  }
  async function toggleAddon(code: string, on: boolean) {
    setSaved(false);
    try {
      if (on) await api.post("/billing/addons", { code });
      else await api.del(`/billing/addons/${code}`);
      await reloadAddons();
      await recomputeBill(licensedTb);
    } catch { /* best-effort; the bill reflects reality on next load */ }
  }
  useEffect(() => { if (plan) void recomputeBill(licensedTb); }, [licensedTb, plan, options, qty]);

  // Arriving from "Order a new appliance": enable the appliance destination and
  // pre-add one unit (incremental order) so the user just picks capacity + Save.
  useEffect(() => {
    if (!orderingAppliance || orderApplied || !pricing || !plan) return;
    setOptions((prev) => new Set([...prev, "appliance"]));
    setQty((cur) => {
      if (Object.values(cur).some((n) => n > 0)) return cur;
      const tiers = [...(pricing.appliance_tiers || [])].sort((a, b) => a.capacity_tb - b.capacity_tb);
      return tiers.length ? { ...cur, [tiers[0].capacity_tb]: 1 } : cur;
    });
    setOrderApplied(true);
    setSaved(false);
  }, [orderingAppliance, orderApplied, pricing, plan]);

  const usedTb = plan?.used_tb || 0;
  const minTb = plan?.license_plan?.min_tb || 0;
  const rate = plan?.license_plan?.price_per_tb_month ?? pricing?.protection_price_per_tb_month ?? 0;
  const maxTb = Math.max(20, Math.ceil(usedTb * 3), Math.ceil(minTb * 2));

  const costs = useMemo(() => {
    if (!pricing) return null;
    const billableTb = Math.max(licensedTb, minTb);
    const protection = billableTb * rate;
    const cloud = options.has("cv-cloud") ? usedTb * pricing.cloud_price_per_tb_month : 0;
    const thirdParty = options.has("customer-cloud") ? usedTb * pricing.s3_price_per_tb_month : 0;
    const applianceMonthly = (pricing.appliance_tiers || []).reduce((s, t) => s + (qty[t.capacity_tb] || 0) * t.monthly, 0);
    const applianceSetup = (pricing.appliance_tiers || []).reduce((s, t) => s + (qty[t.capacity_tb] || 0) * t.setup, 0);
    const totalMonthly = protection + cloud + applianceMonthly;
    const annual = totalMonthly * 12;
    const dataValue = plan?.data_value_total || 0;
    const ratio = annual > 0 ? dataValue / annual : null;
    return { protection, cloud, thirdParty, applianceMonthly, applianceSetup, totalMonthly, annual, dataValue, ratio, billableTb };
  }, [pricing, options, licensedTb, qty, usedTb, plan, minTb, rate]);

  function toggle(id: string) {
    setSaved(false);
    setOptions((cur) => { const n = new Set(cur); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }
  function bump(cap: number, d: number) {
    setSaved(false);
    setQty((cur) => ({ ...cur, [cap]: Math.max(0, (cur[cap] || 0) + d) }));
  }

  async function save() {
    // Unsubscribing from Arkive Cloud schedules permanent deletion of stored data.
    const hadCloud = (plan?.options || []).includes("cv-cloud");
    if (hadCloud && !options.has("cv-cloud")) {
      const ok1 = await confirmDialog({
        title: "Unsubscribe from Arkive Cloud?", tone: "danger", confirmLabel: "Continue",
        message: "Everything you've stored in Arkive Cloud will be scheduled for permanent "
          + "deletion. You have 30 days to re-subscribe before it is erased \u2014 after that it "
          + "is non-recoverable.",
      });
      if (!ok1) { setOptions((cur) => new Set([...cur, "cv-cloud"])); return; }
      const ok2 = await confirmDialog({
        title: "This cannot be undone", tone: "danger", confirmLabel: "Unsubscribe & schedule deletion",
        message: "In 30 days we automatically purge all of your Arkive Cloud data. Re-subscribing "
          + "before then cancels the deletion; once purged, the data cannot be recovered by anyone.",
      });
      if (!ok2) { setOptions((cur) => new Set([...cur, "cv-cloud"])); return; }
    }
    // Server-authoritative preview of the change — confirm the new recurring total
    // and the delta before committing (prices come from the server, never the UI).
    try {
      const prev = await api.post<{ proposed: Estimate; delta_cents: number; one_time_cents: number }>(
        "/billing/estimate/preview", previewBody(licensedTb));
      const dollars = (c: number) => "$" + ((c || 0) / 100).toFixed(2);
      const delta = prev.delta_cents || 0;
      const deltaLine = delta === 0 ? "No change to your recurring charge."
        : delta > 0 ? `That's ${dollars(delta)}/mo more than today.`
          : `That's ${dollars(-delta)}/mo less than today.`;
      const oneTime = prev.one_time_cents ? ` A one-time charge of ${dollars(prev.one_time_cents)} applies.` : "";
      // Keep the on-screen summary in lockstep with the confirmed figure.
      billReq.current++;
      setBill(prev.proposed);
      const ok = await confirmDialog({
        title: "Confirm your plan", confirmLabel: "Confirm & save",
        message: `Your new recurring charge will be ${prev.proposed.recurring_display}/mo. ${deltaLine}${oneTime}`,
      });
      if (!ok) return;
    } catch { /* preview is best-effort — don't block saving if it fails */ }
    setSaving(true); setSaved(false);
    try {
      const appliance_plan = Object.entries(qty).filter(([, q]) => q > 0)
        .map(([cap, q]) => ({ capacity_tb: Number(cap), qty: q }));
      const updated = await api.put<Plan>("/billing/plan", {
        options: [...options], licensed_tb: licensedTb, appliance_plan,
      });
      setPlan(updated); setSaved(true);
      void reloadAddons();
      void recomputeBill(licensedTb);
    } catch { /* surfaced via disabled state */ }
    setSaving(false);
  }

  if (!pricing || !plan || !costs) return <Card><div className="muted">Loading plan…</div></Card>;

  const overLicensed = usedTb > licensedTb;
  const activeAddonCodes = new Set((addons?.active || []).map((a) => a.code));
  const optionalAddons = (addons?.available || []).filter((a) => a.code !== "arkive_cloud" && !a.code.startsWith("appliance_"));
  // Value/return is measured against the ACTUAL server bill (all lines incl. add-ons).
  const billMonthly = (bill?.recurring_cents || 0) / 100;
  const billRatio = billMonthly > 0 ? (costs.dataValue / (billMonthly * 12)) : null;

  return (
    <>
      <div className="stack" style={{ marginBottom: 18 }}>
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <Icon name="shield" size={16} />
          <h2 style={{ margin: 0 }}>Protection Setup</h2>
          {org?.name && groupScope(org?.plan) && (
            <Pill tone="info">{groupScope(org?.plan)!.value === "family" ? "Family-wide" : "Organization-wide"}</Pill>
          )}
        </div>
        <div className="muted" style={{ fontSize: 13 }}>
          {org?.name && groupScope(org?.plan)
            ? <>These settings protect <b>{org.name}</b> — they apply across your whole {groupScope(org?.plan)!.noun} and every member's vaults. </>
            : null}
          Choose how your data is protected, how much you protect, and see your monthly cost.
          Your selections control which storage destinations are available across the platform.
        </div>
      </div>

      {orderingAppliance && (
        <Card style={{ marginBottom: 16, borderColor: "var(--brand)" }}>
          <div className="row" style={{ gap: 12, alignItems: "flex-start" }}>
            <Icon name="server" size={18} />
            <div>
              <div style={{ fontWeight: 700 }}>Ordering a new Arkive appliance</div>
              <div className="faint" style={{ fontSize: 12.5, marginTop: 2 }}>
                We've added an appliance to your plan below — pick the capacity and quantity, then <b>Save</b> to place
                the order. You'll be billed the monthly lease plus a one-time setup fee and we'll ship your appliance;
                your existing protection isn't changed. Once it arrives, plug it in and it appears here automatically.
              </div>
            </div>
          </div>
        </Card>
      )}

      <div className="grid" style={{ gridTemplateColumns: "1fr 340px", gap: 16, alignItems: "start" }}>
        {/* -------- Left: choices -------- */}
        <div className="stack" style={{ gap: 16 }}>
          <Card style={{ order: 2 }}>
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

          {/* Protection level slider — shown first (flex order) */}
          <Card style={{ order: 1 }}>
            <div className="spread" style={{ marginBottom: 4 }}>
              <h3 style={{ margin: 0 }}>Protection level</h3>
              <span className="faint" style={{ fontSize: 12 }}>{money2(rate)} / TB · month</span>
            </div>
            <div className="muted" style={{ fontSize: 12.5, marginBottom: 16 }}>
              {plan.license_plan
                ? <>On the <b>{plan.license_plan.name}</b> plan — {money2(rate)}/TB·mo{minTb > 0 ? `, ${minTb} TB minimum` : ""}. You can raise your licensed data any time as you protect more.</>
                : <>How much data protection you license. You can raise it any time as you protect more.</>}
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
            <input type="range" min={Math.max(0.5, minTb)} max={maxTb} step={0.5} value={Math.min(Math.max(licensedTb, minTb), maxTb)}
                   onChange={(e) => { setSaved(false); setLicensedTb(Math.max(Number(e.target.value), minTb)); }}
                   style={{ width: "100%", accentColor: overLicensed ? "#f2545b" : "#4f7cff" }} />
            <div className="spread faint" style={{ fontSize: 11 }}>
              <span>{Math.max(0.5, minTb)} TB{minTb > 0 ? " (min)" : ""}</span><span>{maxTb} TB</span>
            </div>
            {overLicensed && (
              <div style={{ marginTop: 8 }}>
                <Pill tone="warn">You're using more than you've licensed — raise the slider</Pill>
              </div>
            )}
          </Card>

          {optionalAddons.length > 0 && (
            <Card style={{ order: 3 }}>
              <div className="spread" style={{ marginBottom: 4 }}>
                <h3 style={{ margin: 0 }}>Add-ons</h3>
                <span className="faint" style={{ fontSize: 12 }}>Optional capabilities</span>
              </div>
              <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
                Enhance your protection — each is billed on top of your plan and shows in your bill on the right.
              </div>
              <div className="stack" style={{ gap: 10 }}>
                {optionalAddons.map((a) => {
                  const on = activeAddonCodes.has(a.code);
                  return (
                    <div key={a.code} onClick={() => void toggleAddon(a.code, !on)}
                         style={{ cursor: "pointer", border: `1.5px solid ${on ? "#4f7cff" : "var(--border-soft)"}`,
                                  borderRadius: 12, padding: 14, background: on ? "#4f7cff12" : "transparent", transition: "all .12s" }}>
                      <div className="spread" style={{ alignItems: "flex-start" }}>
                        <div style={{ fontWeight: 700, fontSize: 14 }}>{a.name}</div>
                        <div style={{ width: 20, height: 20, borderRadius: 6, border: `1.5px solid ${on ? "#4f7cff" : "var(--border-soft)"}`,
                                      background: on ? "#4f7cff" : "transparent", display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0 }}>
                          {on && <Icon name="check" size={13} />}
                        </div>
                      </div>
                      {a.description && <div className="faint" style={{ fontSize: 12.5, marginTop: 2 }}>{a.description}</div>}
                      <div style={{ marginTop: 8, fontSize: 12.5, fontWeight: 600, color: "#4f7cff" }}>{addonPrice(a)}</div>
                    </div>
                  );
                })}
              </div>
            </Card>
          )}
        </div>

        {/* -------- Right: cost + value summary -------- */}
        <div className="stack" style={{ gap: 16, position: "sticky", top: 16 }}>
          <Card>
            <div className="spread" style={{ marginBottom: 4 }}>
              <h3 style={{ margin: 0 }}>Your monthly cost</h3>
              {bill && <span className="faint" style={{ fontSize: 11 }}>plan v{bill.price_version}</span>}
            </div>
            <div className="muted" style={{ fontSize: 11.5, marginBottom: 12 }}>
              Itemized and priced by us — the source of truth for your invoice. Includes your plan, usage and add-ons.
            </div>
            {!bill && <div className="muted" style={{ fontSize: 12.5 }}>Calculating…</div>}
            {bill && (
              <>
                <div className="stack" style={{ gap: 8 }}>
                  {bill.lines.filter((l) => l.kind !== "one_time").map((l) => (
                    <div key={l.key} className="spread" style={{ fontSize: 12.5, alignItems: "baseline" }}>
                      <span>
                        {l.label}
                        {l.licensed_qty > 0 && (
                          <span className="faint" style={{ fontSize: 11 }}>
                            {" "}· {l.included_qty > 0 ? `${l.included_qty} incl, ` : ""}{l.quantity} billable
                          </span>
                        )}
                      </span>
                      <span style={{ fontWeight: 600 }}>{money2(l.amount_cents / 100)}</span>
                    </div>
                  ))}
                  {bill.lines.filter((l) => l.kind !== "one_time").length === 0 && (
                    <div className="faint" style={{ fontSize: 12 }}>No recurring charges yet.</div>
                  )}
                </div>
                <div className="spread" style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
                  <div style={{ fontWeight: 700 }}>Total</div>
                  <div style={{ fontSize: 24, fontWeight: 800 }}>{bill.recurring_display}<span className="faint" style={{ fontSize: 13, fontWeight: 500 }}> /mo</span></div>
                </div>
                {bill.one_time_cents > 0 && (
                  <div className="spread faint" style={{ fontSize: 12, marginTop: 6 }}>
                    <span>+ Setup (one-time)</span><span style={{ fontWeight: 600 }}>{money2(bill.one_time_cents / 100)}</span>
                  </div>
                )}
              </>
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
            {billRatio != null && billRatio > 0 && (
              <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
                <div className="row" style={{ gap: 8, alignItems: "center" }}>
                  <div className="result-icon" style={{ width: 32, height: 32, background: "var(--inset)", color: "#35d0a5" }}>
                    <Icon name="check" size={16} />
                  </div>
                  <div style={{ fontSize: 12.5 }}>
                    Every <b>{money2(billMonthly)}</b>/mo protects about <b style={{ color: "#35d0a5" }}>{money(costs.dataValue)}</b> — a <b>{billRatio.toLocaleString(undefined, { maximumFractionDigits: 0 })}×</b> return.
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
