import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api, getToken } from "../api";
import { Card, Pill, Stat, bytes, timeAgo, fmtAbsolute, userTimezone } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { DestIcon } from "../components/DestIcon";
import { FilterBar } from "../components/FilterBar";
import { promptDialog, formDialog, confirmDialog, notify } from "../components/dialog";
import { Ring, Sparkline, AreaChart } from "../components/charts";
import { humanizeAction } from "../components/format";
import { VersionPill, ProductionVersion } from "../components/VersionBadge";
import { JobKindBadge } from "../components/JobKindBadge";

// IANA timezone names for the config editors. Uses the browser's built-in list
// (Intl.supportedValuesOf) with a small fallback for older engines.
let _TZ_CACHE: string[] | null = null;
function tzList(): string[] {
  if (_TZ_CACHE) return _TZ_CACHE;
  try {
    const f = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf;
    if (typeof f === "function") { _TZ_CACHE = f("timeZone"); return _TZ_CACHE; }
  } catch { /* ignore */ }
  _TZ_CACHE = ["UTC", "America/New_York", "America/Chicago", "America/Denver",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Sao_Paulo",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Moscow", "Africa/Johannesburg",
    "Asia/Dubai", "Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "Australia/Sydney"];
  return _TZ_CACHE;
}
import { RichTextEditor } from "../components/RichTextEditor";
import { toEditorHtml } from "../md";

export interface AdminSection { key: string; label: string; icon: IconName; group: string; }

// ---- Feature flags (generic, catalog-driven) --------------------------------
// The flag catalog (name/label/default) comes from GET /admin/feature-flags so new
// flags surface automatically in the user/tenant edit dialogs without code changes.
interface FlagDef { name: string; label: string; default: boolean }
let _flagCatalog: FlagDef[] | null = null;
async function flagCatalog(): Promise<FlagDef[]> {
  if (!_flagCatalog) {
    try { _flagCatalog = await api.get<FlagDef[]>("/admin/feature-flags"); }
    catch { _flagCatalog = []; }
  }
  return _flagCatalog;
}
// Build a "Feature flags" section of form fields. User scope adds an Inherit option
// (fall back to the tenant/default); tenant scope is a straight allow/block override.
function flagFields(cat: FlagDef[], current: Record<string, boolean> | undefined, scope: "user" | "tenant") {
  return cat.map((fl) => {
    const cur = current?.[fl.name];
    const options = scope === "user"
      ? [
          { label: `Inherit (${fl.default ? "allowed" : "blocked"} by default)`, value: "inherit" },
          { label: "Allowed", value: "true" },
          { label: "Blocked", value: "false" },
        ]
      : [
          { label: "Allowed", value: "true" },
          { label: "Blocked — all users (legal hold)", value: "false" },
        ];
    const defaultValue = cur === true ? "true" : cur === false ? "false"
      : scope === "user" ? "inherit" : (fl.default ? "true" : "false");
    return { name: `flag_${fl.name}`, label: fl.label, defaultValue, options, section: "Feature flags" };
  });
}
// Pull flag_* keys out of a submitted form into a feature_flags patch. null = inherit/unset.
function extractFlags(r: Record<string, string>, cat: FlagDef[]): Record<string, boolean | null> {
  const out: Record<string, boolean | null> = {};
  for (const fl of cat) {
    const key = `flag_${fl.name}`;
    const v = r[key];
    delete r[key];
    if (v === undefined) continue;
    out[fl.name] = v === "inherit" ? null : v === "true";
  }
  return out;
}

// Shared user-edit dialog (used from a tenant's Users list AND the global Users tab).
// Returns true when a change was saved.
async function editUserDialog(u: any, isShared: boolean): Promise<boolean> {
  const fields: any[] = isShared
    ? [
        { name: "first_name", label: "First name", defaultValue: u.first_name || "" },
        { name: "last_name", label: "Last name", defaultValue: u.last_name || "" },
        { name: "phone", label: "Phone", defaultValue: u.phone || "" },
      ]
    : [
        { name: "display_name", label: "Name", defaultValue: u.display_name },
        { name: "role", label: "Role", defaultValue: u.role,
          options: ["owner", "security-admin", "member", "support-admin"].map((v) => ({ label: v, value: v })) },
      ];
  fields.push({ name: "status", label: "Status", defaultValue: u.status,
    options: ["active", "suspended"].map((v) => ({ label: v, value: v })) });
  // Platform admin is a special per-user capability, NOT an org role: it grants
  // access to the cross-tenant backend admin console.
  fields.push({ name: "is_platform_admin", label: "Platform administrator",
    defaultValue: u.is_platform_admin ? "true" : "false",
    options: [{ label: "No", value: "false" },
      { label: "Yes — full backend admin console", value: "true" }],
    section: "Platform access",
    hint: "Grants cross-tenant access to the backend admin console. A special capability, separate from the organization role." });
  fields.push({ name: "notification_emails", label: "Additional notification emails",
    type: "textarea", defaultValue: (u.notification_emails || []).join(", "),
    hint: "Extra addresses that also receive this account's email notifications (comma-separated). Never used for login." });
  // Per-account feature flags. A tenant-level block wins regardless of this.
  fields.push(...flagFields(await flagCatalog(), u.feature_flags, "user"));
  const r = await formDialog({ title: `Edit ${u.email}`, confirmLabel: "Save", fields, wide: true });
  if (!r) return false;
  const flags = extractFlags(r, await flagCatalog());
  // Parse the comma/space/semicolon-separated list into the array the API expects.
  r.notification_emails = String(r.notification_emails ?? "")
    .split(/[\s,;]+/).map((s) => s.trim().toLowerCase()).filter(Boolean) as any;
  r.is_platform_admin = (r.is_platform_admin === "true") as any;
  await api.put(`/admin/users/${u.id}`, r);
  if (Object.keys(flags).length) {
    await api.put(`/admin/users/${u.id}/flags`, { feature_flags: flags });
  }
  return true;
}

// Generate (refresh) a user's digital-footprint insights report on demand.
async function generateInsightsFor(u: any): Promise<void> {
  try {
    const r = await api.post<{ status: string; queued?: boolean; message?: string; object_count?: number; card_count?: number }>(
      `/admin/users/${u.id}/insights`, {});
    if (r.queued || r.status === "pending") {
      notify({ message: r.message || `Insights requested for ${u.email} — the tenant's node will report back shortly.`, tone: "info" });
      return;
    }
    notify({
      message: r.status === "ready"
        ? `Insights generated for ${u.email}: ${r.card_count ?? 0} card(s) from ${(r.object_count ?? 0).toLocaleString()} objects.`
        : `Not enough data yet to build insights for ${u.email}.`,
      tone: r.status === "ready" ? "info" : "warn",
    });
  } catch (e) {
    notify({ message: (e as Error)?.message || "Could not generate insights", tone: "danger" });
  }
}

// Left-nav sections for the admin console (M365-style, grouped).
export const ADMIN_SECTIONS: AdminSection[] = [
  { key: "overview", label: "Overview", icon: "grid", group: "" },
  { key: "tenants", label: "Tenants", icon: "user", group: "Customers" },
  { key: "users", label: "Users", icon: "user", group: "Customers" },
  { key: "reports", label: "Reports", icon: "activity", group: "Customers" },
  { key: "customer-analytics", label: "Customer Analytics", icon: "insights", group: "Customers" },
  { key: "billing", label: "Billing", icon: "credit-card", group: "Customers" },
  { key: "config-objects", label: "Configuration objects", icon: "key", group: "Configurations" },
  { key: "config-profiles", label: "Configuration profiles", icon: "puzzle", group: "Configurations" },
  { key: "sources", label: "Sources", icon: "link", group: "Configurations" },
  { key: "integrations", label: "Integrations", icon: "puzzle", group: "Configurations" },
  { key: "service-objects", label: "Service objects", icon: "mail", group: "Configurations" },
  { key: "pricing", label: "Pricing", icon: "database", group: "Configurations" },
  { key: "website", label: "Website", icon: "grid", group: "Configurations" },
  { key: "nodes", label: "Nodes", icon: "server", group: "Infrastructure" },
  { key: "storage-usage", label: "Arkive Cloud", icon: "database", group: "Infrastructure" },
  { key: "backups", label: "Backups", icon: "shield", group: "Infrastructure" },
  { key: "fleet", label: "Appliance fleet", icon: "server", group: "Infrastructure" },
  { key: "crypto", label: "Crypto", icon: "lock", group: "Infrastructure" },
  { key: "updates", label: "Updates", icon: "clock", group: "Infrastructure" },
  { key: "debug", label: "Debug", icon: "activity", group: "Infrastructure" },
  { key: "audit", label: "Audit log", icon: "shield", group: "Infrastructure" },
  { key: "support-tickets", label: "Support tickets", icon: "help", group: "Support" },
  { key: "support-docs", label: "Documentation", icon: "file", group: "Support" },
];

export default function Admin() {
  const { section } = useParams();
  const s = section || "overview";
  return (
    <>
      {s === "overview" && <Overview />}
      {s === "tenants" && <Tenants />}
      {s === "users" && <Users />}
      {s === "reports" && <Reports />}
      {s === "customer-analytics" && <CustomerAnalytics />}
      {s === "billing" && <BillingAdmin />}
      {s === "nodes" && <Nodes />}
      {s === "storage-usage" && <StorageUsageAdmin />}
      {s === "backups" && <BackupsAdmin />}
      {s === "config-objects" && <ConfigObjectsAdmin />}
      {s === "config-profiles" && <ConfigProfiles />}
      {s === "sources" && <SourcesAdmin />}
      {s === "integrations" && <IntegrationsAdmin />}
      {s === "service-objects" && <><ServiceObjectsAdmin /><EmailAdmin /></>}
      {s === "fleet" && <Fleet />}
      {s === "pricing" && <Pricing />}
      {s === "website" && <WebsiteCMS />}
      {s === "crypto" && <Crypto />}
      {s === "audit" && <Audit />}
      {s === "updates" && <Updates />}
      {s === "debug" && <DebugAdmin />}
      {s === "support-tickets" && <SupportTicketsAdmin />}
      {s === "support-docs" && <SupportDocsAdmin />}
    </>
  );
}

function Overview() {
  const [o, setO] = useState<any>(null);
  useEffect(() => { api.get("/admin/overview").then(setO).catch(() => {}); }, []);
  if (!o) return <Card><div className="muted">Loading…</div></Card>;
  return (
    <>
      <div className="grid grid-4">
        <Stat label="Tenants" value={o.tenants} />
        <Stat label="Users" value={o.users} />
        <Stat label="Nodes" value={o.nodes ?? 1} />
        <Stat label="Linked sources" value={o.connectors} />
      </div>
      <div className="grid grid-3" style={{ marginTop: 16 }}>
        <Stat label="Recovery points" value={o.snapshots} hint={`${o.recoverable_snapshots} recoverable`} />
        <Card className="stat">
          <div className="value">{o.pq_available ? "Active" : "Fallback"}</div>
          <div className="label">Post-quantum crypto</div>
          <Pill tone={o.pq_available ? "ok" : "warn"}>{o.pq_available ? "liboqs ML-KEM/ML-DSA" : "dev fallback"}</Pill>
        </Card>
        <Card className="stat">
          <div className="value">{o.audit_chain_valid ? "Valid" : "Broken"}</div>
          <div className="label">Audit ledger integrity</div>
          <Pill tone={o.audit_chain_valid ? "ok" : "danger"}>hash-chained</Pill>
        </Card>
      </div>
    </>
  );
}

const TENANT_TYPE_OPTS = [
  { label: "Shared — personal accounts (no organization)", value: "shared" },
  { label: "Dedicated — family / business (full org management)", value: "dedicated" },
  { label: "Restricted — high-value / enterprise (elevated security)", value: "restricted" },
  { label: "Internal — Arkive operations (employees / platform admin)", value: "internal" },
];
const TENANT_TYPE_LABEL: Record<string, string> = {
  shared: "Shared", dedicated: "Dedicated", restricted: "Restricted", internal: "Internal",
};
const TENANT_TYPE_TONE: Record<string, "ok" | "info" | "warn" | "danger"> = {
  shared: "warn", dedicated: "info", restricted: "danger", internal: "ok",
};

async function nodeOptions(): Promise<{ label: string; value: string }[]> {
  const base = [{ label: "Control plane (default — processed in-box)", value: "" }];
  try {
    const ns = await api.get<{ id: string; name: string; role: string }[]>("/admin/nodes");
    const workers = ns.filter((n) => n.role === "customer-tenant");
    return [...base, ...workers.map((n) => ({ label: `${n.name} (${n.role})`, value: n.id }))];
  } catch { return base; }
}

function Tenants() {
  const [rows, setRows] = useState<any[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  const [q, setQ] = useState("");
  const [typeF, setTypeF] = useState("");
  const [statusF, setStatusF] = useState("");
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }
  async function load() { try { setRows(await api.get<any[]>("/admin/tenants")); } catch { /* ignore */ } }
  useEffect(() => { void load(); }, []);

  async function newTenant() {
    const nodeOpts = await nodeOptions();
    const base = await formDialog({
      title: "New tenant",
      message: "Shared = a pool of isolated personal accounts (each self-manages a Personal plan). Dedicated / Restricted = a managed organization. Internal = Arkive operations.",
      confirmLabel: "Continue",
      fields: [
        { name: "name", label: "Name", required: true, placeholder: "e.g. Personal Accounts — US" },
        { name: "tenant_type", label: "Tenant type", defaultValue: "dedicated", options: TENANT_TYPE_OPTS },
        { name: "key_ownership_model", label: "Key ownership", defaultValue: "customer-managed",
          options: [{ label: "Customer-managed", value: "customer-managed" }, { label: "Zero-knowledge", value: "zero-knowledge" }] },
        { name: "node_id", label: "Processing node", defaultValue: "", options: nodeOpts },
      ],
    });
    if (!base) return;

    // Shared tenants pool isolated personal accounts — no org-level license plan,
    // licensed amount, or single owner. Each account self-manages a Personal plan.
    if (base.tenant_type === "shared") {
      try {
        await api.post("/admin/tenants", {
          name: base.name, tenant_type: "shared", plan: "personal",
          key_ownership_model: base.key_ownership_model, node_id: base.node_id,
        });
        flash("Shared tenant created"); await load();
      } catch { flash("Could not create tenant"); }
      return;
    }

    // Managed org (dedicated / restricted / internal): collect the license plan,
    // licensed data, and an optional owner account.
    let planOpts = [{ label: "business", value: "business" }];
    try {
      const pr = await api.get<any>("/admin/pricing");
      if (pr.license_plans?.length) planOpts = pr.license_plans.map((pl: any) =>
        ({ label: `${pl.name} — $${pl.price_per_tb_month}/TB·mo, min ${pl.min_tb}TB`, value: pl.id }));
    } catch { /* fall back to default */ }
    const r = await formDialog({
      title: `New ${TENANT_TYPE_LABEL[base.tenant_type] || base.tenant_type} tenant`,
      confirmLabel: "Create tenant",
      fields: [
        { name: "plan", label: "License plan", defaultValue: planOpts[0]?.value, options: planOpts },
        { name: "licensed_tb", label: "Licensed data (TB)", defaultValue: "1" },
        { name: "owner_email", label: "Owner email (optional)" },
        { name: "owner_name", label: "Owner name (optional)" },
      ],
    });
    if (!r) return;
    try {
      await api.post("/admin/tenants", {
        name: base.name, tenant_type: base.tenant_type,
        key_ownership_model: base.key_ownership_model, node_id: base.node_id,
        plan: r.plan, licensed_tb: Number(r.licensed_tb) || 0,
        owner_email: r.owner_email, owner_name: r.owner_name,
      });
      flash("Tenant created"); await load();
    } catch { flash("Could not create tenant"); }
  }

  if (sel) return <TenantDetail id={sel} onBack={() => { setSel(null); void load(); }} />;

  const ql = q.trim().toLowerCase();
  const filtered = rows.filter((t) =>
    (!ql || (t.name || "").toLowerCase().includes(ql) || (t.node || "").toLowerCase().includes(ql))
    && (!typeF || t.tenant_type === typeF)
    && (!statusF || t.status === statusF));

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Tenants</h3>
        <button className="btn primary sm" onClick={newTenant}><Icon name="user" size={14} /> New tenant</button>
      </div>
      <Card style={{ marginBottom: 14 }}>
        <FilterBar
          query={q} onQuery={setQ} placeholder="Search tenants, node…"
          filters={[
            { label: "Type", value: typeF, onChange: setTypeF, options: [
              { label: "All types", value: "" },
              ...["shared", "dedicated", "restricted", "internal"].map((v) => ({ label: TENANT_TYPE_LABEL[v], value: v })),
            ] },
            { label: "Status", value: statusF, onChange: setStatusF, options: [
              { label: "Any status", value: "" },
              ...["active", "suspended", "trial"].map((v) => ({ label: v, value: v })),
            ] },
          ]}
        />
      </Card>
      <Card>
        <table className="table">
          <thead><tr><th>Tenant</th><th>Type</th><th>Node</th><th>Plan</th><th>Accounts</th><th>Sources</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {filtered.map((t) => (
              <tr key={t.id} style={{ cursor: "pointer" }} onClick={() => setSel(t.id)}>
                <td style={{ fontWeight: 600 }}>{t.name}</td>
                <td><Pill tone={TENANT_TYPE_TONE[t.tenant_type] || "info"}>{TENANT_TYPE_LABEL[t.tenant_type] || t.tenant_type}</Pill></td>
                <td className="faint">{t.node || "Control plane"}</td>
                <td>{t.tenant_type === "shared"
                  ? <span className="faint" style={{ fontSize: 12 }}>per-account</span>
                  : <Pill tone="info">{t.plan}</Pill>}</td>
                <td>{t.users}</td>
                <td>{t.sources ?? 0}</td>
                <td><Pill tone={t.status === "active" ? "ok" : "warn"}>{t.status}</Pill></td>
                <td className="faint" style={{ textAlign: "right" }}>Manage →</td>
              </tr>
            ))}
            {filtered.length === 0 && <tr><td colSpan={8} className="muted">No tenants match.</td></tr>}
          </tbody>
        </table>
      </Card>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

// Global, filterable directory of every account across all tenants.
function Users() {
  const [rows, setRows] = useState<any[]>([]);
  const [tenants, setTenants] = useState<any[]>([]);
  const [plans, setPlans] = useState<any[]>([]);
  const [q, setQ] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [plan, setPlan] = useState("");
  const [statusF, setStatusF] = useState("");
  const [typeF, setTypeF] = useState("");
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 }>({ key: "email", dir: 1 });
  const [loading, setLoading] = useState(false);
  const [sel, setSel] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    const qs = new URLSearchParams();
    if (q.trim()) qs.set("q", q.trim());
    if (tenantId) qs.set("tenant_id", tenantId);
    if (plan) qs.set("plan", plan);
    if (statusF) qs.set("status", statusF);
    if (typeF) qs.set("tenant_type", typeF);
    try { setRows(await api.get<any[]>(`/admin/users?${qs.toString()}`)); } catch { /* ignore */ }
    finally { setLoading(false); }
  }
  useEffect(() => {
    api.get<any[]>("/admin/tenants").then(setTenants).catch(() => {});
    api.get<any>("/admin/pricing").then((p) => setPlans(p.license_plans || [])).catch(() => {});
  }, []);
  useEffect(() => { const h = setTimeout(load, 250); return () => clearTimeout(h); },
    [q, tenantId, plan, statusF, typeF]);

  const money = (n: number) => "$" + (Math.round((n || 0) * 100) / 100)
    .toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  const sorted = [...rows].sort((a, b) => {
    const k = sort.key;
    let av: string | number, bv: string | number;
    if (k === "usage") { av = a.usage_bytes || 0; bv = b.usage_bytes || 0; }
    else if (k === "billing") { av = a.billing_monthly || 0; bv = b.billing_monthly || 0; }
    else if (k === "last_login") { av = a.last_login_at || ""; bv = b.last_login_at || ""; }
    else if (k === "plan") { av = a.plan?.name || ""; bv = b.plan?.name || ""; }
    else if (k === "name") { av = a.full_name || a.display_name || ""; bv = b.full_name || b.display_name || ""; }
    else { av = (a[k] ?? "").toString(); bv = (b[k] ?? "").toString(); }
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * sort.dir;
    return String(av).localeCompare(String(bv)) * sort.dir;
  });
  function th(key: string, label: string, align?: "right") {
    const active = sort.key === key;
    return (
      <th style={{ cursor: "pointer", textAlign: align, whiteSpace: "nowrap" }}
          onClick={() => setSort((s) => ({ key, dir: s.key === key && s.dir === 1 ? -1 : 1 }))}>
        {label}{active ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
      </th>
    );
  }
  const totalBilling = rows.reduce((s, u) => s + (u.billing_monthly || 0), 0);

  if (sel) return <UserDetail id={sel} backLabel="Users" onBack={() => { setSel(null); void load(); }} />;

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Users</h3>
        <span className="faint" style={{ fontSize: 12 }}>
          {rows.length} account{rows.length === 1 ? "" : "s"} · {money(totalBilling)}/mo
        </span>
      </div>
      <Card style={{ marginBottom: 14 }}>
        <FilterBar
          query={q} onQuery={setQ} placeholder="Search name, email, phone…"
          filters={[
            { label: "Tenant", value: tenantId, onChange: setTenantId, options: [
              { label: "All tenants", value: "" },
              ...tenants.map((t) => ({ label: t.name, value: t.id })),
            ] },
            { label: "Type", value: typeF, onChange: setTypeF, options: [
              { label: "All types", value: "" },
              ...["shared", "dedicated", "restricted", "internal"].map((v) => ({ label: TENANT_TYPE_LABEL[v], value: v })),
            ] },
            { label: "Plan", value: plan, onChange: setPlan, options: [
              { label: "All plans", value: "" },
              ...plans.map((p) => ({ label: p.name, value: p.id })),
            ] },
            { label: "Status", value: statusF, onChange: setStatusF, options: [
              { label: "Any status", value: "" },
              ...["active", "suspended"].map((v) => ({ label: v, value: v })),
            ] },
          ]}
        />
      </Card>
      <Card>
        <table className="table">
          <thead><tr>
            {th("name", "Name")}
            {th("email", "Email")}
            <th>Phone</th>
            {th("tenant_name", "Tenant")}
            {th("plan", "Plan")}
            {th("last_login", "Last login")}
            {th("usage", "Usage", "right")}
            {th("billing", "Billing", "right")}
            {th("status", "Status")}
            <th></th>
          </tr></thead>
          <tbody>
            {sorted.map((u) => (
              <tr key={u.id} style={{ cursor: "pointer" }} onClick={() => setSel(u.id)}>
                <td>
                  <div style={{ fontWeight: 600 }}>{u.full_name || u.display_name || u.email}</div>
                  {u.is_platform_admin && <div className="faint" style={{ fontSize: 11 }}>platform admin</div>}
                </td>
                <td className="faint" style={{ fontSize: 12 }}>{u.email}</td>
                <td className="faint" style={{ fontSize: 12 }}>{u.phone || "—"}</td>
                <td>
                  <div style={{ fontSize: 12.5 }}>{u.tenant_name || "—"}</div>
                  <Pill tone={TENANT_TYPE_TONE[u.tenant_type] || "info"}>{TENANT_TYPE_LABEL[u.tenant_type] || u.tenant_type}</Pill>
                </td>
                <td><Pill tone="info">{u.plan?.name || "—"}</Pill></td>
                <td className="faint" style={{ fontSize: 12 }}>{u.last_login_at ? timeAgo(u.last_login_at) : "Never"}</td>
                <td style={{ textAlign: "right" }}>{bytes(u.usage_bytes || 0)}</td>
                <td style={{ textAlign: "right" }}>{money(u.billing_monthly || 0)}/mo</td>
                <td><Pill tone={u.status === "active" ? "ok" : "warn"}>{u.status}</Pill></td>
                <td className="faint" style={{ textAlign: "right" }}>Manage →</td>
              </tr>
            ))}
            {sorted.length === 0 && <tr><td colSpan={10} className="muted">{loading ? "Loading…" : "No users match."}</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  );
}

// Full-page account detail — profile, plan/billing, storage, sources, activity
// and the management actions (Edit/Reset/Delete/Insights). Extensible: new
// sections are just more <Card>s. `backLabel` powers the standard "← back" link
// (Users list, or the parent tenant when drilled in from a tenant).
function AdminUserNotifs({ user, onSaved }: { user: any; onSaved: () => void }) {
  const types = user.notification_types || [];
  const [prefs, setPrefs] = useState<Record<string, boolean>>(user.notification_prefs || {});
  if (!types.length) return null;
  async function toggle(key: string) {
    const prev = prefs;
    const next = { ...prefs, [key]: !prefs[key] };
    setPrefs(next);
    try { await api.put(`/admin/users/${user.id}`, { notification_prefs: { [key]: next[key] } }); onSaved(); }
    catch (e: any) { void notify({ message: e.message || "Could not save", tone: "danger" }); setPrefs(prev); }
  }
  return (
    <Card style={{ marginBottom: 16 }}>
      <h3 style={{ margin: "0 0 4px" }}>Email notifications</h3>
      <div className="muted" style={{ fontSize: 12.5, marginBottom: 4 }}>Control which emails this account receives.</div>
      <div className="stack" style={{ gap: 0 }}>
        {types.map((t: any) => (
          <div key={t.key} className="spread"
               style={{ padding: "11px 0", borderTop: "1px solid var(--border-soft)", alignItems: "center", gap: 12 }}>
            <div className="row" style={{ gap: 10, alignItems: "center", minWidth: 0 }}>
              <Icon name={(t.icon || "mail") as IconName} size={15} />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 13.5 }}>{t.label}</div>
                <div className="faint" style={{ fontSize: 12 }}>{t.desc}</div>
              </div>
            </div>
            <button onClick={() => toggle(t.key)} aria-pressed={!!prefs[t.key]} title={prefs[t.key] ? "On" : "Off"}
              style={{ width: 40, height: 22, borderRadius: 999, border: "none", cursor: "pointer", flexShrink: 0,
                       background: prefs[t.key] ? "var(--brand)" : "var(--border)", position: "relative", transition: "background .15s" }}>
              <span style={{ position: "absolute", top: 3, left: prefs[t.key] ? 21 : 3, width: 16, height: 16,
                             borderRadius: "50%", background: "#fff", transition: "left .15s" }} />
            </button>
          </div>
        ))}
      </div>
    </Card>
  );
}

const ACT_ICON: Record<string, IconName> = {
  security: "shield", credential: "key", admin: "grid", system: "server", activity: "activity",
};
const ACT_SEV: Record<string, "ok" | "info" | "warn" | "danger"> = {
  info: "info", notice: "info", warning: "warn", critical: "danger",
};

// ---- Shared billing render pieces (admin user/tenant detail) ----------------
interface UserBilling {
  tenant_id: string | null; tenant_name: string | null; tenant_type: string | null;
  is_personal: boolean; org_managed: boolean;
  profile: (AdminBillingProfile & {
    charges: AdminBillingCharge[]; recurring_charges?: AdminBillingCharge[];
    onetime_charges?: AdminBillingCharge[]; invoice?: Invoice; collected_cents?: number;
  }) | null;
  quote?: { amount_cents: number; currency: string; plan_id: string; plan_name: string };
}

function InvoiceBlock({ invoice }: { invoice: Invoice }) {
  if (!invoice.lines?.length) return null;
  return (
    <div style={{ border: "1px solid var(--border-soft)", borderRadius: 10, overflow: "hidden" }}>
      {invoice.lines.map((l, i) => (
        <div key={i} className="spread" style={{ padding: "9px 12px", borderBottom: "1px solid var(--border-soft)" }}>
          <div>
            <div style={{ fontSize: 12.5, fontWeight: 600 }}>{l.label}</div>
            {l.detail && <div className="faint" style={{ fontSize: 11 }}>{l.detail}</div>}
          </div>
          <div style={{ fontWeight: 600 }}>{fmtCents(l.amount_cents, invoice.currency)}</div>
        </div>
      ))}
      <div className="spread" style={{ padding: "10px 12px", background: "var(--inset)" }}>
        <div style={{ fontWeight: 700 }}>Total per {invoice.interval || "month"}</div>
        <div style={{ fontWeight: 700 }}>{fmtCents(invoice.total_cents, invoice.currency)}</div>
      </div>
    </div>
  );
}

function RecurringChargesTable({ charges }: { charges: AdminBillingCharge[] }) {
  return (
    <table className="table">
      <thead><tr><th>When</th><th>Amount</th><th>Attempt</th><th>Status</th><th>Detail</th></tr></thead>
      <tbody>
        {charges.map((c) => (
          <tr key={c.id}>
            <td className="faint" style={{ fontSize: 12 }}>{c.created_at ? billDate(c.created_at) : "—"}</td>
            <td>{fmtCents(c.amount_cents, c.currency)}</td>
            <td className="faint" style={{ fontSize: 12 }}>#{c.attempt}</td>
            <td><Pill tone={BILLING_STATUS_TONE[c.status] || "info"} dot>{c.status}</Pill></td>
            <td className="faint" style={{ fontSize: 11.5 }}>{c.error || c.processor_charge_id || "—"}</td>
          </tr>
        ))}
        {charges.length === 0 && <tr><td colSpan={5} className="muted">No subscription charges yet.</td></tr>}
      </tbody>
    </table>
  );
}

function OneTimeChargesTable({ charges }: { charges: AdminBillingCharge[] }) {
  return (
    <table className="table">
      <thead><tr><th>When</th><th>Description</th><th>Type</th><th>Amount</th><th>Status</th></tr></thead>
      <tbody>
        {charges.map((c) => (
          <tr key={c.id}>
            <td className="faint" style={{ fontSize: 12 }}>{c.created_at ? billDate(c.created_at) : "—"}</td>
            <td style={{ fontSize: 12.5 }}>{c.description || <span className="faint">—</span>}</td>
            <td><Pill tone="info">{CHARGE_KIND_LABEL[c.kind] || c.kind}</Pill></td>
            <td>{fmtCents(c.amount_cents, c.currency)}</td>
            <td><Pill tone={BILLING_STATUS_TONE[c.status] || "info"} dot>{c.status}</Pill></td>
          </tr>
        ))}
        {charges.length === 0 && <tr><td colSpan={5} className="muted">No one-time charges (e.g. appliance purchases) yet.</td></tr>}
      </tbody>
    </table>
  );
}

// Full "Billing Information" panel — payment method, recurring + one-time history,
// with an org-managed notice for organization members.
function BillingInfoPanel({ billing }: { billing: UserBilling | null }) {
  if (!billing) return <Card><div className="muted">Loading billing…</div></Card>;
  const p = billing.profile;
  const recurring = p?.recurring_charges ?? (p?.charges || []).filter((c) => c.recurring ?? c.kind === "recurring");
  const onetime = p?.onetime_charges ?? (p?.charges || []).filter((c) => !(c.recurring ?? c.kind === "recurring"));
  return (
    <>
      {billing.org_managed && (
        <Card style={{ marginBottom: 16, borderLeft: "3px solid var(--brand)" }}>
          <div className="row" style={{ gap: 10, alignItems: "flex-start" }}>
            <Icon name="user" size={16} />
            <div>
              <div style={{ fontWeight: 600 }}>Billing is managed at the organization level</div>
              <div className="faint" style={{ fontSize: 12 }}>
                This account belongs to <b>{billing.tenant_name}</b>. Its subscription and invoices are billed to the organization — shown below for reference.
              </div>
            </div>
          </div>
        </Card>
      )}
      {!p ? (
        <Card><div className="muted">No billing profile yet{billing.quote ? ` — plan quote ${fmtCents(billing.quote.amount_cents, billing.quote.currency)}/mo (${billing.quote.plan_name})` : ""}. A profile is created when a card is saved.</div></Card>
      ) : (
        <>
          <Card style={{ marginBottom: 16 }}>
            <div className="spread" style={{ marginBottom: 10 }}>
              <h3 style={{ margin: 0 }}>Subscription</h3>
              <Pill tone={BILLING_STATUS_TONE[p.status] || "info"} dot>{p.status}</Pill>
            </div>
            <div className="grid grid-4" style={{ gap: 12 }}>
              <Mini label="Plan" value={p.plan_name || p.plan_id || "—"} />
              <Mini label="Amount" value={`${fmtCents(p.amount_cents, p.currency)}/${p.interval}`} />
              <Mini label="Next billing date" value={p.active && p.next_charge_at ? billDate(p.next_charge_at) : (p.status === "canceled" ? "canceled" : "not scheduled")} />
              <Mini label="Collected to date" value={fmtCents(p.collected_cents || 0, p.currency)} />
            </div>
            {p.active && p.next_charge_at && (
              <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>
                Next charge of {fmtCents(p.amount_cents, p.currency)} on <b>{billDate(p.next_charge_at)}</b> ({timeAgo(p.next_charge_at)}).
              </div>
            )}
          </Card>
          <Card style={{ marginBottom: 16 }}>
            <div className="spread" style={{ marginBottom: 10 }}>
              <h3 style={{ margin: 0 }}>Payment method</h3>
              <Pill tone={BILLING_STATUS_TONE[p.status] || "info"} dot>{p.status}</Pill>
            </div>
            {p.payment_method
              ? <div className="result-row" style={{ background: "var(--inset)", borderRadius: 10 }}>
                  <div className="result-icon" style={{ background: "var(--inset)" }}><Icon name="credit-card" size={16} /></div>
                  <div className="flex1">
                    <div style={{ fontWeight: 600 }}>{p.payment_method.brand} •••• {p.payment_method.last4}</div>
                    <div className="faint" style={{ fontSize: 12 }}>Expires {String(p.payment_method.exp_month).padStart(2, "0")}/{p.payment_method.exp_year} · {p.processor || "processor"}</div>
                  </div>
                </div>
              : <div className="muted">No card on file.</div>}
          </Card>
          <Card style={{ marginBottom: 16 }}>
            <div className="spread" style={{ marginBottom: 10 }}>
              <h3 style={{ margin: 0 }}>Recurring payments</h3>
              <span className="faint" style={{ fontSize: 12 }}>Collected {fmtCents(p.collected_cents || 0, p.currency)}</span>
            </div>
            <RecurringChargesTable charges={recurring} />
          </Card>
          <Card style={{ marginBottom: 16 }}>
            <h3 style={{ margin: "0 0 10px" }}>Billing history · one-time charges</h3>
            <OneTimeChargesTable charges={onetime} />
          </Card>
        </>
      )}
    </>
  );
}

type UserTab = "overview" | "personal" | "settings" | "usage" | "subscription" | "activity" | "billing";

function UserDetail({ id, onBack, backLabel }: { id: string; onBack: () => void; backLabel: string }) {
  const [u, setU] = useState<any>(null);
  const [billing, setBilling] = useState<UserBilling | null>(null);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  const [tab, setTab] = useState<UserTab>("overview");
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3200); }
  async function load() {
    try { setErr(""); setU(await api.get<any>(`/admin/users/${id}`)); }
    catch (e) { setErr((e as { message?: string }).message || "Failed to load user"); }
  }
  async function loadBilling() {
    try { setBilling(await api.get<UserBilling>(`/admin/billing/users/${id}`)); }
    catch { setBilling(null); }
  }
  useEffect(() => { void load(); void loadBilling(); }, [id]);

  const money = (n: number) => "$" + (Math.round((n || 0) * 100) / 100)
    .toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });

  const isShared = () => u?.tenant?.tenant_type === "shared";
  async function edit() {
    try { if (await editUserDialog(u, isShared())) { flash("Saved"); await load(); } }
    catch { flash("Update failed"); }
  }
  async function reset() {
    if (!await confirmDialog({ title: "Reset access?", message: `Revoke ${u.email}'s passkeys and email a fresh sign-in code.`, confirmLabel: "Reset" })) return;
    try { const res = await api.post<any>(`/admin/users/${id}/reset`, {}); flash(res.invite?.dev_code ? `Reset · code ${res.invite.dev_code}` : "Access reset & emailed"); await load(); }
    catch { flash("Reset failed"); }
  }
  async function del() {
    if (!await confirmDialog({ title: "Delete user?", message: `Permanently remove ${u.email}.`, tone: "danger", confirmLabel: "Delete" })) return;
    try { await api.del(`/admin/users/${id}`); onBack(); }
    catch (e) { flash((e as { message?: string }).message || "Delete failed"); }
  }
  async function rerunSetup() {
    if (!await confirmDialog({ title: "Re-run setup wizard?", message: `${u.email} will be walked through the first-run setup wizard again on their next visit.`, confirmLabel: "Re-run" })) return;
    try { await api.post(`/admin/users/${id}/reset-setup`, {}); flash("Setup wizard will re-run for this user"); await load(); }
    catch { flash("Couldn't reset setup"); }
  }
  async function changePlan() {
    try {
      const pricing = await api.get<any>("/billing/pricing").catch(() => null);
      const plans: any[] = pricing?.license_plans || [];
      const cur = u.tenant?.plan;
      const pick = await formDialog({
        title: "Change account type",
        message: `Change ${u.email}'s plan. This moves the user to a new tenant and signs them out.`,
        fields: [{ name: "plan", label: "New plan", defaultValue: "",
          options: [{ label: "— choose a plan —", value: "" },
            ...plans.filter((p) => p.id !== cur).map((p) => ({ label: `${p.name} — $${p.price_per_tb_month}/TB·mo${p.min_tb ? `, min ${p.min_tb}TB` : ""}`, value: p.id }))] }],
        confirmLabel: "Preview",
      });
      if (!pick || !pick.plan) return;
      const prev = await api.get<any>(`/admin/billing/plan-change/preview?user_id=${id}&plan=${encodeURIComponent(pick.plan)}`);
      let tenant_name: string | null = null;
      if (prev.requires_new_tenant) {
        const nm = await promptDialog({ title: "Name the organization", label: "Organization name", placeholder: "e.g. Smith Family", confirmLabel: "Next" });
        if (!nm || !nm.trim()) return;
        tenant_name = nm.trim();
      }
      const msg = `${prev.current_plan.name} → ${prev.target_plan.name}  ·  ${money(prev.current_monthly)}/mo → ${money(prev.target_monthly)}/mo`
        + (prev.warnings?.length ? `\n\n${prev.warnings.join("\n")}` : "");
      if (!await confirmDialog({ title: "Apply plan change?", message: msg, confirmLabel: "Apply", tone: prev.is_downgrade ? "danger" : undefined })) return;
      await api.post("/admin/billing/plan-change", { user_id: id, plan: pick.plan, tenant_name });
      flash("Plan changed — the user must sign back in.");
      await load();
    } catch (e) { flash((e as { message?: string }).message || "Plan change failed"); }
  }

  if (!u) return (
    <Card>
      <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 10 }}>← {backLabel}</button>
      {err ? <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 13 }}>Couldn't load user: {err}</div>
           : <div className="muted">Loading user…</div>}
    </Card>
  );

  const b = u.billing;
  const s = u.storage || {};
  const c = u.counts || {};
  const name = u.full_name || u.display_name || u.email;
  const shared = isShared();
  const addrs: any[] = u.addresses || [];
  const prof = billing?.profile || null;
  const USER_TABS: { key: UserTab; label: string; icon: IconName }[] = [
    { key: "overview", label: "Overview", icon: "grid" },
    { key: "personal", label: "Personal information", icon: "user" },
    { key: "settings", label: "Settings", icon: "puzzle" },
    { key: "usage", label: "Usage", icon: "database" },
    ...(shared ? [{ key: "subscription" as UserTab, label: "Subscription", icon: "credit-card" as IconName }] : []),
    { key: "activity", label: "Activity", icon: "activity" },
    { key: "billing", label: "Billing information", icon: "credit-card" },
  ];
  const curTab = USER_TABS.some((t) => t.key === tab) ? tab : "overview";

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <button className="btn ghost sm" onClick={onBack}>← {backLabel}</button>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" onClick={edit}>Edit</button>
          <button className="btn ghost sm" onClick={changePlan}><Icon name="credit-card" size={13} /> Change plan</button>
          <button className="btn ghost sm" onClick={() => void generateInsightsFor(u)}><Icon name="insights" size={13} /> Insights</button>
          <button className="btn ghost sm" onClick={rerunSetup}><Icon name="restore" size={13} /> Re-run setup</button>
          <button className="btn ghost sm" onClick={reset}>Reset access</button>
          <button className="btn danger sm" onClick={del}>Delete</button>
        </div>
      </div>

      <Card style={{ marginBottom: 14 }}>
        <div className="spread">
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="brand-logo" style={{ width: 44, height: 44, fontSize: 16 }}>
              {name.slice(0, 2).toUpperCase()}
            </div>
            <div>
              <h3 style={{ margin: 0 }}>{name}</h3>
              <div className="faint" style={{ fontSize: 12 }}>
                {u.email}{u.phone ? ` · ${u.phone}` : ""}
                {u.tenant ? ` · ${u.tenant.name}` : ""}
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <Pill tone={u.status === "active" ? "ok" : "warn"} dot>{u.status}</Pill>
            {!u.setup_completed_at && <Pill tone="warn">Setup pending</Pill>}
            {u.tenant && <Pill tone={TENANT_TYPE_TONE[u.tenant.tenant_type] || "info"}>{TENANT_TYPE_LABEL[u.tenant.tenant_type] || u.tenant.tenant_type}</Pill>}
            {u.is_platform_admin && <Pill tone="info">platform admin</Pill>}
          </div>
        </div>
        <div className="grid grid-4" style={{ gap: 12, marginTop: 14 }}>
          <Mini label={shared ? "Plan" : "Role"} value={shared ? (b?.plan?.name || "Personal") : u.role} />
          <Mini label="Last login" value={u.last_login_at ? timeAgo(u.last_login_at) : "Never"} />
          <Mini label="Created" value={u.created_at ? timeAgo(u.created_at) : "—"} />
          <Mini label="Passkeys" value={c.passkeys ?? 0} />
        </div>
      </Card>

      <div className="row" style={{ gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        {USER_TABS.map((t) => (
          <button key={t.key} className={`btn sm ${curTab === t.key ? "primary" : "ghost"}`} onClick={() => setTab(t.key)}>
            <Icon name={t.icon} size={13} /> {t.label}
          </button>
        ))}
      </div>

      {curTab === "overview" && (
        <Card style={{ marginBottom: 16 }}>
          <h3 style={{ margin: "0 0 12px" }}>At a glance</h3>
          <div className="grid grid-4" style={{ gap: 12, marginBottom: 14 }}>
            <Mini label="Objects protected" value={(c.objects ?? 0).toLocaleString()} />
            <Mini label="Recovery points" value={c.recovery_points ?? 0} />
            <Mini label="Sources" value={c.sources ?? 0} />
            <Mini label="Vaults" value={c.vaults ?? 0} />
            <Mini label="Data stored" value={bytes((s.cloud_bytes || 0) + (s.appliance_bytes || 0) + (s.customer_bytes || 0))} />
            {b && <Mini label="Billing" value={`${money(b.total_monthly || 0)}/mo`} />}
            {prof && <Mini label="Subscription" value={prof.active ? "Active" : (prof.status || "inactive")} />}
            {prof && prof.active && <Mini label="Next charge" value={billDate(prof.next_charge_at)} />}
          </div>
          <table className="table">
            <thead><tr><th>Storage channel</th><th>Stored</th></tr></thead>
            <tbody>
              <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="cv-cloud" size={13} /> Arkive Cloud</span></td><td>{bytes(s.cloud_bytes || 0)}</td></tr>
              <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="appliance" size={13} /> Appliance storage</span></td><td>{bytes(s.appliance_bytes || 0)}</td></tr>
              <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="byos:x" size={13} /> Your cloud bucket</span></td><td>{bytes(s.customer_bytes || 0)}</td></tr>
            </tbody>
          </table>
        </Card>
      )}

      {curTab === "personal" && (
        <>
          <Card style={{ marginBottom: 16 }}>
            <h3 style={{ margin: "0 0 12px" }}>Contact details</h3>
            <Row2 label="Full name" value={u.full_name || u.display_name || "—"} />
            <Row2 label="First name" value={u.first_name || "—"} />
            <Row2 label="Last name" value={u.last_name || "—"} />
            <Row2 label="Email (sign-in)" value={u.email} />
            <Row2 label="Phone" value={u.phone || "—"} />
            <Row2 label="Organization" value={u.tenant?.name || "—"} />
            <Row2 label="Role" value={u.role} />
            <div className="row" style={{ marginTop: 12 }}>
              <button className="btn sm" onClick={edit}><Icon name="edit" size={13} /> Edit details</button>
            </div>
          </Card>
          <Card style={{ marginBottom: 16 }}>
            <div className="spread" style={{ marginBottom: 10 }}>
              <h3 style={{ margin: 0 }}>Addresses</h3>
              <span className="faint" style={{ fontSize: 11.5 }}>{addrs.length} on file</span>
            </div>
            {addrs.length === 0 ? <div className="muted">No addresses saved for this account.</div> : (
              <div className="stack" style={{ gap: 8 }}>
                {addrs.map((a) => (
                  <div key={a.id} className="result-row" style={{ background: "var(--inset)", borderRadius: 10 }}>
                    <div className="result-icon" style={{ background: "var(--inset)" }}><Icon name="note" size={16} /></div>
                    <div className="flex1">
                      <div className="row" style={{ gap: 8, alignItems: "center" }}>
                        <span style={{ fontWeight: 600, textTransform: "capitalize" }}>{a.kind}</span>
                        {a.label && <span className="faint" style={{ fontSize: 12 }}>· {a.label}</span>}
                        {a.is_default && <Pill tone="ok">Default</Pill>}
                      </div>
                      <div className="faint" style={{ fontSize: 12 }}>
                        {[a.name, a.line1, a.line2, [a.city, a.region, a.postal_code].filter(Boolean).join(", "), a.country]
                          .filter(Boolean).join(" · ")}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </>
      )}

      {curTab === "settings" && (
        <>
          <Card style={{ marginBottom: 16 }}>
            <div className="spread" style={{ marginBottom: 8 }}>
              <div>
                <h3 style={{ margin: 0 }}>Account settings</h3>
                <div className="faint" style={{ fontSize: 12 }}>Feature flags, status and additional notification addresses.</div>
              </div>
              <button className="btn sm" onClick={edit}><Icon name="edit" size={13} /> Edit settings</button>
            </div>
            <Row2 label="Status" value={<Pill tone={u.status === "active" ? "ok" : "warn"} dot>{u.status}</Pill>} />
            <Row2 label="Additional notification emails" value={(u.notification_emails || []).join(", ") || "—"} />
            <div className="divider" />
            <div className="faint" style={{ fontSize: 11.5, textTransform: "uppercase", letterSpacing: ".05em", marginBottom: 8 }}>Feature flags</div>
            {Object.keys(u.feature_flags || {}).length === 0
              ? <div className="muted" style={{ fontSize: 12.5 }}>Using defaults for all features. Click “Edit settings” to override.</div>
              : <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                  {Object.entries(u.feature_flags || {}).map(([k, v]) => (
                    <Pill key={k} tone={v ? "ok" : "danger"} dot>{k}: {v ? "allowed" : "blocked"}</Pill>
                  ))}
                </div>}
          </Card>
          <AdminUserNotifs user={u} onSaved={load} />
        </>
      )}

      {curTab === "usage" && (
        <>
          <Card style={{ marginBottom: 16 }}>
            <h3 style={{ margin: "0 0 12px" }}>Usage & storage</h3>
            <div className="grid grid-4" style={{ gap: 12, marginBottom: 14 }}>
              <Mini label="Objects protected" value={(c.objects ?? 0).toLocaleString()} />
              <Mini label="Recovery points" value={c.recovery_points ?? 0} />
              <Mini label="Sources" value={c.sources ?? 0} />
              <Mini label="Vaults" value={c.vaults ?? 0} />
              <Mini label="Data stored" value={bytes((s.cloud_bytes || 0) + (s.appliance_bytes || 0) + (s.customer_bytes || 0))} />
              {b && <Mini label="Billing" value={`${money(b.total_monthly || 0)}/mo`} />}
            </div>
            <table className="table">
              <thead><tr><th>Storage channel</th><th>Stored</th></tr></thead>
              <tbody>
                <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="cv-cloud" size={13} /> Arkive Cloud</span></td><td>{bytes(s.cloud_bytes || 0)}</td></tr>
                <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="appliance" size={13} /> Appliance storage</span></td><td>{bytes(s.appliance_bytes || 0)}</td></tr>
                <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="byos:x" size={13} /> Your cloud bucket</span></td><td>{bytes(s.customer_bytes || 0)}</td></tr>
              </tbody>
            </table>
          </Card>
          <Card style={{ marginBottom: 16 }}>
            <div className="spread" style={{ margin: "0 0 12px" }}>
              <h3 style={{ margin: 0 }}>Sources</h3>
              <span className="faint" style={{ fontSize: 11.5 }}>{(u.sources || []).length} connected</span>
            </div>
            {(u.sources || []).length === 0 ? (
              <div className="muted">No sources connected by this user yet.</div>
            ) : (
              <table className="table">
                <thead><tr><th>Source</th><th>Type</th><th style={{ textAlign: "right" }}>Objects</th><th style={{ textAlign: "right" }}>Protected</th><th>Last sync</th><th>Status</th></tr></thead>
                <tbody>
                  {u.sources.map((src: any) => (
                    <tr key={src.id}>
                      <td>
                        <div className="row" style={{ gap: 8, alignItems: "center" }}>
                          {brandForSource(src.source_type)
                            ? <BrandIcon name={brandForSource(src.source_type)!} size={16} />
                            : <Icon name="database" size={15} />}
                          <div>
                            <div style={{ fontWeight: 600 }}>{src.name}</div>
                            {src.account_username && <div className="faint" style={{ fontSize: 11 }}>{src.account_username}</div>}
                          </div>
                        </div>
                      </td>
                      <td className="faint" style={{ fontSize: 12 }}>{src.source_type}</td>
                      <td style={{ textAlign: "right" }}>{(src.object_count || 0).toLocaleString()}</td>
                      <td style={{ textAlign: "right" }}>{bytes(src.protected_bytes || 0)}</td>
                      <td className="faint" style={{ fontSize: 12 }}>{src.last_backup_at ? timeAgo(src.last_backup_at) : "never"}</td>
                      <td>
                        {src.needs_reauth ? <Pill tone="warn" dot>reconnect</Pill>
                          : !src.active ? <Pill tone="warn">deactivated</Pill>
                          : src.has_error ? <Pill tone="danger" dot>issue</Pill>
                          : <Pill tone="ok" dot>healthy</Pill>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}

      {curTab === "subscription" && shared && (
        <>
          <Card style={{ marginBottom: 16 }}>
            <div className="spread" style={{ marginBottom: 10 }}>
              <div>
                <h3 style={{ margin: 0 }}>Subscription</h3>
                <div className="faint" style={{ fontSize: 12 }}>The account's recurring Arkive plan.</div>
              </div>
              <div className="row" style={{ gap: 8, alignItems: "center" }}>
                {prof && <Pill tone={BILLING_STATUS_TONE[prof.status] || "info"} dot>{prof.status}</Pill>}
                <button className="btn ghost sm" onClick={changePlan}><Icon name="credit-card" size={13} /> Change plan</button>
              </div>
            </div>
            <div className="grid grid-4" style={{ gap: 12 }}>
              <Mini label="Plan" value={prof?.plan_name || b?.plan?.name || "Personal"} />
              <Mini label="Amount" value={prof ? `${fmtCents(prof.amount_cents, prof.currency)}/${prof.interval}` : (b ? `${money(b.total_monthly || 0)}/mo` : "—")} />
              <Mini label="Next charge" value={prof?.active ? billDate(prof.next_charge_at) : "—"} />
              <Mini label="Data protected" value={`${b?.used_tb ?? 0} TB`} />
            </div>
          </Card>
          {prof?.invoice && prof.invoice.lines.length > 0 && (
            <Card style={{ marginBottom: 16 }}>
              <h3 style={{ margin: "0 0 10px" }}>Itemized invoice · recurring {prof.invoice.interval || "month"}</h3>
              <InvoiceBlock invoice={prof.invoice} />
            </Card>
          )}
        </>
      )}

      {curTab === "activity" && (
        <>
          <Card style={{ marginBottom: 16 }}>
            <h3 style={{ margin: "0 0 12px" }}>Recent activity</h3>
            {(u.activity || []).length === 0 ? (
              <div className="muted">No recorded activity for this user yet.</div>
            ) : (
              <div className="stack" style={{ gap: 8 }}>
                {u.activity.map((e: any, i: number) => (
                  <div key={i} className="row" style={{ gap: 8, fontSize: 12.5, alignItems: "center", flexWrap: "wrap" }}>
                    <Icon name={ACT_ICON[e.category] || "activity"} size={14} />
                    <span style={{ fontWeight: 600 }}>{humanizeAction(e.action)}</span>
                    {e.resource && <span className="faint" style={{ fontSize: 11 }}>{e.resource}</span>}
                    {e.severity && e.severity !== "info" && <Pill tone={ACT_SEV[e.severity] || "info"} dot>{e.severity}</Pill>}
                    <span className="faint" style={{ marginLeft: "auto" }} title={e.at ? fmtAbsolute(e.at) : ""}>{e.at ? timeAgo(e.at) : ""}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>
          <CommsHistory userId={id} />
        </>
      )}

      {curTab === "billing" && <BillingInfoPanel billing={billing} />}

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

type TenantTab = "overview" | "settings" | "usage" | "subscription" | "billing";

function TenantDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const [t, setT] = useState<any>(null);
  const [billing, setBilling] = useState<UserBilling | null>(null);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  const [tab, setTab] = useState<TenantTab>("overview");
  const [userSel, setUserSel] = useState<string | null>(null);
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3200); }
  async function load() {
    try { setErr(""); setT(await api.get<any>(`/admin/tenants/${id}`)); }
    catch (e) { setErr((e as { message?: string }).message || "Failed to load tenant"); }
  }
  async function loadBilling() {
    try { setBilling(await api.get<UserBilling>(`/admin/billing/tenants/${id}`)); }
    catch { setBilling(null); }
  }
  useEffect(() => { void load(); void loadBilling(); }, [id]);

  async function editTenant() {
    const isShared = t.tenant_type === "shared";
    let planOpts = [{ label: t.plan, value: t.plan }];
    if (!isShared) {
      try {
        const pr = await api.get<any>("/admin/pricing");
        if (pr.license_plans?.length) planOpts = pr.license_plans.map((pl: any) =>
          ({ label: `${pl.name} — $${pl.price_per_tb_month}/TB·mo, min ${pl.min_tb}TB`, value: pl.id }));
      } catch { /* fall back to current */ }
    }
    const nodeOpts = await nodeOptions();
    // Shared tenants have no org-level plan / licensed amount (per-account Personal plan).
    const fields: any[] = [
      { name: "name", label: "Name", defaultValue: t.name, required: true },
      { name: "tenant_type", label: "Tenant type", defaultValue: t.tenant_type || "dedicated", options: TENANT_TYPE_OPTS },
      { name: "node_id", label: "Processing node", defaultValue: t.node_id || "", options: nodeOpts },
      { name: "status", label: "Status", defaultValue: t.status,
        options: ["active", "suspended", "trial"].map((v) => ({ label: v, value: v })) },
    ];
    if (!isShared) {
      fields.push(
        { name: "plan", label: "License plan", defaultValue: t.plan, options: planOpts },
        { name: "licensed_tb", label: "Licensed data (TB)", defaultValue: String(((t.licensed_bytes || 0) / (1024 ** 4)).toFixed(2)) },
        // Feature flags are tenant-wide only for dedicated tenants; shared tenants
        // manage them per-account (per user) instead.
        ...flagFields(await flagCatalog(), t.feature_flags, "tenant"),
      );
    }
    const r = await formDialog({ title: "Edit tenant", confirmLabel: "Save", fields, wide: true });
    if (!r) return;
    const flags = extractFlags(r, await flagCatalog());
    const payload: any = { ...r };
    if (r.licensed_tb !== undefined) payload.licensed_tb = Number(r.licensed_tb) || 0;
    try {
      await api.put(`/admin/tenants/${id}`, payload);
      if (!isShared && Object.keys(flags).length) {
        await api.put(`/admin/tenants/${id}/flags`, { feature_flags: flags });
      }
      flash("Saved"); await load();
    } catch { flash("Save failed"); }
  }
  async function suspend() {
    if (!await confirmDialog({ title: "Suspend tenant?", message: `Freeze ${t.name} and deactivate all its users. This is reversible.`, tone: "danger", confirmLabel: "Suspend" })) return;
    try { await api.del(`/admin/tenants/${id}`); flash("Tenant suspended"); await load(); } catch { flash("Failed"); }
  }

  async function newUser() {
    const isShared = t.tenant_type === "shared";
    // Shared tenants hold isolated 1:1 personal accounts — richer contact
    // details, no roles. Org tenants keep the name + role flow.
    const fields: any[] = isShared
      ? [
          { name: "first_name", label: "First name", required: true },
          { name: "last_name", label: "Last name", required: true },
          { name: "email", label: "Email", required: true },
          { name: "phone", label: "Phone" },
        ]
      : [
          { name: "email", label: "Email", required: true },
          { name: "display_name", label: "Name" },
          { name: "role", label: "Role", defaultValue: "member",
            options: ["owner", "security-admin", "member", "support-admin"].map((v) => ({ label: v, value: v })) },
        ];
    const r = await formDialog({
      title: isShared ? "Add account" : "Add user",
      confirmLabel: isShared ? "Create account" : "Create user",
      fields,
    });
    if (!r) return;
    try {
      const res = await api.post<any>(`/admin/tenants/${id}/users`, r);
      flash(res.invite?.dev_code
        ? `Created · code ${res.invite.dev_code}`
        : (isShared ? "Account created & welcome email sent" : "User created & invited"));
      await load();
    } catch (e) { flash((e as { message?: string }).message || "Could not create user"); }
  }
  async function editUser(u: any) {
    try {
      if (await editUserDialog(u, t.tenant_type === "shared")) { flash("User updated"); await load(); }
    } catch { flash("Update failed"); }
  }
  async function resetUser(u: any) {
    if (!await confirmDialog({ title: "Reset access?", message: `Revoke ${u.email}'s passkeys and email a fresh sign-in code.`, confirmLabel: "Reset" })) return;
    try { const res = await api.post<any>(`/admin/users/${u.id}/reset`, {}); flash(res.invite?.dev_code ? `Reset · code ${res.invite.dev_code}` : "Access reset & emailed"); await load(); }
    catch { flash("Reset failed"); }
  }
  async function delUser(u: any) {
    if (!await confirmDialog({ title: "Delete user?", message: `Permanently remove ${u.email}.`, tone: "danger", confirmLabel: "Delete" })) return;
    try { await api.del(`/admin/users/${u.id}`); flash("User deleted"); await load(); }
    catch (e) { flash((e as { message?: string }).message || "Delete failed"); }
  }

  if (!t) return (
    <Card>
      <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 10 }}>← Tenants</button>
      {err ? <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 13 }}>Couldn't load tenant: {err}</div>
           : <div className="muted">Loading tenant…</div>}
    </Card>
  );
  const licensedTb = ((t.licensed_bytes || 0) / (1024 ** 4)).toFixed(2);
  const isShared = t.tenant_type === "shared";

  if (userSel) return <UserDetail id={userSel} backLabel={t.name} onBack={() => { setUserSel(null); void load(); }} />;

  const money = (n: number) => "$" + (Math.round((n || 0) * 100) / 100)
    .toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  const b = t.billing;
  const su = t.storage_usage;
  const channelLabel: Record<string, string> = {
    "cv-cloud": "Arkive Cloud", "appliance": "Offline appliance",
    "customer-cloud": "Your cloud (S3 / Azure)",
  };
  const options: string[] = b?.options || t.protection_options || [];

  const TENANT_TABS: { key: TenantTab; label: string; icon: IconName }[] = [
    { key: "overview", label: "Overview", icon: "grid" },
    { key: "settings", label: "Settings", icon: "puzzle" },
    { key: "usage", label: "Usage", icon: "database" },
    { key: "subscription", label: "Subscription", icon: "credit-card" },
    { key: "billing", label: "Billing information", icon: "credit-card" },
  ];

  const membersTable = (
    <Card>
      <div className="spread" style={{ marginBottom: 10 }}>
        <h3 style={{ margin: 0 }}>{isShared ? "Accounts" : "Users"}</h3>
        <button className="btn primary sm" onClick={newUser}>
          <Icon name="user" size={14} /> {isShared ? "Add account" : "Add user"}
        </button>
      </div>
      {isShared ? (
        <table className="table">
          <thead><tr><th>Account</th><th>Contact</th><th>Last login</th><th>Usage</th><th>Billing</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {(t.members || []).map((u: any) => (
              <tr key={u.id}>
                <td>
                  <div style={{ fontWeight: 600 }}>{u.full_name || u.display_name || u.email}</div>
                  <div className="faint" style={{ fontSize: 11.5 }}>{u.email}</div>
                </td>
                <td className="faint" style={{ fontSize: 12 }}>{u.phone || "—"}</td>
                <td className="faint" style={{ fontSize: 12 }}>{u.last_login_at ? timeAgo(u.last_login_at) : "Never"}</td>
                <td>{bytes(u.billing?.used_bytes || 0)}</td>
                <td>{money(u.billing?.total_monthly || 0)}/mo<div className="faint" style={{ fontSize: 11 }}>{u.billing?.plan?.name || "Personal"}</div></td>
                <td><Pill tone={u.status === "active" ? "ok" : "warn"}>{u.status}</Pill></td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <button className="btn ghost sm" onClick={() => void generateInsightsFor(u)}><Icon name="insights" size={13} /></button>{" "}
                  <button className="btn ghost sm" onClick={() => setUserSel(u.id)}>Manage</button>{" "}
                  <button className="btn ghost sm" onClick={() => resetUser(u)}>Reset</button>{" "}
                  <button className="btn danger sm" onClick={() => delUser(u)}>Delete</button>
                </td>
              </tr>
            ))}
            {(t.members || []).length === 0 && <tr><td colSpan={7} className="muted">No accounts yet.</td></tr>}
          </tbody>
        </table>
      ) : (
        <table className="table">
          <thead><tr><th>User</th><th>Role</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {(t.members || []).map((u: any) => (
              <tr key={u.id}>
                <td><div style={{ fontWeight: 600 }}>{u.display_name || u.email}</div><div className="faint" style={{ fontSize: 11.5 }}>{u.email}{u.is_platform_admin ? " · platform admin" : ""}</div></td>
                <td><Pill tone="info">{u.role}</Pill></td>
                <td><Pill tone={u.status === "active" ? "ok" : "warn"}>{u.status}</Pill></td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <button className="btn ghost sm" onClick={() => void generateInsightsFor(u)}><Icon name="insights" size={13} /></button>{" "}
                  <button className="btn ghost sm" onClick={() => setUserSel(u.id)}>Manage</button>{" "}
                  <button className="btn ghost sm" onClick={() => resetUser(u)}>Reset</button>{" "}
                  <button className="btn danger sm" onClick={() => delUser(u)}>Delete</button>
                </td>
              </tr>
            ))}
            {(t.members || []).length === 0 && <tr><td colSpan={4} className="muted">No users.</td></tr>}
          </tbody>
        </table>
      )}
    </Card>
  );

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <button className="btn ghost sm" onClick={onBack}>← Tenants</button>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" onClick={editTenant}>Edit</button>
          <button className="btn danger sm" onClick={suspend}>Suspend</button>
        </div>
      </div>

      <Card style={{ marginBottom: 14 }}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>{t.name}</h3>
            <div className="faint" style={{ fontSize: 12 }}>
              {TENANT_TYPE_LABEL[t.tenant_type] || t.tenant_type || "Dedicated"}
              {isShared
                ? ` · ${t.users} account${t.users === 1 ? "" : "s"} · per-account Personal plan`
                : ` · ${t.plan} · ${licensedTb} TB licensed`}
              {` · ${t.key_ownership_model}`}
              {t.node ? ` · node: ${t.node.name}` : " · processed on control plane"}
            </div>
          </div>
          <Pill tone={t.status === "active" ? "ok" : "warn"}>{t.status}</Pill>
        </div>
        <div className="grid grid-4" style={{ gap: 12, marginTop: 14 }}>
          <Mini label={isShared ? "Accounts" : "Users"} value={t.users} />
          <Mini label="Appliances" value={t.appliances} />
          <Mini label="Agents" value={t.agents} />
          <Mini label="Sources" value={t.sources} />
          <Mini label="Mappings" value={t.mappings} />
          <Mini label="Objects" value={(t.objects ?? 0).toLocaleString()} />
          <Mini label="Recovery points" value={t.recovery_points} />
          <Mini label="Vaults" value={t.vaults?.length ?? 0} />
        </div>
      </Card>

      <div className="row" style={{ gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        {TENANT_TABS.map((tt) => (
          <button key={tt.key} className={`btn sm ${tab === tt.key ? "primary" : "ghost"}`} onClick={() => setTab(tt.key)}>
            <Icon name={tt.icon} size={13} /> {tt.label}
          </button>
        ))}
      </div>

      {tab === "overview" && membersTable}

      {tab === "settings" && (
        <Card style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <div>
              <h3 style={{ margin: 0 }}>Tenant settings</h3>
              <div className="faint" style={{ fontSize: 12 }}>Type, processing node, plan{isShared ? "" : ", licensed capacity and feature flags"}.</div>
            </div>
            <button className="btn sm" onClick={editTenant}><Icon name="edit" size={13} /> Edit settings</button>
          </div>
          <Row2 label="Tenant type" value={<Pill tone={TENANT_TYPE_TONE[t.tenant_type] || "info"}>{TENANT_TYPE_LABEL[t.tenant_type] || t.tenant_type}</Pill>} />
          <Row2 label="Status" value={<Pill tone={t.status === "active" ? "ok" : "warn"} dot>{t.status}</Pill>} />
          <Row2 label="Processing node" value={t.node?.name || "Control plane"} />
          <Row2 label="Key ownership" value={t.key_ownership_model} />
          {!isShared && <Row2 label="License plan" value={t.plan} />}
          {!isShared && <Row2 label="Licensed capacity" value={`${licensedTb} TB`} />}
          {!isShared && (
            <>
              <div className="divider" />
              <div className="faint" style={{ fontSize: 11.5, textTransform: "uppercase", letterSpacing: ".05em", marginBottom: 8 }}>Feature flags</div>
              {Object.keys(t.feature_flags || {}).length === 0
                ? <div className="muted" style={{ fontSize: 12.5 }}>All features use platform defaults. Click “Edit settings” to override tenant-wide.</div>
                : <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                    {Object.entries(t.feature_flags || {}).map(([k, v]) => (
                      <Pill key={k} tone={v ? "ok" : "danger"} dot>{k}: {v ? "allowed" : "blocked"}</Pill>
                    ))}
                  </div>}
            </>
          )}
          {isShared && (
            <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>
              Feature flags for a shared tenant are managed per-account (open an account → Settings).
            </div>
          )}
        </Card>
      )}

      {tab === "usage" && (
        <Card style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 6 }}>
            <h3 style={{ margin: 0 }}>Usage & storage</h3>
            <span className="faint" style={{ fontSize: 12 }}>{(t.objects ?? 0).toLocaleString()} objects · {t.recovery_points} recovery points</span>
          </div>
          {!isShared && (
            <div className="grid grid-4" style={{ gap: 12, marginBottom: 14 }}>
              <Mini label="Licensed" value={`${b?.licensed_tb ?? licensedTb} TB`} />
              <Mini label="Billable" value={b?.billable_tb != null ? `${b.billable_tb} TB` : "—"} />
              <Mini label="Used" value={`${b?.used_tb ?? 0} TB${b?.percent != null ? ` · ${b.percent}%` : ""}`} />
              <Mini label="Objects" value={(t.objects ?? 0).toLocaleString()} />
            </div>
          )}
          <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>
            {isShared ? "Storage footprint across all accounts in this tenant" : "Storage footprint"}
          </div>
          <table className="table">
            <thead><tr><th>Storage channel</th><th>Stored</th><th>Monthly cost</th></tr></thead>
            <tbody>
              <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="cv-cloud" size={13} /> Arkive Cloud</span></td><td>{bytes(su?.cloud_bytes || 0)}</td><td>{money(b?.costs?.cloud_storage_monthly || 0)}</td></tr>
              <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="appliance" size={13} /> Appliance storage</span></td><td>{bytes(su?.appliance_bytes || 0)}</td><td className="faint">{b?.costs?.appliance_monthly ? `${money(b.costs.appliance_monthly)}/mo lease` : "on-prem · no per-TB cost"}</td></tr>
              <tr><td><span className="row" style={{ gap: 6 }}><DestIcon dest="byos:x" size={13} /> Customer cloud bucket</span></td><td>{bytes(su?.customer_bytes || 0)}</td><td className="faint">{money(b?.costs?.third_party_estimate_monthly || 0)} est. (you pay provider)</td></tr>
            </tbody>
          </table>
        </Card>
      )}
      {tab === "usage" && <IndexReplicasBlock replicas={t.index_replicas} title="Search index replicas (DR copies of this customer's index)" />}

      {tab === "subscription" && (
        <Card style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 6 }}>
            <h3 style={{ margin: 0 }}>{isShared ? "Plans & subscriptions" : "Protection & subscription"}</h3>
            {isShared
              ? <Pill tone="info">{t.users} account{t.users === 1 ? "" : "s"}</Pill>
              : (b?.costs && <Pill tone="info">{money(b.costs.total_monthly)}/mo to Arkive</Pill>)}
          </div>
          <div className="faint" style={{ fontSize: 12, marginBottom: 12 }}>
            {isShared
              ? "A pool of isolated personal accounts — each self-manages its own Personal plan; there is no shared organization plan."
              : "Coupled to what the customer selected in Protection Setup — licensed amount, storage channels, and what they pay us."}
          </div>
          {!isShared && (
            <>
              <div className="grid grid-4" style={{ gap: 12, marginBottom: 14 }}>
                <Mini label="License plan" value={b?.license_plan?.name || t.plan} />
                <Mini label="Licensed" value={`${b?.licensed_tb ?? licensedTb} TB`} />
                <Mini label="Billable" value={b?.billable_tb != null ? `${b.billable_tb} TB` : "—"} />
                <Mini label="Used" value={`${b?.used_tb ?? 0} TB${b?.percent != null ? ` · ${b.percent}%` : ""}`} />
              </div>
              <div style={{ marginBottom: 12 }}>
                <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>Storage channels the customer enabled</div>
                <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                  {options.length === 0 && <span className="muted" style={{ fontSize: 12 }}>None selected yet</span>}
                  {options.map((o) => <Pill key={o} tone="info">{channelLabel[o] || o}</Pill>)}
                </div>
              </div>
              {b?.costs && (
                <div className="grid grid-4" style={{ gap: 12 }}>
                  <Mini label="Protection / license" value={`${money(b.costs.protection_monthly)}/mo`} />
                  <Mini label="Cloud storage" value={`${money(b.costs.cloud_storage_monthly)}/mo`} />
                  <Mini label="Appliance plan" value={`${money(b.costs.appliance_monthly)}/mo`} />
                  <Mini label="Total to Arkive" value={`${money(b.costs.total_monthly)}/mo`} />
                </div>
              )}
            </>
          )}
          {isShared && membersTable}
        </Card>
      )}

      {tab === "billing" && <BillingInfoPanel billing={billing} />}

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function Mini({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div style={{ background: "var(--inset)", borderRadius: 8, padding: "10px 12px" }}>
      <div style={{ fontSize: 20, fontWeight: 700 }}>{value}</div>
      <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
    </div>
  );
}

const COMM_CAT_LABEL: Record<string, string> = {
  signin: "Sign-in code", welcome: "Welcome", access: "Account access",
  broadcast: "Broadcast", support: "Support", email: "Email",
};
function commCatLabel(c: string): string {
  if (c?.startsWith("notification:")) return c.slice(13).replace(/_/g, " ");
  return COMM_CAT_LABEL[c] || c || "Email";
}

function CommsHistory({ userId }: { userId: string }) {
  const [rows, setRows] = useState<any[] | null>(null);
  const [sel, setSel] = useState<any>(null);
  useEffect(() => {
    let live = true;
    void (async () => {
      try { const r = await api.get<{ communications: any[] }>(`/admin/users/${userId}/communications`); if (live) setRows(r.communications || []); }
      catch { if (live) setRows([]); }
    })();
    return () => { live = false; };
  }, [userId]);

  async function open(id: string) {
    try { setSel(await api.get<any>(`/admin/communications/${id}`)); } catch { /* ignore */ }
  }
  // Delivery: whether it actually left the platform. "Logged" = no live email
  // provider was configured on the sending server, so it was recorded but not sent.
  const delivery = (r: any): { label: string; tone: "ok" | "warn" | "danger"; hint: string } => {
    if (r.status === "failed") return { label: "Failed", tone: "danger", hint: r.error || "Delivery failed" };
    if (r.status === "logged") return { label: "Not sent (log only)", tone: "warn", hint: "No email provider was configured on the sending server — recorded but not emailed." };
    return { label: "Delivered", tone: "ok", hint: r.channel ? `Sent via ${r.channel}` : "Sent" };
  };

  return (
    <Card style={{ marginBottom: 16 }}>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Communications history</h3>
        <span className="faint" style={{ fontSize: 11.5 }}>Every email sent to this account, across all nodes.</span>
      </div>
      {rows === null ? <div className="muted">Loading…</div>
        : rows.length === 0 ? <div className="muted">No communications sent to this account yet.</div>
        : (
        <table className="table">
          <thead><tr><th>Sent</th><th>Type</th><th>Subject</th><th>Delivery</th><th>Opened</th></tr></thead>
          <tbody>
            {rows.map((r) => {
              const d = delivery(r);
              return (
              <tr key={r.id} style={{ cursor: "pointer" }} onClick={() => void open(r.id)}>
                <td className="faint" style={{ fontSize: 12, whiteSpace: "nowrap" }} title={r.created_at ? fmtAbsolute(r.created_at) : ""}>{r.created_at ? timeAgo(r.created_at) : "—"}</td>
                <td style={{ fontSize: 12.5 }}>{commCatLabel(r.category)}</td>
                <td style={{ fontSize: 12.5, fontWeight: 600 }}>{r.subject || <span className="faint">(no subject)</span>}</td>
                <td title={d.hint}>
                  <Pill tone={d.tone} dot>{d.label}</Pill>
                  {r.channel && r.channel !== "log" && r.channel !== "error" && <span className="faint" style={{ fontSize: 11, marginLeft: 6 }}>{r.channel}</span>}
                </td>
                <td>
                  {r.opened
                    ? <Pill tone="ok" dot>Opened{r.open_count > 1 ? ` ·${r.open_count}` : ""}</Pill>
                    : <span className="faint" style={{ fontSize: 12 }}>Unopened</span>}
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {sel && (
        <div className="modal-backdrop" onClick={() => setSel(null)}>
          <div className="modal-panel" style={{ width: "min(720px, 100%)" }} onClick={(e) => e.stopPropagation()}>
            <div className="spread" style={{ marginBottom: 12 }}>
              <div style={{ minWidth: 0 }}>
                <h3 style={{ margin: 0, fontSize: 16 }}>{sel.subject || "(no subject)"}</h3>
                <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>{commCatLabel(sel.category)} · to {sel.to_email || "—"}</div>
              </div>
              <button className="btn ghost sm" onClick={() => setSel(null)}>Close</button>
            </div>
            <div style={{ marginBottom: 12 }}>
              <Row2 label="To" value={sel.to_email || "—"} />
              <Row2 label="Delivery" value={
                <span className="row" style={{ gap: 8, alignItems: "center", justifyContent: "flex-end" }}>
                  <Pill tone={delivery(sel).tone} dot>{delivery(sel).label}</Pill>
                  {sel.provider && sel.status === "sent" && <span className="faint" style={{ fontSize: 11.5 }}>via {sel.provider}</span>}
                </span>
              } />
              <Row2 label="Opened" value={
                sel.opened
                  ? <span className="row" style={{ gap: 8, alignItems: "center", justifyContent: "flex-end" }}>
                      <Pill tone="ok" dot>Opened{sel.open_count > 1 ? ` ·${sel.open_count}` : ""}</Pill>
                      <span className="faint" style={{ fontSize: 11.5 }}>{sel.opened_at ? fmtAbsolute(sel.opened_at) : ""}</span>
                    </span>
                  : <span className="faint">Unopened</span>
              } />
              <Row2 label="Sent" value={sel.created_at ? fmtAbsolute(sel.created_at) : "—"} />
              <Row2 label="From node" value={sel.node_name || "Control plane"} />
            </div>
            {sel.error && <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12.5, marginBottom: 10 }}>Delivery error: {sel.error}</div>}
            <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>Message body</div>
            <iframe title="communication body" sandbox="" srcDoc={sel.body_html || `<pre>${sel.body_text || ""}</pre>`}
                    style={{ width: "100%", height: 380, border: "1px solid var(--border)", borderRadius: 8, background: "#fff" }} />
          </div>
        </div>
      )}
    </Card>
  );
}

function Reports() {
  const [d, setD] = useState<any>(null);
  useEffect(() => { api.get("/admin/reports").then(setD).catch(() => {}); }, []);
  if (!d) return <Card><div className="muted">Compiling report…</div></Card>;
  const money = (n: number) => "$" + Math.round(n).toLocaleString();
  return (
    <>
      <div className="grid grid-4" style={{ marginBottom: 16 }}>
        <Stat label="Cloud storage (we pay)" value={bytes(d.totals.cloud_bytes || 0)}
              hint={`≈ ${money(d.totals.cloud_cost_monthly || 0)}/mo provider cost`} />
        <Stat label="Data protected" value={bytes(d.totals.bytes || 0)} />
        <Stat label="Monthly revenue" value={money(d.totals.monthly_revenue || 0)} />
        <Stat label="Tenants" value={d.totals.tenants} />
      </div>
      <Card>
        <h3 style={{ marginTop: 0 }}>Per-tenant usage & billing</h3>
        <table className="table">
          <thead><tr><th>Tenant</th><th>Plan</th><th>Users</th><th>Sources</th><th>Objects</th><th>Data</th><th>Cloud stored</th><th>Recovery pts</th><th>Monthly</th></tr></thead>
          <tbody>
            {d.tenants.map((t: any) => (
              <tr key={t.id}>
                <td style={{ fontWeight: 600 }}>{t.name}{t.status !== "active" ? <span className="faint"> · {t.status}</span> : ""}</td>
                <td><Pill tone="info">{t.plan}</Pill></td>
                <td>{t.users}</td><td>{t.sources + t.agents}</td>
                <td>{(t.objects || 0).toLocaleString()}</td>
                <td>{bytes(t.used_bytes || 0)}</td>
                <td style={{ fontWeight: 600 }}>{bytes(t.cloud_bytes || 0)}</td>
                <td>{t.recovery_points}</td>
                <td style={{ fontWeight: 600 }}>{money(t.monthly_cost || 0)}</td>
              </tr>
            ))}
            {d.tenants.length === 0 && <tr><td colSpan={9} className="muted">No tenants.</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  );
}

function Nodes() {
  const [nodes, setNodes] = useState<any[]>([]);
  const [svcs, setSvcs] = useState<ServiceObj[]>([]);
  const [versions, setVersions] = useState<{ control_plane?: string; platform?: string }>({});
  const [toast, setToast] = useState("");
  const [installCmd, setInstallCmd] = useState("");
  const [sel, setSel] = useState<string | null>(null);
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }
  async function load() {
    try { setNodes(await api.get<any[]>("/admin/nodes")); } catch { /* ignore */ }
    try { setSvcs(await api.get<ServiceObj[]>("/admin/service-objects")); } catch { /* ignore */ }
    try { setVersions(await api.get("/admin/versions")); } catch { /* ignore */ }
  }
  useEffect(() => { void load(); const iv = setInterval(load, 15000); return () => clearInterval(iv); }, []);

  async function registerNode() {
    const r = await formDialog({
      title: "Register node", confirmLabel: "Register",
      fields: [
        { name: "name", label: "Node name", required: true, placeholder: "us-west-storage-1" },
        { name: "region", label: "Region", placeholder: "us-west-2" },
        { name: "role", label: "Role", defaultValue: "control-plane",
          options: ["control-plane", "customer-tenant", "public-web"].map((v) => ({ label: v, value: v })) },
        { name: "endpoint", label: "Endpoint", placeholder: "https://node.arkive.life" },
      ],
    });
    if (!r) return;
    try { await api.post("/admin/nodes", r); flash("Node registered"); await load(); } catch { flash("Failed"); }
  }
  async function editNode(n: any) {
    const r = await formDialog({
      title: `Edit ${n.name}`, confirmLabel: "Save",
      fields: [
        { name: "name", label: "Name", defaultValue: n.name },
        { name: "region", label: "Region", defaultValue: n.region },
        { name: "role", label: "Role", defaultValue: n.role,
          options: ["control-plane", "customer-tenant", "public-web"].map((v) => ({ label: v, value: v })) },
        { name: "endpoint", label: "Endpoint", defaultValue: n.endpoint },
        { name: "status", label: "Status", defaultValue: n.status,
          options: ["active", "draining", "maintenance", "offline"].map((v) => ({ label: v, value: v })) },
      ],
    });
    if (!r) return;
    try { await api.put(`/admin/nodes/${n.id}`, r); flash("Node updated"); await load(); } catch { flash("Failed"); }
  }
  async function setNodeService(n: any, patch: { storage_service_id?: string; email_service_id?: string }) {
    try { await api.put(`/admin/nodes/${n.id}`, patch); flash("Services updated"); await load(); } catch { flash("Failed"); }
  }
  async function removeNode(n: any) {
    if (n.is_self) { void notify({ title: "Not allowed", message: "You can't remove the current node.", tone: "warn" }); return; }
    if (!await confirmDialog({ title: "Remove node?", message: `Remove ${n.name} from the fleet.`, tone: "danger", confirmLabel: "Remove" })) return;
    try { await api.del(`/admin/nodes/${n.id}`); flash("Node removed"); await load(); } catch { flash("Failed"); }
  }

  async function newInstaller() {
    const r = await formDialog({
      title: "Install a node", confirmLabel: "Generate command",
      message: "Generates a one-line command for a clean Ubuntu host. It installs only the selected role's components and links back to this control plane automatically (URL + fleet secret baked in).",
      fields: [
        { name: "role", label: "Node role", defaultValue: "public-web",
          options: [
            { label: "public-web — marketing website", value: "public-web" },
            { label: "customer-tenant — tenant app + portal", value: "customer-tenant" },
            { label: "control-plane — full stack", value: "control-plane" },
          ] },
        { name: "domain", label: "Public domain", placeholder: "arkive.life" },
      ],
    });
    if (!r) return;
    try {
      const res = await api.post<{ command: string }>("/admin/nodes/installer", { role: r.role, domain: r.domain });
      setInstallCmd(res.command);
      flash("Install command generated");
    } catch { flash("Failed"); }
  }

  const storageSvcs = svcs.filter((x) => x.category === "storage" && (x.capabilities || []).includes("cloud"));
  const emailSvcs = svcs.filter((x) => x.category === "email");

  if (sel) return (
    <NodeDetail id={sel} onBack={() => { setSel(null); void load(); }}
                storageSvcs={storageSvcs} emailSvcs={emailSvcs}
                onEdit={editNode} onService={setNodeService} onRemove={removeNode} />
  );

  const CATS = ["Control Plane", "Customer Nodes", "Public Web", "Other"];
  const hbar = (label: string, v: number | null | undefined) => {
    const val = Math.round(v || 0);
    const col = v == null ? "var(--border-soft)"
      : val >= 90 ? "#f2545b" : val >= 75 ? "#f5a623" : "linear-gradient(90deg,#4f7cff,#35d0a5)";
    return (
      <div style={{ flex: 1 }}>
        <div className="spread faint" style={{ fontSize: 10, marginBottom: 3 }}><span>{label}</span><span>{v == null ? "—" : `${val}%`}</span></div>
        <div style={{ height: 5, borderRadius: 999, background: "var(--inset)", overflow: "hidden" }}>
          <div style={{ width: `${val}%`, height: "100%", background: col }} />
        </div>
      </div>
    );
  };

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Node fleet <span className="faint" style={{ fontSize: 12, fontWeight: 400 }}>· {nodes.length} node{nodes.length === 1 ? "" : "s"}</span></h3>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <ProductionVersion label="Production platform" version={versions.platform} />
          <div className="row" style={{ gap: 8 }}>
            <button className="btn sm" onClick={newInstaller}><Icon name="logout" size={14} /> Install a node</button>
            <button className="btn primary sm" onClick={registerNode}><Icon name="server" size={14} /> Register node</button>
          </div>
        </div>
      </div>
      {installCmd && (
        <Card style={{ marginBottom: 12, background: "var(--bg-elev)" }}>
          <div className="spread" style={{ marginBottom: 6 }}>
            <div className="faint" style={{ fontSize: 12 }}>One-line install (run as sudo on a clean Ubuntu host)</div>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn sm" onClick={() => { void navigator.clipboard.writeText(installCmd); flash("Command copied"); }}>
                <Icon name="link" size={13} /> Copy
              </button>
              <button className="btn ghost sm" onClick={() => setInstallCmd("")}>Dismiss</button>
            </div>
          </div>
          <pre className="mono" style={{ fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-all", margin: 0 }}>{installCmd}</pre>
        </Card>
      )}
      {CATS.map((cat) => {
        const list = nodes.filter((n) => (n.category || "Other") === cat);
        if (!list.length) return null;
        return (
          <div key={cat} style={{ marginBottom: 18 }}>
            <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", marginBottom: 8 }}>{cat} · {list.length}</div>
            <div className="grid grid-3">
              {list.map((n) => (
                <Card key={n.id} onClick={() => setSel(n.id)} style={{ cursor: "pointer", padding: 14 }}>
                  <div className="spread" style={{ marginBottom: 10 }}>
                    <div className="row" style={{ gap: 9 }}>
                      <div className="result-icon" style={{ width: 30, height: 30, background: "var(--inset)", color: n.online ? "#35d0a5" : "#8a94a7" }}>
                        <Icon name="server" size={16} />
                      </div>
                      <div>
                        <div style={{ fontWeight: 700, fontSize: 13.5 }}>{n.name}{n.is_self && <span className="faint" style={{ fontWeight: 400, fontSize: 10 }}> · this</span>}</div>
                        <div className="faint" style={{ fontSize: 11 }}>{n.region || n.cloud?.region || "—"}{n.version ? ` · v${n.version}` : ""}</div>
                      </div>
                    </div>
                    <span title={n.online ? "Online" : "Offline"} style={{ width: 8, height: 8, borderRadius: 999, background: n.online ? "#35d0a5" : "#8a94a7", flexShrink: 0 }} />
                  </div>
                  <div className="row" style={{ gap: 8, marginBottom: 10 }}>
                    {hbar("CPU", n.health?.cpu_pct)}
                    {hbar("MEM", n.health?.mem_pct)}
                    {hbar("DISK", n.health?.disk_pct)}
                  </div>
                  <div className="faint" style={{ fontSize: 11, marginBottom: 6 }} title={n.last_heartbeat_at ? fmtAbsolute(n.last_heartbeat_at) : "never"}>
                    <Icon name="clock" size={11} /> Last seen {n.last_heartbeat_at ? timeAgo(n.last_heartbeat_at) : "never"}
                  </div>
                  <div className="row" style={{ gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
                    <VersionPill version={n.version} updateAvailable={n.update_available} />
                    {n.version_updated_at && <span className="faint" style={{ fontSize: 10.5 }}>updated {timeAgo(n.version_updated_at)}</span>}
                  </div>
                  <div className="spread faint" style={{ fontSize: 11 }}>
                    <span>{n.tenants || 0} tenant{n.tenants === 1 ? "" : "s"}</span>
                    <span>{n.status !== "active" ? n.status : uptimeShort(n.uptime_seconds)}</span>
                  </div>
                </Card>
              ))}
            </div>
          </div>
        );
      })}
      {nodes.length === 0 && <Card><div className="muted">No nodes registered.</div></Card>}
      <Workers />
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function uptimeShort(s?: number | null): string {
  if (!s || s <= 0) return "";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `up ${d}d ${h}h`;
  if (h) return `up ${h}h ${m}m`;
  return `up ${m}m`;
}

type NodeTab = "health" | "processes" | "keys" | "logs" | "tenants" | "config" | "queue";
const NODE_TABS: { key: NodeTab; label: string; icon: IconName }[] = [
  { key: "health", label: "System health", icon: "activity" },
  { key: "processes", label: "Processes & services", icon: "grid" },
  { key: "config", label: "Configuration", icon: "puzzle" },
  { key: "queue", label: "Activity queue", icon: "clock" },
  { key: "keys", label: "Keys & certificates", icon: "lock" },
  { key: "logs", label: "Logs", icon: "note" },
  { key: "tenants", label: "Tenant usage", icon: "user" },
];
const HISTORY_WINDOWS = ["1h", "6h", "24h", "7d", "30d", "90d"];
const MAX_LIVE = 60; // ~5 min of 5s live samples

const QUEUE_KIND_LABEL: Record<string, string> = {
  storage_write: "Storage write", appliance_ingest: "Sync to appliance", cloud_sync: "Sync to cloud",
};
const QUEUE_STATUS_TONE: Record<string, "ok" | "info" | "warn" | "danger"> = {
  queued: "warn", delivering: "info", done: "ok", failed: "danger", canceled: "info",
};

function NodeDetail({ id, onBack, storageSvcs, emailSvcs, onEdit, onService, onRemove }: {
  id: string; onBack: () => void; storageSvcs: ServiceObj[]; emailSvcs: ServiceObj[];
  onEdit: (n: any) => Promise<void> | void;
  onService: (n: any, patch: { storage_service_id?: string; email_service_id?: string }) => void;
  onRemove: (n: any) => Promise<void> | void;
}) {
  const [node, setNode] = useState<any>(null);
  const [live, setLive] = useState<any>(null);
  const [tab, setTab] = useState<NodeTab>("health");
  const [win, setWin] = useState("24h");
  const [history, setHistory] = useState<any[]>([]);
  const [keys, setKeys] = useState<any>(null);
  const [tenants, setTenants] = useState<any>(null);
  const [config, setConfig] = useState<any>(null);
  const [ov, setOv] = useState<Record<string, string>>({});
  const [queue, setQueue] = useState<any>(null);
  const [logs, setLogs] = useState<any[]>([]);
  const [logSource, setLogSource] = useState("app");
  const [logPaused, setLogPaused] = useState(false);
  const [toast, setToast] = useState("");
  const buf = useRef<Record<string, number[]>>({ cpu: [], mem: [], disk: [], net_sent: [], net_recv: [] });
  const [, force] = useState(0);
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 2800); }

  async function loadNode() { try { setNode(await api.get<any>(`/admin/nodes/${id}`)); } catch { /* ignore */ } }
  async function loadLive() {
    try {
      const l = await api.get<any>(`/admin/nodes/${id}/live`);
      setLive(l);
      const push = (k: string, v: number) => {
        const a = buf.current[k]; a.push(Number(v) || 0); if (a.length > MAX_LIVE) a.shift();
      };
      push("cpu", l.cpu_pct); push("mem", l.memory?.pct); push("disk", l.storage?.pct);
      push("net_sent", l.net?.sent_rate); push("net_recv", l.net?.recv_rate);
      force((x) => x + 1);
    } catch { /* offline */ }
  }
  async function loadHistory(w: string) { try { setHistory((await api.get<any>(`/admin/nodes/${id}/history?window=${w}`)).series || []); } catch { /* ignore */ } }
  async function loadKeys() { try { setKeys(await api.get<any>(`/admin/nodes/${id}/keys`)); } catch { /* ignore */ } }
  async function loadTenants() { try { setTenants(await api.get<any>(`/admin/nodes/${id}/tenants`)); } catch { /* ignore */ } }
  async function loadConfig() {
    try {
      const c = await api.get<any>(`/admin/nodes/${id}/config`);
      setConfig(c);
      const o: Record<string, string> = {};
      Object.entries(c.overrides || {}).forEach(([k, v]) => { o[k] = String(v); });
      setOv(o);
    } catch { /* ignore */ }
  }
  async function saveOverridesMap(map: Record<string, string>) {
    const payload: Record<string, unknown> = {};
    Object.entries(map).forEach(([k, v]) => { if (String(v).trim() !== "") payload[k] = v; });
    try {
      const c = await api.put<any>(`/admin/nodes/${id}/config-overrides`, { overrides: payload });
      setConfig(c);
      const o: Record<string, string> = {};
      Object.entries(c.overrides || {}).forEach(([k, v]) => { o[k] = String(v); });
      setOv(o);
      flash("Override saved");
    } catch (e) { flash((e as { message?: string }).message || "Save failed"); }
  }
  async function assignProfile(pid: string | null) {
    try {
      const c = await api.put<any>(`/admin/nodes/${id}/config-profile`, { profile_id: pid });
      setConfig(c);
      const o: Record<string, string> = {};
      Object.entries(c.overrides || {}).forEach(([k, v]) => { o[k] = String(v); });
      setOv(o);
      flash(pid ? "Profile assigned" : "Profile unassigned");
    } catch (e) { flash((e as { message?: string }).message || "Assignment failed"); }
  }
  async function openOverride(s: any) {
    const choices = s.choices || (s.key === "service.email" ? "email-service" : s.key === "service.storage" ? "storage-service" : s.key === "service.payment" ? "payment-service" : null);
    let field: any;
    if (choices === "timezone") {
      field = { name: "v", label: s.label || s.key, defaultValue: ov[s.key] ?? "",
        options: [{ label: "— inherit (use profile / UTC) —", value: "" },
          ...tzList().map((z) => ({ label: z, value: z }))] };
    } else if (choices) {
      const svc = config.services?.[choices] || [];
      field = { name: "v", label: s.label || s.key, defaultValue: ov[s.key] ?? "",
        options: [{ label: "— inherit (use profile / local) —", value: "" },
          ...svc.map((x: any) => ({ label: `${x.name}${x.configured ? "" : " (incomplete)"}`, value: x.id }))] };
    } else if (s.type === "bool") {
      field = { name: "v", label: s.label || s.key, defaultValue: ov[s.key] ?? "",
        options: [{ label: "— inherit —", value: "" }, { label: "true", value: "true" }, { label: "false", value: "false" }] };
    } else {
      field = { name: "v", label: s.label || s.key, defaultValue: ov[s.key] ?? "",
        placeholder: s.local_default != null ? `default: ${s.local_default}` : "value" };
    }
    const r = await formDialog({
      title: `Override · ${s.label || s.key}`,
      message: "Force a value on this node — takes precedence over the config profile. Leave blank to clear the override.",
      fields: [field], confirmLabel: "Save override",
    });
    if (!r) return;
    const val = String(r.v ?? "").trim();
    const next = { ...ov };
    if (val === "") delete next[s.key]; else next[s.key] = val;
    await saveOverridesMap(next);
  }
  async function loadLogs() { try { setLogs((await api.get<any>(`/admin/nodes/${id}/logs?source=${logSource}&lines=250`)).lines || []); } catch { /* ignore */ } }
  async function loadQueue() { try { setQueue(await api.get<any>(`/admin/nodes/${id}/queue`)); } catch { /* ignore */ } }
  async function queueAction(qid: string, action: string) {
    try { await api.post(`/admin/nodes/${id}/queue/${qid}/action`, { action }); flash(action === "retry" ? "Retrying now" : "Removed from queue"); await loadQueue(); }
    catch (e) { flash((e as { message?: string }).message || "Action failed"); }
  }

  useEffect(() => { void loadNode(); void loadLive(); void loadKeys(); void loadTenants();
    const iv = setInterval(loadLive, 5000); const nv = setInterval(loadNode, 15000);
    return () => { clearInterval(iv); clearInterval(nv); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  useEffect(() => { void loadHistory(win); const iv = setInterval(() => loadHistory(win), 30000); return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [win, id]);
  useEffect(() => { if (tab !== "logs") return; void loadLogs();
    const iv = setInterval(() => { if (!logPaused) void loadLogs(); }, 5000); return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, logSource, logPaused, id]);
  useEffect(() => { if (tab === "config") void loadConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, id]);
  useEffect(() => { if (tab !== "queue") return; void loadQueue();
    const iv = setInterval(() => void loadQueue(), 6000); return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, id]);

  async function ctl(action: string, unit: string, confirmMsg?: string) {
    if (confirmMsg && !await confirmDialog({ title: "Confirm", message: confirmMsg, tone: "danger", confirmLabel: action })) return;
    try {
      const r = await api.post<any>(`/admin/nodes/${id}/control`, { action, unit });
      flash(r.ok ? `${action} ${unit || ""} ok` : `Failed: ${r.error || "control error"}`);
      setTimeout(loadLive, 1500);
    } catch (e) { flash((e as { message?: string }).message || "Control failed"); }
  }

  async function backupNow() {
    try {
      const r = await api.post<{ ok: boolean; message?: string }>(`/admin/nodes/${id}/backup`, {});
      flash(r.ok ? (r.message || "Backup started") : `Backup failed: ${r.message || "unknown error"}`);
    } catch (e) { flash((e as { message?: string }).message || "Backup failed"); }
  }

  if (!node) return (
    <Card><button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 10 }}>← Nodes</button>
      <div className="muted">Loading node…</div></Card>
  );

  const rate = (n?: number) => `${bytes(n || 0)}/s`;
  const cert = live?.certificate || keys?.certificate;
  const labels = history.map((p) => { const d = new Date(p.ts.endsWith("Z") ? p.ts : p.ts + "Z"); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; });

  return (
    <>
      <div className="spread" style={{ marginBottom: 12, alignItems: "center" }}>
        <button className="btn ghost sm" onClick={onBack}>← Nodes</button>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <ProductionVersion label="Production platform" version={node.production_version} />
          <div className="row" style={{ gap: 8 }}>
            <button className="btn sm" onClick={async () => { await onEdit(node); await loadNode(); }}>Edit</button>
            <button className="btn sm" onClick={() => ctl("update", "", "Trigger a software update on this node? It will pull the latest build and restart.")}>
              <Icon name="clock" size={13} /> Update
            </button>
            {node.role !== "public-web" && (
              <button className="btn sm" onClick={backupNow}>
                <Icon name="shield" size={13} /> Back up now
              </button>
            )}
            <button className="btn sm" onClick={() => ctl("restart", "cv-cloud", "Restart the Arkive application on this node?")}>Restart app</button>
            {!node.is_self && <button className="btn danger sm" onClick={async () => { await onRemove(node); onBack(); }}>Remove</button>}
          </div>
        </div>
      </div>

      <Card style={{ marginBottom: 14 }}>
        <div className="spread">
          <div className="row" style={{ gap: 12 }}>
            <div className="result-icon" style={{ width: 40, height: 40, background: "var(--inset)", color: node.online ? "#35d0a5" : "#8a94a7" }}>
              <Icon name="server" size={20} />
            </div>
            <div>
              <h3 style={{ margin: 0 }}>{node.name} {node.is_self && <span className="faint" style={{ fontWeight: 400, fontSize: 12 }}>· this node</span>}</h3>
              <div className="faint" style={{ fontSize: 12 }}>
                {node.category} · {node.role}{node.region ? ` · ${node.region}` : ""}{node.version ? ` · v${node.version}` : ""}
                {node.version_updated_at ? ` · updated ${timeAgo(node.version_updated_at)}` : ""}
                {live?.uptime_seconds ? ` · ${uptimeShort(live.uptime_seconds)}` : ""}
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <Pill tone={node.online ? "ok" : "warn"}>{node.online ? "Online" : "Offline"}</Pill>
            <Pill tone={node.status === "active" ? "info" : "warn"}>{node.status}</Pill>
            {live?.source === "heartbeat" && <Pill tone="warn">heartbeat only</Pill>}
            <VersionPill version={node.version} updateAvailable={node.update_available} />
          </div>
        </div>
        {(node.cloud?.provider && node.cloud.provider !== "unknown") && (
          <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            <Pill tone="info"><Icon name="database" size={11} /> {String(node.cloud.provider).toUpperCase()}{node.cloud.region ? ` · ${node.cloud.region}` : ""}</Pill>
            {node.cloud.instance_type && <span className="faint" style={{ fontSize: 11.5 }}>{node.cloud.instance_type}</span>}
            {live?.hostname && <span className="faint" style={{ fontSize: 11.5 }}>{live.hostname}</span>}
            {live?.os && <span className="faint" style={{ fontSize: 11.5 }}>{live.os}</span>}
          </div>
        )}
      </Card>

      <div className="row" style={{ gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        {NODE_TABS.map((t) => (
          <button key={t.key} className={`btn sm ${tab === t.key ? "primary" : "ghost"}`} onClick={() => setTab(t.key)}>
            <Icon name={t.icon} size={13} /> {t.label}
          </button>
        ))}
      </div>

      {tab === "health" && (
        <>
          <div className="grid grid-4" style={{ marginBottom: 14 }}>
            <Card><div style={{ display: "flex", justifyContent: "center" }}><Ring value={live?.cpu_pct} label="CPU" sub={`${live?.cpus || node.cpus || "—"} vCPU`} /></div>
              <div style={{ marginTop: 6 }}><Sparkline data={buf.current.cpu} width={999} color="#4f7cff" max={100} /></div></Card>
            <Card><div style={{ display: "flex", justifyContent: "center" }}><Ring value={live?.memory?.pct} label="Memory" sub={live?.memory ? `${bytes(live.memory.used)}/${bytes(live.memory.total)}` : ""} color="#35d0a5" /></div>
              <div style={{ marginTop: 6 }}><Sparkline data={buf.current.mem} width={999} color="#35d0a5" max={100} /></div></Card>
            <Card><div style={{ display: "flex", justifyContent: "center" }}><Ring value={live?.storage?.pct} label="Disk" sub={live?.storage ? `${bytes(live.storage.used)}/${bytes(live.storage.total)}` : ""} color="#f5a623" /></div>
              <div style={{ marginTop: 6 }}><Sparkline data={buf.current.disk} width={999} color="#f5a623" max={100} /></div></Card>
            <Card>
              <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>Network</div>
              <div style={{ fontWeight: 700 }}>↓ {rate(live?.net?.recv_rate)}</div>
              <div style={{ fontWeight: 700, marginBottom: 6 }}>↑ {rate(live?.net?.sent_rate)}</div>
              <Sparkline data={buf.current.net_recv} width={999} color="#4f7cff" />
              <Sparkline data={buf.current.net_sent} width={999} color="#8a94a7" />
              <div className="faint" style={{ fontSize: 10.5, marginTop: 4 }}>Load {(live?.load || node.telemetry?.load || []).join(" ") || "—"}</div>
            </Card>
          </div>
          <Card>
            <div className="spread" style={{ marginBottom: 10 }}>
              <h3 style={{ margin: 0, fontSize: 15 }}>Trends</h3>
              <div className="row" style={{ gap: 4 }}>
                {HISTORY_WINDOWS.map((w) => (
                  <button key={w} className={`btn sm ${win === w ? "primary" : "ghost"}`} onClick={() => setWin(w)}>{w}</button>
                ))}
              </div>
            </div>
            <div className="row" style={{ gap: 14, marginBottom: 6, fontSize: 11.5 }}>
              <span className="faint"><span style={{ color: "#4f7cff" }}>■</span> CPU</span>
              <span className="faint"><span style={{ color: "#35d0a5" }}>■</span> Memory</span>
              <span className="faint"><span style={{ color: "#f5a623" }}>■</span> Disk</span>
            </div>
            {history.length ? (
              <AreaChart height={200} max={100} labels={labels} series={[
                { name: "cpu", color: "#4f7cff", data: history.map((p) => p.cpu) },
                { name: "mem", color: "#35d0a5", data: history.map((p) => p.mem) },
                { name: "disk", color: "#f5a623", data: history.map((p) => p.disk) },
              ]} />
            ) : <div className="muted" style={{ fontSize: 12, padding: "24px 0" }}>No history yet — samples accumulate every minute.</div>}
            {history.length > 0 && (
              <div style={{ marginTop: 14 }}>
                <div className="faint" style={{ fontSize: 11.5, marginBottom: 4 }}>Network throughput (bytes/s)</div>
                <AreaChart height={140} unit="" labels={labels} series={[
                  { name: "recv", color: "#4f7cff", data: history.map((p) => p.net_recv) },
                  { name: "sent", color: "#8a94a7", data: history.map((p) => p.net_sent) },
                ]} />
              </div>
            )}
          </Card>
        </>
      )}

      {tab === "processes" && (
        <>
          <Card style={{ marginBottom: 14 }}>
            <h3 style={{ margin: "0 0 4px", fontSize: 15 }}>Managed services</h3>
            <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>System processes and their state. Controls require the service account to have sudo for systemctl.</div>
            <table className="table">
              <thead><tr><th>Service</th><th>State</th><th>Memory</th><th>Startup</th><th></th></tr></thead>
              <tbody>
                {(live?.services || []).map((s: any) => (
                  <tr key={s.unit}>
                    <td><div style={{ fontWeight: 600 }}>{s.label || s.unit}</div><div className="faint mono" style={{ fontSize: 10.5 }}>{s.unit}</div></td>
                    <td><Pill tone={s.active === "active" ? "ok" : s.active === "failed" ? "danger" : "warn"}>{s.active}{s.sub_state ? ` · ${s.sub_state}` : ""}</Pill></td>
                    <td className="faint">{s.memory_bytes ? bytes(s.memory_bytes) : "—"}</td>
                    <td><Pill tone={s.enabled === "enabled" ? "info" : "warn"}>{s.enabled}</Pill></td>
                    <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                      <button className="btn ghost sm" onClick={() => ctl("restart", s.unit)}>Restart</button>{" "}
                      {s.active === "active"
                        ? <button className="btn ghost sm" onClick={() => ctl("stop", s.unit, `Stop ${s.label || s.unit}?`)}>Stop</button>
                        : <button className="btn ghost sm" onClick={() => ctl("start", s.unit)}>Start</button>}{" "}
                      {s.enabled === "enabled"
                        ? <button className="btn ghost sm" onClick={() => ctl("disable", s.unit, `Disable ${s.label || s.unit} at boot?`)}>Disable</button>
                        : <button className="btn ghost sm" onClick={() => ctl("enable", s.unit)}>Enable</button>}
                    </td>
                  </tr>
                ))}
                {(!live?.services || live.services.length === 0) && <tr><td colSpan={5} className="muted">Service status unavailable{live?.source === "heartbeat" ? " (node reports via heartbeat only)" : ""}.</td></tr>}
              </tbody>
            </table>
          </Card>
          {(live?.workers || []).length > 0 && (
            <Card style={{ marginBottom: 14 }}>
              <h3 style={{ margin: "0 0 4px", fontSize: 15 }}>Background workers</h3>
              <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>In-process workers (replication, index integrity). Health is live and resets on restart.</div>
              <table className="table">
                <thead><tr><th>Worker</th><th>Health</th><th>State</th><th>Last activity</th><th>Detail</th></tr></thead>
                <tbody>
                  {(live?.workers || []).map((w: any) => (
                    <tr key={w.name}>
                      <td><div style={{ fontWeight: 600 }}>{w.name}</div>{w.runs != null && <div className="faint" style={{ fontSize: 10.5 }}>{w.runs} run(s)</div>}</td>
                      <td><Pill tone={w.health === "ok" ? "ok" : w.health === "error" ? "danger" : "warn"} dot>{w.health}</Pill></td>
                      <td><Pill tone={w.state === "running" ? "info" : w.state === "failed" ? "danger" : "ok"}>{w.state}</Pill></td>
                      <td className="faint" style={{ fontSize: 12 }}>{w.updated_at ? fmtAbsolute(w.updated_at) : "—"}</td>
                      <td className="faint" style={{ fontSize: 12 }}>{w.message || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
          <div className="grid grid-2">
            <Card>
              <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Top processes</h3>
              <table className="table">
                <thead><tr><th>Process</th><th>CPU</th><th>Mem</th><th>RSS</th></tr></thead>
                <tbody>
                  {(live?.processes || []).map((p: any, i: number) => (
                    <tr key={i}><td className="mono" style={{ fontSize: 12 }}>{p.name}</td>
                      <td className="faint">{p.cpu_pct}%</td><td className="faint">{p.mem_pct}%</td><td className="faint">{bytes(p.rss_bytes)}</td></tr>
                  ))}
                  {(!live?.processes || live.processes.length === 0) && <tr><td colSpan={4} className="muted">—</td></tr>}
                </tbody>
              </table>
            </Card>
            <Card>
              <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Database (PostgreSQL)</h3>
              <div className="grid grid-2" style={{ gap: 10 }}>
                <Mini label="Size on disk" value={live?.db?.size_bytes != null ? bytes(live.db.size_bytes) : "—"} />
                <Mini label="Active connections" value={live?.db?.connections ?? "—"} />
                <Mini label="Recovery points" value={(node.telemetry?.recovery_points ?? "—").toLocaleString?.() ?? "—"} />
                <Mini label="Tenants on node" value={node.tenants} />
              </div>
              <div className="divider" />
              <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", marginBottom: 8 }}>Assigned services</div>
              <div className="faint" style={{ fontSize: 12 }}>
                <Icon name="database" size={12} /> Storage: <b>{node.storage_service || "default"}</b> · <Icon name="mail" size={12} /> Email: <b>{node.email_service || "default"}</b>
              </div>
              <div className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>
                Storage &amp; email backends are now assigned in the <b>Configuration</b> tab — set <code>service.storage</code> / <code>service.email</code> via a configuration profile (fleet-wide) or a per-node override.
              </div>
            </Card>
          </div>
        </>
      )}

      {tab === "keys" && (
        <div className="grid grid-2">
          <Card>
            <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>TLS certificate</h3>
            {cert ? (cert.reachable ? (
              <>
                <div className="row" style={{ gap: 8, marginBottom: 10 }}>
                  <Pill tone={cert.valid === false ? "danger" : (cert.days_left != null && cert.days_left < 14) ? "warn" : "ok"}>
                    {cert.valid === false ? "Invalid" : "Valid"}
                  </Pill>
                  {cert.days_left != null && <Pill tone={cert.days_left < 14 ? "warn" : "info"}>{cert.days_left} days left</Pill>}
                </div>
                <Row2 label="Subject" value={cert.subject?.commonName || "—"} />
                <Row2 label="Issuer" value={cert.issuer?.organizationName || cert.issuer?.commonName || "—"} />
                <Row2 label="Expires" value={cert.expires_epoch ? new Date(cert.expires_epoch * 1000).toLocaleString() : "—"} />
              </>
            ) : <div className="muted" style={{ fontSize: 12.5 }}>Certificate not reachable{cert.error ? ` · ${cert.error}` : ""}.</div>)
              : <div className="muted" style={{ fontSize: 12.5 }}>Loading…</div>}
          </Card>
          <Card>
            <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Key integrity</h3>
            <div className="grid grid-2" style={{ gap: 10 }}>
              <Mini label="Vault keys" value={keys ? `${keys.vault_keys?.provisioned ?? 0} / ${keys.vault_keys?.total ?? 0}` : "—"} />
              <Mini label="Post-quantum" value={keys?.pq_hybrid ? "Hybrid" : keys ? "Classical" : "—"} />
            </div>
            <div style={{ marginTop: 10 }}>
              <Row2 label="Command signer" value={keys?.signer_key_id ? String(keys.signer_key_id).slice(0, 20) + "…" : "—"} />
              <Row2 label="Provisioned" value={keys ? `${keys.vault_keys?.total ? Math.round((keys.vault_keys.provisioned / keys.vault_keys.total) * 100) : 100}% of vaults` : "—"} />
            </div>
            <div className="row" style={{ gap: 6, marginTop: 12 }}>
              <Pill tone={keys?.pq_hybrid ? "ok" : "info"}>{keys?.pq_hybrid ? "ML-KEM / ML-DSA active" : "classical fallback"}</Pill>
            </div>
          </Card>
        </div>
      )}

      {tab === "logs" && (
        <Card>
          <div className="spread" style={{ marginBottom: 10 }}>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <h3 style={{ margin: 0, fontSize: 15 }}>Logs</h3>
              <select className="input sm" value={logSource} onChange={(e) => setLogSource(e.target.value)}>
                {[["app", "Application"], ["database", "PostgreSQL"], ["proxy", "Reverse proxy"], ["heartbeat", "Heartbeat"], ["system", "System"]].map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </div>
            <div className="row" style={{ gap: 8 }}>
              <button className="btn ghost sm" onClick={() => setLogPaused((p) => !p)}>{logPaused ? "Resume" : "Pause"}</button>
              <button className="btn ghost sm" onClick={() => void loadLogs()}>Refresh</button>
            </div>
          </div>
          <div className="mono" style={{ fontSize: 11, background: "var(--bg-elev-2,#0b0f17)", borderRadius: 8, padding: 12, maxHeight: 460, overflow: "auto", lineHeight: 1.5 }}>
            {logs.length ? logs.map((l, i) => (
              <div key={i} style={{ color: l.level === "error" ? "#f2545b" : l.level === "warn" ? "#f5a623" : "var(--muted-c,#8a94a7)", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{l.text}</div>
            )) : <div className="muted">No log output{live?.source === "heartbeat" ? " (unavailable for this node type)" : ""}.</div>}
          </div>
        </Card>
      )}

      {tab === "tenants" && (
        <Card>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h3 style={{ margin: 0, fontSize: 15 }}>Tenant usage on this node</h3>
            <span className="faint" style={{ fontSize: 12 }}>{tenants?.tenants?.length || 0} tenants · {bytes(tenants?.total_bytes || 0)} total</span>
          </div>
          <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>Heaviest tenants are highlighted — candidates to rebalance onto another node.</div>
          <table className="table">
            <thead><tr><th>Tenant</th><th>Share</th><th>Data</th><th>Objects</th><th>Users</th><th>Recovery pts</th><th>Last activity</th></tr></thead>
            <tbody>
              {(tenants?.tenants || []).map((t: any) => (
                <tr key={t.id} style={t.heavy ? { background: "rgba(242,84,91,.06)" } : undefined}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{t.name} {t.heavy && <Pill tone="danger">heavy</Pill>}</div>
                    <div className="faint" style={{ fontSize: 11 }}>{t.tenant_type}</div>
                  </td>
                  <td style={{ minWidth: 90 }}>
                    <div className="spread faint" style={{ fontSize: 10.5, marginBottom: 3 }}><span>{t.share}%</span></div>
                    <div style={{ height: 5, borderRadius: 999, background: "var(--inset)", overflow: "hidden" }}>
                      <div style={{ width: `${Math.min(100, t.share)}%`, height: "100%", background: t.heavy ? "#f2545b" : "linear-gradient(90deg,#4f7cff,#35d0a5)" }} />
                    </div>
                  </td>
                  <td style={{ fontWeight: 600 }}>{bytes(t.bytes)}</td>
                  <td className="faint">{t.objects.toLocaleString()}</td>
                  <td className="faint">{t.users}</td>
                  <td className="faint">{t.recovery_points}</td>
                  <td className="faint">{t.last_activity ? timeAgo(t.last_activity) : "—"}</td>
                </tr>
              ))}
              {(!tenants?.tenants || tenants.tenants.length === 0) && <tr><td colSpan={7} className="muted">No tenants on this node.</td></tr>}
            </tbody>
          </table>
        </Card>
      )}
      {tab === "config" && (
        <>
          <Card style={{ marginBottom: 14 }}>
            <div className="spread" style={{ marginBottom: 8 }}>
              <h3 style={{ margin: 0, fontSize: 15 }}>Assigned configuration profile</h3>
              <span className="faint" style={{ fontSize: 12 }}>One profile per node</span>
            </div>
            {!config ? <div className="muted">Loading…</div> : (
              <>
                {config.config_profile ? (
                  <div className="spread" style={{ border: "1px solid var(--border)", borderRadius: 10, padding: "10px 12px", marginBottom: 12 }}>
                    <div>
                      <div style={{ fontWeight: 700 }}>{config.config_profile.name} <span className="faint" style={{ fontWeight: 400, fontSize: 11.5 }}>· {config.config_profile.key_count} setting{config.config_profile.key_count === 1 ? "" : "s"}</span></div>
                      {config.config_profile.description && <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>{config.config_profile.description}</div>}
                    </div>
                    <div className="row" style={{ gap: 6, alignItems: "center" }}>
                      <Pill tone={config.config_profile.enabled ? "ok" : "warn"} dot>{config.config_profile.enabled ? "enabled" : "disabled"}</Pill>
                      <button className="btn ghost sm" onClick={() => assignProfile(null)}>Unassign</button>
                    </div>
                  </div>
                ) : (
                  <div className="muted" style={{ marginBottom: 12 }}>No configuration profile assigned — this node uses built-in defaults (plus any overrides below).</div>
                )}
                <label className="row" style={{ gap: 8, alignItems: "center" }}>
                  <span className="faint" style={{ fontSize: 12 }}>Assign profile</span>
                  <select className="input sm" style={{ maxWidth: 320 }} value={config.config_profile?.id || ""} onChange={(e) => assignProfile(e.target.value || null)}>
                    <option value="">— none —</option>
                    {(config.available_profiles || []).map((p: any) => (
                      <option key={p.id} value={p.id}>{p.name}{p.enabled ? "" : " (disabled)"}</option>
                    ))}
                  </select>
                </label>
                {(config.available_profiles || []).length === 0 && (
                  <div className="faint" style={{ fontSize: 11.5, marginTop: 6 }}>No node-type profiles exist yet — create one under Configurations → Configuration profiles.</div>
                )}
              </>
            )}
          </Card>

          <Card>
            <div className="spread" style={{ marginBottom: 8 }}>
              <h3 style={{ margin: 0, fontSize: 15 }}>Effective settings</h3>
              <div className="row" style={{ gap: 6 }}>
                {config?.applied_source === "unreachable" && <Pill tone="warn" dot>node unreachable</Pill>}
                {config?.applied_source === "node" && <Pill tone="info" dot>live from node</Pill>}
              </div>
            </div>
            <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
              Precedence: <b>override</b> → <b>config profile</b> → <b>local default</b>.
            </div>
            {!config ? <div className="muted">Loading…</div>
              : (
              <table className="table">
                <thead><tr><th>Setting</th><th>Effective value</th><th>Source</th><th style={{ textAlign: "right" }}>Override</th></tr></thead>
                <tbody>
                  {config.settings.map((s: any) => {
                    const choices = s.choices || (s.key === "service.email" ? "email-service" : s.key === "service.storage" ? "storage-service" : s.key === "service.payment" ? "payment-service" : null);
                    const services = choices ? (config.services?.[choices] || []) : null;
                    const svcName = (id: string) => (services || []).find((x: any) => x.id === id)?.name || id;
                    const shownVal = choices && s.value ? svcName(String(s.value)) : (s.value == null || s.value === "" ? "—" : String(s.value));
                    const appliedVal = config.applied ? config.applied[s.key] : undefined;
                    const drift = config.applied != null && s.source !== "local" && String(appliedVal ?? "") !== String(s.value ?? "");
                    return (
                      <tr key={s.key}>
                        <td>
                          <div style={{ fontWeight: 600, fontSize: 12.5 }}>{s.label || s.key}</div>
                          <div className="faint" style={{ fontSize: 11 }}>{s.key}{s.group ? ` · ${s.group}` : ""}</div>
                        </td>
                        <td style={{ fontWeight: 600 }}>
                          {shownVal}{s.unit ? <span className="faint" style={{ fontSize: 11, marginLeft: 4 }}>{s.unit}</span> : null}
                          {drift && <div style={{ fontSize: 10.5 }}><Pill tone="warn" dot>node has: {String(appliedVal ?? "unset")}</Pill></div>}
                        </td>
                        <td>
                          {s.source === "override" ? <Pill tone="info" dot>Override</Pill>
                            : s.source === "profile" ? <Pill tone="ok" dot>Config profile</Pill>
                            : <span className="faint" style={{ fontSize: 12 }}>Local default</span>}
                        </td>
                        <td style={{ textAlign: "right" }}>
                          <button className="btn ghost sm" onClick={() => openOverride(s)}>
                            {s.source === "override" ? "Edit override" : "Override"}
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
      {tab === "queue" && (
        <Card>
          <div className="spread" style={{ marginBottom: 8 }}>
            <div>
              <h3 style={{ margin: 0, fontSize: 15 }}>Activity queue</h3>
              <div className="faint" style={{ fontSize: 12 }}>
                Backups pending delivery to an offline appliance or unreachable storage (appliance, Arkive Cloud, or your cloud). Retries run automatically and the queue empties once the connection is restored.
              </div>
            </div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <Pill tone={(queue?.counts?.active || 0) > 0 ? "warn" : "ok"} dot>{queue?.counts?.active || 0} pending</Pill>
              {(queue?.counts?.failed || 0) > 0 && <Pill tone="danger" dot>{queue.counts.failed} failed</Pill>}
              <button className="btn ghost sm" onClick={() => void loadQueue()}><Icon name="repeat" size={13} /> Refresh</button>
            </div>
          </div>
          {queue?.unreachable && <div className="muted" style={{ fontSize: 12.5, marginBottom: 8 }}>Node unreachable — can't read its live queue right now.</div>}
          {!queue ? <div className="muted">Loading…</div>
            : ([...(queue.active || []), ...(queue.recent || [])].length === 0 ? (
              <div className="result-row" style={{ background: "var(--inset)", borderRadius: 10 }}>
                <div className="result-icon" style={{ background: "var(--inset)", color: "#35d0a5" }}><Icon name="check" size={16} /></div>
                <div className="flex1"><div style={{ fontWeight: 600 }}>Queue is empty</div><div className="faint" style={{ fontSize: 12 }}>All destinations reachable — nothing waiting to deliver.</div></div>
              </div>
            ) : (
              <table className="table">
                <thead><tr><th>Activity</th><th>Destination</th><th>Status</th><th>Attempts</th><th>Next retry</th><th>Last error</th><th></th></tr></thead>
                <tbody>
                  {[...(queue.active || []), ...(queue.recent || [])].map((q: any) => (
                    <tr key={q.id}>
                      <td>
                        <div style={{ fontWeight: 600, fontSize: 12.5 }}>{q.label || q.target_label}</div>
                        <div className="faint" style={{ fontSize: 11 }}>{QUEUE_KIND_LABEL[q.kind] || q.kind}</div>
                      </td>
                      <td><span className="row" style={{ gap: 6 }}><DestIcon dest={q.target} size={13} /> {q.target_label}</span></td>
                      <td><Pill tone={QUEUE_STATUS_TONE[q.status] || "info"} dot>{q.status}</Pill></td>
                      <td className="faint" style={{ fontSize: 12 }}>{q.attempts}/{q.max_attempts}</td>
                      <td className="faint" style={{ fontSize: 12 }}>
                        {q.status === "queued" && q.next_attempt_at ? timeAgo(q.next_attempt_at)
                          : q.status === "done" ? "delivered" : "—"}
                      </td>
                      <td className="faint" style={{ fontSize: 11.5, maxWidth: 260, whiteSpace: "normal" }}>{q.last_error || "—"}</td>
                      <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                        {["queued", "failed", "delivering"].includes(q.status) && <button className="btn ghost sm" onClick={() => void queueAction(q.id, "retry")}>Retry now</button>}{" "}
                        {["queued", "failed"].includes(q.status) && <button className="btn ghost sm" onClick={() => void queueAction(q.id, "cancel")}>Cancel</button>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ))}
        </Card>
      )}
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function Row2({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="spread" style={{ padding: "7px 0", borderBottom: "1px solid var(--border-soft)" }}>
      <span className="faint" style={{ fontSize: 12 }}>{label}</span>
      <span style={{ fontSize: 12.5, fontWeight: 500 }}>{value}</span>
    </div>
  );
}

function Workers() {
  const [data, setData] = useState<{ active: number; jobs: any[] } | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [logJob, setLogJob] = useState<any | null>(null);
  async function load() {
    try {
      setData(await api.get<{ active: number; jobs: any[] }>(
        `/admin/jobs?active=${showAll ? "false" : "true"}&limit=${showAll ? 120 : 60}`));
    } catch { /* ignore */ }
  }
  useEffect(() => { void load(); const iv = setInterval(load, 4000); return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showAll]);

  async function openLog(j: any) {
    try { setLogJob(await api.get<any>(`/admin/jobs/${j.id}/log`)); } catch { /* ignore */ }
  }

  async function cancel(j: any) {
    const ok = await confirmDialog({
      title: "Stop worker?", tone: "danger", confirmLabel: "Stop worker",
      message: `Cancel the ${j.source_type || j.kind} job for ${j.tenant}? It aborts at its next checkpoint; already-stored recovery points are kept.`,
    });
    if (!ok) return;
    try { await api.post(`/admin/jobs/${j.id}/cancel`, {}); await load(); } catch { /* ignore */ }
  }

  const jobs = data?.jobs || [];
  const active = data?.active ?? 0;
  const isActive = (s: string) => ["queued", "running", "cancelling"].includes(s);
  const statusColor = (s: string): string =>
    s === "running" ? "#4f7cff" : s === "done" ? "#35d0a5" : s === "failed" ? "#f2545b"
      : s === "cancelled" ? "#8a94a7" : "#f5a623";

  return (
    <Card style={{ marginTop: 16 }}>
      <div className="spread" style={{ marginBottom: 8 }}>
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>Source Sync Worker Processes</h3>
          <Pill tone={active > 0 ? "info" : "ok"}>{active} active</Pill>
        </div>
        <button className="btn ghost sm" onClick={() => setShowAll((v) => !v)}>
          {showAll ? "Active only" : "Show recent"}
        </button>
      </div>
      <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
        Background jobs across all tenants: recent <b>Sync</b> (scheduled / manual) and paced deep-history <b>Backfill</b> crawls.
        Stopping a worker aborts it at its next chunk boundary.
      </div>
      <table className="table">
        <thead><tr><th></th><th>Tenant / Source</th><th>Node</th><th>Result</th><th>Time</th><th></th></tr></thead>
        <tbody>
          {jobs.map((j) => (
            <tr key={j.id}>
              <td style={{ width: 24, textAlign: "center" }}>
                <span className="faint" title={j.trigger === "schedule" ? "Scheduled" : "Manual"} style={{ display: "inline-flex" }}>
                  <Icon name={j.trigger === "schedule" ? "clock" : "user"} size={15} />
                </span>
              </td>
              <td>
                <div className="row" style={{ gap: 6, alignItems: "center" }}>
                  <span style={{ fontWeight: 600 }}>{j.tenant}{j.owner ? ` → ${j.owner}` : ""}</span>
                  <JobKindBadge kind={j.kind} />
                </div>
                <div className="faint row" style={{ fontSize: 11, gap: 5, alignItems: "center" }}>
                  {j.source_type && (brandForSource(j.source_type)
                    ? <BrandIcon name={brandForSource(j.source_type)!} size={13} />
                    : <Icon name="database" size={12} />)}
                  <span>{j.source}{j.source_username && j.source_username !== j.source ? ` (${j.source_username})` : ""}</span>
                </div>
              </td>
              <td style={{ whiteSpace: "nowrap" }}>{j.node || "Control plane"}</td>
              <td>
                <div className="row" style={{ gap: 8, alignItems: "center" }}>
                  <span title={j.status} style={{ width: 9, height: 9, borderRadius: 999, background: statusColor(j.status), flexShrink: 0 }} />
                  <span className="faint">{(j.processed || 0).toLocaleString()}{j.total ? ` / ${(j.total).toLocaleString()}` : ""}</span>
                </div>
                {isActive(j.status) && j.total > 0 && (
                  <div className={`progress ${j.kind === "backfill" ? "backfill" : ""}`} style={{ marginTop: 4, maxWidth: 160 }}>
                    <span style={{ width: `${Math.min(100, (j.processed / j.total) * 100)}%` }} />
                  </div>
                )}
                {(j.source_type || j.message) && (
                  <div className="faint" style={{ fontSize: 10.5 }}>{j.source_type || j.kind}{j.message ? ` · ${j.message}` : ""}</div>
                )}
              </td>
              <td className="faint" style={{ fontSize: 10.5, whiteSpace: "nowrap" }}>
                {j.started_at && <div>started {fmtAbsolute(j.started_at)}</div>}
                {j.finished_at
                  ? <div>finished {fmtAbsolute(j.finished_at)}</div>
                  : (!j.started_at && <div>queued {fmtAbsolute(j.created_at)}</div>)}
              </td>
              <td style={{ textAlign: "right" }}>
                <div className="row" style={{ gap: 8, justifyContent: "flex-end", alignItems: "center" }}>
                  {j.has_log && <button className="btn ghost sm" onClick={() => openLog(j)}><Icon name="note" size={12} /> Log</button>}
                  {isActive(j.status) && j.status !== "cancelling"
                    ? <button className="btn danger sm" onClick={() => cancel(j)}>Stop</button>
                    : <span className="faint" style={{ fontSize: 11 }}>{j.status === "cancelling" ? "stopping…" : ""}</span>}
                </div>
              </td>
            </tr>
          ))}
          {jobs.length === 0 && <tr><td colSpan={6} className="muted">{showAll ? "No jobs yet." : "No active workers."}</td></tr>}
        </tbody>
      </table>
      {logJob && (
        <div className="modal-backdrop" onClick={() => setLogJob(null)}>
          <div className="modal-panel" style={{ width: "min(860px, 100%)" }} onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div>
                <h3 style={{ margin: 0 }}>Process log</h3>
                <div className="faint" style={{ fontSize: 12 }}>{logJob.trigger === "schedule" ? "Scheduled" : "Manual"} · {logJob.status}</div>
              </div>
              <button className="btn ghost sm" onClick={() => setLogJob(null)}>Close</button>
            </div>
            <div className="modal-body">
              {logJob.error && <div className="faint" style={{ color: "var(--danger)", fontSize: 12.5, marginBottom: 8 }}>{logJob.error}</div>}
              <div className="terminal-log">
                {(logJob.log || []).length === 0
                  ? <div className="faint">No log captured for this job.</div>
                  : (logJob.log || []).map((l: any, i: number) => (
                      <div key={i} className={`tlog-line lvl-${(l.level || "INFO").toLowerCase()}`}>
                        <span className="tlog-ts">{fmtAbsolute(l.ts)}</span>
                        <span className="tlog-lvl">{(l.level || "INFO").padEnd(7)}</span>
                        <span className="tlog-msg">{l.msg}</span>
                      </div>
                    ))}
              </div>
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

interface CatalogItem { key: string; label: string; type: string; group: string; example: string; unit?: string; description: string; choices?: string; }
interface NodeRef { id: string; name: string; role: string; }
interface Profile {
  id: string; name: string; description: string; data: Record<string, any>;
  kind: string; target: "node" | "appliance"; enabled: boolean;
  key_count: number; assigned_count: number; updated_at?: string;
}

function ConfigProfiles({ flash: extFlash }: { flash?: (m: string) => void } = {}) {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [toast, setToast] = useState("");
  const flash = (m: string) => { if (extFlash) extFlash(m); else { setToast(m); setTimeout(() => setToast(""), 3000); } };
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [nodes, setNodes] = useState<NodeRef[]>([]);
  const [editing, setEditing] = useState<Profile | "new" | null>(null);

  async function load() {
    try {
      const r = await api.get<{ profiles: Profile[]; catalog: CatalogItem[]; nodes: NodeRef[] }>("/admin/config-profiles");
      setProfiles(r.profiles || []); setCatalog(r.catalog || []); setNodes(r.nodes || []);
    } catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  async function remove(p: Profile) {
    if (!(await confirmDialog({ title: `Delete “${p.name}”?`, message: `${p.target === "appliance" ? "Appliances" : "Nodes"} assigned to it revert to default settings.`, tone: "danger", confirmLabel: "Delete" }))) return;
    try { await api.del(`/admin/config-profiles/${p.id}`); flash("Profile deleted"); await load(); }
    catch (e: any) { void notify({ message: e.message, tone: "danger" }); }
  }

  if (editing) {
    return (
      <ProfileEditor profile={editing === "new" ? null : editing} catalog={catalog}
                     onDone={() => { setEditing(null); void load(); }} onCancel={() => setEditing(null)} />
    );
  }

  return (
    <>
      <div className="spread" style={{ margin: "22px 0 12px" }}>
        <div>
          <h3 style={{ margin: 0 }}>Configuration profiles</h3>
          <span className="faint" style={{ fontSize: 12 }}>Reusable settings bundles you assign to a node or an appliance (from its own section)</span>
        </div>
        <button className="btn sm primary" onClick={() => setEditing("new")}><Icon name="edit" size={14} /> New profile</button>
      </div>
      {profiles.length === 0 ? (
        <Card><div className="muted" style={{ padding: "8px 0" }}>No configuration profiles yet — create one, then assign it to a node or appliance from its section.</div></Card>
      ) : (
        <div className="grid grid-3">
          {profiles.map((p) => (
            <Card key={p.id}>
              <div className="spread" style={{ marginBottom: 6 }}>
                <div style={{ fontWeight: 700 }}>{p.name}</div>
                <div className="row" style={{ gap: 4 }}>
                  <Pill tone={p.target === "appliance" ? "warn" : "info"}>{p.target === "appliance" ? "Appliance" : "Node"}</Pill>
                  {!p.enabled && <Pill tone="warn">disabled</Pill>}
                </div>
              </div>
              {p.description && <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>{p.description}</div>}
              <div className="faint" style={{ fontSize: 11.5, marginBottom: 10 }}>
                {p.key_count} setting{p.key_count === 1 ? "" : "s"} · assigned to {p.assigned_count} {p.target === "appliance" ? "appliance" : "node"}{p.assigned_count === 1 ? "" : "s"}
                {p.updated_at ? ` · ${timeAgo(p.updated_at)}` : ""}
              </div>
              <div className="row" style={{ gap: 6 }}>
                <button className="btn ghost sm" onClick={() => setEditing(p)}><Icon name="edit" size={13} /> Edit</button>
                <button className="btn ghost sm" onClick={() => remove(p)} title="Delete"><Icon name="trash" size={13} /></button>
              </div>
            </Card>
          ))}
        </div>
      )}
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function ProfileEditor({ profile, catalog, onDone, onCancel }: {
  profile: Profile | null; catalog: CatalogItem[];
  onDone: () => void; onCancel: () => void;
}) {
  const catIndex = useMemo(() => Object.fromEntries(catalog.map((c) => [c.key, c])), [catalog]);
  const [services, setServices] = useState<any[]>([]);
  useEffect(() => { api.get<any[]>("/admin/service-objects").then(setServices).catch(() => {}); }, []);
  const groups = useMemo(() => {
    const g: Record<string, CatalogItem[]> = {};
    catalog.forEach((c) => { (g[c.group] ||= []).push(c); });
    return g;
  }, [catalog]);

  const toStr = (v: any) => Array.isArray(v) ? v.join(", ") : (v === null || v === undefined ? "" : String(v));

  const [name, setName] = useState(profile?.name || "");
  const [desc, setDesc] = useState(profile?.description || "");
  const [enabled, setEnabled] = useState(profile?.enabled ?? true);
  const [kind, setKind] = useState<"node" | "appliance">(profile?.target || "node");
  const [mode, setMode] = useState<"table" | "json">("table");
  const [rows, setRows] = useState<{ key: string; value: string }[]>(
    Object.entries(profile?.data || {}).map(([k, v]) => ({ key: k, value: toStr(v) })));
  const [jsonText, setJsonText] = useState(JSON.stringify(profile?.data || {}, null, 2));
  const [busy, setBusy] = useState(false);

  function coerce(key: string, value: string): any {
    const t = catIndex[key]?.type;
    if (t === "int") { const n = parseInt(value, 10); return isNaN(n) ? value : n; }
    if (t === "float") { const n = parseFloat(value); return isNaN(n) ? value : n; }
    if (t === "bool") return ["1", "true", "yes", "on"].includes(value.trim().toLowerCase());
    if (t === "csv") return value.split(",").map((s) => s.trim()).filter(Boolean);
    return value;
  }
  function rowsToData(): Record<string, any> {
    const d: Record<string, any> = {};
    rows.forEach((r) => { const k = r.key.trim(); if (k) d[k] = coerce(k, r.value); });
    return d;
  }
  function switchMode(m: "table" | "json") {
    if (m === mode) return;
    if (m === "json") { setJsonText(JSON.stringify(rowsToData(), null, 2)); setMode("json"); return; }
    try {
      const d = JSON.parse(jsonText || "{}");
      setRows(Object.entries(d).map(([k, v]) => ({ key: k, value: toStr(v) })));
      setMode("table");
    } catch { void notify({ message: "Fix the JSON before switching to the table view.", tone: "warn" }); }
  }

  const addRow = (key = "") => setRows((r) => [...r, { key, value: key ? (catIndex[key]?.example || "") : "" }]);
  const setRow = (i: number, patch: Partial<{ key: string; value: string }>) =>
    setRows((r) => r.map((row, idx) => (idx === i ? { ...row, ...patch } : row)));
  const delRow = (i: number) => setRows((r) => r.filter((_, idx) => idx !== i));

  async function save() {
    if (!name.trim()) { void notify({ message: "Name is required", tone: "danger" }); return; }
    let data: any;
    if (mode === "json") { try { data = JSON.parse(jsonText || "{}"); } catch { void notify({ message: "Invalid JSON", tone: "danger" }); return; } }
    else data = rowsToData();
    setBusy(true);
    try {
      const payload = { name, description: desc, kind, data, enabled };
      if (profile) await api.put(`/admin/config-profiles/${profile.id}`, payload);
      else await api.post("/admin/config-profiles", payload);
      onDone();
    } catch (e: any) { void notify({ message: e.message, tone: "danger" }); }
    finally { setBusy(false); }
  }

  return (
    <Card>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>{profile ? "Edit profile" : "New configuration profile"}</h3>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm ghost" onClick={onCancel}>Cancel</button>
          <button className="btn sm primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
        </div>
      </div>
      <div className="stack" style={{ gap: 12 }}>
        <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label className="stack flex1" style={{ gap: 5, minWidth: 220 }}>
            <span className="faint" style={{ fontSize: 12 }}>Name</span>
            <input className="input sm" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. High-frequency backups" />
          </label>
          <label className="row" style={{ gap: 8, alignItems: "center", height: 30 }}>
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            <span style={{ fontSize: 13 }}>Enabled</span>
          </label>
        </div>
        <label className="stack" style={{ gap: 5 }}>
          <span className="faint" style={{ fontSize: 12 }}>Description</span>
          <input className="input sm" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="What this profile is for" />
        </label>
        <label className="stack" style={{ gap: 5, maxWidth: 260 }}>
          <span className="faint" style={{ fontSize: 12 }}>Profile type</span>
          <select className="input sm" value={kind} disabled={!!profile} onChange={(e) => setKind(e.target.value as "node" | "appliance")}>
            <option value="node">Node</option>
            <option value="appliance">Appliance</option>
          </select>
          <span className="faint" style={{ fontSize: 11 }}>{profile ? "Type can't be changed after creation." : "Which kind of device this profile can be assigned to."}</span>
        </label>

        <div className="spread" style={{ alignItems: "center" }}>
          <span className="faint" style={{ fontSize: 12 }}>Settings</span>
          <div className="row" style={{ gap: 4 }}>
            <button className={`btn sm ${mode === "table" ? "primary" : "ghost"}`} onClick={() => switchMode("table")}>Table</button>
            <button className={`btn sm ${mode === "json" ? "primary" : "ghost"}`} onClick={() => switchMode("json")}>JSON</button>
          </div>
        </div>

        {mode === "table" ? (
          <div className="stack" style={{ gap: 8 }}>
            <datalist id="cfg-catalog-keys">
              {catalog.map((c) => <option key={c.key} value={c.key}>{c.group} · {c.label}</option>)}
            </datalist>
            {rows.map((row, i) => {
              const spec = catIndex[row.key.trim()];
              // Service-assignment keys always render a service picker, even if the
              // catalog metadata hasn't loaded yet.
              const key = row.key.trim();
              const choices = spec?.choices
                || (key === "service.email" ? "email-service"
                    : key === "service.storage" ? "storage-service"
                    : key === "service.payment" ? "payment-service" : null);
              return (
                <div key={i} className="stack" style={{ gap: 3 }}>
                  <div className="row" style={{ gap: 8, alignItems: "center" }}>
                    <input className="input sm" list="cfg-catalog-keys" style={{ flex: "0 0 260px" }}
                           value={row.key} placeholder="setting key"
                           onChange={(e) => {
                             const k = e.target.value;
                             const ex = catIndex[k.trim()];
                             setRow(i, { key: k, value: (ex && !row.value) ? (ex.example || "") : row.value });
                           }} />
                    {choices === "timezone" ? (
                      <select className="input sm flex1" value={row.value} onChange={(e) => setRow(i, { value: e.target.value })}>
                        <option value="">— none (use UTC) —</option>
                        {tzList().map((z) => <option key={z} value={z}>{z}</option>)}
                      </select>
                    ) : choices ? (
                      <select className="input sm flex1" value={row.value} onChange={(e) => setRow(i, { value: e.target.value })}>
                        <option value="">— none (use default) —</option>
                        {services.filter((x) => x.category === (choices === "email-service" ? "email" : choices === "payment-service" ? "payment" : "storage")).map((x) => (
                          <option key={x.id} value={x.id}>{x.name}{x.configured ? "" : " (incomplete)"}</option>
                        ))}
                      </select>
                    ) : (
                      <input className="input sm flex1" value={row.value}
                             placeholder={spec ? `e.g. ${spec.example}` : "value"}
                             onChange={(e) => setRow(i, { value: e.target.value })} />
                    )}
                    <button className="btn ghost sm" onClick={() => delRow(i)} title="Remove"><Icon name="trash" size={13} /></button>
                  </div>
                  {spec && (
                    <div className="faint" style={{ fontSize: 11, paddingLeft: 2 }}>
                      {spec.description} <span style={{ opacity: .7 }}>({spec.type}{spec.unit ? `, ${spec.unit}` : ""})</span>
                    </div>
                  )}
                </div>
              );
            })}
            <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <button className="btn ghost sm" onClick={() => addRow()}><Icon name="edit" size={13} /> Add setting</button>
              <select className="input sm" style={{ maxWidth: 280 }} value=""
                      onChange={(e) => { if (e.target.value) { addRow(e.target.value); e.currentTarget.value = ""; } }}>
                <option value="">Add from catalog…</option>
                {Object.entries(groups).map(([g, items]) => (
                  <optgroup key={g} label={g}>
                    {items.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
                  </optgroup>
                ))}
              </select>
            </div>
          </div>
        ) : (
          <textarea className="input" spellCheck={false}
                    style={{ fontFamily: "ui-monospace, monospace", fontSize: 13, minHeight: 220 }}
                    value={jsonText} onChange={(e) => setJsonText(e.target.value)} />
        )}
      </div>
    </Card>
  );
}

function WebsiteCMS() {
  const [content, setContent] = useState<any>(null);
  const [defaults, setDefaults] = useState<any>({});
  const [published, setPublished] = useState(true);
  const [raw, setRaw] = useState("");
  const [rawMode, setRawMode] = useState(false);
  const [toast, setToast] = useState("");
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }

  async function load() {
    try {
      const r = await api.get<any>("/admin/site");
      setContent(r.content || r.defaults || {});
      setDefaults(r.defaults || {});
      setPublished(!!r.published);
      setRaw(JSON.stringify(r.content || r.defaults || {}, null, 2));
    } catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  function setPath(path: (string | number)[], value: any) {
    setContent((prev: any) => {
      const next = JSON.parse(JSON.stringify(prev || {}));
      let o = next;
      for (let i = 0; i < path.length - 1; i++) { o[path[i]] = o[path[i]] ?? {}; o = o[path[i]]; }
      o[path[path.length - 1]] = value;
      return next;
    });
  }

  async function save() {
    let body: any = content;
    if (rawMode) {
      try { body = JSON.parse(raw); } catch { void notify({ title: "Invalid JSON", message: "Fix the JSON before saving.", tone: "warn" }); return; }
    }
    try {
      await api.put("/admin/site", { content: body, published });
      if (rawMode) setContent(body);
      flash("Website content published");
    } catch { flash("Failed to save"); }
  }

  async function resetDefaults() {
    if (!await confirmDialog({ title: "Reset to defaults?", message: "Replace all website content with the built-in defaults.", tone: "danger", confirmLabel: "Reset" })) return;
    setContent(JSON.parse(JSON.stringify(defaults)));
    setRaw(JSON.stringify(defaults, null, 2));
    flash("Reset — remember to publish");
  }

  if (!content) return <Card><div className="muted">Loading…</div></Card>;
  const hero = content.hero || {};
  const contact = content.contact || {};
  const plans: any[] = content.pricing?.plans || [];

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <div>
          <h3 style={{ margin: 0 }}>Public website</h3>
          <span className="faint" style={{ fontSize: 12 }}>Edit the marketing site content served by the Public Web Node.</span>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn ghost sm" onClick={() => setRawMode((v) => !v)}>{rawMode ? "Guided editor" : "Raw JSON"}</button>
          <button className="btn ghost sm" onClick={resetDefaults}>Reset</button>
          <button className="btn primary sm" onClick={save}><Icon name="check" size={14} /> Publish</button>
        </div>
      </div>

      <Card style={{ marginBottom: 12 }}>
        <label className="row" style={{ gap: 8, alignItems: "center" }}>
          <input type="checkbox" checked={published} onChange={(e) => setPublished(e.target.checked)} />
          <span>Published — the public site is live and serving this content.</span>
        </label>
      </Card>

      {rawMode ? (
        <Card>
          <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>Full content document (JSON). Advanced.</div>
          <textarea className="input" value={raw} onChange={(e) => setRaw(e.target.value)}
                    style={{ minHeight: 460, fontFamily: "ui-monospace, monospace", fontSize: 12.5 }} />
        </Card>
      ) : (
        <>
          <Card style={{ marginBottom: 12 }}>
            <div style={{ fontWeight: 700, marginBottom: 10 }}>Brand</div>
            <div className="grid grid-2" style={{ gap: 10 }}>
              <CmsField label="Brand name" value={content.brand || ""} onChange={(v) => setPath(["brand"], v)} />
              <CmsField label="Tagline" value={content.tagline || ""} onChange={(v) => setPath(["tagline"], v)} />
            </div>
          </Card>

          <Card style={{ marginBottom: 12 }}>
            <div style={{ fontWeight: 700, marginBottom: 10 }}>Hero</div>
            <div className="stack" style={{ gap: 10 }}>
              <CmsField label="Eyebrow" value={hero.eyebrow || ""} onChange={(v) => setPath(["hero", "eyebrow"], v)} />
              <CmsField label="Headline" value={hero.h1 || ""} onChange={(v) => setPath(["hero", "h1"], v)} />
              <CmsField label="Lead paragraph" area value={hero.lead || ""} onChange={(v) => setPath(["hero", "lead"], v)} />
              <div className="grid grid-2" style={{ gap: 10 }}>
                <CmsField label="Primary button" value={hero.ctaPrimary || ""} onChange={(v) => setPath(["hero", "ctaPrimary"], v)} />
                <CmsField label="Secondary button" value={hero.ctaSecondary || ""} onChange={(v) => setPath(["hero", "ctaSecondary"], v)} />
              </div>
            </div>
          </Card>

          <Card style={{ marginBottom: 12 }}>
            <div style={{ fontWeight: 700, marginBottom: 10 }}>Pricing plans</div>
            <div className="grid grid-3">
              {plans.map((p, i) => (
                <div key={i} className="stack" style={{ gap: 8, padding: 12, border: "1px solid var(--border-soft)", borderRadius: 12 }}>
                  <CmsField label="Name" value={p.name || ""} onChange={(v) => setPath(["pricing", "plans", i, "name"], v)} />
                  <div className="grid grid-2" style={{ gap: 8 }}>
                    <CmsField label="Price" value={p.price || ""} onChange={(v) => setPath(["pricing", "plans", i, "price"], v)} />
                    <CmsField label="Per" value={p.per || ""} onChange={(v) => setPath(["pricing", "plans", i, "per"], v)} />
                  </div>
                  <CmsField label="Blurb" area value={p.blurb || ""} onChange={(v) => setPath(["pricing", "plans", i, "blurb"], v)} />
                  <CmsField label="Button" value={p.cta || ""} onChange={(v) => setPath(["pricing", "plans", i, "cta"], v)} />
                </div>
              ))}
            </div>
          </Card>

          <Card>
            <div style={{ fontWeight: 700, marginBottom: 10 }}>Contact</div>
            <div className="grid grid-3" style={{ gap: 10 }}>
              <CmsField label="General email" value={contact.email || ""} onChange={(v) => setPath(["contact", "email"], v)} />
              <CmsField label="Sales email" value={contact.sales || ""} onChange={(v) => setPath(["contact", "sales"], v)} />
              <CmsField label="Support email" value={contact.support || ""} onChange={(v) => setPath(["contact", "support"], v)} />
            </div>
          </Card>
        </>
      )}
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function CmsField({ label, value, onChange, area }: { label: string; value: string; onChange: (v: string) => void; area?: boolean }) {
  return (
    <label className="stack" style={{ gap: 4 }}>
      <span className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".05em" }}>{label}</span>
      {area
        ? <textarea className="input" value={value} onChange={(e) => onChange(e.target.value)} style={{ minHeight: 72 }} />
        : <input className="input" value={value} onChange={(e) => onChange(e.target.value)} />}
    </label>
  );
}

function Fleet() {
  const [rows, setRows] = useState<any[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [profiles, setProfiles] = useState<any[]>([]);
  const [pending, setPending] = useState<any[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  async function loadFleet() { try { setRows(await api.get<any[]>("/admin/fleet")); } catch { /* ignore */ } }
  async function loadStats() { try { setStats(await api.get<any>("/admin/fleet/stats")); } catch { /* ignore */ } }
  async function loadPending() { try { setPending(await api.get<any[]>("/admin/pending-appliances")); } catch { /* ignore */ } }
  useEffect(() => {
    void loadFleet(); void loadStats(); void loadPending();
    api.get<{ profiles: any[] }>("/admin/config-profiles").then((r) => setProfiles((r.profiles || []).filter((p) => p.target === "appliance"))).catch(() => {});
    const iv = setInterval(() => { void loadPending(); }, 10000);
    return () => clearInterval(iv);
  }, []);

  if (sel) return <ApplianceAdminDetail id={sel} profiles={profiles} onBack={() => { setSel(null); void loadFleet(); void loadStats(); }} />;

  const st = stats || {};
  return (
    <>
      <div className="spread" style={{ marginBottom: 12, alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>Appliance fleet</h3>
        <ProductionVersion label="Production appliance software" version={st.production_version || ""} />
      </div>

      <div className="insights-stats" style={{ marginBottom: 16 }}>
        <AdminStat icon="server" label="Appliances" value={String(st.total || 0)} tint="#4f7cff" />
        <AdminStat icon="activity" label="Online now" value={`${st.online || 0}/${st.total || 0}`} tint="#2dbe60" />
        <AdminStat icon="database" label="Storage used" value={`${bytes(st.used_bytes || 0)} / ${bytes(st.capacity_bytes || 0)}`} tint="#f5a623" />
        <AdminStat icon="clock" label="Updates available" value={String(st.update_available || 0)} tint="#c56cf0" />
        <AdminStat icon="link" label="Awaiting pairing" value={String(st.pending || 0)} tint={st.pending ? "#f5a623" : "#2dbe60"} />
        <AdminStat icon="shield" label="Attestation issues" value={String(st.attestation_failed || 0)} tint={st.attestation_failed ? "#f2545b" : "#2dbe60"} />
        <AdminStat icon="alert" label="Tamper alerts" value={String(st.tamper_alerts || 0)} tint={st.tamper_alerts ? "#f2545b" : "#2dbe60"} />
      </div>

      {pending.length > 0 && (
        <Card style={{ marginBottom: 16, borderColor: "var(--warn, #f5a623)" }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h3 style={{ margin: 0, fontSize: 14 }}>Awaiting pairing</h3>
            <Pill tone="warn">{pending.length}</Pill>
          </div>
          <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
            Zero-touch appliances that registered but haven’t been claimed by a customer yet.
            Share the pairing code with the customer to complete setup.
          </div>
          <table className="table">
            <thead><tr><th>Serial</th><th>Model</th><th>Host / IP</th><th>Pairing code</th><th>Seen</th></tr></thead>
            <tbody>
              {pending.map((p) => (
                <tr key={p.id}>
                  <td className="mono" style={{ fontSize: 12 }}>{p.serial}</td>
                  <td className="faint" style={{ fontSize: 12 }}>{p.model}</td>
                  <td className="faint" style={{ fontSize: 12 }}>{p.hostname || "—"}{p.local_ip ? ` · ${p.local_ip}` : ""}</td>
                  <td><span className="mono" style={{ fontSize: 13, fontWeight: 700, letterSpacing: 1 }}>{p.pairing_code}</span></td>
                  <td><Pill tone={p.online ? "ok" : "warn"} dot>{p.online ? "online" : timeAgo(p.last_seen_at)}</Pill></td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
        <Card>
          <h3 style={{ margin: "0 0 10px", fontSize: 14 }}>By state</h3>
          {(st.by_state || []).map((s: any) => (
            <div key={s.state} className="spread" style={{ fontSize: 12.5, padding: "3px 0" }}>
              <span style={{ textTransform: "capitalize" }}>{String(s.state).toLowerCase()}</span><span className="faint">{s.count}</span>
            </div>
          ))}
          {(st.by_state || []).length === 0 && <div className="muted">—</div>}
        </Card>
        <Card>
          <h3 style={{ margin: "0 0 10px", fontSize: 14 }}>By software version</h3>
          {(st.by_version || []).map((s: any) => (
            <div key={s.version} className="spread" style={{ fontSize: 12.5, padding: "3px 0" }}>
              <span className="row" style={{ gap: 6 }}>v{s.version}{s.version === st.production_version && <Pill tone="ok">current</Pill>}</span>
              <span className="faint">{s.count}</span>
            </div>
          ))}
          {(st.by_version || []).length === 0 && <div className="muted">—</div>}
        </Card>
      </div>

      <Card>
        <table className="table">
          <thead><tr><th>Appliance</th><th>Customer</th><th>State</th><th>Storage</th><th style={{ textAlign: "right" }}>Recovery pts</th><th>Health</th><th>Version</th><th>Heartbeat</th></tr></thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id} style={{ cursor: "pointer" }} onClick={() => setSel(a.id)}>
                <td>
                  <div style={{ fontWeight: 600 }}>{a.name || a.serial}</div>
                  <div className="faint mono" style={{ fontSize: 11 }}>{a.serial} · {a.model}</div>
                </td>
                <td className="faint" style={{ fontSize: 12 }}>{a.tenant_name}</td>
                <td>
                  <div className="row" style={{ gap: 5 }}>
                    <Pill tone={a.online ? "ok" : "warn"} dot>{a.online ? "online" : "offline"}</Pill>
                    <span className="faint" style={{ fontSize: 11 }}>{String(a.state).toLowerCase()}</span>
                  </div>
                </td>
                <td style={{ minWidth: 110 }}>
                  <div className="faint" style={{ fontSize: 11 }}>{bytes(a.used_bytes || 0)} / {bytes(a.capacity_bytes || 0)}</div>
                  <div style={{ height: 5, borderRadius: 999, background: "var(--inset)", overflow: "hidden", marginTop: 2 }}>
                    <div style={{ width: `${Math.min(100, a.storage_pct || 0)}%`, height: "100%", background: (a.storage_pct || 0) >= 90 ? "#f2545b" : "linear-gradient(90deg,#4f7cff,#35d0a5)" }} />
                  </div>
                </td>
                <td style={{ textAlign: "right" }}>{(a.recovery_points || 0).toLocaleString()}</td>
                <td>
                  {a.tamper_state && a.tamper_state !== "normal" ? <Pill tone="danger" dot>tamper</Pill>
                    : !a.attestation_ok ? <Pill tone="warn" dot>attest</Pill>
                    : <Pill tone="ok" dot>ok</Pill>}
                </td>
                <td><VersionPill version={a.software_version} updateAvailable={a.update_available} /></td>
                <td className="faint" style={{ fontSize: 12 }}>{timeAgo(a.last_heartbeat_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="muted">No appliances in fleet.</div>}
      </Card>
    </>
  );
}

function RemoteTerminal({ id, name, onClose }: { id: string; name: string; onClose: () => void }) {
  const hostRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const termRef = useRef<Terminal | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const [status, setStatus] = useState<"connecting" | "waiting" | "open" | "closed" | "error">("connecting");

  useEffect(() => {
    let cancelled = false;
    let ws: WebSocket | null = null;

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 13,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      theme: { background: "#0a0e1a", foreground: "#d7e2ff", cursor: "#4f7cff" },
      scrollback: 5000,
      convertEol: true,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    termRef.current = term;
    fitRef.current = fit;
    // Full keyboard UX: copy the selection with Cmd+C / Ctrl+Shift+C (a bare
    // Ctrl+C still sends SIGINT). Paste (Cmd+V / Ctrl+V / Ctrl+Shift+V) is handled
    // natively by xterm's browser paste event → onData, which also brackets
    // multi-line pastes. Tab, arrows and control keys flow through onData for
    // shell completion and line editing.
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== "keydown") return true;
      if (e.key.toLowerCase() === "c" && (e.metaKey || (e.ctrlKey && e.shiftKey))) {
        const sel = term.getSelection();
        if (sel) {
          e.preventDefault();
          void navigator.clipboard?.writeText(sel).catch(() => {});
          return false;
        }
      }
      return true;
    });
    if (hostRef.current) term.open(hostRef.current);
    // Give the modal a frame to lay out before sizing the grid.
    requestAnimationFrame(() => { try { fit.fit(); term.focus(); } catch { /* ignore */ } });
    term.writeln("Requesting a secure terminal session with the appliance…");

    const sendResize = () => {
      const w = wsRef.current;
      if (w && w.readyState === WebSocket.OPEN) {
        w.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      }
    };
    // Typed input / paste → appliance PTY.
    term.onData((data) => {
      const w = wsRef.current;
      if (w && w.readyState === WebSocket.OPEN) w.send(JSON.stringify({ type: "input", data }));
    });
    const onWinResize = () => { try { fit.fit(); sendResize(); } catch { /* ignore */ } };
    window.addEventListener("resize", onWinResize);

    (async () => {
      try {
        setStatus("waiting");
        const r = await api.post<{ ws_url: string }>(`/admin/appliances/${id}/terminal`, {});
        if (cancelled) return;
        const proto = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${proto}//${location.host}${r.ws_url}`);
        wsRef.current = ws;
        ws.onopen = () => {
          setStatus("open");
          try { fit.fit(); } catch { /* ignore */ }
          sendResize();
          term.focus();
        };
        ws.onmessage = (ev) => { if (typeof ev.data === "string") term.write(ev.data); };
        ws.onerror = () => setStatus("error");
        ws.onclose = () => { setStatus("closed"); term.write("\r\n\x1b[90m[session closed]\x1b[0m\r\n"); };
      } catch (e) {
        if (!cancelled) {
          setStatus("error");
          term.write(`\r\n\x1b[31mCould not start terminal: ${(e as { message?: string }).message || "failed"}\x1b[0m\r\n`);
        }
      }
    })();

    return () => {
      cancelled = true;
      window.removeEventListener("resize", onWinResize);
      try { ws?.close(); } catch { /* ignore */ }
      try { term.dispose(); } catch { /* ignore */ }
      termRef.current = null;
      fitRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  const statusLabel = { connecting: "Connecting…", waiting: "Establishing tunnel…", open: "Connected", closed: "Closed", error: "Error" }[status];
  const statusTone = status === "open" ? "ok" : status === "error" || status === "closed" ? "danger" : "warn";

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" style={{ width: "min(980px, 100%)" }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>Remote terminal</h3>
            <div className="faint" style={{ fontSize: 12 }}>{name} · reverse-tunnel via control plane</div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <Pill tone={statusTone as any} dot>{statusLabel}</Pill>
            <button className="btn ghost sm" onClick={onClose}>Close</button>
          </div>
        </div>
        <div className="modal-body">
          <div
            ref={hostRef}
            onClick={() => termRef.current?.focus()}
            style={{
              background: "#0a0e1a", border: "1px solid var(--border)", borderRadius: 10,
              padding: 8, height: 440, overflow: "hidden",
            }}
          />
          <div className="faint" style={{ fontSize: 11, marginTop: 6 }}>
            Click the terminal and type to send keystrokes to the appliance shell. The session runs
            as the unprivileged <span className="mono">cvagent</span> service account and closes automatically when you leave.
          </div>
        </div>
      </div>
    </div>
  );
}

const INDEX_REPLICA_STATUS: Record<string, { tone: "ok" | "warn" | "info" | "danger"; label: string }> = {
  ok: { tone: "ok", label: "Protected" }, stale: { tone: "warn", label: "Stale" },
  error: { tone: "danger", label: "Error" }, pending: { tone: "info", label: "Pending" },
  verifying: { tone: "info", label: "Verifying" },
};

// Reusable DR search-index replica health block for admin detail pages.
function IndexReplicasBlock({ replicas, title = "Search index replicas" }: { replicas?: any[]; title?: string }) {
  const rows = replicas || [];
  return (
    <Card>
      <h3 style={{ margin: "0 0 4px", fontSize: 15 }}>{title}</h3>
      <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
        Encrypted disaster-recovery copies of the search index, verified on a schedule.
      </div>
      {rows.length === 0 ? <div className="muted">No index replicas yet.</div> : (
        <table className="table">
          <thead><tr><th>Destination</th><th>Status</th><th>Items</th><th>Replicated</th><th>Verified</th></tr></thead>
          <tbody>
            {rows.map((r) => {
              const m = INDEX_REPLICA_STATUS[r.status] || INDEX_REPLICA_STATUS.pending;
              return (
                <tr key={r.id}>
                  <td><div style={{ fontWeight: 600 }}>{r.destination_label}</div>{r.node_name && <div className="faint" style={{ fontSize: 10.5 }}>{r.node_name}</div>}</td>
                  <td><Pill tone={m.tone} dot>{m.label}</Pill>{r.error ? <div className="faint" style={{ fontSize: 10.5, color: "var(--danger)" }}>{r.error}</div> : null}</td>
                  <td>{r.status === "ok" ? (r.object_count || 0).toLocaleString() : "—"}</td>
                  <td className="faint" style={{ fontSize: 12 }}>{r.last_replicated_at ? fmtAbsolute(r.last_replicated_at) : "—"}</td>
                  <td className="faint" style={{ fontSize: 12 }}>{r.last_verified_at ? fmtAbsolute(r.last_verified_at) : "never"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Card>
  );
}

type ApplianceTab = "health" | "storage" | "config" | "security" | "logs" | "commands";
const APPLIANCE_TABS: { key: ApplianceTab; label: string; icon: IconName }[] = [
  { key: "health", label: "System health", icon: "activity" },
  { key: "storage", label: "Storage & data", icon: "database" },
  { key: "config", label: "Configuration", icon: "puzzle" },
  { key: "security", label: "Keys & security", icon: "lock" },
  { key: "logs", label: "Logs", icon: "note" },
  { key: "commands", label: "Commands", icon: "grid" },
];

function ApplianceAdminDetail({ id, profiles, onBack }: { id: string; profiles: any[]; onBack: () => void }) {
  const [a, setA] = useState<any>(null);
  const [tab, setTab] = useState<ApplianceTab>("health");
  const [toast, setToast] = useState("");
  const [term, setTerm] = useState(false);
  const [openStore, setOpenStore] = useState<string | null>(null);
  const buf = useRef<Record<string, number[]>>({ cpu: [], mem: [], disk: [], os: [] });
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 2800); }
  async function load() {
    try {
      const d = await api.get<any>(`/admin/appliances/${id}`);
      setA(d);
      const t = d.telemetry || {};
      const cc = Number(t.cpu_count) || 0;
      const l1 = Array.isArray(t.load_avg) ? Number(t.load_avg[0]) : 0;
      const mt = Number(t.mem_total_bytes) || 0, ma = Number(t.mem_available_bytes) || 0;
      const ct = Number(t.capacity_total_bytes) || 0, cu = Number(t.capacity_used_bytes) || 0;
      const push = (k: string, v: number) => { const arr = buf.current[k]; arr.push(Number(v) || 0); if (arr.length > MAX_LIVE) arr.shift(); };
      push("cpu", cc ? Math.min(100, (l1 / cc) * 100) : 0);
      push("mem", mt ? ((mt - ma) / mt) * 100 : 0);
      push("disk", ct ? (cu / ct) * 100 : 0);
      push("os", Number(t.os_storage?.pct) || 0);
    } catch { /* ignore */ }
  }
  useEffect(() => { void load(); const iv = setInterval(load, 7000); return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  async function assign(pid: string) {
    try { const r = await api.put<any>(`/admin/appliances/${id}/config-profile`, { profile_id: pid || null }); setA((prev: any) => ({ ...prev, config_profile: r.config_profile })); flash(pid ? "Profile assigned" : "Profile unassigned"); }
    catch (e) { flash((e as { message?: string }).message || "Assignment failed"); }
  }
  async function reassign() {
    let tenants: any[] = [];
    try { tenants = await api.get<any[]>("/admin/tenants"); } catch { flash("Could not load tenants"); return; }
    const r = await formDialog({
      title: "Re-link appliance to an account",
      message: `Move ${a.name || a.serial} to another tenant. The appliance keeps its identity and stays paired — only its ownership changes. Use this to re-link an appliance that was orphaned when its owner upgraded/downgraded their plan.`,
      confirmLabel: "Re-link",
      fields: [{ name: "tenant_id", label: "Target account / tenant", defaultValue: a.tenant?.id || "",
        options: tenants.map((t) => ({ label: `${t.name} (${TENANT_TYPE_LABEL[t.tenant_type] || t.tenant_type})`, value: t.id })) }],
    });
    if (!r || !r.tenant_id) return;
    try { const res = await api.post<any>(`/admin/appliances/${id}/reassign`, { tenant_id: r.tenant_id }); flash(`Re-linked to ${res.tenant_name}`); await load(); }
    catch (e) { flash((e as { message?: string }).message || "Re-link failed"); }
  }
  if (!a) return <Card><button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 10 }}>← Fleet</button><div className="muted">Loading appliance…</div></Card>;

  const tel = a.telemetry || {};
  const sd = a.stored_data || {};
  const cpuCount = Number(tel.cpu_count) || 0;
  const load1 = Array.isArray(tel.load_avg) ? Number(tel.load_avg[0]) : undefined;
  const cpuPct = (load1 != null && cpuCount) ? Math.min(100, (load1 / cpuCount) * 100) : undefined;
  const memTotal = Number(tel.mem_total_bytes) || 0;
  const memUsed = memTotal ? memTotal - (Number(tel.mem_available_bytes) || 0) : 0;
  const memPct = memTotal ? (memUsed / memTotal) * 100 : undefined;
  const capTotal = Number(tel.capacity_total_bytes) || 0;
  const capUsed = Number(tel.capacity_used_bytes) || 0;
  const diskPct = capTotal ? (capUsed / capTotal) * 100 : undefined;
  const osTotal = Number(tel.os_storage?.total_bytes) || 0;
  const osUsed = Number(tel.os_storage?.used_bytes) || 0;
  const osPct = osTotal ? (osUsed / osTotal) * 100 : undefined;
  const logLines: string[] = Array.isArray(tel.recent_logs) ? tel.recent_logs : [];

  return (
    <>
      <div className="spread" style={{ marginBottom: 12, alignItems: "center" }}>
        <button className="btn ghost sm" onClick={onBack}>← Fleet</button>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <ProductionVersion label="Production appliance software" version={a.production_version || ""} />
          <div className="row" style={{ gap: 8 }}>
            <button className="btn sm" disabled={!a.online} title={a.online ? "Open a remote terminal" : "Appliance is offline"} onClick={() => setTerm(true)}>
              <Icon name="server" size={13} /> Remote terminal
            </button>
            <button className="btn sm" onClick={reassign} title="Move this appliance to another account/tenant (re-link)">
              <Icon name="link" size={13} /> Re-link
            </button>
            <button className="btn sm" onClick={() => void load()}><Icon name="activity" size={13} /> Refresh</button>
          </div>
        </div>
      </div>

      {term && <RemoteTerminal id={id} name={a.name || a.serial} onClose={() => setTerm(false)} />}

      <Card style={{ marginBottom: 14 }}>
        <div className="spread">
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="result-icon" style={{ width: 40, height: 40, background: "var(--inset)", color: a.online ? "#35d0a5" : "#8a94a7" }}>
              <Icon name="server" size={20} />
            </div>
            <div>
              <h3 style={{ margin: 0 }}>{a.name || a.serial}</h3>
              <div className="faint" style={{ fontSize: 12 }}>
                {a.model} · {a.serial} · {a.tenant?.name || "—"}{a.software_version ? ` · v${a.software_version}` : ""}
                {tel.uptime_seconds ? ` · ${uptimeShort(tel.uptime_seconds)}` : ""}
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
            <Pill tone={a.online ? "ok" : "warn"} dot>{a.online ? "Online" : "Offline"}</Pill>
            <Pill tone="info">{String(a.state).toLowerCase()}</Pill>
            <Pill tone={a.attestation_ok ? "ok" : "warn"} dot>{a.attestation_ok ? "attested" : "unattested"}</Pill>
            {a.tamper_state && a.tamper_state !== "normal" && <Pill tone="danger" dot>tamper: {a.tamper_state}</Pill>}
            <VersionPill version={a.software_version} updateAvailable={a.update_available} />
          </div>
        </div>
        {(tel.hostname || tel.local_ip || tel.model_kind) && (
          <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            {tel.model_kind && <Pill tone="info"><Icon name="server" size={11} /> {tel.model_kind === "vm" ? `VM · ${tel.virtualization || "virtual"}` : "Hardware"}</Pill>}
            {tel.hostname && <span className="faint" style={{ fontSize: 11.5 }}>{tel.hostname}</span>}
            {tel.os && <span className="faint" style={{ fontSize: 11.5 }}>{tel.os}</span>}
            {tel.local_ip && <span className="faint" style={{ fontSize: 11.5 }}>{tel.local_ip}</span>}
          </div>
        )}
        <div className="grid grid-4" style={{ gap: 12, marginTop: 14 }}>
          <Mini label="Recovery points" value={(sd.recovery_points ?? 0).toLocaleString()} />
          <Mini label="Objects" value={(sd.objects ?? 0).toLocaleString()} />
          <Mini label="Protected" value={bytes(sd.bytes || 0)} />
          <Mini label="Last heartbeat" value={a.last_heartbeat_at ? timeAgo(a.last_heartbeat_at) : "never"} />
        </div>
      </Card>

      <div className="row" style={{ gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        {APPLIANCE_TABS.map((t) => (
          <button key={t.key} className={`btn sm ${tab === t.key ? "primary" : "ghost"}`} onClick={() => setTab(t.key)}>
            <Icon name={t.icon} size={13} /> {t.label}
          </button>
        ))}
      </div>

      {tab === "health" && (
        <>
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(165px, 1fr))", gap: 12, marginBottom: 14 }}>
            <Card><div style={{ display: "flex", justifyContent: "center" }}><Ring value={cpuPct ?? 0} label="CPU load" sub={`${cpuCount || "—"} core${cpuCount === 1 ? "" : "s"}`} /></div>
              <div style={{ marginTop: 6 }}><Sparkline data={buf.current.cpu} width={999} color="#4f7cff" max={100} /></div></Card>
            <Card><div style={{ display: "flex", justifyContent: "center" }}><Ring value={memPct ?? 0} label="Memory" sub={memTotal ? `${bytes(memUsed)}/${bytes(memTotal)}` : ""} color="#35d0a5" /></div>
              <div style={{ marginTop: 6 }}><Sparkline data={buf.current.mem} width={999} color="#35d0a5" max={100} /></div></Card>
            <Card><div style={{ display: "flex", justifyContent: "center" }}><Ring value={diskPct ?? 0} label={tel.storage_kind === "dedicated" ? "Dedicated storage" : "Storage"} sub={capTotal ? `${bytes(capUsed)}/${bytes(capTotal)}` : ""} color="#f5a623" /></div>
              <div style={{ marginTop: 6 }}><Sparkline data={buf.current.disk} width={999} color="#f5a623" max={100} /></div></Card>
            <Card><div style={{ display: "flex", justifyContent: "center" }}><Ring value={osPct ?? 0} label="OS / system disk" sub={osTotal ? `${bytes(osUsed)}/${bytes(osTotal)}` : "—"} color="#c56cf0" /></div>
              <div style={{ marginTop: 6 }}><Sparkline data={buf.current.os} width={999} color="#c56cf0" max={100} /></div></Card>
            <Card>
              <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>Network &amp; cloud</div>
              <div style={{ fontWeight: 700 }}>↓ {bytes(tel.net_bytes_recv || 0)}</div>
              <div style={{ fontWeight: 700, marginBottom: 6 }}>↑ {bytes(tel.net_bytes_sent || 0)}</div>
              <Row2 label="Latency" value={tel.cloud_latency_ms != null ? `${tel.cloud_latency_ms} ms` : "—"} />
              <Row2 label="Channel" value={tel.channel_encryption || "—"} />
              <div className="faint" style={{ fontSize: 10.5, marginTop: 4 }}>Load {(tel.load_avg || []).join(" ") || "—"}</div>
            </Card>
          </div>
          <div className="grid grid-2">
            <Card>
              <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>System</h3>
              <Row2 label="Hostname" value={tel.hostname || "—"} />
              <Row2 label="Operating system" value={tel.os || "—"} />
              <Row2 label="Kernel" value={tel.kernel || "—"} />
              <Row2 label="Architecture" value={tel.arch || "—"} />
              <Row2 label="CPU cores" value={cpuCount || "—"} />
              <Row2 label="Uptime" value={tel.uptime_seconds ? uptimeShort(tel.uptime_seconds).replace("up ", "") : "—"} />
              <Row2 label="Data path" value={tel.data_path || "—"} />
            </Card>
            <Card>
              <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Hardware &amp; environment</h3>
              <Row2 label="Model" value={a.model || "—"} />
              <Row2 label="Type" value={tel.model_kind === "vm" ? `Virtual · ${tel.virtualization || "—"}` : tel.model_kind === "hardware" ? "Physical hardware" : "—"} />
              {tel.hardware_product && <Row2 label="Product" value={`${tel.hardware_vendor ? tel.hardware_vendor + " " : ""}${tel.hardware_product}`} />}
              <Row2 label="Drive health" value={tel.drive_health || "—"} />
              <Row2 label="Temperature" value={tel.temperature_c != null ? `${tel.temperature_c}°C` : "—"} />
              <Row2 label="Power" value={tel.power || "—"} />
              <Row2 label="Free space" value={tel.disk_free_bytes != null ? bytes(tel.disk_free_bytes) : "—"} />
            </Card>
          </div>
        </>
      )}

      {tab === "storage" && (
        <div className="grid grid-2">
          <IndexReplicasBlock replicas={a.index_replicas} title="Search index replicas (this appliance)" />
          <Card>
            <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Storage volumes</h3>
            <div className="stack" style={{ gap: 10 }}>
              {(a.stores || []).map((s: any) => {
                const pct = s.capacity_bytes ? Math.min(100, (s.used_bytes / s.capacity_bytes) * 100) : 0;
                const h = s.health || {};
                const raid = h.raid || {};
                const smart = h.smart || {};
                const open = openStore === s.id;
                return (
                  <div key={s.id}>
                    <div className="spread" style={{ fontSize: 12.5 }}>
                      <span style={{ fontWeight: 600 }}>{s.name} <span className="faint" style={{ fontWeight: 400, fontSize: 11 }}>· {s.kind === "dedicated" ? "Dedicated" : s.kind === "builtin" ? "Built-in" : "External"}</span></span>
                      <span className="faint">{bytes(s.used_bytes || 0)} / {bytes(s.capacity_bytes || 0)}</span>
                    </div>
                    <div style={{ height: 6, background: "var(--inset)", borderRadius: 3, marginTop: 3 }}>
                      <div style={{ height: "100%", width: `${pct}%`, borderRadius: 3, background: pct >= 90 ? "#f2545b" : "linear-gradient(90deg,#4f7cff,#35d0a5)" }} />
                    </div>
                    <div className="row" style={{ gap: 6, marginTop: 5, flexWrap: "wrap", alignItems: "center" }}>
                      <Pill tone={h.drive_health === "healthy" ? "ok" : h.drive_health ? "danger" : "info"} dot>Drive {h.drive_health || "unknown"}</Pill>
                      <Pill tone={!raid.enabled ? "info" : raid.status === "optimal" ? "ok" : raid.status === "rebuilding" ? "warn" : "danger"} dot={!!raid.enabled}>RAID {raid.enabled ? raid.status : "not configured"}</Pill>
                      <Pill tone={!smart.enabled ? "info" : smart.status === "passed" ? "ok" : "danger"} dot={!!smart.enabled}>SMART {smart.enabled ? smart.status : "n/a"}</Pill>
                      {h.temperature_c != null && <Pill tone={h.temperature_c >= 60 ? "warn" : "info"}>{h.temperature_c}°C</Pill>}
                      <button className="btn ghost sm" style={{ marginLeft: "auto" }} onClick={() => setOpenStore(open ? null : s.id)}>
                        {open ? "Hide" : "Drive health"} {open ? "▴" : "▾"}
                      </button>
                    </div>
                    {open && (
                      <div style={{ marginTop: 8, padding: "4px 12px", background: "var(--inset)", borderRadius: 8 }}>
                        <Row2 label="Drive health" value={<Pill tone={h.drive_health === "healthy" ? "ok" : h.drive_health ? "danger" : "info"} dot>{h.drive_health || "unknown"}</Pill>} />
                        <Row2 label="Filesystem" value={h.filesystem || "—"} />
                        <Row2 label="Backing device" value={h.device || "—"} />
                        <Row2 label="Temperature" value={h.temperature_c != null ? `${h.temperature_c}°C` : "not reported"} />
                        <Row2 label="RAID" value={raid.enabled
                          ? <span className="row" style={{ gap: 6, alignItems: "center", justifyContent: "flex-end", flexWrap: "wrap" }}>
                              <Pill tone={raid.status === "optimal" ? "ok" : raid.status === "rebuilding" ? "warn" : "danger"} dot>{raid.status}</Pill>
                              {(raid.arrays || []).map((ar: any) => <span key={ar.name} className="faint" style={{ fontSize: 11 }}>{ar.name} · {ar.level}{ar.member_map ? ` ${ar.member_map}` : ""}</span>)}
                            </span>
                          : "Not configured (single disk)"} />
                        <Row2 label="SMART" value={smart.enabled
                          ? <span className="row" style={{ gap: 6, alignItems: "center", justifyContent: "flex-end", flexWrap: "wrap" }}>
                              <Pill tone={smart.status === "passed" ? "ok" : "danger"} dot>{smart.status}</Pill>
                              {(smart.drives || []).map((dr: any) => <span key={dr.device} className="faint" style={{ fontSize: 11 }}>{dr.device.replace("/dev/", "")}:{dr.status}</span>)}
                            </span>
                          : "Not available (no SMART passthrough / smartmontools)"} />
                      </div>
                    )}
                  </div>
                );
              })}
              {(a.stores || []).length === 0 && <div className="muted">No storage reported.</div>}
              {tel.os_storage && (
                <div style={{ borderTop: "1px solid var(--border-soft)", paddingTop: 10 }}>
                  <div className="spread" style={{ fontSize: 12.5 }}>
                    <span style={{ fontWeight: 600 }}>OS / system disk <span className="faint" style={{ fontWeight: 400, fontSize: 11 }}>· built-in</span></span>
                    <span className="faint">{bytes(tel.os_storage.used_bytes || 0)} / {bytes(tel.os_storage.total_bytes || 0)}</span>
                  </div>
                  <div style={{ height: 6, background: "var(--inset)", borderRadius: 3, marginTop: 3 }}>
                    <div style={{ height: "100%", width: `${Math.min(100, tel.os_storage.pct || 0)}%`, borderRadius: 3, background: (tel.os_storage.pct || 0) >= 90 ? "#f2545b" : "#c56cf0" }} />
                  </div>
                  <div className="faint" style={{ fontSize: 11, marginTop: 4 }}>
                    {tel.os_storage.filesystem || "—"} · {tel.os_storage.device || "—"} · {bytes(tel.os_storage.free_bytes || 0)} free
                  </div>
                </div>
              )}
            </div>
          </Card>
          <Card>
            <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Stored data by source</h3>
            {(sd.sources || []).length === 0 ? <div className="muted">No data stored on this appliance.</div> : (
              <table className="table">
                <thead><tr><th>Source</th><th style={{ textAlign: "right" }}>Points</th><th style={{ textAlign: "right" }}>Size</th></tr></thead>
                <tbody>
                  {(sd.sources || []).map((s: any, i: number) => (
                    <tr key={i}>
                      <td>
                        <div className="row" style={{ gap: 8, alignItems: "center" }}>
                          {brandForSource(s.source_type) ? <BrandIcon name={brandForSource(s.source_type)!} size={15} /> : <Icon name="database" size={14} />}
                          <span style={{ fontWeight: 600 }}>{s.source}</span>
                        </div>
                      </td>
                      <td style={{ textAlign: "right" }}>{s.recovery_points}</td>
                      <td style={{ textAlign: "right" }}>{bytes(s.bytes || 0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </div>
      )}

      {tab === "config" && (
        <Card>
          <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Assigned configuration profile</h3>
          {a.config_profile ? (
            <div className="spread" style={{ border: "1px solid var(--border)", borderRadius: 10, padding: "10px 12px", marginBottom: 10 }}>
              <div>
                <div style={{ fontWeight: 700 }}>{a.config_profile.name} <span className="faint" style={{ fontWeight: 400, fontSize: 11.5 }}>· {a.config_profile.key_count} setting{a.config_profile.key_count === 1 ? "" : "s"}</span></div>
                {a.config_profile.description && <div className="faint" style={{ fontSize: 12, marginTop: 2 }}>{a.config_profile.description}</div>}
              </div>
              <button className="btn ghost sm" onClick={() => assign("")}>Unassign</button>
            </div>
          ) : (
            <div className="muted" style={{ marginBottom: 10 }}>No configuration profile assigned.</div>
          )}
          <label className="row" style={{ gap: 8, alignItems: "center" }}>
            <span className="faint" style={{ fontSize: 12 }}>Assign profile</span>
            <select className="input sm" style={{ maxWidth: 300 }} value={a.config_profile?.id || ""} onChange={(e) => assign(e.target.value)}>
              <option value="">— none —</option>
              {(a.available_profiles || profiles).map((p: any) => <option key={p.id} value={p.id}>{p.name}{p.enabled ? "" : " (disabled)"}</option>)}
            </select>
          </label>
        </Card>
      )}

      {tab === "security" && (
        <div className="grid grid-2">
          <Card>
            <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Encryption</h3>
            <div className="row" style={{ gap: 6, marginBottom: 10 }}>
              <Pill tone={tel.quantum_safe ? "ok" : "info"} dot>{tel.quantum_safe ? "Quantum-safe (hybrid)" : "Classical"}</Pill>
            </div>
            <Row2 label="Content cipher" value={tel.content_alg || "—"} />
            <Row2 label="Signing algorithm" value={tel.signing_alg || "—"} />
            <Row2 label="Isolation state" value={a.isolation_state || tel.isolation_state || "—"} />
          </Card>
          <Card>
            <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Attestation &amp; integrity</h3>
            <div className="row" style={{ gap: 6, marginBottom: 10, flexWrap: "wrap" }}>
              <Pill tone={a.attestation_ok ? "ok" : "warn"} dot>{a.attestation_ok ? "Attested" : "Unattested"}</Pill>
              <Pill tone={(a.tamper_state || "normal") === "normal" ? "ok" : "danger"} dot>{(a.tamper_state || "normal") === "normal" ? "No tamper" : `tamper: ${a.tamper_state}`}</Pill>
            </div>
            <Row2 label="State" value={String(a.state).toLowerCase()} />
            <Row2 label="Last attestation" value={a.last_attestation_at ? timeAgo(a.last_attestation_at) : "—"} />
            <Row2 label="Last heartbeat" value={a.last_heartbeat_at ? timeAgo(a.last_heartbeat_at) : "—"} />
            <Row2 label="Software version" value={a.software_version || "—"} />
          </Card>
        </div>
      )}

      {tab === "logs" && (
        <Card>
          <div className="spread" style={{ marginBottom: 10 }}>
            <h3 style={{ margin: 0, fontSize: 15 }}>Recent agent logs</h3>
            <button className="btn ghost sm" onClick={() => void load()}>Refresh</button>
          </div>
          <div className="mono" style={{ fontSize: 11, background: "var(--bg-elev-2,#0b0f17)", borderRadius: 8, padding: 12, maxHeight: 460, overflow: "auto", lineHeight: 1.5 }}>
            {logLines.length ? logLines.map((line, i) => {
              const low = String(line).toLowerCase();
              const color = low.includes("error") ? "#f2545b" : (low.includes("warn") ? "#f5a623" : "var(--muted-c,#8a94a7)");
              return <div key={i} style={{ color, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{line}</div>;
            }) : <div className="muted">No log output reported. The appliance forwards its recent agent log on each heartbeat.</div>}
          </div>
        </Card>
      )}

      {tab === "commands" && (
        <Card>
          <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Recent signed commands</h3>
          {(a.recent_commands || []).length === 0 ? <div className="muted">No commands issued.</div> : (
            <table className="table">
              <thead><tr><th>Command</th><th>Status</th><th style={{ textAlign: "right" }}>Seq</th><th style={{ textAlign: "right" }}>When</th></tr></thead>
              <tbody>
                {(a.recent_commands || []).map((c: any, i: number) => (
                  <tr key={i}>
                    <td style={{ fontWeight: 600 }}>{c.type}</td>
                    <td><Pill tone={c.status === "acked" ? "ok" : c.status === "rejected" || c.status === "expired" ? "danger" : "info"} dot>{c.status}</Pill></td>
                    <td className="faint" style={{ textAlign: "right" }}>{c.sequence ?? "—"}</td>
                    <td className="faint" style={{ textAlign: "right" }}>{c.at ? timeAgo(c.at) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      )}

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function Crypto() {
  const [d, setD] = useState<any>(null);
  useEffect(() => { api.get("/admin/crypto-profiles").then(setD).catch(() => {}); }, []);
  if (!d) return <Card><div className="muted">Loading…</div></Card>;
  return (
    <Card>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h2>Cryptographic profile registry</h2>
        <Pill tone={d.pq_available ? "ok" : "warn"}>{d.pq_available ? "PQC active" : "fallback"}</Pill>
      </div>
      <table className="table">
        <thead><tr><th>Profile</th><th>Content</th><th>KEM</th><th>Signature</th><th>Hash</th><th>Status</th></tr></thead>
        <tbody>
          {d.profiles.map((p: any) => (
            <tr key={p.profile_id}>
              <td className="mono">{p.profile_id}</td>
              <td>{p.content_algo}</td>
              <td className="faint">{p.kem_classical} + {p.kem_pq}</td>
              <td className="faint">{p.sig_classical} + {p.sig_pq}</td>
              <td>{p.hash_algo}</td>
              <td><Pill tone={p.status === "preferred" ? "ok" : "info"}>{p.status}</Pill></td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function Audit() {
  const [d, setD] = useState<any>(null);
  useEffect(() => { api.get("/admin/audit").then(setD).catch(() => {}); }, []);
  if (!d) return <Card><div className="muted">Loading…</div></Card>;
  return (
    <Card>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h2>Tamper-evident audit ledger</h2>
        <Pill tone={d.chain_valid ? "ok" : "danger"}>{d.chain_valid ? "Chain valid" : "Chain broken"}</Pill>
      </div>
      <table className="table">
        <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Resource</th><th>Hash</th></tr></thead>
        <tbody>
          {d.events.map((e: any, i: number) => (
            <tr key={i}>
              <td className="faint">{timeAgo(e.created_at)}</td>
              <td>{e.actor}</td>
              <td><Pill tone="info">{e.action}</Pill></td>
              <td className="mono faint">{(e.resource || "").slice(0, 10)}</td>
              <td className="mono faint">{e.entry_hash}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

// ---- Debug / diagnostics (key-gated live troubleshooting) ------------------
function NotifTest({ dbg, flash }: {
  dbg: (method: string, path: string, body?: unknown) => Promise<any>;
  flash: (m: string) => void;
}) {
  const [meta, setMeta] = useState<any>(null);
  const [q, setQ] = useState("");
  const [users, setUsers] = useState<any[]>([]);
  const [user, setUser] = useState<any>(null);
  const [type, setType] = useState("daily_summary");
  const [busy, setBusy] = useState(false);
  const [repeat, setRepeat] = useState(24);
  const [footprint, setFootprint] = useState(true);

  async function load() {
    try {
      const m = await dbg("GET", "/debug/notifications");
      setMeta(m);
      setType(m.types?.[0]?.key || "daily_summary");
      setRepeat(m.settings?.source_repeat_hours ?? 24);
      setFootprint((m.settings?.enabled_insights || []).includes("footprint"));
    } catch { /* debug key not set yet */ }
  }
  useEffect(() => { void load(); }, []);

  async function search(v: string) {
    setQ(v);
    if (v.trim().length < 2) { setUsers([]); return; }
    try { const r = await dbg("GET", `/debug/notifications/users?q=${encodeURIComponent(v.trim())}`); setUsers(r.users || []); }
    catch { setUsers([]); }
  }
  async function send() {
    if (!user) { void notify({ message: "Pick a recipient first.", tone: "danger" }); return; }
    setBusy(true);
    try {
      const r = await dbg("POST", "/debug/notifications/test", { type, user_id: user.id });
      if (r.ok) flash(`Sent “${r.subject}” to ${r.sent_to}`);
      else void notify({ message: r.message || "Nothing to send", tone: "warn" });
      await load();
    } catch (e: any) { void notify({ message: e.message, tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function saveSettings() {
    try {
      await dbg("PUT", "/debug/notifications/settings",
        { source_repeat_hours: Number(repeat) || 24, enabled_insights: footprint ? ["footprint"] : [] });
      flash("Notification settings saved");
      await load();
    } catch (e: any) { void notify({ message: e.message, tone: "danger" }); }
  }

  if (!meta) return null;
  return (
    <Card style={{ marginBottom: 14 }}>
      <div className="spread" style={{ marginBottom: 10 }}>
        <h3 style={{ margin: 0, fontSize: 15 }}>Notification testing</h3>
        <span className="faint" style={{ fontSize: 11.5 }}>Send any notification to a chosen account (bypasses their preference).</span>
      </div>
      <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label className="stack" style={{ gap: 5, minWidth: 200 }}>
          <span className="faint" style={{ fontSize: 12 }}>Type</span>
          <select className="input sm" value={type} onChange={(e) => setType(e.target.value)}>
            {meta.types.map((t: any) => <option key={t.key} value={t.key}>{t.label}</option>)}
          </select>
        </label>
        <label className="stack flex1" style={{ gap: 5, minWidth: 220, position: "relative" }}>
          <span className="faint" style={{ fontSize: 12 }}>Recipient</span>
          <input className="input sm" placeholder="Search by email…" value={user ? user.email : q}
                 onChange={(e) => { setUser(null); void search(e.target.value); }} />
          {!user && users.length > 0 && (
            <div style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 5, background: "var(--panel)",
                          border: "1px solid var(--border)", borderRadius: 8, marginTop: 2, maxHeight: 200, overflow: "auto" }}>
              {users.map((u) => (
                <div key={u.id} onClick={() => { setUser(u); setUsers([]); }}
                     style={{ padding: "7px 10px", cursor: "pointer", fontSize: 13, borderBottom: "1px solid var(--border-soft)" }}>
                  {u.email} <span className="faint">· {u.name || u.role}</span>
                </div>
              ))}
            </div>
          )}
        </label>
        <button className="btn primary sm" disabled={busy || !user} onClick={send}>{busy ? "Sending…" : "Send test"}</button>
      </div>

      <div className="row" style={{ gap: 12, marginTop: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label className="stack" style={{ gap: 5, width: 200 }}>
          <span className="faint" style={{ fontSize: 12 }}>Source-problem repeat (hours)</span>
          <input className="input sm" type="number" value={repeat} onChange={(e) => setRepeat(Number(e.target.value))} />
        </label>
        <label className="row" style={{ gap: 8, alignItems: "center", height: 30 }}>
          <input type="checkbox" checked={footprint} onChange={(e) => setFootprint(e.target.checked)} />
          <span style={{ fontSize: 13 }}>Include “footprint” insight in daily summary</span>
        </label>
        <button className="btn ghost sm" onClick={saveSettings}>Save settings</button>
      </div>

      {(meta.recent || []).length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>Recent notifications</div>
          <table className="table"><tbody>
            {meta.recent.slice(0, 8).map((r: any, i: number) => (
              <tr key={i}>
                <td style={{ fontSize: 12 }}>{r.type}</td>
                <td style={{ fontSize: 12 }}>{r.to}</td>
                <td className="faint" style={{ fontSize: 11.5 }}>{r.subject}</td>
                <td style={{ textAlign: "right" }}>{r.ok ? <Pill tone="ok">sent</Pill> : <Pill tone="danger">failed</Pill>}</td>
              </tr>
            ))}
          </tbody></table>
        </div>
      )}
    </Card>
  );
}

function DebugAdmin() {
  const [key, setKey] = useState<string | null>(null);
  const [reveal, setReveal] = useState(false);
  const [stats, setStats] = useState<any>(null);
  const [health, setHealth] = useState<any>(null);
  const [bench, setBench] = useState<any>(null);
  const [nodes, setNodes] = useState<any>(null);
  const [sql, setSql] = useState("SELECT relname, n_live_tup, n_dead_tup FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 20");
  const [qres, setQres] = useState<any>(null);
  const [qerr, setQerr] = useState("");
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState("");
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3200); }

  async function loadKey() {
    try { const r = await api.get<{ enabled: boolean; key: string }>("/admin/debug-key"); setKey(r.key || ""); }
    catch { setKey(""); }
  }
  useEffect(() => { void loadKey(); }, []);

  // Debug endpoints require the X-Debug-Key header (in addition to the admin session).
  async function dbg<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`/api${path}`, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
        "X-Debug-Key": key || "",
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail ?? d; } catch { /* */ } throw new Error(d); }
    return res.json() as Promise<T>;
  }

  async function generate() {
    try { const r = await api.put<{ key: string }>("/admin/debug-key", { rotate: true }); setKey(r.key); flash("Debug key generated"); }
    catch (e) { await notify({ message: (e as Error).message, tone: "danger" }); }
  }
  async function disable() {
    if (!await confirmDialog({ title: "Disable debug API?", message: "This clears the key and disables all /debug endpoints.", tone: "danger", confirmLabel: "Disable" })) return;
    try { await api.del("/admin/debug-key"); setKey(""); setStats(null); setHealth(null); flash("Debug API disabled"); }
    catch (e) { await notify({ message: (e as Error).message, tone: "danger" }); }
  }
  async function run(name: string, fn: () => Promise<void>) {
    setBusy(name);
    try { await fn(); }
    catch (e) { await notify({ title: "Debug call failed", message: (e as Error).message, tone: "danger" }); }
    finally { setBusy(""); }
  }
  async function maintenance(action: string) {
    if (action !== "analyze" && !await confirmDialog({ title: "Run VACUUM?", message: "VACUUM reclaims dead-tuple bloat and refreshes planner stats. Safe to run live but can take a while on large tables.", confirmLabel: "Run" })) return;
    await run(`maint-${action}`, async () => { const r = await dbg<any>("POST", "/debug/db/maintenance", { action }); flash(`${r.ran} · ${r.ms}ms`); const s = await dbg<any>("GET", "/debug/db/stats"); setStats(s); });
  }

  if (key === null) return <Card><div className="muted">Loading…</div></Card>;

  if (!key) return (
    <>
      <h3 style={{ marginTop: 0 }}>Debug &amp; diagnostics</h3>
      <Card>
        <div className="stack" style={{ alignItems: "center", gap: 12, padding: "26px 12px", textAlign: "center" }}>
          <div className="insight-card-ic" style={{ background: "var(--inset)", width: 48, height: 48 }}><Icon name="activity" size={22} /></div>
          <div style={{ fontWeight: 700 }}>The debug API is disabled</div>
          <div className="faint" style={{ fontSize: 12.5, maxWidth: 460 }}>
            Generate a debug key to unlock live diagnostics — DB stats, query benchmarks, a read-only
            SQL console, maintenance (VACUUM/ANALYZE) and per-node fleet health. The key gates every
            <code> /debug</code> endpoint; keep it secret and rotate it when you're done.
          </div>
          <button className="btn primary" onClick={generate}><Icon name="key" size={14} /> Generate debug key</button>
        </div>
      </Card>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Debug &amp; diagnostics</h3>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn ghost sm" onClick={generate}>Rotate key</button>
          <button className="btn danger sm" onClick={disable}>Disable</button>
        </div>
      </div>

      <Card style={{ marginBottom: 14 }}>
        <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <Icon name="key" size={15} />
          <span style={{ fontWeight: 600, fontSize: 13 }}>Debug key</span>
          <code style={{ fontSize: 12, background: "var(--inset)", padding: "3px 8px", borderRadius: 6 }}>
            {reveal ? key : `${key.slice(0, 8)}${"•".repeat(16)}`}
          </code>
          <button className="btn ghost sm" onClick={() => setReveal((r) => !r)}>{reveal ? "Hide" : "Reveal"}</button>
          <button className="btn ghost sm" onClick={() => { void navigator.clipboard?.writeText(key); flash("Copied"); }}>Copy</button>
          <span className="faint" style={{ fontSize: 11.5 }}>Send as <code>X-Debug-Key</code> header to call <code>/api/debug/*</code>.</span>
        </div>
      </Card>

      <div className="grid grid-2" style={{ gap: 14, marginBottom: 14 }}>
        <Card>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h3 style={{ margin: 0, fontSize: 15 }}>Health</h3>
            <button className="btn ghost sm" disabled={busy === "health"} onClick={() => void run("health", async () => setHealth(await dbg("GET", "/debug/health")))}>Check</button>
          </div>
          {health ? (
            <div className="stack" style={{ gap: 4, fontSize: 12.5 }}>
              <div className="row" style={{ gap: 8 }}><Pill tone={health.db_ok ? "ok" : "danger"} dot>{health.db_ok ? "DB up" : "DB down"}</Pill><span className="faint">ping {health.db_ping_ms} ms</span><span className="faint">· {health.dialect}</span></div>
              <div className="faint">web pool: {health.pools?.web?.checked_out}/{health.pools?.web?.size} out · worker pool: {health.pools?.worker?.checked_out}/{health.pools?.worker?.size} out</div>
              <div className="faint">role {health.node_role} · federated {String(health.federated)} · sync {String(health.sync_enabled)}</div>
            </div>
          ) : <div className="muted" style={{ fontSize: 12.5 }}>Run a health check.</div>}
        </Card>
        <Card>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h3 style={{ margin: 0, fontSize: 15 }}>Query benchmark</h3>
            <button className="btn ghost sm" disabled={busy === "bench"} onClick={() => void run("bench", async () => setBench(await dbg("POST", "/debug/db/benchmark", { iterations: 3 })))}>{busy === "bench" ? "Running…" : "Run"}</button>
          </div>
          {bench ? (
            <table className="table"><tbody>
              {bench.results.map((r: any) => (
                <tr key={r.name}>
                  <td style={{ fontSize: 12 }}>{r.name}</td>
                  <td style={{ textAlign: "right" }}>{r.ok ? <span style={{ color: (r.ms > 250) ? "var(--warn)" : undefined }}>{r.ms} ms</span> : <span style={{ color: "var(--danger-c)" }}>err</span>}</td>
                  <td className="faint" style={{ textAlign: "right", fontSize: 11 }}>{r.rows ?? ""}</td>
                </tr>
              ))}
            </tbody></table>
          ) : <div className="muted" style={{ fontSize: 12.5 }}>Time representative queries to find slow paths.</div>}
        </Card>
      </div>

      <NotifTest dbg={dbg} flash={flash} />

      <Card style={{ marginBottom: 14 }}>
        <div className="spread" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Database</h3>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn ghost sm" disabled={busy === "stats"} onClick={() => void run("stats", async () => setStats(await dbg("GET", "/debug/db/stats")))}>{busy === "stats" ? "Loading…" : "Load stats"}</button>
            <button className="btn ghost sm" disabled={!!busy} onClick={() => void run("prune", async () => { const r = await dbg<any>("POST", "/debug/db/prune"); const p = r.pruned || {}; flash(`pruned: ${Object.entries(p).filter(([, v]) => v).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ") || "nothing to prune"}`); setStats(await dbg("GET", "/debug/db/stats")); })}>Prune tables</button>
            <button className="btn ghost sm" disabled={!!busy} onClick={() => void maintenance("analyze")}>ANALYZE</button>
            <button className="btn ghost sm" disabled={!!busy} onClick={() => void maintenance("vacuum")}>VACUUM ANALYZE</button>
          </div>
        </div>
        {stats ? (
          <>
            <div className="row" style={{ gap: 16, marginBottom: 10, fontSize: 12.5, flexWrap: "wrap" }}>
              {stats.database_size_bytes != null && <span className="faint">DB size <b style={{ color: "var(--text)" }}>{bytes(stats.database_size_bytes)}</b></span>}
              {stats.idle_in_transaction != null && <span className="faint">idle-in-tx <b style={{ color: stats.idle_in_transaction > 0 ? "var(--warn)" : "var(--text)" }}>{stats.idle_in_transaction}</b></span>}
              {(stats.connections || []).map((c: any) => <span key={c.state} className="faint">{c.state}: <b style={{ color: "var(--text)" }}>{c.count}</b>{c.longest_xact_s ? ` (${c.longest_xact_s}s)` : ""}</span>)}
            </div>
            <table className="table">
              <thead><tr><th>Table</th><th style={{ textAlign: "right" }}>Live</th><th style={{ textAlign: "right" }}>Dead</th><th style={{ textAlign: "right" }}>Bloat</th><th style={{ textAlign: "right" }}>Size</th><th>Last analyze</th></tr></thead>
              <tbody>
                {(stats.tables || []).map((t: any) => (
                  <tr key={t.table}>
                    <td style={{ fontSize: 12 }}>{t.table}</td>
                    <td style={{ textAlign: "right" }}>{(t.live ?? t.rows ?? 0).toLocaleString?.() ?? t.rows}</td>
                    <td style={{ textAlign: "right" }}>{(t.dead ?? 0).toLocaleString?.() ?? ""}</td>
                    <td style={{ textAlign: "right", color: (t.dead_ratio ?? 0) > 0.2 ? "var(--warn)" : undefined }}>{t.dead_ratio != null ? `${Math.round(t.dead_ratio * 100)}%` : ""}</td>
                    <td style={{ textAlign: "right" }}>{t.total_bytes != null ? bytes(t.total_bytes) : ""}</td>
                    <td className="faint" style={{ fontSize: 11 }}>{t.last_analyze ? timeAgo(t.last_analyze) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : <div className="muted" style={{ fontSize: 12.5 }}>Load DB stats to see table sizes, dead-tuple bloat and connection activity (a high bloat % or idle-in-transaction count explains broad slowness — run VACUUM ANALYZE).</div>}
      </Card>

      <Card style={{ marginBottom: 14 }}>
        <div className="spread" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Read-only SQL console</h3>
          <button className="btn primary sm" disabled={busy === "query"} onClick={() => void run("query", async () => { setQerr(""); try { setQres(await dbg("POST", "/debug/query", { sql })); } catch (e) { setQerr((e as Error).message); setQres(null); } })}>{busy === "query" ? "Running…" : "Run query"}</button>
        </div>
        <textarea className="input" style={{ fontFamily: "monospace", fontSize: 12.5, minHeight: 80 }} value={sql} onChange={(e) => setSql(e.target.value)} />
        {qerr && <div style={{ color: "var(--danger-c)", fontSize: 12, marginTop: 6 }}>{qerr}</div>}
        {qres && (
          <div style={{ marginTop: 10, overflowX: "auto" }}>
            <div className="faint" style={{ fontSize: 11.5, marginBottom: 4 }}>{qres.row_count} row(s) · {qres.ms} ms{qres.truncated ? " · truncated" : ""}</div>
            <table className="table">
              <thead><tr>{(qres.columns || []).map((c: string) => <th key={c}>{c}</th>)}</tr></thead>
              <tbody>
                {(qres.rows || []).map((row: any[], i: number) => (
                  <tr key={i}>{row.map((v, j) => <td key={j} className="mono" style={{ fontSize: 11.5, maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{v === null ? <span className="faint">null</span> : String(v)}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card>
        <div className="spread" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Fleet DB health</h3>
          <button className="btn ghost sm" disabled={busy === "nodes"} onClick={() => void run("nodes", async () => setNodes(await dbg("GET", "/debug/nodes")))}>{busy === "nodes" ? "Probing…" : "Probe nodes"}</button>
        </div>
        {nodes ? (
          <table className="table">
            <thead><tr><th>Node</th><th>Reachable</th><th style={{ textAlign: "right" }}>DB size</th><th style={{ textAlign: "right" }}>Idle-in-tx</th><th>Top table</th></tr></thead>
            <tbody>
              {(nodes.nodes || []).map((n: any) => {
                const st = n.stats || {};
                const top = (st.top_tables || [])[0];
                return (
                  <tr key={n.id}>
                    <td style={{ fontWeight: 600 }}>{n.name}<div className="faint" style={{ fontSize: 11 }}>{n.role}{n.is_self ? " · self" : ""}</div></td>
                    <td>{n.reachable === false ? <Pill tone="danger" dot>unreachable</Pill> : n.reachable ? <Pill tone="ok" dot>ok</Pill> : <Pill tone="warn">—</Pill>}{n.error && <div className="faint" style={{ fontSize: 10.5, color: "var(--warn)" }}>{String(n.error).slice(0, 60)}</div>}</td>
                    <td style={{ textAlign: "right" }}>{st.database_size_bytes != null ? bytes(st.database_size_bytes) : "—"}</td>
                    <td style={{ textAlign: "right", color: st.idle_in_transaction > 0 ? "var(--warn)" : undefined }}>{st.idle_in_transaction ?? "—"}</td>
                    <td className="faint" style={{ fontSize: 11.5 }}>{top ? `${top.table} · ${bytes(top.total_bytes || 0)}` : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <div className="muted" style={{ fontSize: 12.5 }}>Probe every fleet node's database (via the fleet secret) to find which one is slow.</div>}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function Updates() {
  const [releases, setReleases] = useState<any[]>([]);
  const [fleet, setFleet] = useState<any[]>([]);
  const [toast, setToast] = useState("");

  async function load() {
    setReleases(await api.get<any[]>("/updates/releases"));
    setFleet(await api.get<any[]>("/admin/fleet"));
  }
  useEffect(() => { void load(); }, []);

  async function publish(component: string) {
    const version = await promptDialog({
      title: `Publish ${component} release`,
      label: "New version",
      defaultValue: "1.0.1",
      placeholder: "1.0.1",
      confirmLabel: "Publish",
    });
    if (!version) return;
    await api.post("/updates/releases", {
      component, version,
      package_url: `https://vault.arkive.life/releases/${component}-${version}.tar.gz`,
      package_hash: `sha384:${version}-placeholder`,
      security_floor: "1.0.0",
    });
    setToast(`Published ${component} ${version}`);
    await load();
    setTimeout(() => setToast(""), 2500);
  }

  async function trigger(r: any, target_type: string, target_id?: string) {
    await api.post("/updates/trigger", { release_id: r.id, target_type, target_id });
    setToast(`Update ${r.version} triggered for ${target_type}`);
    setTimeout(() => setToast(""), 2500);
  }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h2>Software releases</h2>
          <div className="row">
            <button className="btn sm" onClick={() => publish("cloud")}>Publish cloud</button>
            <button className="btn sm" onClick={() => publish("appliance")}>Publish appliance</button>
          </div>
        </div>
        <table className="table">
          <thead><tr><th>Component</th><th>Version</th><th>Hash</th><th>Trigger</th></tr></thead>
          <tbody>
            {releases.map((r) => (
              <tr key={r.id}>
                <td><Pill tone="info">{r.component}</Pill></td>
                <td className="mono">{r.version}</td>
                <td className="mono faint">{r.package_hash.slice(0, 16)}…</td>
                <td>
                  {r.component === "cloud" ? (
                    <button className="btn sm" onClick={() => trigger(r, "cloud")}>Roll out to cloud</button>
                  ) : (
                    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
                      {fleet.map((a) => (
                        <button key={a.id} className="btn sm" onClick={() => trigger(r, "appliance", a.id)}>
                          {a.serial.slice(0, 8)}
                        </button>
                      ))}
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {releases.length === 0 && <div className="muted">No releases published.</div>}
      </Card>
      <div className="muted" style={{ fontSize: 12.5 }}>
        Cloud updates are picked up by the cloud updater script; appliance updates are delivered
        as signed STAGE_UPDATE commands over the management plane with rollback protection.
      </div>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

interface LicensePlan { id: string; name: string; price_per_tb_month: number; min_tb: number; }
interface PricingCfg {
  currency: string;
  protection_price_per_tb_month: number;
  cloud_price_per_tb_month: number;
  s3_price_per_tb_month: number;
  azure_price_per_tb_month: number;
  license_plans: LicensePlan[];
  appliance_tiers: { capacity_tb: number; monthly: number; setup: number; model: string }[];
  data_value_per_type: Record<string, number>;
}

function Pricing() {
  const [p, setP] = useState<PricingCfg | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => { api.get<PricingCfg>("/admin/pricing").then(setP).catch(() => {}); }, []);
  function set<K extends keyof PricingCfg>(k: K, v: PricingCfg[K]) { setSaved(false); setP((c) => c ? { ...c, [k]: v } : c); }
  async function save() {
    if (!p) return;
    try { const r = await api.put<PricingCfg>("/admin/pricing", p); setP(r); setSaved(true); } catch { /* ignore */ }
  }
  if (!p) return <Card><div className="muted">Loading pricing…</div></Card>;

  const num = (v: string) => Number(v) || 0;
  const valueKeys = ["email", "credential", "document", "photo", "media", "file", "contact"];
  const plans = p.license_plans || [];
  function setPlan(i: number, patch: Partial<LicensePlan>) {
    const a = plans.map((pl, idx) => idx === i ? { ...pl, ...patch } : pl);
    set("license_plans", a);
  }
  function addPlan() {
    const id = `plan-${Date.now().toString(36)}`;
    set("license_plans", [...plans, { id, name: "New plan", price_per_tb_month: 6, min_tb: 1 }]);
  }
  function removePlan(i: number) { set("license_plans", plans.filter((_, idx) => idx !== i)); }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 4 }}>
          <h3 style={{ marginTop: 0 }}>Recurring license plans</h3>
          <button className="btn sm" onClick={addPlan}>+ Add plan</button>
        </div>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
          Each tenant is assigned a plan (on the Tenants page). The plan sets the recurring
          data-protection rate per TB · month and the minimum TB the customer is billed for.
        </div>
        <table className="table">
          <thead><tr><th>Plan name</th><th>Plan ID</th><th>Price / TB · mo ($)</th><th>Minimum (TB)</th><th></th></tr></thead>
          <tbody>
            {plans.map((pl, i) => (
              <tr key={i}>
                <td><input className="input sm" value={pl.name} onChange={(e) => setPlan(i, { name: e.target.value })} /></td>
                <td><input className="input sm" value={pl.id} onChange={(e) => setPlan(i, { id: e.target.value.trim() })} style={{ width: 130 }} /></td>
                <td><input className="input sm" type="number" step="0.01" value={pl.price_per_tb_month} onChange={(e) => setPlan(i, { price_per_tb_month: num(e.target.value) })} style={{ width: 120 }} /></td>
                <td><input className="input sm" type="number" step="0.5" value={pl.min_tb} onChange={(e) => setPlan(i, { min_tb: num(e.target.value) })} style={{ width: 110 }} /></td>
                <td><button className="btn ghost sm" onClick={() => removePlan(i)} title="Remove">Remove</button></td>
              </tr>
            ))}
            {plans.length === 0 && <tr><td colSpan={5} className="faint" style={{ fontSize: 12.5 }}>No plans — add one to define recurring pricing.</td></tr>}
          </tbody>
        </table>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Recurring pricing (per TB · month)</h3>
        <div className="grid grid-4" style={{ gap: 12 }}>
          <PriceField label="Arkive Cloud storage" value={p.cloud_price_per_tb_month} onChange={(v) => set("cloud_price_per_tb_month", num(v))} />
        </div>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Appliance hardware (leased)</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>Monthly lease fee plus a one-time setup fee per appliance.</div>
        <table className="table">
          <thead><tr><th>Model</th><th>Capacity (TB)</th><th>Monthly ($)</th><th>Setup ($)</th></tr></thead>
          <tbody>
            {p.appliance_tiers.map((t, i) => (
              <tr key={i}>
                <td><input className="input sm" value={t.model} onChange={(e) => { const a = [...p.appliance_tiers]; a[i] = { ...t, model: e.target.value }; set("appliance_tiers", a); }} /></td>
                <td><input className="input sm" type="number" value={t.capacity_tb} onChange={(e) => { const a = [...p.appliance_tiers]; a[i] = { ...t, capacity_tb: num(e.target.value) }; set("appliance_tiers", a); }} style={{ width: 90 }} /></td>
                <td><input className="input sm" type="number" value={t.monthly} onChange={(e) => { const a = [...p.appliance_tiers]; a[i] = { ...t, monthly: num(e.target.value) }; set("appliance_tiers", a); }} style={{ width: 110 }} /></td>
                <td><input className="input sm" type="number" value={t.setup} onChange={(e) => { const a = [...p.appliance_tiers]; a[i] = { ...t, setup: num(e.target.value) }; set("appliance_tiers", a); }} style={{ width: 110 }} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Estimated value per object ($)</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>Drives the customer's data-value / cost-benefit estimate.</div>
        <div className="grid grid-4" style={{ gap: 12 }}>
          {valueKeys.map((k) => (
            <PriceField key={k} label={k[0].toUpperCase() + k.slice(1)} value={p.data_value_per_type[k] ?? 0}
                        onChange={(v) => set("data_value_per_type", { ...p.data_value_per_type, [k]: num(v) })} />
          ))}
        </div>
        <div style={{ marginTop: 18, paddingTop: 16, borderTop: "1px solid var(--border-soft)" }}>
          <h4 style={{ margin: "0 0 4px" }}>Third-party cloud storage estimates (per TB · month)</h4>
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
            Shown to customers who bring their own bucket, to estimate what they'll pay their provider directly.
          </div>
          <div className="grid grid-4" style={{ gap: 12 }}>
            <PriceField label="AWS S3 estimate" value={p.s3_price_per_tb_month} onChange={(v) => set("s3_price_per_tb_month", num(v))} />
            <PriceField label="Azure estimate" value={p.azure_price_per_tb_month} onChange={(v) => set("azure_price_per_tb_month", num(v))} />
          </div>
        </div>
      </Card>

      <div className="row">
        <button className="btn primary" onClick={save}>{saved ? "Saved ✓" : "Save pricing"}</button>
      </div>
    </>
  );
}

function PriceField({ label, value, onChange }: { label: string; value: number; onChange: (v: string) => void }) {
  return (
    <div className="stack" style={{ gap: 4 }}>
      <label className="faint" style={{ fontSize: 12 }}>{label}</label>
      <input className="input sm" type="number" step="0.01" value={value} onChange={(e) => onChange(e.target.value)} />
    </div>
  );
}

interface EmailCfg {
  provider: string; enabled: boolean; from_email: string; from_name: string;
  reply_to: string; region: string;
  aws_access_key_id: string; has_aws_secret: boolean;
}
interface AdminUser { id: string; email: string; display_name: string; role: string; status: string; tenant_id: string; tenant_name: string; }

function EmailAdmin() {
  const [cfg, setCfg] = useState<EmailCfg | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [toast, setToast] = useState("");
  const [testTo, setTestTo] = useState("");
  const [awsSecret, setAwsSecret] = useState("");

  // Broadcast composer
  const [audience, setAudience] = useState<"all" | "selected">("all");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [subject, setSubject] = useState("");
  const [message, setMessage] = useState("");
  const [ctaLabel, setCtaLabel] = useState("");
  const [ctaUrl, setCtaUrl] = useState("");
  const [sending, setSending] = useState(false);

  useEffect(() => {
    api.get<EmailCfg>("/admin/email-config").then(setCfg).catch(() => {});
    api.get<AdminUser[]>("/admin/users").then(setUsers).catch(() => {});
  }, []);
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3500); }
  function setC<K extends keyof EmailCfg>(k: K, v: EmailCfg[K]) { setCfg((c) => c ? { ...c, [k]: v } : c); }

  async function saveCfg() {
    if (!cfg) return;
    // Only send the secret when the admin typed a new one (blank preserves it).
    const payload: Record<string, unknown> = { ...cfg };
    if (awsSecret.trim()) payload.aws_secret_access_key = awsSecret.trim();
    try { const r = await api.put<EmailCfg>("/admin/email-config", payload); setCfg(r); setAwsSecret(""); flash("Email settings saved"); }
    catch { flash("Could not save settings"); }
  }
  async function sendTest() {
    if (!testTo.trim()) return;
    try {
      const r = await api.post<{ channel: string; provider: string; error?: string | null; delivered: boolean }>("/admin/email-test", { to: testTo.trim() });
      if (r.delivered) flash(`Test sent via ${r.channel}`);
      else if (r.channel === "log") flash("Logged only — enable SES (provider=ses + Enabled)");
      else flash(`Send failed: ${r.error || "unknown error"}`);
    }
    catch { flash("Test failed"); }
  }
  function toggleUser(id: string) {
    setSelected((cur) => { const n = new Set(cur); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  }
  const recipientCount = audience === "all" ? users.filter((u) => u.status === "active").length : selected.size;
  async function broadcast() {
    if (!subject.trim() || !message.trim() || recipientCount === 0) return;
    setSending(true);
    try {
      const r = await api.post<{ sent: number; failed: number; recipients: number; channel: string }>("/admin/email-broadcast", {
        audience, user_ids: [...selected], subject, message,
        cta_label: ctaLabel || undefined, cta_url: ctaUrl || undefined,
      });
      flash(`Sent to ${r.sent} of ${r.recipients} (via ${r.channel}${r.failed ? `, ${r.failed} failed` : ""})`);
      setSubject(""); setMessage(""); setCtaLabel(""); setCtaUrl("");
    } catch { flash("Broadcast failed"); }
    setSending(false);
  }

  if (!cfg) return <Card><div className="muted">Loading email settings…</div></Card>;

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Default email service (AWS SES)</h3>
          <Pill tone={cfg.enabled ? "ok" : "warn"}>{cfg.enabled ? "Enabled" : "Disabled"}</Pill>
        </div>
        <div className="grid grid-3" style={{ gap: 12 }}>
          <Field label="Provider">
            <select className="input sm" value={cfg.provider} onChange={(e) => setC("provider", e.target.value)}>
              <option value="ses">AWS SES</option>
              <option value="smtp">SMTP</option>
              <option value="log">Log only (dev)</option>
            </select>
          </Field>
          <Field label="From name"><input className="input sm" value={cfg.from_name} onChange={(e) => setC("from_name", e.target.value)} /></Field>
          <Field label="From email"><input className="input sm" value={cfg.from_email} onChange={(e) => setC("from_email", e.target.value)} placeholder="notifications@arkive.life" /></Field>
          <Field label="Reply-to"><input className="input sm" value={cfg.reply_to} onChange={(e) => setC("reply_to", e.target.value)} placeholder="support@arkive.life" /></Field>
          <Field label="AWS region"><input className="input sm" value={cfg.region} onChange={(e) => setC("region", e.target.value)} placeholder="us-east-1" /></Field>
          <Field label="Enabled">
            <label className="row" style={{ gap: 8, fontSize: 13 }}>
              <input type="checkbox" checked={cfg.enabled} onChange={(e) => setC("enabled", e.target.checked)} /> Send live emails
            </label>
          </Field>
          <Field label="AWS access key ID"><input className="input sm" value={cfg.aws_access_key_id} onChange={(e) => setC("aws_access_key_id", e.target.value)} placeholder="AKIA…" autoComplete="off" /></Field>
          <Field label={`AWS secret access key${cfg.has_aws_secret ? " (saved)" : ""}`}>
            <input className="input sm" type="password" value={awsSecret} onChange={(e) => setAwsSecret(e.target.value)}
                   placeholder={cfg.has_aws_secret ? "•••••••• leave blank to keep" : "secret access key"} autoComplete="off" />
          </Field>
        </div>
        <div className="row" style={{ gap: 10, marginTop: 14, flexWrap: "wrap" }}>
          <button className="btn primary" onClick={saveCfg}>Save settings</button>
          <input className="input sm" placeholder="you@example.com" value={testTo} onChange={(e) => setTestTo(e.target.value)} style={{ width: 220 }} />
          <button className="btn" onClick={sendTest}>Send test</button>
        </div>
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          Set the AWS access key + secret of an IAM user with <b>ses:SendEmail</b>, or leave them blank to
          use the instance IAM role / environment. The secret is encrypted at rest and never returned.
          Verify the <b>arkive.life</b> domain in SES and publish SPF, DKIM & DMARC records for deliverability.
        </div>
      </Card>

      <Card>
        <h3 style={{ marginTop: 0 }}>Send a message</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>Email all users or a selected group. Each recipient is sent an individual, branded message.</div>
        <div className="row" style={{ gap: 8, marginBottom: 12 }}>
          {(["all", "selected"] as const).map((a) => (
            <span key={a} className={`chip ${audience === a ? "active" : ""}`} onClick={() => setAudience(a)}>
              {a === "all" ? "All users" : "Selected users"}
            </span>
          ))}
          <span className="faint" style={{ fontSize: 12, alignSelf: "center" }}>{recipientCount} recipient{recipientCount === 1 ? "" : "s"}</span>
        </div>

        {audience === "selected" && (
          <div style={{ maxHeight: 200, overflow: "auto", border: "1px solid var(--border-soft)", borderRadius: 8, padding: 8, marginBottom: 12 }}>
            {users.map((u) => (
              <label key={u.id} className="row" style={{ gap: 8, padding: "4px 2px", fontSize: 12.5, cursor: "pointer" }}>
                <input type="checkbox" checked={selected.has(u.id)} onChange={() => toggleUser(u.id)} />
                <span style={{ fontWeight: 600 }}>{u.display_name || u.email}</span>
                <span className="faint">{u.email} · {u.tenant_name}</span>
              </label>
            ))}
            {users.length === 0 && <div className="muted">No users.</div>}
          </div>
        )}

        <div className="stack" style={{ gap: 10 }}>
          <Field label="Subject"><input className="input sm" value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="Subject line" /></Field>
          <Field label="Message">
            <textarea className="input" value={message} onChange={(e) => setMessage(e.target.value)} rows={6}
                      placeholder="Write your message. Blank lines separate paragraphs." style={{ resize: "vertical" }} />
          </Field>
          <div className="grid grid-2" style={{ gap: 10 }}>
            <Field label="Button label (optional)"><input className="input sm" value={ctaLabel} onChange={(e) => setCtaLabel(e.target.value)} placeholder="Open Arkive" /></Field>
            <Field label="Button link (optional)"><input className="input sm" value={ctaUrl} onChange={(e) => setCtaUrl(e.target.value)} placeholder="https://vault.arkive.life" /></Field>
          </div>
        </div>
        <div className="row" style={{ marginTop: 14 }}>
          <button className="btn primary" onClick={broadcast} disabled={sending || !subject.trim() || !message.trim() || recipientCount === 0}>
            {sending ? "Sending…" : `Send to ${recipientCount} recipient${recipientCount === 1 ? "" : "s"}`}
          </button>
        </div>
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="stack" style={{ gap: 4 }}>
      <label className="faint" style={{ fontSize: 12 }}>{label}</label>
      {children}
    </div>
  );
}

interface ConfigKey { secret: boolean; set: boolean; value: string }
interface ConfigObj { id: string; name: string; kind: string; keys: Record<string, ConfigKey>; updated_at?: string }
interface SourceSlot { type: string; label: string; kind: string; keys: string[]; enabled: boolean; config_object_id: string | null; configured: boolean; icon: string; color: string; family: string; category: string; backfill_supported?: boolean; backfill_enabled?: boolean }
interface DraftRow { key: string; value: string; secret: boolean; set: boolean }
interface ServiceKind { kind: string; label: string; category: string; credential_keys: string[]; settings: string[]; setting_defaults?: Record<string, string>; required: string[]; capabilities?: string[] }
interface ServiceObj { id: string; name: string; kind: string; kind_label: string; category: string; enabled: boolean; config_object_id: string | null; settings: Record<string, string>; setting_keys: string[]; credential_keys: string[]; capabilities?: string[]; capability_options?: string[]; configured: boolean; updated_at?: string }

function ConfigObjectsAdmin() {
  const [objects, setObjects] = useState<ConfigObj[]>([]);
  const [toast, setToast] = useState("");
  const [draft, setDraft] = useState<{ id?: string; name: string; kind: string; rows: DraftRow[] } | null>(null);
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }

  async function load() {
    try { setObjects(await api.get<ConfigObj[]>("/admin/config-objects")); } catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  function newDraft(kind = "oauth") {
    const preset: Record<string, string[]> = {
      oauth: ["client_id", "client_secret"],
      ses: ["aws_access_key_id", "aws_secret_access_key", "region", "from_email"],
      "api-key": ["api_key"],
      generic: [""],
    };
    setDraft({ name: "", kind, rows: (preset[kind] || [""]).map((k) => ({ key: k, value: "", secret: /secret|password|token|private/i.test(k), set: false })) });
  }
  function editDraft(o: ConfigObj) {
    setDraft({
      id: o.id, name: o.name, kind: o.kind,
      rows: Object.entries(o.keys).map(([k, v]) => ({ key: k, value: v.value, secret: v.secret, set: v.set })),
    });
  }
  async function saveDraft() {
    if (!draft) return;
    const values: Record<string, string> = {};
    for (const r of draft.rows) {
      if (!r.key.trim()) continue;
      if (r.secret && !r.value) continue;  // blank secret preserves stored value
      values[r.key.trim()] = r.value;
    }
    try {
      if (draft.id) await api.put(`/admin/config-objects/${draft.id}`, { name: draft.name, kind: draft.kind, values });
      else await api.post("/admin/config-objects", { name: draft.name || "Config", kind: draft.kind, values });
      setDraft(null); flash("Configuration saved"); await load();
    } catch { flash("Could not save"); }
  }
  async function delObject(o: ConfigObj) {
    if (!await confirmDialog({ title: "Delete config object?", message: `Delete "${o.name}". Any linked sources will be unlinked.`, tone: "danger", confirmLabel: "Delete" })) return;
    try { await api.del(`/admin/config-objects/${o.id}`); flash("Deleted"); await load(); } catch { flash("Delete failed"); }
  }

  return (
    <>
      <Card>
        <div className="spread" style={{ marginBottom: 10 }}>
          <div>
            <h3 style={{ margin: 0 }}>Configuration objects</h3>
            <div className="muted" style={{ fontSize: 12.5 }}>Encrypted key-value credentials (OAuth keys, API keys, SES) — link them to sources.</div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <button className="btn sm" onClick={() => newDraft("oauth")}>+ OAuth</button>
            <button className="btn sm" onClick={() => newDraft("ses")}>+ SES</button>
            <button className="btn sm" onClick={() => newDraft("generic")}>+ Generic</button>
          </div>
        </div>

        {draft && (
          <div style={{ border: "1px solid var(--border-soft)", borderRadius: 10, padding: 14, marginBottom: 12, background: "var(--inset)" }}>
            <div className="grid grid-2" style={{ gap: 12, marginBottom: 10 }}>
              <Field label="Name"><input className="input sm" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="e.g. Google OAuth" /></Field>
              <Field label="Kind"><input className="input sm" value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value })} /></Field>
            </div>
            <div className="stack" style={{ gap: 6 }}>
              {draft.rows.map((r, i) => (
                <div key={i} className="row" style={{ gap: 8 }}>
                  <input className="input sm" style={{ width: 200 }} value={r.key} placeholder="key"
                         onChange={(e) => { const rows = [...draft.rows]; rows[i] = { ...r, key: e.target.value, secret: /secret|password|token|private/i.test(e.target.value) }; setDraft({ ...draft, rows }); }} />
                  <input className="input sm flex1" type={r.secret ? "password" : "text"}
                         value={r.value} placeholder={r.secret && r.set ? "•••••• leave blank to keep" : "value"}
                         onChange={(e) => { const rows = [...draft.rows]; rows[i] = { ...r, value: e.target.value }; setDraft({ ...draft, rows }); }} />
                  <button className="btn ghost sm" onClick={() => setDraft({ ...draft, rows: draft.rows.filter((_, j) => j !== i) })}>✕</button>
                </div>
              ))}
              <button className="btn ghost sm" style={{ alignSelf: "flex-start" }} onClick={() => setDraft({ ...draft, rows: [...draft.rows, { key: "", value: "", secret: false, set: false }] })}>+ Add key</button>
            </div>
            <div className="row" style={{ gap: 8, marginTop: 12 }}>
              <button className="btn primary sm" onClick={saveDraft}>Save</button>
              <button className="btn ghost sm" onClick={() => setDraft(null)}>Cancel</button>
            </div>
          </div>
        )}

        <table className="table">
          <thead><tr><th>Name</th><th>Kind</th><th>Keys</th><th></th></tr></thead>
          <tbody>
            {objects.map((o) => (
              <tr key={o.id}>
                <td style={{ fontWeight: 600 }}>{o.name}</td>
                <td><Pill tone="info">{o.kind}</Pill></td>
                <td className="faint" style={{ fontSize: 12 }}>{Object.entries(o.keys).map(([k, v]) => `${k}${v.secret ? (v.set ? " ✓" : " –") : ""}`).join(", ")}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <button className="btn ghost sm" onClick={() => editDraft(o)}>Edit</button>{" "}
                  <button className="btn danger sm" onClick={() => delObject(o)}>Delete</button>
                </td>
              </tr>
            ))}
            {objects.length === 0 && <tr><td colSpan={4} className="muted">No configuration objects yet.</td></tr>}
          </tbody>
        </table>
      </Card>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function SourcesAdmin() {
  const [sources, setSources] = useState<SourceSlot[]>([]);
  const [objects, setObjects] = useState<ConfigObj[]>([]);
  const [toast, setToast] = useState("");
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }

  async function load() {
    try { setSources(await api.get<SourceSlot[]>("/admin/sources")); } catch { /* ignore */ }
    try { setObjects(await api.get<ConfigObj[]>("/admin/config-objects")); } catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  async function setSource(s: SourceSlot, patch: { enabled?: boolean; config_object_id?: string | null; family?: string; backfill_enabled?: boolean }) {
    try { await api.put(`/admin/sources/${s.type}`, patch); await load(); } catch { flash("Update failed"); }
  }

  async function editFamily(s: SourceSlot) {
    const families = [...new Set(sources.map((x) => x.family).filter(Boolean))].sort();
    const r = await formDialog({
      title: `Family for ${s.label}`,
      message: "Group this source under a family, or type a new family to create one.",
      confirmLabel: "Save",
      fields: [
        { name: "family", label: "Family", defaultValue: s.family,
          options: families.map((f) => ({ label: f, value: f })) },
        { name: "new_family", label: "…or add a new family", placeholder: "e.g. Financial" },
      ],
    });
    if (!r) return;
    const family = (r.new_family || "").trim() || r.family;
    if (family && family !== s.family) { await setSource(s, { family }); flash("Family updated"); }
  }

  // Group sources by family, families sorted, "Other" last.
  const families = [...new Set(sources.map((s) => s.family || "Other"))]
    .sort((a, b) => (a === "Other" ? 1 : b === "Other" ? -1 : a.localeCompare(b)));

  return (
    <>
      <Card>
        <h3 style={{ marginTop: 0 }}>Sources</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 16 }}>
          Enable each integration and link the configuration object that supplies its credentials.
          Sources are grouped by family — change a source's family to regroup it (this also applies on the customer catalog).
          Deep backfill (where supported) runs a paced background crawl of full history alongside the fast recent sync.
        </div>
        <div className="stack" style={{ gap: 20 }}>
          {families.map((fam) => (
            <div key={fam}>
              <div className="row" style={{ gap: 8, marginBottom: 8, alignItems: "center" }}>
                <div className="nav-section" style={{ padding: 0 }}>{fam}</div>
                <span className="faint" style={{ fontSize: 11 }}>{sources.filter((s) => (s.family || "Other") === fam).length}</span>
              </div>
              <table className="table">
                <thead><tr><th>Source</th><th>Enabled</th><th>Configuration</th><th>Status</th><th>Backfill</th><th></th></tr></thead>
                <tbody>
                  {sources.filter((s) => (s.family || "Other") === fam).map((s) => {
                    const brand = brandForSource(s.type);
                    return (
                      <tr key={s.type}>
                        <td>
                          <div className="row" style={{ gap: 10, alignItems: "center" }}>
                            <div className="result-icon" style={{ width: 30, height: 30, background: brand ? "var(--inset)" : s.color }}>
                              {brand ? <BrandIcon name={brand} size={16} /> : <Icon name={s.icon as IconName} size={15} />}
                            </div>
                            <div>
                              <div style={{ fontWeight: 600 }}>{s.label}</div>
                              <div className="faint" style={{ fontSize: 11 }}>{s.type} · {s.category}</div>
                            </div>
                          </div>
                        </td>
                        <td><input type="checkbox" checked={s.enabled} onChange={(e) => setSource(s, { enabled: e.target.checked })} /></td>
                        <td>
                          <select className="input sm" value={s.config_object_id || ""} onChange={(e) => setSource(s, { config_object_id: e.target.value })}>
                            <option value="">— none —</option>
                            {objects.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
                          </select>
                        </td>
                        <td><Pill tone={s.configured ? "ok" : "warn"}>{s.configured ? "Configured" : "Not set"}</Pill></td>
                        <td>
                          {s.backfill_supported
                            ? <label className="row" style={{ gap: 6, alignItems: "center", fontSize: 11.5 }}>
                                <input type="checkbox" checked={!!s.backfill_enabled}
                                       onChange={(e) => setSource(s, { backfill_enabled: e.target.checked })} />
                                <span className="faint">Deep history</span>
                              </label>
                            : <span className="faint" style={{ fontSize: 11 }}>—</span>}
                        </td>
                        <td><button className="btn ghost sm" onClick={() => editFamily(s)}>Change family</button></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      </Card>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

const STORAGE_CLASS_OPTS = ["INTELLIGENT_TIERING", "STANDARD_IA", "ONEZONE_IA", "STANDARD", "GLACIER_IR"];
const ACCESS_TIER_OPTS = ["Hot", "Cool", "Cold"];

const CAPABILITY_LABELS: Record<string, string> = { cloud: "Arkive Cloud", backup: "Backup storage" };

function prettyKey(k: string) {
  return k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

interface ServiceDraft { id?: string; name: string; kind: string; enabled: boolean; config_object_id: string; settings: Record<string, string>; capabilities: string[] }

interface TestCheck { name: string; ok: boolean; detail: string }

const TEST_CHECK_LABELS: Record<string, string> = {
  configuration: "Configuration",
  reachability: "Reachability",
  writeability: "Writeability",
};

async function showStorageTestResult(o: ServiceObj, checks: TestCheck[], ok: boolean, error: string | null) {
  const lines = checks.map((c) => {
    const label = TEST_CHECK_LABELS[c.name] || c.name;
    return `${c.ok ? "✓" : "✗"} ${label} — ${c.detail}`;
  });
  if (!checks.length) lines.push(error || "Test could not run.");
  await notify({
    title: `${o.name} · ${ok ? "storage test passed" : "storage test failed"}`,
    message: lines.join("\n"),
    tone: ok ? "ok" : "danger",
  });
}

interface AdminBillingProfile {
  id: string; tenant_id: string; tenant_name: string; account_name?: string; processor: string;
  plan_id: string; plan_name: string; amount_cents: number; currency: string;
  interval: string; status: string; active: boolean;
  processor_customer: string; processor_subscription: string;
  activated_at: string | null; next_charge_at: string | null;
  current_period_end: string | null; last_charge_at: string | null; last_status: string;
  dunning_attempts?: number;
  payment_method: { brand: string; last4: string; exp_month: number; exp_year: number } | null;
  charges_total: number; charges_succeeded: number; charges_failed: number; collected_cents?: number;
}
interface AdminBillingSummary {
  currency: string; mrr_cents: number; arr_cents: number; fy_revenue_cents: number; fy_label: string;
  month_revenue_cents: number; all_time_cents: number; active_subscriptions: number;
  arpu_cents: number; fy_per_user_cents: number; charges_succeeded: number; charges_failed: number;
}
interface AdminBillingCharge {
  id: string; amount_cents: number; currency: string; status: string; attempt: number;
  kind: string; description?: string; recurring?: boolean;
  processor_charge_id: string; error: string; created_at: string | null;
}
interface InvoiceLine { label: string; detail?: string; amount_cents: number }
interface Invoice { lines: InvoiceLine[]; total_cents: number; currency: string; interval?: string }
interface AdminBillingDetail extends AdminBillingProfile {
  charges: AdminBillingCharge[];
  recurring_charges?: AdminBillingCharge[];
  onetime_charges?: AdminBillingCharge[];
  invoice?: Invoice;
}

function fmtCents(cents: number, cur = "USD"): string {
  const sym = cur === "USD" ? "$" : "";
  return `${sym}${(cents / 100).toFixed(2)}${cur === "USD" ? "" : " " + cur}`;
}
// Absolute billing date (naive-UTC API datetimes → append Z), date only.
function billDate(s: string | null | undefined): string {
  if (!s) return "—";
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: userTimezone() });
}
const CHARGE_KIND_LABEL: Record<string, string> = {
  recurring: "Subscription", "one-time": "One-time", setup: "Setup fee", manual: "Manual",
};
const BILLING_STATUS_TONE: Record<string, "ok" | "warn" | "danger" | "info"> = {
  active: "ok", paused: "warn", inactive: "info", past_due: "danger", canceled: "danger",
  succeeded: "ok", failed: "danger", pending: "warn",
};

function RevCard({ label, value, sub, accent }: { label: string; value: string; sub: string; accent: string }) {
  return (
    <div className="card" style={{ padding: 14, borderLeft: `3px solid ${accent}` }}>
      <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em" }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, marginTop: 4 }}>{value}</div>
      <div className="faint" style={{ fontSize: 11.5, marginTop: 2 }}>{sub}</div>
    </div>
  );
}

function BillingAdmin() {
  const [profiles, setProfiles] = useState<AdminBillingProfile[]>([]);
  const [summary, setSummary] = useState<AdminBillingSummary | null>(null);
  const [detail, setDetail] = useState<AdminBillingDetail | null>(null);
  const [busy, setBusy] = useState("");
  const [toast, setToast] = useState("");
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3200); }

  async function load() {
    try { setProfiles((await api.get<{ profiles: AdminBillingProfile[] }>("/admin/billing/profiles")).profiles); }
    catch { /* ignore */ }
    try { setSummary(await api.get<AdminBillingSummary>("/admin/billing/summary")); }
    catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  async function openDetail(id: string) {
    try { setDetail(await api.get<AdminBillingDetail>(`/admin/billing/profiles/${id}`)); }
    catch (e: any) { flash(e.message || "Could not load profile"); }
  }
  async function toggle(p: AdminBillingProfile, enable: boolean) {
    setBusy(p.id);
    try {
      await api.post(`/admin/billing/profiles/${p.id}/${enable ? "enable" : "disable"}`, {});
      flash(enable ? "Recurring billing enabled" : "Recurring billing paused");
      await load();
      if (detail?.id === p.id) await openDetail(p.id);
    } catch (e: any) { flash(e.message || "Action failed"); }
    finally { setBusy(""); }
  }
  async function chargeNow(p: AdminBillingProfile) {
    if (!await confirmDialog({ title: "Charge now?", message: `Capture ${fmtCents(p.amount_cents, p.currency)} from ${p.account_name || p.tenant_name}'s card via ${p.processor || "the processor"}?`, confirmLabel: "Charge" })) return;
    setBusy(p.id);
    try {
      const c = await api.post<AdminBillingCharge>(`/admin/billing/profiles/${p.id}/charge`, {});
      flash(c.status === "succeeded" ? `Charged ${fmtCents(c.amount_cents, c.currency)}` : `Charge ${c.status}: ${c.error || ""}`);
      await load();
      if (detail?.id === p.id) await openDetail(p.id);
    } catch (e: any) { flash(e.message || "Charge failed"); }
    finally { setBusy(""); }
  }
  async function chargeOnce(p: AdminBillingProfile) {
    const r = await formDialog({
      title: "One-time charge",
      message: `A non-recurring charge against ${p.account_name || p.tenant_name}'s card (e.g. an appliance purchase or setup fee). Shows in Billing history, never scheduled.`,
      confirmLabel: "Charge",
      fields: [
        { name: "description", label: "Description", placeholder: "e.g. Arkive appliance — CV Edge 5", required: true },
        { name: "amount", label: "Amount (USD)", placeholder: "149.00", required: true },
        { name: "kind", label: "Type", defaultValue: "one-time",
          options: [
            { label: "One-time purchase", value: "one-time" },
            { label: "Setup fee", value: "setup" },
            { label: "Manual adjustment", value: "manual" },
          ] },
      ],
    });
    if (!r) return;
    const amount_cents = Math.round(parseFloat(r.amount || "0") * 100);
    if (!amount_cents || amount_cents <= 0) { flash("Enter a valid amount"); return; }
    setBusy(p.id);
    try {
      const c = await api.post<AdminBillingCharge>(`/admin/billing/profiles/${p.id}/charge-once`,
        { amount_cents, description: r.description, kind: r.kind });
      flash(c.status === "succeeded" ? `Charged ${fmtCents(c.amount_cents, c.currency)}` : `Charge ${c.status}: ${c.error || ""}`);
      await load();
      if (detail?.id === p.id) await openDetail(p.id);
    } catch (e: any) { flash(e.message || "Charge failed"); }
    finally { setBusy(""); }
  }

  return (
    <>
      {summary && (
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 12, marginBottom: 16 }}>
          <RevCard label="ARR" value={fmtCents(summary.arr_cents, summary.currency)} sub="Annual recurring revenue" accent="#4f7cff" />
          <RevCard label="MRR" value={fmtCents(summary.mrr_cents, summary.currency)} sub={`${summary.active_subscriptions} active sub(s)`} accent="#35d0a5" />
          <RevCard label={`${summary.fy_label} revenue`} value={fmtCents(summary.fy_revenue_cents, summary.currency)} sub="Collected this fiscal year" accent="#f5a623" />
          <RevCard label="This month" value={fmtCents(summary.month_revenue_cents, summary.currency)} sub="Collected month-to-date" accent="#c56cf0" />
          <RevCard label="Revenue / user" value={fmtCents(summary.fy_per_user_cents, summary.currency)} sub={`FY · ARPU ${fmtCents(summary.arpu_cents, summary.currency)}/mo`} accent="#2dbe60" />
          <RevCard label="All-time" value={fmtCents(summary.all_time_cents, summary.currency)} sub={`${summary.charges_succeeded}✓ / ${summary.charges_failed}✗ charges`} accent="#ea4335" />
        </div>
      )}
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 10 }}>
          <div>
            <h3 style={{ margin: 0 }}>Billing profiles</h3>
            <div className="muted" style={{ fontSize: 12.5 }}>Every tenant's recurring subscription. Enable to start charging their card at the plan price on the monthly anniversary; disable to pause.</div>
          </div>
          <button className="btn ghost sm" onClick={load}><Icon name="repeat" size={13} /> Refresh</button>
        </div>
        <table className="table">
          <thead><tr><th>Account Name</th><th>Plan</th><th>Amount</th><th>Next charge</th><th>Method</th><th>Status</th><th>Collected</th><th>Last</th><th></th></tr></thead>
          <tbody>
            {profiles.map((p) => (
              <tr key={p.id}>
                <td style={{ fontWeight: 600 }}>{p.account_name || p.tenant_name}</td>
                <td>{p.plan_name || p.plan_id || "—"}</td>
                <td>{fmtCents(p.amount_cents, p.currency)}<span className="faint" style={{ fontSize: 11 }}>/{p.interval}</span></td>
                <td className="faint" style={{ fontSize: 12 }}>{p.active && p.next_charge_at ? billDate(p.next_charge_at) : "—"}</td>
                <td className="faint" style={{ fontSize: 12 }}>{p.payment_method ? `${p.payment_method.brand} ••${p.payment_method.last4}` : <span className="warn">no card</span>}</td>
                <td><Pill tone={BILLING_STATUS_TONE[p.status] || "info"} dot>{p.active ? p.status : (p.status === "inactive" ? "inactive" : "paused")}</Pill>{p.processor === "test" && <> <Pill tone="warn">TEST</Pill></>}{p.status === "past_due" && p.dunning_attempts ? <span className="faint" style={{ fontSize: 11 }}> · retry {p.dunning_attempts}/4</span> : null}</td>
                <td className="faint" style={{ fontSize: 12 }}>{fmtCents(p.collected_cents || 0, p.currency)}</td>
                <td className="faint" style={{ fontSize: 11.5 }}>{p.last_charge_at ? `${p.last_status} · ${timeAgo(p.last_charge_at)}` : "—"}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  {p.active
                    ? <button className="btn ghost sm" disabled={busy === p.id} onClick={() => toggle(p, false)}>Disable</button>
                    : <button className="btn primary sm" disabled={busy === p.id} onClick={() => toggle(p, true)}>Enable</button>}
                  {" "}<button className="btn ghost sm" disabled={busy === p.id || !p.payment_method} onClick={() => chargeNow(p)}>Charge</button>
                  {" "}<button className="btn ghost sm" onClick={() => openDetail(p.id)}>View</button>
                </td>
              </tr>
            ))}
            {profiles.length === 0 && <tr><td colSpan={9} className="muted">No billing profiles yet. They're created when a customer saves a card.</td></tr>}
          </tbody>
        </table>
      </Card>

      {detail && (
        <div className="modal-backdrop" onClick={() => setDetail(null)}>
          <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div>
                <h3 style={{ margin: 0 }}>{detail.account_name || detail.tenant_name} · billing</h3>
                <div className="faint" style={{ fontSize: 12 }}>
                  {fmtCents(detail.amount_cents, detail.currency)}/{detail.interval} · {detail.processor || "unassigned"}
                  {detail.processor_subscription ? ` · ${detail.processor_subscription}` : ""}
                </div>
              </div>
              <button className="btn ghost sm" onClick={() => setDetail(null)}><Icon name="logout" size={14} /></button>
            </div>
            <div className="modal-body">
              <div className="row" style={{ gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
                <Pill tone={BILLING_STATUS_TONE[detail.status] || "info"} dot>{detail.status}</Pill>
                {detail.payment_method && <Pill tone="info">{detail.payment_method.brand} ••{detail.payment_method.last4} · {String(detail.payment_method.exp_month).padStart(2, "0")}/{detail.payment_method.exp_year}</Pill>}
                {detail.active && detail.next_charge_at && <span className="faint" style={{ fontSize: 12 }}>Next charge {billDate(detail.next_charge_at)}</span>}
                {detail.current_period_end && <span className="faint" style={{ fontSize: 12 }}>Renews {billDate(detail.current_period_end)}</span>}
              </div>

              {detail.invoice && detail.invoice.lines.length > 0 && (
                <div style={{ marginBottom: 16 }}>
                  <div className="faint" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: ".06em", margin: "0 0 6px" }}>
                    Itemized invoice · recurring {detail.invoice.interval || "month"}
                  </div>
                  <div style={{ border: "1px solid var(--border-soft)", borderRadius: 10, overflow: "hidden" }}>
                    {detail.invoice.lines.map((l, i) => (
                      <div key={i} className="spread" style={{ padding: "9px 12px", borderBottom: "1px solid var(--border-soft)" }}>
                        <div>
                          <div style={{ fontSize: 12.5, fontWeight: 600 }}>{l.label}</div>
                          {l.detail && <div className="faint" style={{ fontSize: 11 }}>{l.detail}</div>}
                        </div>
                        <div style={{ fontWeight: 600 }}>{fmtCents(l.amount_cents, detail.invoice!.currency)}</div>
                      </div>
                    ))}
                    <div className="spread" style={{ padding: "10px 12px", background: "var(--inset)" }}>
                      <div style={{ fontWeight: 700 }}>Total per {detail.invoice.interval || "month"}</div>
                      <div style={{ fontWeight: 700 }}>{fmtCents(detail.invoice.total_cents, detail.invoice.currency)}</div>
                    </div>
                  </div>
                </div>
              )}

              <div className="faint" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: ".06em", margin: "0 0 6px" }}>Recurring payments</div>
              <table className="table">
                <thead><tr><th>When</th><th>Amount</th><th>Attempt</th><th>Status</th><th>Detail</th></tr></thead>
                <tbody>
                  {(detail.recurring_charges ?? detail.charges.filter((c) => c.recurring ?? c.kind === "recurring")).map((c) => (
                    <tr key={c.id}>
                      <td className="faint" style={{ fontSize: 12 }}>{c.created_at ? billDate(c.created_at) : "—"}</td>
                      <td>{fmtCents(c.amount_cents, c.currency)}</td>
                      <td className="faint" style={{ fontSize: 12 }}>#{c.attempt}</td>
                      <td><Pill tone={BILLING_STATUS_TONE[c.status] || "info"} dot>{c.status}</Pill></td>
                      <td className="faint" style={{ fontSize: 11.5 }}>{c.error || c.processor_charge_id || "—"}</td>
                    </tr>
                  ))}
                  {(detail.recurring_charges ?? detail.charges.filter((c) => c.recurring ?? c.kind === "recurring")).length === 0 &&
                    <tr><td colSpan={5} className="muted">No subscription charges yet.</td></tr>}
                </tbody>
              </table>

              <div className="spread" style={{ margin: "18px 0 6px" }}>
                <div className="faint" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: ".06em" }}>Billing history · one-time charges</div>
                <button className="btn ghost sm" disabled={!detail.payment_method} onClick={() => chargeOnce(detail)}>
                  <Icon name="credit-card" size={13} /> Add one-time charge
                </button>
              </div>
              <table className="table">
                <thead><tr><th>When</th><th>Description</th><th>Type</th><th>Amount</th><th>Status</th></tr></thead>
                <tbody>
                  {(detail.onetime_charges ?? detail.charges.filter((c) => !(c.recurring ?? c.kind === "recurring"))).map((c) => (
                    <tr key={c.id}>
                      <td className="faint" style={{ fontSize: 12 }}>{c.created_at ? billDate(c.created_at) : "—"}</td>
                      <td style={{ fontSize: 12.5 }}>{c.description || <span className="faint">—</span>}</td>
                      <td><Pill tone="info">{CHARGE_KIND_LABEL[c.kind] || c.kind}</Pill></td>
                      <td>{fmtCents(c.amount_cents, c.currency)}</td>
                      <td><Pill tone={BILLING_STATUS_TONE[c.status] || "info"} dot>{c.status}</Pill></td>
                    </tr>
                  ))}
                  {(detail.onetime_charges ?? detail.charges.filter((c) => !(c.recurring ?? c.kind === "recurring"))).length === 0 &&
                    <tr><td colSpan={5} className="muted">No one-time charges. Use “Add one-time charge” for an appliance purchase or setup fee.</td></tr>}
                </tbody>
              </table>
            </div>
            <div className="modal-foot">
              {detail.active
                ? <button className="btn ghost sm" onClick={() => toggle(detail, false)}>Pause charges</button>
                : <button className="btn primary sm" onClick={() => toggle(detail, true)}>Enable charges</button>}
              <button className="btn sm" disabled={!detail.payment_method} onClick={() => chargeNow(detail)}>Charge now</button>
              <button className="btn sm ghost" disabled={!detail.payment_method} onClick={() => chargeOnce(detail)}>One-time charge</button>
              <button className="btn ghost sm" onClick={() => setDetail(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function ServiceObjectsAdmin() {
  const [items, setItems] = useState<ServiceObj[]>([]);
  const [kinds, setKinds] = useState<ServiceKind[]>([]);
  const [objects, setObjects] = useState<ConfigObj[]>([]);
  const [toast, setToast] = useState("");
  const [draft, setDraft] = useState<ServiceDraft | null>(null);
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3200); }

  async function load() {
    try { setItems(await api.get<ServiceObj[]>("/admin/service-objects")); } catch { /* ignore */ }
    try { setKinds(await api.get<ServiceKind[]>("/admin/service-object-kinds")); } catch { /* ignore */ }
    try { setObjects(await api.get<ConfigObj[]>("/admin/config-objects")); } catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  function specFor(kind: string) { return kinds.find((k) => k.kind === kind); }
  function newDraft(kind: string) {
    const spec = specFor(kind);
    setDraft({ name: "", kind, enabled: true, config_object_id: "", settings: { ...(spec?.setting_defaults || {}) }, capabilities: [...(spec?.capabilities || [])] });
  }
  function editDraft(o: ServiceObj) {
    const spec = specFor(o.kind);
    setDraft({ id: o.id, name: o.name, kind: o.kind, enabled: o.enabled, config_object_id: o.config_object_id || "", settings: { ...(o.settings || {}) }, capabilities: [...(o.capabilities || spec?.capabilities || [])] });
  }
  async function saveDraft() {
    if (!draft) return;
    const spec = specFor(draft.kind);
    const payload: Record<string, unknown> = { name: draft.name || "Service", enabled: draft.enabled, config_object_id: draft.config_object_id || null, settings: draft.settings };
    if ((spec?.capabilities || []).length) payload.capabilities = draft.capabilities;
    try {
      if (draft.id) await api.put(`/admin/service-objects/${draft.id}`, payload);
      else await api.post("/admin/service-objects", { ...payload, kind: draft.kind });
      setDraft(null); flash("Service saved"); await load();
    } catch { flash("Could not save"); }
  }
  async function delObject(o: ServiceObj) {
    if (!await confirmDialog({ title: "Delete service object?", message: `Delete "${o.name}". Any node using it falls back to defaults.`, tone: "danger", confirmLabel: "Delete" })) return;
    try { await api.del(`/admin/service-objects/${o.id}`); flash("Deleted"); await load(); } catch { flash("Delete failed"); }
  }
  async function testObject(o: ServiceObj) {
    let payload: Record<string, string> = {};
    if (o.category === "email") {
      const to = await promptDialog({ title: "Send test email", label: "Recipient address", placeholder: "you@example.com" });
      if (!to || !to.trim()) return;
      payload = { to: to.trim() };
    }
    flash("Testing…");
    try {
      const r = await api.post<{ ok: boolean; error?: string | null; checks?: TestCheck[] }>(`/admin/service-objects/${o.id}/test`, payload);
      if (o.category === "storage") {
        await showStorageTestResult(o, r.checks || [], r.ok, r.error || null);
        flash(r.ok ? "Storage test passed" : "Storage test failed");
      } else {
        flash(r.ok ? "Test email sent" : `Test failed: ${r.error || "unknown error"}`);
      }
    } catch { flash("Test failed"); }
  }

  const storage = items.filter((i) => i.category === "storage");
  const email = items.filter((i) => i.category === "email");
  const payment = items.filter((i) => i.category === "payment");
  const draftSpec = draft ? specFor(draft.kind) : undefined;
  const settingOptions = (key: string): string[] | null =>
    key === "storage_class" ? STORAGE_CLASS_OPTS : key === "access_tier" ? ACCESS_TIER_OPTS : null;

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 10 }}>
          <div>
            <h3 style={{ margin: 0 }}>Service objects</h3>
            <div className="muted" style={{ fontSize: 12.5 }}>Storage, email &amp; payment backends. Credentials come from a linked configuration object; assign a service to a node under Nodes / Configuration.</div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            {kinds.map((k) => <button key={k.kind} className="btn sm" onClick={() => newDraft(k.kind)}>+ {k.label}</button>)}
          </div>
        </div>

        {draft && (
          <div style={{ border: "1px solid var(--border-soft)", borderRadius: 10, padding: 14, marginBottom: 12, background: "var(--inset)" }}>
            <div className="row" style={{ gap: 8, marginBottom: 10 }}>
              <Pill tone="info">{draftSpec?.label || draft.kind}</Pill>
              <label className="row" style={{ gap: 6, fontSize: 12.5, marginLeft: "auto" }}>
                <input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled
              </label>
            </div>
            <div className="grid grid-2" style={{ gap: 12, marginBottom: 10 }}>
              <Field label="Name"><input className="input sm" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder="e.g. Arkive US-East S3" /></Field>
              <Field label={`Configuration object (${(draftSpec?.credential_keys || []).join(", ")})`}>
                <select className="input sm" value={draft.config_object_id} onChange={(e) => setDraft({ ...draft, config_object_id: e.target.value })}>
                  <option value="">— none —</option>
                  {objects.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
                </select>
              </Field>
            </div>
            <div className="grid grid-2" style={{ gap: 12 }}>
              {(draftSpec?.settings || []).map((key) => {
                const opts = settingOptions(key);
                return (
                  <Field key={key} label={prettyKey(key)}>
                    {opts ? (
                      <select className="input sm" value={draft.settings[key] || ""} onChange={(e) => setDraft({ ...draft, settings: { ...draft.settings, [key]: e.target.value } })}>
                        {opts.map((o) => <option key={o} value={o}>{o}</option>)}
                      </select>
                    ) : (
                      <input className="input sm" value={draft.settings[key] || ""}
                             onChange={(e) => setDraft({ ...draft, settings: { ...draft.settings, [key]: e.target.value } })}
                             placeholder={draftSpec?.required?.includes(key) ? "required" : "optional"} />
                    )}
                  </Field>
                );
              })}
            </div>
            {(draftSpec?.capabilities || []).length > 0 && (
              <div style={{ marginTop: 12 }}>
                <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", marginBottom: 6 }}>Used for</div>
                <div className="row" style={{ gap: 14 }}>
                  {(draftSpec?.capabilities || []).map((cap) => (
                    <label key={cap} className="row" style={{ gap: 6, fontSize: 12.5, alignItems: "center", cursor: "pointer" }}>
                      <input type="checkbox" checked={draft.capabilities.includes(cap)}
                             onChange={(e) => setDraft({
                               ...draft,
                               capabilities: e.target.checked
                                 ? [...draft.capabilities, cap]
                                 : draft.capabilities.filter((c) => c !== cap),
                             })} />
                      {CAPABILITY_LABELS[cap] || cap}
                    </label>
                  ))}
                </div>
                <div className="faint" style={{ fontSize: 11, marginTop: 6 }}>
                  Controls where this backend can be selected — Arkive Cloud object storage and/or infrastructure backups.
                </div>
              </div>
            )}
            <div className="row" style={{ gap: 8, marginTop: 12 }}>
              <button className="btn primary sm" onClick={saveDraft}>Save</button>
              <button className="btn ghost sm" onClick={() => setDraft(null)}>Cancel</button>
            </div>
          </div>
        )}

        <ServiceTable title="Storage services" rows={storage} onEdit={editDraft} onDelete={delObject} onTest={testObject} testable />
        <div style={{ height: 14 }} />
        <ServiceTable title="Email services" rows={email} onEdit={editDraft} onDelete={delObject} onTest={testObject} testable />
        <div style={{ height: 14 }} />
        <ServiceTable title="Payment services" rows={payment} onEdit={editDraft} onDelete={delObject} onTest={testObject} />
        <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>
          Storage services back Arkive Cloud: mappings routed to <b>cv-cloud</b> store and restore through
          the storage service selected on the running node. S3 defaults to Intelligent-Tiering and Azure to the
          Cool tier for low cost while keeping restore instant. Payment services (Stripe / PayPal) process customer
          billing; assign one to a node under <b>Configuration → Services</b> (<code>service.payment</code>).
        </div>
      </Card>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function ServiceTable({ title, rows, onEdit, onDelete, onTest, testable }: {
  title: string; rows: ServiceObj[]; onEdit: (o: ServiceObj) => void; onDelete: (o: ServiceObj) => void; onTest: (o: ServiceObj) => void; testable?: boolean;
}) {
  return (
    <div>
      <div className="faint" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: ".06em", margin: "0 0 6px" }}>{title}</div>
      <table className="table">
        <thead><tr><th>Name</th><th>Backend</th><th>Routing</th><th>Enabled</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {rows.map((o) => (
            <tr key={o.id}>
              <td style={{ fontWeight: 600 }}>{o.name}</td>
              <td>
                <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                  <Pill tone="info">{o.kind_label}</Pill>
                  {o.category === "storage" && (o.capabilities || []).map((c) => (
                    <Pill key={c} tone="ok">{CAPABILITY_LABELS[c] || c}</Pill>
                  ))}
                </div>
              </td>
              <td className="faint" style={{ fontSize: 11.5 }}>{o.setting_keys.map((k) => o.settings[k] ? `${k}=${o.settings[k]}` : null).filter(Boolean).join(" · ") || "—"}</td>
              <td>{o.enabled ? <Pill tone="ok">on</Pill> : <Pill tone="warn">off</Pill>}</td>
              <td><Pill tone={o.configured ? "ok" : "warn"}>{o.configured ? "Configured" : "Incomplete"}</Pill></td>
              <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                {testable && <><button className="btn ghost sm" onClick={() => onTest(o)}>Test</button>{" "}</>}
                <button className="btn ghost sm" onClick={() => onEdit(o)}>Edit</button>{" "}
                <button className="btn danger sm" onClick={() => onDelete(o)}>Delete</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={6} className="muted">None configured.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

interface CloudIndexReplica { status: string; object_count: number; last_replicated_at: string | null; error: string }
interface StorageUsage {
  cloud_total: { bytes: number; objects: number; recovery_points: number; tenants: number };
  by_tenant: { tenant_id: string; customer_id?: string; scope?: string; tenant_name: string; plan: string; licensed_bytes: number; bytes: number; objects: number; recovery_points: number; index_replica?: CloudIndexReplica | null }[];
  services: { id: string; name: string; kind: string; kind_label: string; enabled: boolean; nodes: string[]; active: boolean; settings: Record<string, string> }[];
}

function StorageUsageAdmin() {
  const [d, setD] = useState<StorageUsage | null>(null);
  useEffect(() => { api.get<StorageUsage>("/admin/storage-usage").then(setD).catch(() => {}); }, []);
  if (!d) return <Card><div className="muted">Loading storage usage…</div></Card>;
  const t = d.cloud_total;
  return (
    <>
      <div className="grid grid-4">
        <Stat label="Cloud data stored" value={bytes(t.bytes)} />
        <Stat label="Recovery points" value={t.recovery_points.toLocaleString()} />
        <Stat label="Objects" value={t.objects.toLocaleString()} />
        <Stat label="Tenants using cloud" value={t.tenants} />
      </div>

      <Card style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>Storage services</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>Arkive Cloud backends and the nodes that write to them. New backups land on each node's active service.</div>
        <div className="grid grid-2">
          {d.services.map((s) => (
            <Card key={s.id}>
              <div className="spread" style={{ marginBottom: 8 }}>
                <div className="row" style={{ gap: 10 }}>
                  <div className="result-icon" style={{ width: 32, height: 32, background: "var(--inset)" }}><Icon name="database" size={16} /></div>
                  <div>
                    <div style={{ fontWeight: 700 }}>{s.name}</div>
                    <div className="faint" style={{ fontSize: 11.5 }}>{s.kind_label}</div>
                  </div>
                </div>
                <Pill tone={s.active ? "ok" : "warn"}>{s.active ? "Active" : "Idle"}</Pill>
              </div>
              <div className="faint" style={{ fontSize: 12 }}>
                {s.settings.bucket ? `bucket ${s.settings.bucket}` : s.settings.container ? `container ${s.settings.container}` : "—"}
                {s.settings.region ? ` · ${s.settings.region}` : ""}
              </div>
              <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: "wrap" }}>
                {s.nodes.length
                  ? s.nodes.map((n) => <Pill key={n} tone="info"><Icon name="server" size={11} /> {n}</Pill>)
                  : <span className="faint" style={{ fontSize: 12 }}>No node assigned</span>}
              </div>
            </Card>
          ))}
          {d.services.length === 0 && <div className="muted">No storage services configured.</div>}
        </div>
      </Card>

      <Card style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>Data stored by customer</h3>
        <table className="table">
          <thead><tr><th>Customer</th><th>Plan</th><th>Recovery points</th><th>Objects</th><th>Data stored</th><th>Of licensed</th><th>Index replica</th></tr></thead>
          <tbody>
            {d.by_tenant.map((r) => {
              const pct = r.licensed_bytes ? Math.round((r.bytes / r.licensed_bytes) * 100) : null;
              const rep = r.index_replica;
              const m = rep ? (INDEX_REPLICA_STATUS[rep.status] || INDEX_REPLICA_STATUS.pending) : null;
              return (
                <tr key={`${r.scope || "tenant"}:${r.customer_id || r.tenant_id}`}>
                  <td style={{ fontWeight: 600 }}>{r.tenant_name}</td>
                  <td><Pill tone="info">{r.plan}</Pill></td>
                  <td>{r.recovery_points.toLocaleString()}</td>
                  <td>{r.objects.toLocaleString()}</td>
                  <td style={{ fontWeight: 600 }}>{bytes(r.bytes)}</td>
                  <td className="faint">{r.licensed_bytes ? `${bytes(r.licensed_bytes)} · ${pct}%` : "—"}</td>
                  <td>
                    {m ? (
                      <>
                        <Pill tone={m.tone} dot>{m.label}</Pill>
                        {rep!.error ? <div className="faint" style={{ fontSize: 10.5, color: "var(--danger)" }}>{rep!.error}</div>
                          : rep!.status === "ok" ? <div className="faint" style={{ fontSize: 10.5 }}>{rep!.last_replicated_at ? fmtAbsolute(rep!.last_replicated_at) : ""}</div> : null}
                      </>
                    ) : <span className="faint">—</span>}
                  </td>
                </tr>
              );
            })}
            {d.by_tenant.length === 0 && <tr><td colSpan={7} className="muted">No cloud data stored yet.</td></tr>}
          </tbody>
        </table>
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          Totals sum bytes written across all cloud recovery points (before de-duplication). The index
          replica is the encrypted disaster-recovery copy of that customer's search index in Arkive Cloud.
        </div>
      </Card>
    </>
  );
}

// --------------------------------------------------------------------------- //
// Infrastructure Backups (node/CP core-state backups to storage services)     //
// --------------------------------------------------------------------------- //
interface BackupDest { service_id: string; name: string; kind: string; status: string; bytes: number; error?: string | null; }
interface BackupRunView { id: string; status: string; total_bytes: number; components: string[]; destinations: BackupDest[]; message?: string; error?: string; has_log?: boolean; created_at?: string | null; finished_at?: string | null; }
interface BackupNode { id: string; name: string; role: string; category: string; is_self: boolean; backup_service_ids: string[]; backup_services: string[]; last_backup: BackupRunView | null; }
interface BackupService { id: string; name: string; kind: string; kind_label: string; enabled: boolean; settings: Record<string, string>; nodes: string[]; bytes: number; backup_count?: number; backed_up_nodes: number; }
interface StoredBackup {
  id: string; node_name: string; role: string; status: string; total_bytes: number;
  components: string[]; has_log?: boolean; created_at?: string | null; finished_at?: string | null;
  destinations: { name: string; kind: string; bytes: number; key?: string }[];
}
interface BackupsData {
  summary: { nodes_total: number; nodes_protected: number; total_stored_bytes: number; success_24h: number; failed_24h: number; interval_minutes: number; last_run_at: string | null };
  nodes: BackupNode[];
  services: BackupService[];
  stored_backups: StoredBackup[];
  recent: { id: string; node_name: string; role: string; status: string; total_bytes: number; message?: string; error?: string; has_log?: boolean; destinations: BackupDest[]; components: string[]; created_at?: string | null; finished_at?: string | null }[];
}

function backupTone(status: string): "ok" | "warn" | "danger" | "info" {
  if (status === "success") return "ok";
  if (status === "partial") return "warn";
  if (status === "failed") return "danger";
  return "info";  // running | skipped
}

function BackupsAdmin() {
  const [d, setD] = useState<BackupsData | null>(null);
  const [storeServices, setStoreServices] = useState<{ id: string; name: string; kind: string; enabled: boolean }[]>([]);
  const [editNode, setEditNode] = useState<string | null>(null);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState("");
  const [logRun, setLogRun] = useState<any | null>(null);
  const [showStored, setShowStored] = useState(false);

  async function load() {
    try { setD(await api.get<BackupsData>("/admin/backups")); } catch { /* ignore */ }
  }
  async function openBackupLog(id: string) {
    try { setLogRun(await api.get<any>(`/admin/backups/${id}/log`)); } catch { /* ignore */ }
  }
  useEffect(() => {
    void load();
    api.get<{ id: string; name: string; kind: string; enabled: boolean; category: string; capabilities?: string[] }[]>("/admin/service-objects")
      .then((rows) => setStoreServices(rows.filter((r) => r.kind.startsWith("storage-") && (r.capabilities || []).includes("backup"))))
      .catch(() => {});
    const iv = setInterval(load, 8000);
    return () => clearInterval(iv);
  }, []);

  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3500); }

  function startEdit(n: BackupNode) { setEditNode(n.id); setSel(new Set(n.backup_service_ids || [])); }
  function toggleSel(id: string) { setSel((c) => { const s = new Set(c); s.has(id) ? s.delete(id) : s.add(id); return s; }); }
  async function saveEdit(nodeId: string) {
    try {
      await api.put(`/admin/nodes/${nodeId}`, { backup_service_ids: [...sel] });
      setEditNode(null); flash("Backup destinations updated"); await load();
    } catch (e) { await notify({ title: "Couldn't save", message: (e as Error).message, tone: "danger" }); }
  }
  async function runNow() {
    setBusy(true);
    try { await api.post("/admin/backups/run", {}); flash("Control-plane backup started — refreshing…"); setTimeout(load, 3000); }
    catch (e) { await notify({ title: "Couldn't start backup", message: (e as Error).message, tone: "danger" }); }
    finally { setBusy(false); }
  }

  if (!d) return <Card><div className="muted">Loading backups…</div></Card>;
  const sum = d.summary;
  return (
    <>
      <div className="spread" style={{ marginBottom: 16, alignItems: "center", flexWrap: "wrap", gap: 10 }}>
        <div>
          <h2 style={{ margin: 0 }}>Infrastructure backups</h2>
          <div className="muted" style={{ fontSize: 12.5, maxWidth: 620 }}>
            Each node and the control plane back up their core state — database (config, tenants,
            recovery points, search index), key material and config — to their assigned backup storage
            services. Backups are encrypted and replicated to every assigned destination for resiliency.
          </div>
        </div>
        <button className="btn primary sm" disabled={busy} onClick={runNow}>
          <Icon name="shield" size={14} /> {busy ? "Starting…" : "Back up control plane now"}
        </button>
      </div>

      <div className="grid grid-4">
        <Stat label="Nodes protected" value={`${sum.nodes_protected} / ${sum.nodes_total}`} />
        <Stat label="Backup storage used" value={bytes(sum.total_stored_bytes)} />
        <Stat label="Succeeded (24h)" value={sum.success_24h} />
        <Stat label="Failed (24h)" value={sum.failed_24h} />
      </div>

      <Card style={{ marginTop: 16 }}>
        <div className="spread" style={{ marginBottom: 4 }}>
          <h3 style={{ margin: 0 }}>Backup storage services</h3>
          <button className="btn ghost sm" onClick={() => setShowStored(true)}>
            <Icon name="database" size={13} /> View stored backups ({d.stored_backups.length})
          </button>
        </div>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
          Storage backends holding infrastructure backups, and the nodes that write to each. Assign
          destinations per node below — use different services so no single failure loses every copy.
        </div>
        <div className="grid grid-2">
          {d.services.map((s) => (
            <Card key={s.id}>
              <div className="spread" style={{ marginBottom: 8 }}>
                <div className="row" style={{ gap: 10 }}>
                  <div className="result-icon" style={{ width: 32, height: 32, background: "var(--inset)" }}><Icon name="database" size={16} /></div>
                  <div>
                    <div style={{ fontWeight: 700 }}>{s.name}</div>
                    <div className="faint" style={{ fontSize: 11.5 }}>{s.kind_label}</div>
                  </div>
                </div>
                <Pill tone={s.bytes ? "ok" : "warn"}>{s.bytes ? `${bytes(s.bytes)} · ${s.backup_count ?? 0} backup${(s.backup_count ?? 0) === 1 ? "" : "s"}` : "Unused"}</Pill>
              </div>
              <div className="faint" style={{ fontSize: 12 }}>
                {s.settings.bucket ? `bucket ${s.settings.bucket}` : s.settings.container ? `container ${s.settings.container}` : "—"}
                {s.settings.region ? ` · ${s.settings.region}` : ""}
              </div>
              <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: "wrap" }}>
                {s.nodes.length
                  ? s.nodes.map((n) => <Pill key={n} tone="info"><Icon name="server" size={11} /> {n}</Pill>)
                  : <span className="faint" style={{ fontSize: 12 }}>No node backs up here yet</span>}
              </div>
            </Card>
          ))}
          {d.services.length === 0 && <div className="muted">No storage services configured. Create one under Service objects first.</div>}
        </div>
      </Card>

      <Card style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>Nodes</h3>
        <table className="table">
          <thead><tr><th>Node</th><th>Backup destinations</th><th>Last backup</th><th>Size</th><th></th></tr></thead>
          <tbody>
            {d.nodes.map((n) => {
              const editing = editNode === n.id;
              const lb = n.last_backup;
              return (
                <tr key={n.id}>
                  <td>
                    <div style={{ fontWeight: 600 }}>{n.name}{n.is_self && <span className="faint" style={{ fontWeight: 400 }}> · this node</span>}</div>
                    <div className="faint" style={{ fontSize: 11 }}>{n.role}</div>
                  </td>
                  <td>
                    {editing ? (
                      <div className="stack" style={{ gap: 6 }}>
                        {storeServices.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No storage services yet.</span>}
                        {storeServices.map((s) => (
                          <label key={s.id} className="row" style={{ gap: 6, alignItems: "center", cursor: "pointer" }}>
                            <input type="checkbox" checked={sel.has(s.id)} onChange={() => toggleSel(s.id)} />
                            <span style={{ fontSize: 12.5 }}>{s.name}</span>
                            <span className="faint" style={{ fontSize: 10.5 }}>{s.kind.replace("storage-", "")}</span>
                          </label>
                        ))}
                        <div className="row" style={{ gap: 6, marginTop: 4 }}>
                          <button className="btn primary sm" onClick={() => saveEdit(n.id)}>Save</button>
                          <button className="btn ghost sm" onClick={() => setEditNode(null)}>Cancel</button>
                        </div>
                      </div>
                    ) : (
                      <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                        {n.backup_services.length
                          ? n.backup_services.map((nm) => <Pill key={nm} tone="info">{nm}</Pill>)
                          : <span className="faint" style={{ fontSize: 12 }}>None assigned</span>}
                      </div>
                    )}
                  </td>
                  <td>
                    {lb ? (
                      <div className="stack" style={{ gap: 2 }}>
                        <Pill tone={backupTone(lb.status)}>{lb.status}</Pill>
                        <span className="faint" style={{ fontSize: 10.5 }} title={lb.created_at ? fmtAbsolute(lb.created_at) : ""}>
                          {lb.created_at ? timeAgo(lb.created_at) : ""}
                          {lb.error ? ` · ${lb.error.slice(0, 60)}` : ""}
                        </span>
                      </div>
                    ) : <span className="faint" style={{ fontSize: 12 }}>Never</span>}
                  </td>
                  <td style={{ fontWeight: 600 }}>{lb && lb.total_bytes ? bytes(lb.total_bytes) : "—"}</td>
                  <td style={{ textAlign: "right" }}>
                    {!editing && <button className="btn ghost sm" onClick={() => startEdit(n)}><Icon name="gear" size={13} /> Destinations</button>}
                  </td>
                </tr>
              );
            })}
            {d.nodes.length === 0 && <tr><td colSpan={5} className="muted">No nodes.</td></tr>}
          </tbody>
        </table>
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          Backups run on each node's <code>cv-backup</code> timer ({Math.round((sum.interval_minutes || 1440) / 60)}h cadence).
          "Back up now" runs the control plane immediately; other nodes back up on their next timer.
        </div>
      </Card>

      <Card style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>Recent backup runs</h3>
        <table className="table">
          <thead><tr><th>Node</th><th>Status</th><th>Components</th><th>Destinations</th><th>Size</th><th>When</th><th></th></tr></thead>
          <tbody>
            {d.recent.map((r) => (
              <tr key={r.id}>
                <td style={{ fontWeight: 600 }}>{r.node_name}<div className="faint" style={{ fontSize: 11 }}>{r.role}</div></td>
                <td><Pill tone={backupTone(r.status)}>{r.status}</Pill>{r.error && <div className="faint" style={{ fontSize: 10.5, color: "var(--warn)" }}>{r.error.slice(0, 80)}</div>}</td>
                <td className="faint" style={{ fontSize: 12 }}>{(r.components || []).join(", ") || "—"}</td>
                <td>
                  <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
                    {(r.destinations || []).map((x, i) => (
                      <Pill key={i} tone={x.status === "ok" ? "ok" : "danger"} >{x.name}</Pill>
                    ))}
                    {(r.destinations || []).length === 0 && <span className="faint" style={{ fontSize: 12 }}>—</span>}
                  </div>
                </td>
                <td>{r.total_bytes ? bytes(r.total_bytes) : "—"}</td>
                <td className="faint" style={{ fontSize: 11, whiteSpace: "nowrap" }} title={r.created_at ? fmtAbsolute(r.created_at) : ""}>{r.created_at ? timeAgo(r.created_at) : ""}</td>
                <td style={{ textAlign: "right" }}>
                  {r.has_log && <button className="btn ghost sm" onClick={() => void openBackupLog(r.id)}><Icon name="note" size={12} /> Log</button>}
                </td>
              </tr>
            ))}
            {d.recent.length === 0 && <tr><td colSpan={7} className="muted">No backup runs yet.</td></tr>}
          </tbody>
        </table>
      </Card>

      {logRun && (
        <div className="modal-backdrop" onClick={() => setLogRun(null)}>
          <div className="modal-panel" style={{ width: "min(860px, 100%)" }} onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div>
                <h3 style={{ margin: 0 }}>Backup log — {logRun.node_name}</h3>
                <div className="faint" style={{ fontSize: 12 }}>{logRun.status}{logRun.message ? ` · ${logRun.message}` : ""}</div>
              </div>
              <button className="btn ghost sm" onClick={() => setLogRun(null)}>Close</button>
            </div>
            <div className="modal-body">
              {logRun.error && <div className="faint" style={{ color: "var(--danger)", fontSize: 12.5, marginBottom: 8 }}>{logRun.error}</div>}
              <div className="terminal-log">
                {(logRun.log || []).length === 0
                  ? <div className="faint">No log captured for this run.</div>
                  : (logRun.log || []).map((l: any, i: number) => (
                      <div key={i} className={`tlog-line lvl-${(l.level || "INFO").toLowerCase()}`}>
                        <span className="tlog-ts">{fmtAbsolute(l.ts)}</span>
                        <span className="tlog-lvl">{(l.level || "INFO").padEnd(7)}</span>
                        <span className="tlog-msg">{l.msg}</span>
                      </div>
                    ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {showStored && (
        <div className="modal-backdrop" onClick={() => setShowStored(false)}>
          <div className="modal-panel" style={{ width: "min(920px, 100%)" }} onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div>
                <h3 style={{ margin: 0 }}>Stored backups</h3>
                <div className="faint" style={{ fontSize: 12 }}>
                  {d.stored_backups.length} backup{d.stored_backups.length === 1 ? "" : "s"} retained · {bytes(sum.total_stored_bytes)} total
                </div>
              </div>
              <button className="btn ghost sm" onClick={() => setShowStored(false)}>Close</button>
            </div>
            <div className="modal-body" style={{ maxHeight: "68vh", overflow: "auto" }}>
              <table className="table">
                <thead><tr><th>Node</th><th>Status</th><th>Components</th><th>Stored on</th><th style={{ textAlign: "right" }}>Size</th><th>When</th></tr></thead>
                <tbody>
                  {d.stored_backups.map((r) => (
                    <tr key={r.id}>
                      <td style={{ fontWeight: 600 }}>{r.node_name}<div className="faint" style={{ fontSize: 11 }}>{r.role}</div></td>
                      <td><Pill tone={backupTone(r.status)}>{r.status}</Pill></td>
                      <td className="faint" style={{ fontSize: 12 }}>{(r.components || []).join(", ") || "—"}</td>
                      <td>
                        <div className="stack" style={{ gap: 2 }}>
                          {r.destinations.map((x, i) => (
                            <span key={i} className="faint" style={{ fontSize: 11.5 }}>
                              {x.name} · {bytes(x.bytes)}
                            </span>
                          ))}
                        </div>
                      </td>
                      <td style={{ textAlign: "right" }}>{bytes(r.total_bytes)}</td>
                      <td className="faint" style={{ fontSize: 11, whiteSpace: "nowrap" }} title={r.created_at ? fmtAbsolute(r.created_at) : ""}>{r.created_at ? timeAgo(r.created_at) : ""}</td>
                    </tr>
                  ))}
                  {d.stored_backups.length === 0 && <tr><td colSpan={6} className="muted">No stored backups yet.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}
function IntegrationsAdmin() {
  const [rows, setRows] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [flash, setFlash] = useState("");

  async function load() {
    setLoading(true);
    try { setRows(await api.get<any[]>("/admin/integrations")); } catch { /* ignore */ }
    finally { setLoading(false); }
  }
  useEffect(() => { void load(); }, []);

  async function toggle(r: any) {
    try {
      await api.put(`/admin/integrations/${r.integration_type}`, { enabled: !r.enabled });
      setFlash(`${r.display_name} ${r.enabled ? "disabled" : "enabled"}`);
      setTimeout(() => setFlash(""), 1800);
      await load();
    } catch (e) { notify({ message: (e as Error).message, tone: "danger" }); }
  }

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Integrations</h3>
        <span className="faint" style={{ fontSize: 12 }}>{rows.length} integration type(s)</span>
      </div>
      <Card>
        <table className="table">
          <thead><tr><th>Integration</th><th>Runs on</th><th>Category</th><th>Customers using</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.integration_type}>
                <td>
                  <div style={{ fontWeight: 600 }}>{r.display_name}</div>
                  <div className="faint" style={{ fontSize: 11.5, maxWidth: 420 }}>{r.description}</div>
                </td>
                <td><Pill tone="info">{r.runs_on}</Pill></td>
                <td className="faint" style={{ fontSize: 12 }}>{r.category}</td>
                <td>{r.instances}</td>
                <td><Pill tone={r.enabled ? "ok" : "warn"}>{r.enabled ? "enabled" : "disabled"}</Pill></td>
                <td style={{ textAlign: "right" }}>
                  <button className="btn ghost sm" onClick={() => toggle(r)}>{r.enabled ? "Disable" : "Enable"}</button>
                </td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={6} className="muted">{loading ? "Loading…" : "No integrations registered."}</td></tr>}
          </tbody>
        </table>
      </Card>
      {flash && <div className="toast"><Icon name="check" size={15} /> {flash}</div>}
    </>
  );
}

// ---- Customer Analytics (cross-customer app/service intelligence) -----------
function TrendArrow({ pct }: { pct: number | null | undefined }) {
  if (pct == null) return <span className="faint" style={{ fontSize: 11 }}>new</span>;
  const up = pct >= 0;
  return (
    <span style={{ fontSize: 11, fontWeight: 600, color: up ? "#4f7cff" : "var(--muted-c,#8a94a7)" }}>
      {up ? "▲" : "▼"} {Math.abs(pct)}%
    </span>
  );
}

function CustomerAnalytics() {
  const [scope, setScope] = useState<"platform" | "tenant">("platform");
  const [tenantId, setTenantId] = useState("");
  const [win, setWin] = useState("30d");
  const [tenants, setTenants] = useState<any[]>([]);
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    const qs = new URLSearchParams({ scope, window: win });
    if (scope === "tenant" && tenantId) qs.set("tenant_id", tenantId);
    try { setData(await api.get<any>(`/admin/analytics?${qs.toString()}`)); } catch { /* ignore */ }
    finally { setLoading(false); }
  }
  useEffect(() => { api.get<any[]>("/admin/tenants").then(setTenants).catch(() => {}); }, []);
  useEffect(() => { void load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [scope, tenantId, win]);

  const t = data?.totals || {};
  const maxApp = Math.max(1, ...((data?.top_apps || []).map((a: any) => a.total_bytes)));
  const ds = data?.data_sources || {};
  const dsMax = Math.max(1, ...((ds.sources || []).map((s: any) => s.protected_bytes || 0)));
  const nt = data?.network_trends || {};

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Customer Analytics</h3>
        <div className="row" style={{ gap: 8 }}>
          <div className="row" style={{ gap: 4 }}>
            {["7d", "30d", "90d"].map((w) => (
              <button key={w} className={`btn sm ${win === w ? "primary" : "ghost"}`}
                      onClick={() => setWin(w)}>{w}</button>
            ))}
          </div>
          <div className="row" style={{ gap: 0, border: "1px solid var(--border-soft)", borderRadius: 8, overflow: "hidden" }}>
            <button className={`btn sm ${scope === "platform" ? "primary" : "ghost"}`} style={{ borderRadius: 0 }}
                    onClick={() => setScope("platform")}>Platform</button>
            <button className={`btn sm ${scope === "tenant" ? "primary" : "ghost"}`} style={{ borderRadius: 0 }}
                    onClick={() => setScope("tenant")}>By customer</button>
          </div>
          {scope === "tenant" && (
            <select className="input sm" value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
              <option value="">Select a customer…</option>
              {tenants.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
            </select>
          )}
        </div>
      </div>

      <div className="insights-stats" style={{ marginBottom: 16 }}>
        <AdminStat icon="activity" label="Apps & services" value={String(t.apps || 0)} tint="#c56cf0" />
        <AdminStat icon="user" label="Clients seen" value={String(t.clients || 0)} tint="#4f7cff" />
        <AdminStat icon="grid" label="Customers reporting" value={String(t.tenants || 0)} tint="#2dbe60" />
        <AdminStat icon="cloud" label="Traffic observed" value={bytes(t.bytes || 0)} tint="#f5a623" />
      </div>

      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0, fontSize: 15 }}>Network usage trends</h3>
          <span className="faint" style={{ fontSize: 12 }}>
            {(nt.summary?.total_bytes != null) ? <>Traffic {bytes(nt.summary.total_bytes || 0)} · <TrendArrow pct={nt.summary?.change_pct} /> vs previous {nt.days || 30}d · {nt.summary?.active_devices || 0} devices</> : "Traffic over time"}
          </span>
        </div>
        {(nt.series || []).every((p: any) => !p.bytes) ? (
          <div className="muted" style={{ fontSize: 12.5 }}>{loading ? "Loading…" : "No network telemetry in this window yet."}</div>
        ) : (
          <>
            <AreaChart height={190} unit="B" fmt={bytes} labels={(nt.series || []).map((p: any) => p.day.slice(5))}
                       series={[{ name: "traffic", color: "#4f7cff", data: (nt.series || []).map((p: any) => p.bytes) }]} />
            <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16, marginTop: 14 }}>
              <div>
                <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>Top apps & services (trend)</h4>
                <div className="stack" style={{ gap: 2 }}>
                  {(nt.top_apps || []).map((a: any) => (
                    <div key={a.name} className="row" style={{ gap: 10, alignItems: "center", padding: "5px 0", borderBottom: "1px solid var(--border-soft)" }}>
                      <div className="flex1" style={{ minWidth: 0 }}>
                        <div style={{ fontWeight: 600, fontSize: 12.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{a.name}</div>
                        <div className="faint" style={{ fontSize: 11 }}>{a.category || "app"} · {bytes(a.total_bytes)} · {a.tenant_count} customer{a.tenant_count === 1 ? "" : "s"}{a.has_source ? "" : " · no connector"}</div>
                      </div>
                      <div style={{ width: 96, flexShrink: 0 }}><Sparkline data={(a.series || []).map((p: any) => p.bytes)} color="#c56cf0" height={26} /></div>
                      <div style={{ width: 52, textAlign: "right", flexShrink: 0 }}><TrendArrow pct={a.change_pct} /></div>
                    </div>
                  ))}
                  {(nt.top_apps || []).length === 0 && <div className="muted" style={{ fontSize: 12 }}>No apps in this window.</div>}
                </div>
              </div>
              <div>
                <h4 style={{ margin: "0 0 6px", fontSize: 13 }}>Active devices / day</h4>
                <AreaChart height={120} unit="" labels={(nt.device_series || []).map((p: any) => p.day.slice(5))}
                           series={[{ name: "devices", color: "#2dbe60", data: (nt.device_series || []).map((p: any) => p.count) }]} />
              </div>
            </div>
          </>
        )}
      </Card>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <Card>
          <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Sources to build next</h3>
          <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
            Popular services seen in customer traffic that Arkive doesn't have a connector for yet.
          </div>
          <table className="table">
            <thead><tr><th>Service</th><th>Kind</th><th>Customers</th><th>Traffic</th></tr></thead>
            <tbody>
              {(data?.recommended_sources || []).map((r: any) => (
                <tr key={r.name}>
                  <td style={{ fontWeight: 600 }}>{r.name}</td>
                  <td className="faint" style={{ fontSize: 12 }}>{r.kind}</td>
                  <td>{r.tenant_count}</td>
                  <td>{bytes(r.total_bytes)}</td>
                </tr>
              ))}
              {(data?.recommended_sources || []).length === 0 && (
                <tr><td colSpan={4} className="muted">{loading ? "Loading…" : "No unmet sources detected yet."}</td></tr>
              )}
            </tbody>
          </table>
        </Card>

        <Card>
          <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Device mix</h3>
          <div className="stack" style={{ gap: 8 }}>
            {(data?.device_types || []).map((d: any) => (
              <div key={d.type} className="row" style={{ gap: 10, alignItems: "center" }}>
                <span style={{ width: 90, fontSize: 12.5, textTransform: "capitalize" }}>{d.type}</span>
                <div style={{ flex: 1, height: 8, background: "var(--inset)", borderRadius: 4 }}>
                  <div style={{ height: "100%", borderRadius: 4, background: "#4f7cff",
                    width: `${(d.count / Math.max(1, ...(data?.device_types || []).map((x: any) => x.count))) * 100}%` }} />
                </div>
                <span className="faint" style={{ fontSize: 12, width: 40, textAlign: "right" }}>{d.count}</span>
              </div>
            ))}
            {(data?.device_types || []).length === 0 && <div className="muted">No devices reported.</div>}
          </div>
        </Card>
      </div>

      <Card style={{ marginTop: 16 }}>
        <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Top apps & services</h3>
        <table className="table">
          <thead><tr><th>App / service</th><th>Category</th><th>Source</th><th>Customers</th><th>Traffic</th></tr></thead>
          <tbody>
            {(data?.top_apps || []).map((a: any) => (
              <tr key={a.name}>
                <td>
                  <div style={{ fontWeight: 600 }}>{a.name}</div>
                  <div style={{ height: 4, background: "var(--inset)", borderRadius: 3, marginTop: 3, width: 160 }}>
                    <div style={{ height: "100%", width: `${(a.total_bytes / maxApp) * 100}%`, background: "#c56cf0", borderRadius: 3 }} />
                  </div>
                </td>
                <td className="faint" style={{ fontSize: 12 }}>{a.category || "—"}</td>
                <td>{a.has_source ? <Pill tone="ok">supported</Pill> : <Pill tone="warn">no connector</Pill>}</td>
                <td>{a.tenant_count}</td>
                <td>{bytes(a.total_bytes)}</td>
              </tr>
            ))}
            {(data?.top_apps || []).length === 0 && <tr><td colSpan={5} className="muted">{loading ? "Loading…" : "No app telemetry yet."}</td></tr>}
          </tbody>
        </table>
      </Card>

      <div className="spread" style={{ margin: "22px 0 10px" }}>
        <h3 style={{ margin: 0 }}>Data source usage</h3>
        <span className="faint" style={{ fontSize: 12 }}>Which connected data sources customers actually use</span>
      </div>
      <div className="insights-stats" style={{ marginBottom: 16 }}>
        <AdminStat icon="link" label="Connected sources" value={String(ds.totals?.connected || 0)} tint="#4f7cff" />
        <AdminStat icon="grid" label="Source types" value={String(ds.totals?.source_types || 0)} tint="#c56cf0" />
        <AdminStat icon="user" label="Users with sources" value={String(ds.totals?.users_with_sources || 0)} tint="#2dbe60" />
        <AdminStat icon="database" label="Data protected" value={bytes(ds.totals?.protected_bytes || 0)} tint="#f5a623" />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16 }}>
        <Card>
          <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Source adoption</h3>
          <table className="table">
            <thead><tr><th>Source</th><th style={{ textAlign: "right" }}>Accounts</th><th style={{ textAlign: "right" }}>Users</th><th style={{ textAlign: "right" }}>Objects</th><th style={{ textAlign: "right" }}>Protected</th><th>Health</th></tr></thead>
            <tbody>
              {(ds.sources || []).map((s: any) => (
                <tr key={s.source_type}>
                  <td>
                    <div className="row" style={{ gap: 8, alignItems: "center" }}>
                      {brandForSource(s.source_type) ? <BrandIcon name={brandForSource(s.source_type)!} size={16} /> : <Icon name="database" size={15} />}
                      <span style={{ fontWeight: 600 }}>{s.display_name}</span>
                    </div>
                  </td>
                  <td style={{ textAlign: "right" }}>{s.accounts}</td>
                  <td style={{ textAlign: "right" }}>{s.users}</td>
                  <td style={{ textAlign: "right" }}>{(s.objects || 0).toLocaleString()}</td>
                  <td style={{ textAlign: "right" }}>{bytes(s.protected_bytes || 0)}</td>
                  <td>
                    {s.issues > 0 ? <Pill tone="warn" dot>{s.issues} issue{s.issues === 1 ? "" : "s"}</Pill>
                      : s.accounts > 0 ? <Pill tone="ok" dot>healthy</Pill>
                      : <span className="faint" style={{ fontSize: 12 }}>—</span>}
                  </td>
                </tr>
              ))}
              {(ds.sources || []).length === 0 && <tr><td colSpan={6} className="muted">{loading ? "Loading…" : "No connected sources yet."}</td></tr>}
            </tbody>
          </table>
        </Card>

        <Card>
          <h3 style={{ margin: "0 0 10px", fontSize: 15 }}>Top sources by data</h3>
          <div className="stack" style={{ gap: 10 }}>
            {(ds.sources || []).slice(0, 8).map((s: any) => (
              <div key={s.source_type}>
                <div className="spread" style={{ fontSize: 12, marginBottom: 3 }}>
                  <span className="row" style={{ gap: 6, alignItems: "center" }}>
                    {brandForSource(s.source_type) ? <BrandIcon name={brandForSource(s.source_type)!} size={13} /> : <Icon name="database" size={12} />}
                    {s.display_name}
                  </span>
                  <span className="faint">{bytes(s.protected_bytes || 0)}</span>
                </div>
                <div style={{ height: 6, background: "var(--inset)", borderRadius: 3 }}>
                  <div style={{ height: "100%", width: `${((s.protected_bytes || 0) / dsMax) * 100}%`, background: "linear-gradient(90deg,#4f7cff,#35d0a5)", borderRadius: 3 }} />
                </div>
              </div>
            ))}
            {(ds.sources || []).length === 0 && <div className="muted">No source data yet.</div>}
          </div>
          <div style={{ borderTop: "1px solid var(--border-soft)", margin: "14px 0 10px" }} />
          <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>Source health</div>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            <Pill tone="ok" dot>{ds.health?.healthy || 0} healthy</Pill>
            {(ds.health?.reauth || 0) > 0 && <Pill tone="warn" dot>{ds.health.reauth} reconnect</Pill>}
            {(ds.health?.error || 0) > 0 && <Pill tone="danger" dot>{ds.health.error} error</Pill>}
            {(ds.health?.deactivated || 0) > 0 && <Pill tone="warn">{ds.health.deactivated} off</Pill>}
          </div>
        </Card>
      </div>
    </>
  );
}

function AdminStat({ icon, label, value, tint }: { icon: IconName; label: string; value: string; tint: string }) {
  return (
    <div className="insights-stat">
      <div className="insights-stat-ic" style={{ background: `${tint}22`, color: tint }}>
        <Icon name={icon} size={16} />
      </div>
      <div className="stack" style={{ gap: 1 }}>
        <div style={{ fontSize: 17, fontWeight: 700 }}>{value}</div>
        <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
      </div>
    </div>
  );
}

// ===========================================================================
// Support — documentation CMS
// ===========================================================================

interface AdminDoc {
  id: string; slug: string; title: string; section: string; section_order: number;
  nav_order: number; icon: string; summary: string; body: string;
  help_routes: string[]; published: boolean;
}
interface AdminSectionRow { id: string; name: string; order: number; icon: string; count: number; }

function SupportDocsAdmin() {
  const [docs, setDocs] = useState<AdminDoc[] | null>(null);
  const [sections, setSections] = useState<AdminSectionRow[]>([]);
  const [editing, setEditing] = useState<AdminDoc | "new" | null>(null);

  async function load() {
    try {
      const r = await api.get<{ docs: AdminDoc[]; sections: AdminSectionRow[] }>("/admin/support/docs");
      setDocs(r.docs);
      setSections(r.sections || []);
    } catch (e: any) {
      notify({ message: e.message || "Could not load docs", tone: "danger" });
      setDocs([]);
    }
  }
  useEffect(() => { void load(); }, []);

  async function seed() {
    try {
      const r = await api.post<{ created: number }>("/admin/support/seed", {});
      notify({ message: `Published ${r.created} starter page(s).`, tone: "ok" });
      void load();
    } catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }
  async function seedUpdates() {
    try {
      const p = await api.post<{ created: string[]; updated: string[]; skipped_customized: string[]; sections_added: number }>(
        "/admin/support/seed-updates?preview=true", {});
      const total = p.created.length + p.updated.length + p.sections_added;
      if (total === 0) { notify({ message: "The Help Center is already up to date.", tone: "ok" }); return; }
      const ok = await confirmDialog({
        title: "Publish documentation updates?",
        confirmLabel: "Publish updates",
        message: `${p.created.length} new page(s), ${p.updated.length} refreshed page(s)`
          + (p.sections_added ? `, ${p.sections_added} new section(s)` : "")
          + (p.skipped_customized.length ? `. ${p.skipped_customized.length} admin-edited page(s) are preserved unchanged.` : "."),
      });
      if (!ok) return;
      const r = await api.post<{ created: string[]; updated: string[] }>("/admin/support/seed-updates", {});
      notify({ message: `Published ${r.created.length} new + ${r.updated.length} refreshed page(s).`, tone: "ok" });
      void load();
    } catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }
  async function remove(d: AdminDoc) {
    if (!(await confirmDialog({ title: `Delete “${d.title}”?`, message: "This removes the page from the public Help Center.", tone: "danger", confirmLabel: "Delete" }))) return;
    try { await api.del(`/admin/support/docs/${d.id}`); void load(); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }
  // Create a section inline (from the editor's section dropdown) and return its name.
  async function createSection(): Promise<string | null> {
    const name = await promptDialog({ title: "New section", label: "Section name",
      placeholder: "e.g. Getting Started", confirmLabel: "Create" });
    if (!name || !name.trim()) return null;
    try {
      await api.post("/admin/support/sections", { name: name.trim() });
      await load();
      return name.trim();
    } catch (e: any) { notify({ message: e.message, tone: "danger" }); return null; }
  }

  if (editing) {
    return (
      <DocEditor
        doc={editing === "new" ? null : editing}
        sections={sections}
        onCreateSection={createSection}
        onDone={() => { setEditing(null); void load(); }}
        onCancel={() => setEditing(null)}
      />
    );
  }

  // Order the doc groups by the managed section order.
  const orderOf = (name: string) => sections.find((s) => s.name === name)?.order ?? 999;
  const groups: Record<string, AdminDoc[]> = {};
  (docs || []).forEach((d) => { (groups[d.section] ||= []).push(d); });
  const orderedGroups = Object.entries(groups).sort((a, b) => orderOf(a[0]) - orderOf(b[0]));

  return (
    <>
      <div className="spread" style={{ marginBottom: 14, alignItems: "center" }}>
        <div className="muted" style={{ fontSize: 13, maxWidth: 560 }}>
          Manage the public Help Center. Published pages sync to the support site on the next node heartbeat.
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" onClick={seed}><Icon name="sparkle" size={14} /> Seed defaults</button>
          <button className="btn sm" onClick={seedUpdates}><Icon name="clock" size={14} /> Publish updates</button>
          <button className="btn sm primary" onClick={() => setEditing("new")}><Icon name="edit" size={14} /> New page</button>
        </div>
      </div>

      <SectionManager sections={sections} reload={load} />

      {docs === null ? (
        <Card><div className="muted">Loading…</div></Card>
      ) : docs.length === 0 ? (
        <Card><div className="muted" style={{ padding: "10px 0" }}>
          No documentation yet. Use <b>Seed defaults</b> to publish the starter Help Center, then edit freely.
        </div></Card>
      ) : (
        orderedGroups.map(([section, items]) => (
          <Card key={section} style={{ marginBottom: 12 }}>
            <div style={{ fontWeight: 700, marginBottom: 6 }}>{section}</div>
            <div className="stack" style={{ gap: 0 }}>
              {items.sort((a, b) => a.nav_order - b.nav_order).map((d) => (
                <div key={d.id} className="spread"
                     style={{ padding: "9px 0", borderTop: "1px solid var(--border-soft)", alignItems: "center" }}>
                  <div style={{ minWidth: 0 }}>
                    <div className="row" style={{ gap: 8, alignItems: "center" }}>
                      <span style={{ fontWeight: 600 }}>{d.title}</span>
                      {!d.published && <Pill tone="warn">draft</Pill>}
                      {(d.help_routes || []).length > 0 && <Pill tone="info">contextual</Pill>}
                    </div>
                    <div className="faint" style={{ fontSize: 12 }}>
                      /{d.slug}{d.summary ? ` · ${d.summary}` : ""}
                    </div>
                  </div>
                  <div className="row" style={{ gap: 6 }}>
                    <button className="btn sm ghost" onClick={() => setEditing(d)}><Icon name="edit" size={13} /> Edit</button>
                    <button className="btn sm ghost" onClick={() => remove(d)} title="Delete"><Icon name="trash" size={13} /></button>
                  </div>
                </div>
              ))}
            </div>
          </Card>
        ))
      )}
    </>
  );
}

function SectionManager({ sections, reload }: { sections: AdminSectionRow[]; reload: () => void }) {
  async function add() {
    const name = await promptDialog({ title: "New section", label: "Section name",
      placeholder: "e.g. Getting Started", confirmLabel: "Create" });
    if (!name || !name.trim()) return;
    try { await api.post("/admin/support/sections", { name: name.trim() }); reload(); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }
  async function rename(s: AdminSectionRow) {
    const name = await promptDialog({ title: "Rename section", label: "New name",
      defaultValue: s.name, confirmLabel: "Rename" });
    if (!name || !name.trim() || name.trim() === s.name) return;
    try { await api.put(`/admin/support/sections/${s.id}`, { name: name.trim() }); reload(); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }
  async function del(s: AdminSectionRow) {
    if (s.count > 0) { notify({ message: `Move or delete the ${s.count} page(s) in “${s.name}” first.`, tone: "danger" }); return; }
    if (!(await confirmDialog({ title: `Delete section “${s.name}”?`, message: "The empty section will be removed from the docs navigation.", tone: "danger", confirmLabel: "Delete" }))) return;
    try { await api.del(`/admin/support/sections/${s.id}`); reload(); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }
  async function move(i: number, dir: number) {
    const j = i + dir;
    if (j < 0 || j >= sections.length) return;
    const a = sections[i], b = sections[j];
    try {
      await api.put(`/admin/support/sections/${a.id}`, { order: b.order });
      await api.put(`/admin/support/sections/${b.id}`, { order: a.order });
      reload();
    } catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }
  return (
    <Card style={{ marginBottom: 14 }}>
      <div className="spread" style={{ marginBottom: 8, alignItems: "center" }}>
        <div style={{ fontWeight: 700 }}>Sections</div>
        <button className="btn sm" onClick={add}><Icon name="edit" size={13} /> New section</button>
      </div>
      <div className="stack" style={{ gap: 0 }}>
        {sections.map((s, i) => (
          <div key={s.id} className="spread"
               style={{ padding: "8px 0", borderTop: "1px solid var(--border-soft)", alignItems: "center" }}>
            <div className="row" style={{ gap: 8, alignItems: "baseline" }}>
              <span style={{ fontWeight: 600 }}>{s.name}</span>
              <span className="faint" style={{ fontSize: 12 }}>{s.count} page{s.count === 1 ? "" : "s"}</span>
            </div>
            <div className="row" style={{ gap: 4 }}>
              <button className="btn sm ghost" title="Move up" disabled={i === 0} onClick={() => move(i, -1)}>↑</button>
              <button className="btn sm ghost" title="Move down" disabled={i === sections.length - 1} onClick={() => move(i, 1)}>↓</button>
              <button className="btn sm ghost" onClick={() => rename(s)}>Rename</button>
              <button className="btn sm ghost" title="Delete" onClick={() => del(s)}><Icon name="trash" size={13} /></button>
            </div>
          </div>
        ))}
        {sections.length === 0 && <div className="muted" style={{ fontSize: 13, padding: "6px 0" }}>No sections yet — “Seed defaults” or add one.</div>}
      </div>
    </Card>
  );
}

function DocEditor({ doc, sections, onCreateSection, onDone, onCancel }: {
  doc: AdminDoc | null; sections: AdminSectionRow[];
  onCreateSection: () => Promise<string | null>;
  onDone: () => void; onCancel: () => void;
}) {
  const [f, setF] = useState<AdminDoc>(doc || {
    id: "", slug: "", title: "", section: sections[0]?.name || "General", section_order: 100,
    nav_order: 100, icon: "book", summary: "", body: "", help_routes: [], published: true,
  });
  const [body, setBody] = useState(toEditorHtml(doc?.body || ""));
  const [routes, setRoutes] = useState((doc?.help_routes || []).join(", "));
  const [busy, setBusy] = useState(false);
  const set = (k: keyof AdminDoc, v: any) => setF((s) => ({ ...s, [k]: v }));

  async function save() {
    if (!f.title.trim()) { notify({ message: "Title is required", tone: "danger" }); return; }
    setBusy(true);
    const sectOrder = sections.find((s) => s.name === f.section)?.order ?? f.section_order ?? 100;
    const payload = {
      slug: f.slug || undefined, title: f.title, section: f.section || "General",
      section_order: sectOrder, nav_order: Number(f.nav_order) || 100,
      icon: f.icon || "book", summary: f.summary, body, published: f.published,
      help_routes: routes.split(",").map((r) => r.trim()).filter(Boolean),
    };
    try {
      if (doc) await api.put(`/admin/support/docs/${doc.id}`, payload);
      else await api.post("/admin/support/docs", payload);
      onDone();
    } catch (e: any) { notify({ message: e.message, tone: "danger" }); }
    finally { setBusy(false); }
  }

  const knownSection = sections.some((s) => s.name === f.section);

  return (
    <Card>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>{doc ? "Edit page" : "New page"}</h3>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm ghost" onClick={onCancel}>Cancel</button>
          <button className="btn sm primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save"}</button>
        </div>
      </div>
      <div className="stack" style={{ gap: 12 }}>
        <label className="stack" style={{ gap: 5 }}>
          <span className="faint" style={{ fontSize: 12 }}>Title</span>
          <input className="input" value={f.title} onChange={(e) => set("title", e.target.value)} />
        </label>
        <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
          <label className="stack flex1" style={{ gap: 5, minWidth: 200 }}>
            <span className="faint" style={{ fontSize: 12 }}>Slug (URL key, optional)</span>
            <input className="input" value={f.slug} placeholder="auto from title" onChange={(e) => set("slug", e.target.value)} />
          </label>
          <label className="stack flex1" style={{ gap: 5, minWidth: 180 }}>
            <span className="faint" style={{ fontSize: 12 }}>Section</span>
            <select className="input" value={f.section} onChange={(e) => {
              if (e.target.value === "__new__") { onCreateSection().then((n) => { if (n) set("section", n); }); }
              else set("section", e.target.value);
            }}>
              {sections.map((s) => <option key={s.id} value={s.name}>{s.name}</option>)}
              {!knownSection && f.section && <option value={f.section}>{f.section}</option>}
              <option value="__new__">+ New section…</option>
            </select>
          </label>
          <label className="stack" style={{ gap: 5, width: 120 }}>
            <span className="faint" style={{ fontSize: 12 }}>Page order</span>
            <input className="input" type="number" value={f.nav_order} onChange={(e) => set("nav_order", e.target.value)} />
          </label>
        </div>
        <div className="row" style={{ gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label className="stack flex1" style={{ gap: 5, minWidth: 200 }}>
            <span className="faint" style={{ fontSize: 12 }}>Summary (one line)</span>
            <input className="input" value={f.summary} onChange={(e) => set("summary", e.target.value)} />
          </label>
          <label className="stack" style={{ gap: 5, width: 120 }}>
            <span className="faint" style={{ fontSize: 12 }}>Nav icon</span>
            <input className="input" value={f.icon} onChange={(e) => set("icon", e.target.value)} />
          </label>
          <label className="row" style={{ gap: 8, alignItems: "center", height: 34 }}>
            <input type="checkbox" checked={f.published} onChange={(e) => set("published", e.target.checked)} />
            <span style={{ fontSize: 13 }}>Published</span>
          </label>
        </div>
        <label className="stack" style={{ gap: 5 }}>
          <span className="faint" style={{ fontSize: 12 }}>Contextual help routes (comma‑separated portal paths, e.g. /search, /restore)</span>
          <input className="input" value={routes} onChange={(e) => setRoutes(e.target.value)} placeholder="/search, /restore" />
        </label>
        <div className="stack" style={{ gap: 5 }}>
          <span className="faint" style={{ fontSize: 12 }}>Page content</span>
          <RichTextEditor value={body} onChange={setBody} />
        </div>
      </div>
    </Card>
  );
}

// ===========================================================================
// Support — ticket triage
// ===========================================================================

interface AdminTicketMsg { id: string; author_name: string; is_staff: boolean; body: string; created_at: string | null; }
interface AdminTicket {
  id: string; ref: string; subject: string; category: string; priority: string; status: string;
  requester_email: string; requester_name: string; assignee_user_id: string | null;
  last_activity_at: string | null; created_at: string | null; message_count: number;
  messages?: AdminTicketMsg[];
}

const TICKET_STATUSES = ["open", "pending", "resolved", "closed"];
const TICKET_PRIORITIES = ["low", "normal", "high", "urgent"];
const T_STATUS_TONE: Record<string, "info" | "ok" | "warn" | "danger"> = {
  open: "info", pending: "warn", resolved: "ok", closed: "info",
};
const T_PRIORITY_TONE: Record<string, "info" | "ok" | "warn" | "danger"> = {
  low: "info", normal: "info", high: "warn", urgent: "danger",
};

function SupportTicketsAdmin() {
  const [status, setStatus] = useState("open");
  const [list, setList] = useState<AdminTicket[] | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [active, setActive] = useState<AdminTicket | null>(null);

  async function load() {
    try {
      const r = await api.get<{ tickets: AdminTicket[]; counts: Record<string, number> }>(
        `/admin/support/tickets?status=${status}`);
      setList(r.tickets);
      setCounts(r.counts || {});
    } catch (e: any) {
      notify({ message: e.message || "Could not load tickets", tone: "danger" });
      setList([]);
    }
  }
  useEffect(() => { void load(); }, [status]);

  async function openTicket(id: string) {
    try { setActive(await api.get<AdminTicket>(`/admin/support/tickets/${id}`)); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }

  if (active) {
    return <AdminTicketDetail ticket={active} onBack={() => { setActive(null); void load(); }} onChange={setActive} />;
  }

  return (
    <>
      <div className="row" style={{ gap: 6, marginBottom: 14, flexWrap: "wrap" }}>
        {TICKET_STATUSES.map((st) => (
          <button key={st}
                  className={`btn sm ${status === st ? "primary" : "ghost"}`}
                  onClick={() => setStatus(st)}>
            {st} {counts[st] ? `(${counts[st]})` : ""}
          </button>
        ))}
      </div>
      {list === null ? (
        <Card><div className="muted">Loading…</div></Card>
      ) : list.length === 0 ? (
        <Card><div className="muted" style={{ padding: "10px 0" }}>No {status} tickets.</div></Card>
      ) : (
        <div className="stack" style={{ gap: 8 }}>
          {list.map((t) => (
            <Card key={t.id} onClick={() => openTicket(t.id)}>
              <div className="spread" style={{ alignItems: "flex-start", gap: 12, cursor: "pointer" }}>
                <div style={{ minWidth: 0 }}>
                  <div className="row" style={{ gap: 8, alignItems: "center" }}>
                    <span className="faint" style={{ fontSize: 12, fontFamily: "ui-monospace, monospace" }}>{t.ref}</span>
                    <span style={{ fontWeight: 600 }}>{t.subject}</span>
                  </div>
                  <div className="faint" style={{ fontSize: 12, marginTop: 3 }}>
                    {t.requester_name} &lt;{t.requester_email}&gt; · {t.category} · updated {timeAgo(t.last_activity_at)}
                  </div>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  <Pill tone={T_PRIORITY_TONE[t.priority] || "info"}>{t.priority}</Pill>
                  <Pill tone={T_STATUS_TONE[t.status] || "info"} dot>{t.status}</Pill>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}

function AdminTicketDetail({ ticket, onBack, onChange }: {
  ticket: AdminTicket; onBack: () => void; onChange: (t: AdminTicket) => void;
}) {
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);

  async function sendReply() {
    if (!reply.trim()) return;
    setBusy(true);
    try { onChange(await api.post<AdminTicket>(`/admin/support/tickets/${ticket.id}/reply`, { body: reply })); setReply(""); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function update(patch: Record<string, string>) {
    try { onChange(await api.put<AdminTicket>(`/admin/support/tickets/${ticket.id}`, patch)); }
    catch (e: any) { notify({ message: e.message, tone: "danger" }); }
  }

  return (
    <>
      <button className="btn sm ghost" style={{ marginBottom: 12 }} onClick={onBack}>
        <Icon name="logout" size={13} /> All tickets
      </button>
      <Card style={{ marginBottom: 12 }}>
        <div className="spread" style={{ alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
          <div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <span className="faint" style={{ fontSize: 12.5, fontFamily: "ui-monospace, monospace" }}>{ticket.ref}</span>
              <Pill tone={T_STATUS_TONE[ticket.status] || "info"} dot>{ticket.status}</Pill>
            </div>
            <h3 style={{ margin: "8px 0 2px" }}>{ticket.subject}</h3>
            <div className="faint" style={{ fontSize: 12 }}>
              {ticket.requester_name} &lt;{ticket.requester_email}&gt; · {ticket.category} · opened {fmtAbsolute(ticket.created_at)}
            </div>
          </div>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <label className="stack" style={{ gap: 4 }}>
              <span className="faint" style={{ fontSize: 11 }}>Status</span>
              <select className="input" value={ticket.status} onChange={(e) => update({ status: e.target.value })}>
                {TICKET_STATUSES.map((st) => <option key={st} value={st}>{st}</option>)}
              </select>
            </label>
            <label className="stack" style={{ gap: 4 }}>
              <span className="faint" style={{ fontSize: 11 }}>Priority</span>
              <select className="input" value={ticket.priority} onChange={(e) => update({ priority: e.target.value })}>
                {TICKET_PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </label>
          </div>
        </div>
      </Card>

      <div className="stack" style={{ gap: 10, marginBottom: 12 }}>
        {(ticket.messages || []).map((m) => (
          <div key={m.id} className={`ticket-msg ${m.is_staff ? "staff" : ""}`}>
            <div className="spread" style={{ marginBottom: 6 }}>
              <span style={{ fontWeight: 600, fontSize: 13 }}>
                {m.is_staff && <Icon name="shield" size={12} />} {m.author_name || (m.is_staff ? "Arkive Support" : "Customer")}
              </span>
              <span className="faint" style={{ fontSize: 11.5 }}>{fmtAbsolute(m.created_at)}</span>
            </div>
            <div style={{ fontSize: 14, lineHeight: 1.55, whiteSpace: "pre-wrap" }}>{m.body}</div>
          </div>
        ))}
      </div>

      <Card>
        <div className="stack" style={{ gap: 10 }}>
          <textarea className="input" rows={4} value={reply} onChange={(e) => setReply(e.target.value)}
                    placeholder="Reply to the customer… (they'll be emailed)" />
          <div className="row" style={{ justifyContent: "flex-end", gap: 8 }}>
            <button className="btn sm" disabled={busy} onClick={() => update({ status: "resolved" })}>
              <Icon name="check" size={13} /> Mark resolved
            </button>
            <button className="btn primary" disabled={busy || !reply.trim()} onClick={sendReply}>
              {busy ? "Sending…" : "Send reply"}
            </button>
          </div>
        </div>
      </Card>
    </>
  );
}
