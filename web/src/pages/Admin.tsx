import { ReactNode, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, Stat, bytes, timeAgo } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { promptDialog, formDialog, confirmDialog, notify } from "../components/dialog";

export interface AdminSection { key: string; label: string; icon: IconName; group: string; }

// Left-nav sections for the admin console (M365-style, grouped).
export const ADMIN_SECTIONS: AdminSection[] = [
  { key: "overview", label: "Overview", icon: "grid", group: "" },
  { key: "tenants", label: "Tenants", icon: "user", group: "Customers" },
  { key: "reports", label: "Reports", icon: "activity", group: "Customers" },
  { key: "config-objects", label: "Configuration objects", icon: "key", group: "Integrations" },
  { key: "sources", label: "Sources", icon: "link", group: "Integrations" },
  { key: "service-objects", label: "Service objects", icon: "mail", group: "Integrations" },
  { key: "pricing", label: "Pricing", icon: "database", group: "Integrations" },
  { key: "nodes", label: "Nodes", icon: "server", group: "Infrastructure" },
  { key: "storage-usage", label: "Storage usage", icon: "database", group: "Infrastructure" },
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
      {s === "reports" && <Reports />}
      {s === "nodes" && <Nodes />}
      {s === "storage-usage" && <StorageUsageAdmin />}
      {s === "config-objects" && <ConfigObjectsAdmin />}
      {s === "sources" && <SourcesAdmin />}
      {s === "service-objects" && <><ServiceObjectsAdmin /><EmailAdmin /></>}
      {s === "fleet" && <Fleet />}
      {s === "pricing" && <Pricing />}
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

function Tenants() {
  const [rows, setRows] = useState<any[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [toast, setToast] = useState("");
  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }
  async function load() { try { setRows(await api.get<any[]>("/admin/tenants")); } catch { /* ignore */ } }
  useEffect(() => { void load(); }, []);

  async function newTenant() {
    const r = await formDialog({
      title: "New tenant", confirmLabel: "Create tenant",
      fields: [
        { name: "name", label: "Organization name", required: true },
        { name: "plan", label: "Plan", defaultValue: "business",
          options: ["consumer", "family", "business", "enterprise"].map((v) => ({ label: v, value: v })) },
        { name: "key_ownership_model", label: "Key ownership", defaultValue: "customer-managed",
          options: [{ label: "Customer-managed", value: "customer-managed" }, { label: "Zero-knowledge", value: "zero-knowledge" }] },
        { name: "licensed_tb", label: "Licensed data (TB)", defaultValue: "1" },
        { name: "owner_email", label: "Owner email (optional)" },
        { name: "owner_name", label: "Owner name (optional)" },
      ],
    });
    if (!r) return;
    try {
      await api.post("/admin/tenants", { ...r, licensed_tb: Number(r.licensed_tb) || 0 });
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
          <thead><tr><th>Tenant</th><th>Plan</th><th>Users</th><th>Appliances</th><th>Sources</th><th>Status</th><th></th></tr></thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id} style={{ cursor: "pointer" }} onClick={() => setSel(t.id)}>
                <td style={{ fontWeight: 600 }}>{t.name}</td>
                <td><Pill tone="info">{t.plan}</Pill></td>
                <td>{t.users}</td><td>{t.appliances}</td><td>{t.sources}</td>
                <td><Pill tone={t.status === "active" ? "ok" : "warn"}>{t.status}</Pill></td>
                <td className="faint" style={{ textAlign: "right" }}>Manage →</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={7} className="muted">No tenants.</td></tr>}
          </tbody>
        </table>
      </Card>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
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
    const r = await formDialog({
      title: "Edit tenant", confirmLabel: "Save",
      fields: [
        { name: "name", label: "Name", defaultValue: t.name, required: true },
        { name: "plan", label: "Plan", defaultValue: t.plan,
          options: ["consumer", "family", "business", "enterprise"].map((v) => ({ label: v, value: v })) },
        { name: "status", label: "Status", defaultValue: t.status,
          options: ["active", "suspended", "trial"].map((v) => ({ label: v, value: v })) },
        { name: "licensed_tb", label: "Licensed data (TB)", defaultValue: String(((t.licensed_bytes || 0) / (1024 ** 4)).toFixed(2)) },
      ],
    });
    if (!r) return;
    try { await api.put(`/admin/tenants/${id}`, { ...r, licensed_tb: Number(r.licensed_tb) || 0 }); flash("Saved"); await load(); }
    catch { flash("Save failed"); }
  }
  async function suspend() {
    if (!await confirmDialog({ title: "Suspend tenant?", message: `Freeze ${t.name} and deactivate all its users. This is reversible.`, tone: "danger", confirmLabel: "Suspend" })) return;
    try { await api.del(`/admin/tenants/${id}`); flash("Tenant suspended"); await load(); } catch { flash("Failed"); }
  }

  async function newUser() {
    const r = await formDialog({
      title: "Add user", confirmLabel: "Create user",
      fields: [
        { name: "email", label: "Email", required: true },
        { name: "display_name", label: "Name" },
        { name: "role", label: "Role", defaultValue: "member",
          options: ["owner", "security-admin", "member", "support-admin"].map((v) => ({ label: v, value: v })) },
      ],
    });
    if (!r) return;
    try { const res = await api.post<any>(`/admin/tenants/${id}/users`, r); flash(res.invite?.dev_code ? `Created · code ${res.invite.dev_code}` : "User created & invited"); await load(); }
    catch (e) { flash((e as { message?: string }).message || "Could not create user"); }
  }
  async function editUser(u: any) {
    const r = await formDialog({
      title: `Edit ${u.email}`, confirmLabel: "Save",
      fields: [
        { name: "display_name", label: "Name", defaultValue: u.display_name },
        { name: "role", label: "Role", defaultValue: u.role,
          options: ["owner", "security-admin", "member", "support-admin"].map((v) => ({ label: v, value: v })) },
        { name: "status", label: "Status", defaultValue: u.status,
          options: ["active", "suspended"].map((v) => ({ label: v, value: v })) },
      ],
    });
    if (!r) return;
    try { await api.put(`/admin/users/${u.id}`, r); flash("User updated"); await load(); } catch { flash("Update failed"); }
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
            <div className="faint" style={{ fontSize: 12 }}>{t.plan} · {t.key_ownership_model} · {licensedTb} TB licensed</div>
          </div>
          <Pill tone={t.status === "active" ? "ok" : "warn"}>{t.status}</Pill>
        </div>
        <div className="grid grid-4" style={{ gap: 12, marginTop: 14 }}>
          <Mini label="Users" value={t.users} />
          <Mini label="Appliances" value={t.appliances} />
          <Mini label="Agents" value={t.agents} />
          <Mini label="Sources" value={t.sources} />
          <Mini label="Mappings" value={t.mappings} />
          <Mini label="Objects" value={(t.objects ?? 0).toLocaleString()} />
          <Mini label="Recovery points" value={t.recovery_points} />
          <Mini label="Vaults" value={t.vaults?.length ?? 0} />
        </div>
      </Card>

      <Card>
        <div className="spread" style={{ marginBottom: 10 }}>
          <h3 style={{ margin: 0 }}>Users</h3>
          <button className="btn primary sm" onClick={newUser}><Icon name="user" size={14} /> Add user</button>
        </div>
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
        <Stat label="Tenants" value={d.totals.tenants} />
        <Stat label="Objects protected" value={(d.totals.objects || 0).toLocaleString()} />
        <Stat label="Data protected" value={bytes(d.totals.bytes || 0)} />
        <Stat label="Monthly revenue" value={money(d.totals.monthly_revenue || 0)} />
      </div>
      <Card>
        <h3 style={{ marginTop: 0 }}>Per-tenant usage & billing</h3>
        <table className="table">
          <thead><tr><th>Tenant</th><th>Plan</th><th>Users</th><th>Sources</th><th>Objects</th><th>Data</th><th>Recovery pts</th><th>Monthly</th></tr></thead>
          <tbody>
            {d.tenants.map((t: any) => (
              <tr key={t.id}>
                <td style={{ fontWeight: 600 }}>{t.name}{t.status !== "active" ? <span className="faint"> · {t.status}</span> : ""}</td>
                <td><Pill tone="info">{t.plan}</Pill></td>
                <td>{t.users}</td><td>{t.sources + t.agents}</td>
                <td>{(t.objects || 0).toLocaleString()}</td>
                <td>{bytes(t.used_bytes || 0)}</td>
                <td>{t.recovery_points}</td>
                <td style={{ fontWeight: 600 }}>{money(t.monthly_cost || 0)}</td>
              </tr>
            ))}
            {d.tenants.length === 0 && <tr><td colSpan={8} className="muted">No tenants.</td></tr>}
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
          options: ["control-plane", "storage", "worker", "edge"].map((v) => ({ label: v, value: v })) },
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
          options: ["control-plane", "storage", "worker", "edge"].map((v) => ({ label: v, value: v })) },
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

  const storageSvcs = svcs.filter((x) => x.category === "storage");
  const emailSvcs = svcs.filter((x) => x.category === "email");

  return (
    <>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Node fleet</h3>
        <button className="btn primary sm" onClick={registerNode}><Icon name="server" size={14} /> Register node</button>
      </div>
      <div className="grid grid-2">
        {nodes.map((n) => {
          const st = n.telemetry?.storage;
          const mem = n.telemetry?.memory;
          const pct = (u: any) => u && u.total ? Math.round((u.used / u.total) * 100) : 0;
          return (
            <Card key={n.id}>
              <div className="spread" style={{ marginBottom: 10 }}>
                <div className="row" style={{ gap: 10 }}>
                  <div className="result-icon" style={{ width: 34, height: 34, background: "var(--inset)", color: n.online ? "#35d0a5" : "#8a94a7" }}>
                    <Icon name="server" size={18} />
                  </div>
                  <div>
                    <div style={{ fontWeight: 700 }}>{n.name} {n.is_self && <span className="faint" style={{ fontWeight: 400, fontSize: 11 }}>· this node</span>}</div>
                    <div className="faint" style={{ fontSize: 11.5 }}>{n.role}{n.region ? ` · ${n.region}` : ""}</div>
                  </div>
                </div>
                <div className="row" style={{ gap: 6 }}>
                  <Pill tone={n.online ? "ok" : "warn"}>{n.online ? "Online" : "Offline"}</Pill>
                  <Pill tone={n.status === "active" ? "info" : "warn"}>{n.status}</Pill>
                </div>
              </div>
              {st && (
                <div style={{ marginBottom: 8 }}>
                  <div className="spread faint" style={{ fontSize: 11.5, marginBottom: 4 }}>
                    <span>Storage</span><span>{bytes(st.used)} / {bytes(st.total)} ({pct(st)}%)</span>
                  </div>
                  <div style={{ height: 6, borderRadius: 999, background: "var(--inset)", overflow: "hidden" }}>
                    <div style={{ width: `${pct(st)}%`, height: "100%", background: pct(st) > 90 ? "var(--danger-c,#f2545b)" : "linear-gradient(90deg,#4f7cff,#35d0a5)" }} />
                  </div>
                </div>
              )}
              <div className="row" style={{ gap: 14, flexWrap: "wrap", fontSize: 11.5 }} >
                {mem && <span className="faint">Mem {bytes(mem.used)}/{bytes(mem.total)}</span>}
                {n.telemetry?.load && <span className="faint">Load {n.telemetry.load.join(" ")}{n.telemetry.cpus ? ` · ${n.telemetry.cpus} vCPU` : ""}</span>}
                {n.telemetry?.recovery_points != null && <span className="faint">{n.telemetry.recovery_points.toLocaleString()} recovery pts</span>}
                {n.endpoint && <span className="faint">{n.endpoint}</span>}
              </div>
              <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: "wrap" }}>
                <Pill tone={n.storage_service ? "info" : "warn"}>
                  <Icon name="database" size={11} /> {n.storage_service || "Storage: default"}
                </Pill>
                <Pill tone={n.email_service ? "info" : "warn"}>
                  <Icon name="mail" size={11} /> {n.email_service || "Email: default"}
                </Pill>
              </div>
              <div className="stack" style={{ gap: 8, marginTop: 12, paddingTop: 12, borderTop: "1px solid var(--border-soft)" }}>
                <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em" }}>Assigned services</div>
                <label className="row" style={{ gap: 8, alignItems: "center" }}>
                  <Icon name="database" size={13} />
                  <span className="faint" style={{ fontSize: 12, width: 52 }}>Storage</span>
                  <select className="input sm flex1" value={n.storage_service_id || ""}
                          onChange={(e) => setNodeService(n, { storage_service_id: e.target.value })}>
                    <option value="">Default (env / local)</option>
                    {storageSvcs.map((x) => <option key={x.id} value={x.id}>{x.name}{x.configured ? "" : " (incomplete)"}</option>)}
                  </select>
                </label>
                <label className="row" style={{ gap: 8, alignItems: "center" }}>
                  <Icon name="mail" size={13} />
                  <span className="faint" style={{ fontSize: 12, width: 52 }}>Email</span>
                  <select className="input sm flex1" value={n.email_service_id || ""}
                          onChange={(e) => setNodeService(n, { email_service_id: e.target.value })}>
                    <option value="">Default</option>
                    {emailSvcs.map((x) => <option key={x.id} value={x.id}>{x.name}{x.configured ? "" : " (incomplete)"}</option>)}
                  </select>
                </label>
              </div>
              <div className="row" style={{ gap: 8, marginTop: 12 }}>
                <button className="btn ghost sm" onClick={() => editNode(n)}>Edit</button>
                {!n.is_self && <button className="btn danger sm" onClick={() => removeNode(n)}>Remove</button>}
              </div>
            </Card>
          );
        })}
      </div>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
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

interface PricingCfg {
  currency: string;
  protection_price_per_tb_month: number;
  cloud_price_per_tb_month: number;
  s3_price_per_tb_month: number;
  azure_price_per_tb_month: number;
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

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Recurring pricing (per TB · month)</h3>
        <div className="grid grid-4" style={{ gap: 12 }}>
          <PriceField label="Data protection" value={p.protection_price_per_tb_month} onChange={(v) => set("protection_price_per_tb_month", num(v))} />
          <PriceField label="Arkive Cloud storage" value={p.cloud_price_per_tb_month} onChange={(v) => set("cloud_price_per_tb_month", num(v))} />
          <PriceField label="AWS S3 estimate" value={p.s3_price_per_tb_month} onChange={(v) => set("s3_price_per_tb_month", num(v))} />
          <PriceField label="Azure estimate" value={p.azure_price_per_tb_month} onChange={(v) => set("azure_price_per_tb_month", num(v))} />
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
interface SourceSlot { type: string; label: string; kind: string; keys: string[]; enabled: boolean; config_object_id: string | null; configured: boolean }
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

  async function setSource(s: SourceSlot, patch: { enabled?: boolean; config_object_id?: string | null }) {
    try { await api.put(`/admin/sources/${s.type}`, patch); await load(); } catch { flash("Update failed"); }
  }

  return (
    <>
      <Card>
        <h3 style={{ marginTop: 0 }}>Sources</h3>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>Enable each integration and link the configuration object that supplies its credentials.</div>
        <table className="table">
          <thead><tr><th>Source</th><th>Required keys</th><th>Enabled</th><th>Configuration</th><th>Status</th></tr></thead>
          <tbody>
            {sources.map((s) => (
              <tr key={s.type}>
                <td><div style={{ fontWeight: 600 }}>{s.label}</div><div className="faint" style={{ fontSize: 11 }}>{s.type} · {s.kind}</div></td>
                <td className="faint" style={{ fontSize: 11.5 }}>{s.keys.join(", ")}</td>
                <td><input type="checkbox" checked={s.enabled} onChange={(e) => setSource(s, { enabled: e.target.checked })} /></td>
                <td>
                  <select className="input sm" value={s.config_object_id || ""} onChange={(e) => setSource(s, { config_object_id: e.target.value })}>
                    <option value="">— none —</option>
                    {objects.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
                  </select>
                </td>
                <td><Pill tone={s.configured ? "ok" : "warn"}>{s.configured ? "Configured" : "Not set"}</Pill></td>
              </tr>
            ))}
          </tbody>
        </table>
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
      const r = await api.post<{ ok: boolean; error?: string | null }>(`/admin/service-objects/${o.id}/test`, payload);
      flash(r.ok ? (o.category === "email" ? "Test email sent" : "Storage reachable — write/read OK") : `Test failed: ${r.error || "unknown error"}`);
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
