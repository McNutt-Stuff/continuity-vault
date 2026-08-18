import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Card, Pill } from "../components/ui";
import { Icon } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { confirmDialog, notify } from "../components/dialog";

interface Account { id: string; connector_type: string; account_label: string; }
interface Vault { id: string; name: string; }
interface StorageTarget {
  id: string; kind: string; label: string; detail?: string;
  state?: string; online?: boolean;
}
interface Mapping {
  id: string; name: string; source_type: string; source_display: string;
  source_label: string; is_agent: boolean; vault_id: string;
  vault_name: string | null; connector_account_id: string | null;
  account_label: string | null; sensitivity: string; destinations: string[];
  index_fields: string[]; available_fields: string[];
  last_backup_at: string | null; last_object_count: number; last_recoverable: boolean;
}
interface ActivityEvent {
  kind: string; source: string; destination?: string; object_count?: number;
  status: string; at?: string; command?: string;
}
interface Activity {
  in_flight: ActivityEvent[]; events: ActivityEvent[];
  summary: { recent: number; pending: number; queued_agents: number };
}

export default function Mappings() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [targets, setTargets] = useState<StorageTarget[]>([]);
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [toast, setToast] = useState("");

  // New-mapping form
  const [accountId, setAccountId] = useState("");
  const [vaultId, setVaultId] = useState("");
  const [dests, setDests] = useState<string[]>(["cv-cloud"]);

  // Inline routing editor for an existing mapping.
  const [editId, setEditId] = useState<string | null>(null);
  const [editDests, setEditDests] = useState<string[]>([]);
  const [editFields, setEditFields] = useState<string[]>([]);
  const [activity, setActivity] = useState<Activity | null>(null);

  async function load() {
    const [acc, tenant, coll, tgts] = await Promise.all([
      api.get<Account[]>("/connectors/accounts"),
      api.get<{ vaults: Vault[] }>("/tenant"),
      api.get<Mapping[]>("/collections"),
      api.get<StorageTarget[]>("/tenant/storage-targets"),
    ]);
    setAccounts(acc);
    setVaults(tenant.vaults);
    setMappings(coll);
    setTargets(tgts);
    if (!vaultId && tenant.vaults[0]) setVaultId(tenant.vaults[0].id);
    if (!accountId && acc[0]) setAccountId(acc[0].id);
    try { setActivity(await api.get<Activity>("/activity")); } catch { /* ignore */ }
  }
  useEffect(() => {
    void load();
    const t = setInterval(() => { api.get<Activity>("/activity").then(setActivity).catch(() => {}); }, 6000);
    return () => clearInterval(t);
  }, []);

  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }

  function destLabel(id: string): string {
    const t = targets.find((x) => x.id === id);
    if (t) return t.label;
    if (id === "cv-cloud") return "Arkive Cloud";
    if (id === "customer-s3") return "Customer S3";
    if (id.startsWith("appliance:")) return "Appliance (removed)";
    return id;
  }

  function toggleDest(id: string) {
    setDests((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  async function addMapping() {
    const acct = accounts.find((a) => a.id === accountId);
    if (!acct || !vaultId || dests.length === 0) {
      return flash("Pick a source, a vault, and at least one destination");
    }
    const vault = vaults.find((v) => v.id === vaultId);
    try {
      await api.post("/collections", {
        vault_id: vaultId,
        name: `${acct.account_label} → ${vault?.name ?? "vault"}`,
        source_type: acct.connector_type,
        connector_account_id: acct.id,
        destinations: dests,
      });
      flash("Mapping created");
      await load();
    } catch (e) { flash((e as ApiError).message); }
  }

  async function remove(m: Mapping) {
    const ok = await confirmDialog({
      title: "Remove mapping",
      message: `Remove "${m.name}"? This deletes the routing and its backup history index. Stored recovery points are not deleted.`,
      confirmLabel: "Remove mapping",
    });
    if (!ok) return;
    try { await api.del(`/collections/${m.id}`); flash("Mapping removed"); await load(); }
    catch (e) {
      await notify({ title: "Couldn't remove mapping", message: (e as ApiError).message, tone: "danger" });
    }
  }

  function startEdit(m: Mapping) {
    setEditId(m.id);
    setEditDests(m.destinations && m.destinations.length ? [...m.destinations] : ["cv-cloud"]);
    // Default the indexed fields to the mapping's override, or all available.
    setEditFields(m.index_fields && m.index_fields.length ? [...m.index_fields] : [...m.available_fields]);
  }

  function toggleEditDest(id: string) {
    setEditDests((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  function toggleEditField(id: string) {
    setEditFields((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  async function saveRouting(m: Mapping) {
    if (editDests.length === 0) return flash("Pick at least one destination");
    try {
      await api.put(`/collections/${m.id}`, { destinations: editDests, index_fields: editFields });
      setEditId(null);
      flash("Mapping updated");
      await load();
    } catch (e) {
      await notify({ title: "Couldn't update mapping", message: (e as ApiError).message, tone: "danger" });
    }
  }

  async function syncNow(m: Mapping) {
    try {
      const res = await api.post<{ kind: string; queued_agents?: number; object_count?: number }>(`/collections/${m.id}/sync`, {});
      if (res.kind === "agent") flash(`Queued sync on ${res.queued_agents} agent(s)`);
      else flash(`Synced ${res.object_count ?? 0} objects`);
      await load();
    } catch (e) {
      await notify({ title: "Sync failed", message: (e as ApiError).message, tone: "danger" });
    }
  }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>Map sources to vaults</h2>
        <div className="muted" style={{ fontSize: 13, marginBottom: 14 }}>
          Route each source into a vault and choose exactly where that mapping stores its data —
          the Arkive cloud, a specific appliance, or your own cloud bucket. A source can feed many
          vaults and a vault can hold many sources. Backups run automatically when the source syncs
          (connector poll or desktop-agent push); this page only defines the routing.
        </div>
        <div className="grid grid-3" style={{ gap: 12, alignItems: "end" }}>
          <label className="stack">
            <span className="faint" style={{ fontSize: 11.5 }}>Source</span>
            <select className="input" value={accountId} onChange={(e) => setAccountId(e.target.value)}>
              {accounts.length === 0 && <option value="">No sources linked</option>}
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>{a.account_label} ({a.connector_type})</option>
              ))}
            </select>
          </label>
          <label className="stack">
            <span className="faint" style={{ fontSize: 11.5 }}>Vault</span>
            <select className="input" value={vaultId} onChange={(e) => setVaultId(e.target.value)}>
              {vaults.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
            </select>
          </label>
          <button className="btn primary" onClick={addMapping}>
            <Icon name="link" size={15} /> Add mapping
          </button>
        </div>
        <div className="stack" style={{ gap: 6, marginTop: 14 }}>
          <span className="faint" style={{ fontSize: 11.5 }}>Destinations</span>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            {targets.map((t) => (
              <span
                key={t.id}
                className={`chip ${dests.includes(t.id) ? "active" : ""}`}
                onClick={() => toggleDest(t.id)}
                title={t.detail}
              >
                <Icon name={t.kind === "appliance" ? "server" : "cloud"} size={13} />
                {t.label}
                {t.kind === "appliance" && t.online === false && (
                  <span className="faint" style={{ marginLeft: 4 }}>· offline</span>
                )}
              </span>
            ))}
          </div>
        </div>
      </Card>

      {activity && (activity.events.length > 0 || activity.in_flight.length > 0) && (
        <Card style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 10 }}>
            <h3 style={{ margin: 0 }}>Activity</h3>
            <div className="row" style={{ gap: 8 }}>
              {activity.summary.queued_agents > 0 && <Pill tone="warn">{activity.summary.queued_agents} syncing</Pill>}
              {activity.summary.pending > 0 && <Pill tone="info">{activity.summary.pending} sealing</Pill>}
              <Pill tone="ok">{activity.summary.recent} recent</Pill>
            </div>
          </div>
          {activity.in_flight.map((e, i) => (
            <div key={`f${i}`} className="row" style={{ gap: 8, padding: "6px 0", fontSize: 13 }}>
              <span className="spinner-dot" />
              <span style={{ fontWeight: 600 }}>{e.source}</span>
              <span className="faint">collecting on desktop agent…</span>
            </div>
          ))}
          {activity.events.slice(0, 8).map((e, i) => (
            <div key={`e${i}`} className="row" style={{ gap: 8, padding: "6px 0", fontSize: 13, flexWrap: "wrap" }}>
              <Icon name={e.destination?.startsWith("appliance") ? "server" : "cloud"} size={13} />
              <span style={{ fontWeight: 600 }}>{e.source}</span>
              <span className="faint">→ {destLabel(e.destination || "cv-cloud")}</span>
              <span className="faint">· {e.object_count ?? 0} objects</span>
              <Pill tone={e.status === "recoverable" ? "ok" : "warn"}>{e.status}</Pill>
              <span className="faint" style={{ marginLeft: "auto", fontSize: 11 }}>{fmtTime(e.at)}</span>
            </div>
          ))}
        </Card>
      )}

      <Card>
        <h3 style={{ marginBottom: 12 }}>Mappings</h3>
        {mappings.length === 0 && <div className="muted">No mappings yet. Add one above.</div>}
        {mappings.map((m) => {
          const brand = brandForSource(m.source_type);
          const editing = editId === m.id;
          return (
            <div key={m.id} className="result-row" style={{ alignItems: "flex-start" }}>
              <div className="result-icon" style={{ background: brand ? "#0e1524" : "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
                {brand ? <BrandIcon name={brand} size={18} /> : <Icon name="database" size={17} />}
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>
                  {m.source_label} <span className="faint">→</span> {m.vault_name ?? m.vault_id}
                </div>
                <div className="faint" style={{ fontSize: 11.5, marginTop: 2 }}>
                  {m.source_display}{m.is_agent ? " · desktop agent" : ""}
                  {m.last_backup_at
                    ? ` · last sync ${fmtTime(m.last_backup_at)} · ${m.last_object_count} objects ${m.last_recoverable ? "✓" : "(sealing)"}`
                    : " · never synced"}
                </div>
                {!editing && (
                  <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                    {(m.destinations || []).map((d) => (
                      <Pill key={d} tone={d.startsWith("appliance") ? "ok" : "info"}>{destLabel(d)}</Pill>
                    ))}
                    {(m.index_fields && m.index_fields.length ? m.index_fields : m.available_fields)
                      .slice(0, 6).map((f) => (
                        <span key={f} className="chip" style={{ padding: "1px 8px", fontSize: 10.5 }}>{f}</span>
                      ))}
                    {m.sensitivity === "restricted" && <Pill tone="danger">restricted</Pill>}
                  </div>
                )}
                {editing && (
                  <div className="stack" style={{ gap: 10, marginTop: 8 }}>
                    <div className="stack" style={{ gap: 6 }}>
                      <span className="faint" style={{ fontSize: 11.5 }}>Route this source to</span>
                      <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                        {targets.map((t) => (
                          <span
                            key={t.id}
                            className={`chip ${editDests.includes(t.id) ? "active" : ""}`}
                            onClick={() => toggleEditDest(t.id)}
                            title={t.detail}
                          >
                            <Icon name={t.kind === "appliance" ? "server" : "cloud"} size={13} />
                            {t.label}
                            {t.kind === "appliance" && t.online === false && (
                              <span className="faint" style={{ marginLeft: 4 }}>· offline</span>
                            )}
                          </span>
                        ))}
                      </div>
                    </div>
                    {m.available_fields.length > 0 && (
                      <div className="stack" style={{ gap: 6 }}>
                        <span className="faint" style={{ fontSize: 11.5 }}>
                          What to index (discrete metadata shown in search — never file/message contents)
                        </span>
                        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                          {m.available_fields.map((f) => (
                            <span
                              key={f}
                              className={`chip ${editFields.includes(f) ? "active" : ""}`}
                              onClick={() => toggleEditField(f)}
                            >
                              {editFields.includes(f) && <Icon name="check" size={12} />} {f}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                    <div className="row" style={{ gap: 8 }}>
                      <button className="btn sm primary" onClick={() => saveRouting(m)}>Save</button>
                      <button className="btn sm ghost" onClick={() => setEditId(null)}>Cancel</button>
                    </div>
                  </div>
                )}
              </div>
              {!editing && (
                <>
                  <button className="btn sm primary" onClick={() => syncNow(m)}>Sync now</button>
                  <button className="btn sm" onClick={() => startEdit(m)}>Edit</button>
                  <button className="btn sm ghost" onClick={() => remove(m)}>Remove</button>
                </>
              )}
            </div>
          );
        })}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "never";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
