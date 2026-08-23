import { ReactNode, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, Stat, bytes, timeAgo, fmtAbsolute } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { FilterBar } from "../components/FilterBar";
import { promptDialog, formDialog, confirmDialog, notify } from "../components/dialog";
import { Ring, Sparkline, AreaChart } from "../components/charts";

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
  // Per-account feature flags. A tenant-level block wins regardless of this.
  fields.push(...flagFields(await flagCatalog(), u.feature_flags, "user"));
  const r = await formDialog({ title: `Edit ${u.email}`, confirmLabel: "Save", fields, wide: true });
  if (!r) return false;
  const flags = extractFlags(r, await flagCatalog());
  await api.put(`/admin/users/${u.id}`, r);
  if (Object.keys(flags).length) {
    await api.put(`/admin/users/${u.id}/flags`, { feature_flags: flags });
  }
  return true;
}

// Left-nav sections for the admin console (M365-style, grouped).
export const ADMIN_SECTIONS: AdminSection[] = [
  { key: "overview", label: "Overview", icon: "grid", group: "" },
  { key: "tenants", label: "Tenants", icon: "user", group: "Customers" },
  { key: "users", label: "Users", icon: "user", group: "Customers" },
  { key: "reports", label: "Reports", icon: "activity", group: "Customers" },
  { key: "config-objects", label: "Configuration objects", icon: "key", group: "Integrations" },
  { key: "sources", label: "Sources", icon: "link", group: "Integrations" },
  { key: "service-objects", label: "Service objects", icon: "mail", group: "Integrations" },
  { key: "pricing", label: "Pricing", icon: "database", group: "Integrations" },
  { key: "website", label: "Website", icon: "grid", group: "Integrations" },
  { key: "nodes", label: "Nodes", icon: "server", group: "Infrastructure" },
  { key: "storage-usage", label: "Arkive Cloud", icon: "database", group: "Infrastructure" },
  { key: "backups", label: "Backups", icon: "shield", group: "Infrastructure" },
  { key: "fleet", label: "Appliance fleet", icon: "server", group: "Infrastructure" },
  { key: "crypto", label: "Crypto", icon: "lock", group: "Infrastructure" },
  { key: "updates", label: "Updates", icon: "clock", group: "Infrastructure" },
  { key: "audit", label: "Audit log", icon: "shield", group: "Infrastructure" },
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
      {s === "nodes" && <Nodes />}
      {s === "storage-usage" && <StorageUsageAdmin />}
      {s === "backups" && <BackupsAdmin />}
      {s === "config-objects" && <ConfigObjectsAdmin />}
      {s === "sources" && <SourcesAdmin />}
      {s === "service-objects" && <><ServiceObjectsAdmin /><EmailAdmin /></>}
      {s === "fleet" && <Fleet />}
      {s === "pricing" && <Pricing />}
      {s === "website" && <WebsiteCMS />}
      {s === "crypto" && <Crypto />}
      {s === "audit" && <Audit />}
      {s === "updates" && <Updates />}
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

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Tenants</h3>
        <button className="btn primary sm" onClick={newTenant}><Icon name="user" size={14} /> New tenant</button>
      </div>
      <Card>
        <table className="table">
          <thead><tr><th>Tenant</th><th>Type</th><th>Node</th><th>Plan</th><th>Accounts</th><th>Sources</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {rows.map((t) => (
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
            {rows.length === 0 && <tr><td colSpan={8} className="muted">No tenants.</td></tr>}
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
            { value: tenantId, onChange: setTenantId, options: [
              { label: "All tenants", value: "" },
              ...tenants.map((t) => ({ label: t.name, value: t.id })),
            ] },
            { value: typeF, onChange: setTypeF, options: [
              { label: "All types", value: "" },
              ...["shared", "dedicated", "restricted", "internal"].map((v) => ({ label: TENANT_TYPE_LABEL[v], value: v })),
            ] },
            { value: plan, onChange: setPlan, options: [
              { label: "All plans", value: "" },
              ...plans.map((p) => ({ label: p.name, value: p.id })),
            ] },
            { value: statusF, onChange: setStatusF, options: [
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
              <tr key={u.id}>
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
                <td style={{ textAlign: "right" }}>
                  <button className="btn ghost sm" onClick={async () => { try { if (await editUserDialog(u, u.tenant_type === "shared")) void load(); } catch { /* ignore */ } }}>Edit</button>
                </td>
              </tr>
            ))}
            {sorted.length === 0 && <tr><td colSpan={10} className="muted">{loading ? "Loading…" : "No users match."}</td></tr>}
          </tbody>
        </table>
      </Card>
    </>
  );
}

function TenantDetail({ id, onBack }: { id: string; onBack: () => void }) {
  const [t, setT] = useState<any>(null);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3200); }
  async function load() {
    try { setErr(""); setT(await api.get<any>(`/admin/tenants/${id}`)); }
    catch (e) { setErr((e as { message?: string }).message || "Failed to load tenant"); }
  }
  useEffect(() => { void load(); }, [id]);

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

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <button className="btn ghost sm" onClick={onBack}>← Tenants</button>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" onClick={editTenant}>Edit</button>
          <button className="btn danger sm" onClick={suspend}>Suspend</button>
        </div>
      </div>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>{t.name}</h3>
            <div className="faint" style={{ fontSize: 12 }}>
              {TENANT_TYPE_LABEL[t.tenant_type] || t.tenant_type || "Dedicated"}
              {t.tenant_type === "shared"
                ? ` · ${t.users} account${t.users === 1 ? "" : "s"} · per-account Personal plan`
                : ` · ${t.plan} · ${licensedTb} TB licensed`}
              {` · ${t.key_ownership_model}`}
              {t.node ? ` · node: ${t.node.name}` : " · processed on control plane"}
            </div>
          </div>
          <Pill tone={t.status === "active" ? "ok" : "warn"}>{t.status}</Pill>
        </div>
        <div className="grid grid-4" style={{ gap: 12, marginTop: 14 }}>
          <Mini label={t.tenant_type === "shared" ? "Accounts" : "Users"} value={t.users} />
          <Mini label="Appliances" value={t.appliances} />
          <Mini label="Agents" value={t.agents} />
          <Mini label="Sources" value={t.sources} />
          <Mini label="Mappings" value={t.mappings} />
          <Mini label="Objects" value={(t.objects ?? 0).toLocaleString()} />
          <Mini label="Recovery points" value={t.recovery_points} />
          <Mini label="Vaults" value={t.vaults?.length ?? 0} />
        </div>
      </Card>

      {(() => {
        const b = t.billing;
        const su = t.storage_usage;
        const isShared = t.tenant_type === "shared";
        const money = (n: number) => "$" + (Math.round((n || 0) * 100) / 100)
          .toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
        const channelLabel: Record<string, string> = {
          "cv-cloud": "Arkive Cloud", "appliance": "Offline appliance",
          "customer-cloud": "Your cloud (S3 / Azure)",
        };
        const options: string[] = b?.options || t.protection_options || [];
        return (
          <Card style={{ marginBottom: 16 }}>
            <div className="spread" style={{ marginBottom: 6 }}>
              <h3 style={{ margin: 0 }}>{isShared ? "Accounts & storage" : "Protection & billing"}</h3>
              {isShared
                ? <Pill tone="info">{t.users} account{t.users === 1 ? "" : "s"}</Pill>
                : (b?.costs && <Pill tone="info">{money(b.costs.total_monthly)}/mo to Arkive</Pill>)}
            </div>
            <div className="faint" style={{ fontSize: 12, marginBottom: 12 }}>
              {isShared
                ? "A pool of isolated personal accounts — each self-manages its own Personal plan; there is no shared organization plan or licensed amount."
                : "Coupled to what the customer selected in Protection Setup — licensed amount, storage channels, and what they pay us."}
            </div>
            {!isShared && (
              <div className="grid grid-4" style={{ gap: 12, marginBottom: 14 }}>
                <Mini label="License plan" value={b?.license_plan?.name || t.plan} />
                <Mini label="Licensed" value={`${b?.licensed_tb ?? licensedTb} TB`} />
                <Mini label="Billable" value={b?.billable_tb != null ? `${b.billable_tb} TB` : "—"} />
                <Mini label="Used" value={`${b?.used_tb ?? 0} TB${b?.percent != null ? ` · ${b.percent}%` : ""}`} />
              </div>
            )}
            {!isShared && (
              <div style={{ marginBottom: 12 }}>
                <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>Storage channels the customer enabled</div>
                <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                  {options.length === 0 && <span className="muted" style={{ fontSize: 12 }}>None selected yet</span>}
                  {options.map((o) => <Pill key={o} tone="info">{channelLabel[o] || o}</Pill>)}
                </div>
              </div>
            )}
            <div className="faint" style={{ fontSize: 11.5, marginBottom: 6 }}>
              {isShared ? "Storage footprint across all accounts in this tenant" : "Storage footprint"}
            </div>
            <table className="table">
              <thead><tr><th>Storage channel</th><th>Stored</th><th>Monthly cost</th></tr></thead>
              <tbody>
                <tr><td>Arkive Cloud</td><td>{bytes(su?.cloud_bytes || 0)}</td><td>{money(b?.costs?.cloud_storage_monthly || 0)}</td></tr>
                <tr><td>Appliance storage</td><td>{bytes(su?.appliance_bytes || 0)}</td><td className="faint">{b?.costs?.appliance_monthly ? `${money(b.costs.appliance_monthly)}/mo lease` : "on-prem · no per-TB cost"}</td></tr>
                <tr><td>Customer cloud bucket</td><td>{bytes(su?.customer_bytes || 0)}</td><td className="faint">{money(b?.costs?.third_party_estimate_monthly || 0)} est. (you pay provider)</td></tr>
              </tbody>
            </table>
            {!isShared && b?.costs && (
              <div className="grid grid-4" style={{ gap: 12, marginTop: 14 }}>
                <Mini label="Protection / license" value={`${money(b.costs.protection_monthly)}/mo`} />
                <Mini label="Cloud storage" value={`${money(b.costs.cloud_storage_monthly)}/mo`} />
                <Mini label="Appliance plan" value={`${money(b.costs.appliance_monthly)}/mo`} />
                <Mini label="Total to Arkive" value={`${money(b.costs.total_monthly)}/mo`} />
              </div>
            )}
          </Card>
        );
      })()}

      <Card>
        <div className="spread" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>{t.tenant_type === "shared" ? "Accounts" : "Users"}</h3>
          <button className="btn primary sm" onClick={newUser}>
            <Icon name="user" size={14} /> {t.tenant_type === "shared" ? "Add account" : "Add user"}
          </button>
        </div>
        {t.tenant_type === "shared" ? (() => {
          const money = (n: number) => "$" + (Math.round((n || 0) * 100) / 100)
            .toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
          return (
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
                      <button className="btn ghost sm" onClick={() => editUser(u)}>Edit</button>{" "}
                      <button className="btn ghost sm" onClick={() => resetUser(u)}>Reset</button>{" "}
                      <button className="btn danger sm" onClick={() => delUser(u)}>Delete</button>
                    </td>
                  </tr>
                ))}
                {(t.members || []).length === 0 && <tr><td colSpan={7} className="muted">No accounts yet.</td></tr>}
              </tbody>
            </table>
          );
        })() : (
          <table className="table">
            <thead><tr><th>User</th><th>Role</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {(t.members || []).map((u: any) => (
                <tr key={u.id}>
                  <td><div style={{ fontWeight: 600 }}>{u.display_name || u.email}</div><div className="faint" style={{ fontSize: 11.5 }}>{u.email}{u.is_platform_admin ? " · platform admin" : ""}</div></td>
                  <td><Pill tone="info">{u.role}</Pill></td>
                  <td><Pill tone={u.status === "active" ? "ok" : "warn"}>{u.status}</Pill></td>
                  <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                    <button className="btn ghost sm" onClick={() => editUser(u)}>Edit</button>{" "}
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
  const [toast, setToast] = useState("");
  const [installCmd, setInstallCmd] = useState("");
  const [sel, setSel] = useState<string | null>(null);
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }
  async function load() {
    try { setNodes(await api.get<any[]>("/admin/nodes")); } catch { /* ignore */ }
    try { setSvcs(await api.get<ServiceObj[]>("/admin/service-objects")); } catch { /* ignore */ }
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

  const storageSvcs = svcs.filter((x) => x.category === "storage");
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
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" onClick={newInstaller}><Icon name="logout" size={14} /> Install a node</button>
          <button className="btn primary sm" onClick={registerNode}><Icon name="server" size={14} /> Register node</button>
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
      <NodeBlueprints flash={flash} />
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

type NodeTab = "health" | "processes" | "keys" | "logs" | "tenants";
const NODE_TABS: { key: NodeTab; label: string; icon: IconName }[] = [
  { key: "health", label: "System health", icon: "activity" },
  { key: "processes", label: "Processes & services", icon: "grid" },
  { key: "keys", label: "Keys & certificates", icon: "lock" },
  { key: "logs", label: "Logs", icon: "note" },
  { key: "tenants", label: "Tenant usage", icon: "user" },
];
const HISTORY_WINDOWS = ["1h", "6h", "24h", "7d", "30d", "90d"];
const MAX_LIVE = 60; // ~5 min of 5s live samples

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
  async function loadLogs() { try { setLogs((await api.get<any>(`/admin/nodes/${id}/logs?source=${logSource}&lines=250`)).lines || []); } catch { /* ignore */ } }

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

  async function ctl(action: string, unit: string, confirmMsg?: string) {
    if (confirmMsg && !await confirmDialog({ title: "Confirm", message: confirmMsg, tone: "danger", confirmLabel: action })) return;
    try {
      const r = await api.post<any>(`/admin/nodes/${id}/control`, { action, unit });
      flash(r.ok ? `${action} ${unit || ""} ok` : `Failed: ${r.error || "control error"}`);
      setTimeout(loadLive, 1500);
    } catch (e) { flash((e as { message?: string }).message || "Control failed"); }
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
      <div className="spread" style={{ marginBottom: 12 }}>
        <button className="btn ghost sm" onClick={onBack}>← Nodes</button>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn sm" onClick={async () => { await onEdit(node); await loadNode(); }}>Edit</button>
          <button className="btn sm" onClick={() => ctl("update", "", "Trigger a software update on this node? It will pull the latest build and restart.")}>
            <Icon name="clock" size={13} /> Update
          </button>
          <button className="btn sm" onClick={() => ctl("restart", "cv-cloud", "Restart the Arkive application on this node?")}>Restart app</button>
          {!node.is_self && <button className="btn danger sm" onClick={async () => { await onRemove(node); onBack(); }}>Remove</button>}
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
                {live?.uptime_seconds ? ` · ${uptimeShort(live.uptime_seconds)}` : ""}
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <Pill tone={node.online ? "ok" : "warn"}>{node.online ? "Online" : "Offline"}</Pill>
            <Pill tone={node.status === "active" ? "info" : "warn"}>{node.status}</Pill>
            {live?.source === "heartbeat" && <Pill tone="warn">heartbeat only</Pill>}
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
              <label className="row" style={{ gap: 8, alignItems: "center", marginBottom: 8 }}>
                <Icon name="database" size={13} /><span className="faint" style={{ fontSize: 12, width: 52 }}>Storage</span>
                <select className="input sm flex1" value={node.storage_service_id || ""} onChange={(e) => { onService(node, { storage_service_id: e.target.value }); setTimeout(loadNode, 400); }}>
                  <option value="">Default (env / local)</option>
                  {storageSvcs.map((x) => <option key={x.id} value={x.id}>{x.name}{x.configured ? "" : " (incomplete)"}</option>)}
                </select>
              </label>
              <label className="row" style={{ gap: 8, alignItems: "center" }}>
                <Icon name="mail" size={13} /><span className="faint" style={{ fontSize: 12, width: 52 }}>Email</span>
                <select className="input sm flex1" value={node.email_service_id || ""} onChange={(e) => { onService(node, { email_service_id: e.target.value }); setTimeout(loadNode, 400); }}>
                  <option value="">Default</option>
                  {emailSvcs.map((x) => <option key={x.id} value={x.id}>{x.name}{x.configured ? "" : " (incomplete)"}</option>)}
                </select>
              </label>
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
        Background backup / sync jobs across all tenants. Stopping a worker aborts it at its next chunk boundary.
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
                <div style={{ fontWeight: 600 }}>{j.tenant}{j.owner ? ` → ${j.owner}` : ""}</div>
                <div className="faint" style={{ fontSize: 11 }}>{j.source}{j.source_username && j.source_username !== j.source ? ` (${j.source_username})` : ""}</div>
              </td>
              <td style={{ whiteSpace: "nowrap" }}>{j.node || "Control plane"}</td>
              <td>
                <div className="row" style={{ gap: 8, alignItems: "center" }}>
                  <span title={j.status} style={{ width: 9, height: 9, borderRadius: 999, background: statusColor(j.status), flexShrink: 0 }} />
                  <span className="faint">{(j.processed || 0).toLocaleString()}{j.total ? ` / ${(j.total).toLocaleString()}` : ""}</span>
                </div>
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

function NodeBlueprints({ flash }: { flash: (m: string) => void }) {
  const [rows, setRows] = useState<any[]>([]);
  async function load() {
    try { setRows(await api.get<any[]>("/admin/node-blueprints")); } catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  async function edit(bp: any) {
    const r = await formDialog({
      title: `Blueprint · ${bp.role}`, confirmLabel: "Save",
      message: "Pushed to every node with this role on heartbeat. Config and settings are JSON.",
      fields: [
        { name: "target_version", label: "Target version", defaultValue: bp.target_version, placeholder: "e.g. 0.4.1 or main" },
        { name: "config", label: "Config (JSON)", type: "textarea", defaultValue: JSON.stringify(bp.config || {}, null, 2) },
        { name: "settings", label: "Settings (JSON)", type: "textarea", defaultValue: JSON.stringify(bp.settings || {}, null, 2) },
      ],
    });
    if (!r) return;
    let config: any, settings: any;
    try { config = r.config ? JSON.parse(r.config) : {}; settings = r.settings ? JSON.parse(r.settings) : {}; }
    catch { void notify({ title: "Invalid JSON", message: "Config and settings must be valid JSON.", tone: "warn" }); return; }
    try {
      await api.put(`/admin/node-blueprints/${bp.role}`, { target_version: r.target_version, config, settings });
      flash("Blueprint saved"); await load();
    } catch { flash("Failed"); }
  }

  return (
    <>
      <div className="spread" style={{ margin: "22px 0 12px" }}>
        <h3 style={{ margin: 0 }}>Role blueprints</h3>
        <span className="faint" style={{ fontSize: 12 }}>Central config &amp; update target per node role</span>
      </div>
      <div className="grid grid-3">
        {rows.map((bp) => (
          <Card key={bp.role}>
            <div className="spread" style={{ marginBottom: 8 }}>
              <div style={{ fontWeight: 700 }}>{bp.role}</div>
              <Pill tone={bp.target_version ? "info" : "warn"}>{bp.target_version ? `v${bp.target_version}` : "no target"}</Pill>
            </div>
            <div className="faint" style={{ fontSize: 11.5, marginBottom: 10 }}>
              {Object.keys(bp.config || {}).length} config · {Object.keys(bp.settings || {}).length} settings
              {bp.updated_at ? ` · updated ${timeAgo(bp.updated_at)}` : ""}
            </div>
            <button className="btn ghost sm" onClick={() => edit(bp)}>Edit blueprint</button>
          </Card>
        ))}
      </div>
    </>
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
  useEffect(() => { api.get<any[]>("/admin/fleet").then(setRows).catch(() => {}); }, []);
  return (
    <Card>
      <table className="table">
        <thead><tr><th>Serial</th><th>Model</th><th>State</th><th>Isolation</th><th>Attestation</th><th>Version</th><th>Heartbeat</th></tr></thead>
        <tbody>
          {rows.map((a) => (
            <tr key={a.id}>
              <td className="mono">{a.serial}</td><td>{a.model}</td>
              <td><Pill tone="info">{a.state}</Pill></td>
              <td className="faint">{a.isolation_state}</td>
              <td>{a.attestation_ok ? <Pill tone="ok">ok</Pill> : <Pill tone="danger">failed</Pill>}</td>
              <td>{a.software_version}</td>
              <td className="faint">{timeAgo(a.last_heartbeat_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="muted">No appliances in fleet.</div>}
    </Card>
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
interface SourceSlot { type: string; label: string; kind: string; keys: string[]; enabled: boolean; config_object_id: string | null; configured: boolean; icon: string; color: string; family: string; category: string }
interface DraftRow { key: string; value: string; secret: boolean; set: boolean }
interface ServiceKind { kind: string; label: string; category: string; credential_keys: string[]; settings: string[]; setting_defaults?: Record<string, string>; required: string[] }
interface ServiceObj { id: string; name: string; kind: string; kind_label: string; category: string; enabled: boolean; config_object_id: string | null; settings: Record<string, string>; setting_keys: string[]; credential_keys: string[]; configured: boolean; updated_at?: string }

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

  async function setSource(s: SourceSlot, patch: { enabled?: boolean; config_object_id?: string | null; family?: string }) {
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
        </div>
        <div className="stack" style={{ gap: 20 }}>
          {families.map((fam) => (
            <div key={fam}>
              <div className="row" style={{ gap: 8, marginBottom: 8, alignItems: "center" }}>
                <div className="nav-section" style={{ padding: 0 }}>{fam}</div>
                <span className="faint" style={{ fontSize: 11 }}>{sources.filter((s) => (s.family || "Other") === fam).length}</span>
              </div>
              <table className="table">
                <thead><tr><th>Source</th><th>Enabled</th><th>Configuration</th><th>Status</th><th></th></tr></thead>
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

function prettyKey(k: string) {
  return k.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

interface ServiceDraft { id?: string; name: string; kind: string; enabled: boolean; config_object_id: string; settings: Record<string, string> }

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
    setDraft({ name: "", kind, enabled: true, config_object_id: "", settings: { ...(spec?.setting_defaults || {}) } });
  }
  function editDraft(o: ServiceObj) {
    setDraft({ id: o.id, name: o.name, kind: o.kind, enabled: o.enabled, config_object_id: o.config_object_id || "", settings: { ...(o.settings || {}) } });
  }
  async function saveDraft() {
    if (!draft) return;
    const payload = { name: draft.name || "Service", enabled: draft.enabled, config_object_id: draft.config_object_id || null, settings: draft.settings };
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
  const draftSpec = draft ? specFor(draft.kind) : undefined;
  const settingOptions = (key: string): string[] | null =>
    key === "storage_class" ? STORAGE_CLASS_OPTS : key === "access_tier" ? ACCESS_TIER_OPTS : null;

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread" style={{ marginBottom: 10 }}>
          <div>
            <h3 style={{ margin: 0 }}>Service objects</h3>
            <div className="muted" style={{ fontSize: 12.5 }}>Storage &amp; email backends for Arkive Cloud. Credentials come from a linked configuration object; assign a service to a node under Nodes.</div>
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
            <div className="row" style={{ gap: 8, marginTop: 12 }}>
              <button className="btn primary sm" onClick={saveDraft}>Save</button>
              <button className="btn ghost sm" onClick={() => setDraft(null)}>Cancel</button>
            </div>
          </div>
        )}

        <ServiceTable title="Storage services" rows={storage} onEdit={editDraft} onDelete={delObject} onTest={testObject} testable />
        <div style={{ height: 14 }} />
        <ServiceTable title="Email services" rows={email} onEdit={editDraft} onDelete={delObject} onTest={testObject} testable />
        <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>
          Storage services back Arkive Cloud: mappings routed to <b>cv-cloud</b> store and restore through
          the storage service selected on the running node. S3 defaults to Intelligent-Tiering and Azure to the
          Cool tier for low cost while keeping restore instant.
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
              <td><Pill tone="info">{o.kind_label}</Pill></td>
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

interface StorageUsage {
  cloud_total: { bytes: number; objects: number; recovery_points: number; tenants: number };
  by_tenant: { tenant_id: string; tenant_name: string; plan: string; licensed_bytes: number; bytes: number; objects: number; recovery_points: number }[];
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
        <h3 style={{ marginTop: 0 }}>Data stored by tenant</h3>
        <table className="table">
          <thead><tr><th>Tenant</th><th>Plan</th><th>Recovery points</th><th>Objects</th><th>Data stored</th><th>Of licensed</th></tr></thead>
          <tbody>
            {d.by_tenant.map((r) => {
              const pct = r.licensed_bytes ? Math.round((r.bytes / r.licensed_bytes) * 100) : null;
              return (
                <tr key={r.tenant_id}>
                  <td style={{ fontWeight: 600 }}>{r.tenant_name}</td>
                  <td><Pill tone="info">{r.plan}</Pill></td>
                  <td>{r.recovery_points.toLocaleString()}</td>
                  <td>{r.objects.toLocaleString()}</td>
                  <td style={{ fontWeight: 600 }}>{bytes(r.bytes)}</td>
                  <td className="faint">{r.licensed_bytes ? `${bytes(r.licensed_bytes)} · ${pct}%` : "—"}</td>
                </tr>
              );
            })}
            {d.by_tenant.length === 0 && <tr><td colSpan={6} className="muted">No cloud data stored yet.</td></tr>}
          </tbody>
        </table>
        <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
          Totals sum bytes written across all cloud recovery points (before de-duplication). Per-service
          attribution follows each node's active storage target.
        </div>
      </Card>
    </>
  );
}

// --------------------------------------------------------------------------- //
// Infrastructure Backups (node/CP core-state backups to storage services)     //
// --------------------------------------------------------------------------- //
interface BackupDest { service_id: string; name: string; kind: string; status: string; bytes: number; error?: string | null; }
interface BackupRunView { id: string; status: string; total_bytes: number; components: string[]; destinations: BackupDest[]; message?: string; error?: string; created_at?: string | null; finished_at?: string | null; }
interface BackupNode { id: string; name: string; role: string; category: string; is_self: boolean; backup_service_ids: string[]; backup_services: string[]; last_backup: BackupRunView | null; }
interface BackupService { id: string; name: string; kind: string; kind_label: string; enabled: boolean; settings: Record<string, string>; nodes: string[]; bytes: number; backed_up_nodes: number; }
interface BackupsData {
  summary: { nodes_total: number; nodes_protected: number; total_stored_bytes: number; success_24h: number; failed_24h: number; interval_minutes: number; last_run_at: string | null };
  nodes: BackupNode[];
  services: BackupService[];
  recent: { id: string; node_name: string; role: string; status: string; total_bytes: number; message?: string; error?: string; destinations: BackupDest[]; components: string[]; created_at?: string | null; finished_at?: string | null }[];
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

  async function load() {
    try { setD(await api.get<BackupsData>("/admin/backups")); } catch { /* ignore */ }
  }
  useEffect(() => {
    void load();
    api.get<{ id: string; name: string; kind: string; enabled: boolean; category: string }[]>("/admin/service-objects")
      .then((rows) => setStoreServices(rows.filter((r) => r.kind.startsWith("storage-"))))
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
        <h3 style={{ marginTop: 0 }}>Backup storage services</h3>
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
                <Pill tone={s.nodes.length ? "ok" : "warn"}>{s.nodes.length ? `${bytes(s.bytes)} · ${s.backed_up_nodes} node(s)` : "Unused"}</Pill>
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
          <thead><tr><th>Node</th><th>Status</th><th>Components</th><th>Destinations</th><th>Size</th><th>When</th></tr></thead>
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
              </tr>
            ))}
            {d.recent.length === 0 && <tr><td colSpan={6} className="muted">No backup runs yet.</td></tr>}
          </tbody>
        </table>
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}
