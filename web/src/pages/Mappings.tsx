import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Card, Pill, bytes } from "../components/ui";
import { Icon } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { confirmDialog, notify } from "../components/dialog";

interface Account { id: string; connector_type: string; account_label: string; }
interface Agent { id: string; name: string; hostname: string; collectors: string[]; }
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
  kind: string; collection_id?: string; source: string; source_type?: string;
  destination?: string; object_count?: number; total_bytes?: number;
  status: string; at?: string; command?: string;
}
interface Activity {
  in_flight: ActivityEvent[]; events: ActivityEvent[];
  summary: { recent: number; pending: number; queued_agents: number };
}

export default function Mappings() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [targets, setTargets] = useState<StorageTarget[]>([]);
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [toast, setToast] = useState("");

  // New-mapping form. sourceSel encodes the chosen source:
  //   "acct:<accountId>"  (cloud connector account)
  //   "agent:<agentId>:<collector>"  (agent-discovered collector)
  const [sourceSel, setSourceSel] = useState("");
  const [vaultId, setVaultId] = useState("");
  const [dests, setDests] = useState<string[]>(["cv-cloud"]);

  // Inline routing editor for an existing mapping.
  const [editId, setEditId] = useState<string | null>(null);
  const [editDests, setEditDests] = useState<string[]>([]);
  const [editFields, setEditFields] = useState<string[]>([]);
  const [activity, setActivity] = useState<Activity | null>(null);
  // collection_id -> epoch ms when a sync was triggered (live indicator).
  const [syncing, setSyncing] = useState<Record<string, number>>({});
  // collection_id -> whether the activity window is expanded.
  const [openActivity, setOpenActivity] = useState<Record<string, boolean>>({});

  async function load() {
    const [acc, ags, tenant, coll, tgts] = await Promise.all([
      api.get<Account[]>("/connectors/accounts"),
      api.get<Agent[]>("/agents").catch(() => [] as Agent[]),
      api.get<{ vaults: Vault[] }>("/tenant"),
      api.get<Mapping[]>("/collections"),
      api.get<StorageTarget[]>("/tenant/storage-targets"),
    ]);
    setAccounts(acc);
    setAgents(ags);
    setVaults(tenant.vaults);
    setMappings(coll);
    setTargets(tgts);
    if (!vaultId && tenant.vaults[0]) setVaultId(tenant.vaults[0].id);
    try { setActivity(await api.get<Activity>("/activity")); } catch { /* ignore */ }
  }
  useEffect(() => {
    void load();
    const t = setInterval(() => { api.get<Activity>("/activity").then(setActivity).catch(() => {}); }, 4000);
    return () => clearInterval(t);
  }, []);

  // Clear a mapping's "syncing" indicator once a fresh event lands for it.
  useEffect(() => {
    if (!activity) return;
    setSyncing((cur) => {
      const next = { ...cur };
      let changed = false;
      for (const [cid, ts] of Object.entries(cur)) {
        const landed = activity.events.some(
          (e) => e.collection_id === cid && e.at && new Date(e.at).getTime() >= ts - 2000);
        const stale = Date.now() - ts > 90000; // give up after 90s
        if (landed || stale) { delete next[cid]; changed = true; }
      }
      return changed ? next : cur;
    });
  }, [activity]);

  function eventsFor(collectionId: string): ActivityEvent[] {
    return (activity?.events || []).filter((e) => e.collection_id === collectionId).slice(0, 4);
  }

  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }

  function destLabel(id: string): string {
    const t = targets.find((x) => x.id === id);
    if (t) return t.label;
    if (id === "cv-cloud") return "Arkive Cloud";
    if (id === "customer-s3") return "Customer S3";
    if (id.startsWith("store:")) return "Appliance storage (removed)";
    if (id.startsWith("appliance:")) return "Appliance (removed)";
    return id;
  }

  const isApplianceDest = (d: string) => d.startsWith("appliance") || d.startsWith("store:");

  function toggleDest(id: string) {
    setDests((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  async function addMapping() {
    if (!sourceSel || !vaultId || dests.length === 0) {
      return flash("Pick a source, a vault, and at least one destination");
    }
    const vault = vaults.find((v) => v.id === vaultId);
    try {
      if (sourceSel.startsWith("agent:")) {
        const [, agentId, collector] = sourceSel.split(":");
        const agent = agents.find((a) => a.id === agentId);
        await api.post("/collections", {
          vault_id: vaultId,
          name: `${collector} (${agent?.hostname || agent?.name || "agent"})`,
          source_type: collector,
          agent_id: agentId,
          sensitivity: "restricted",
          destinations: dests,
        });
      } else {
        const acct = accounts.find((a) => a.id === sourceSel.replace(/^acct:/, ""));
        if (!acct) return flash("Pick a source");
        await api.post("/collections", {
          vault_id: vaultId,
          name: `${acct.account_label} → ${vault?.name ?? "vault"}`,
          source_type: acct.connector_type,
          connector_account_id: acct.id,
          destinations: dests,
        });
      }
      flash("Source added");
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
    setSyncing((cur) => ({ ...cur, [m.id]: Date.now() }));
    setOpenActivity((cur) => ({ ...cur, [m.id]: true }));
    try {
      const res = await api.post<{ kind: string; queued_agents?: number; object_count?: number }>(`/collections/${m.id}/sync`, {});
      if (res.kind === "agent") flash(`Queued sync on ${res.queued_agents} agent(s)`);
      else flash(`Synced ${res.object_count ?? 0} objects`);
      await load();
    } catch (e) {
      setSyncing((cur) => { const n = { ...cur }; delete n[m.id]; return n; });
      await notify({ title: "Sync failed", message: (e as ApiError).message, tone: "danger" });
    }
  }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>Map sources to vaults</h2>
        <div className="muted" style={{ fontSize: 13, marginBottom: 14 }}>
          Add a source and route it into a vault, choosing where it stores its data — the Arkive
          cloud, an appliance storage, or your own cloud bucket. Cloud connectors and desktop-agent
          collectors (e.g. 1Password, discovered from a linked agent) are both added here in the
          portal; agents never create sources on their own. Backups run on sync.
        </div>
        <div className="grid grid-3" style={{ gap: 12, alignItems: "end" }}>
          <label className="stack">
            <span className="faint" style={{ fontSize: 11.5 }}>Source</span>
            <select className="input" value={sourceSel} onChange={(e) => setSourceSel(e.target.value)}>
              <option value="">Choose a source…</option>
              {accounts.length > 0 && (
                <optgroup label="Cloud connectors">
                  {accounts.map((a) => (
                    <option key={a.id} value={`acct:${a.id}`}>{a.account_label} ({a.connector_type})</option>
                  ))}
                </optgroup>
              )}
              {agents.some((a) => (a.collectors || []).length) && (
                <optgroup label="Desktop agents">
                  {agents.flatMap((a) => (a.collectors || []).map((c) => (
                    <option key={`${a.id}:${c}`} value={`agent:${a.id}:${c}`}>
                      {c} — {a.hostname || a.name}
                    </option>
                  )))}
                </optgroup>
              )}
            </select>
          </label>
          <label className="stack">
            <span className="faint" style={{ fontSize: 11.5 }}>Vault</span>
            <select className="input" value={vaultId} onChange={(e) => setVaultId(e.target.value)}>
              {vaults.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
            </select>
          </label>
          <button className="btn primary" onClick={addMapping}>
            <Icon name="link" size={15} /> Add source
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

      {activity && (activity.summary.recent > 0 || activity.summary.queued_agents > 0) && (
        <div className="row" style={{ gap: 8, marginBottom: 12, justifyContent: "flex-end" }}>
          {activity.summary.queued_agents > 0 && <Pill tone="warn">{activity.summary.queued_agents} syncing</Pill>}
          {activity.summary.pending > 0 && <Pill tone="info">{activity.summary.pending} sealing</Pill>}
          <span className="faint" style={{ fontSize: 12, alignSelf: "center" }}>
            Live · full timeline in <a href="/activity">Activity</a>
          </span>
        </div>
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
                      <Pill key={d} tone={isApplianceDest(d) ? "ok" : "info"}>{destLabel(d)}</Pill>
                    ))}
                    {(m.index_fields && m.index_fields.length ? m.index_fields : m.available_fields)
                      .slice(0, 6).map((f) => (
                        <span key={f} className="chip" style={{ padding: "1px 8px", fontSize: 10.5 }}>{f}</span>
                      ))}
                    {m.sensitivity === "restricted" && <Pill tone="danger">restricted</Pill>}
                  </div>
                )}
                {!editing && (syncing[m.id] || eventsFor(m.id).length > 0) && (() => {
                  const evs = eventsFor(m.id);
                  const isSyncing = !!syncing[m.id];
                  const expanded = openActivity[m.id] || isSyncing;
                  const latest = evs[0];
                  return (
                    <div style={{ marginTop: 8 }}>
                      <button
                        className="map-activity-toggle"
                        onClick={() => setOpenActivity((cur) => ({ ...cur, [m.id]: !expanded }))}
                      >
                        <span className="row" style={{ gap: 6, alignItems: "center" }}>
                          {isSyncing ? <span className="spinner-dot" /> : <Icon name="activity" size={13} />}
                          <span style={{ fontWeight: 600, fontSize: 12 }}>
                            {isSyncing ? "Syncing…" : "Activity"}
                          </span>
                          {evs.length > 0 && (
                            <span className="faint" style={{ fontSize: 11.5 }}>
                              · {latest.object_count ?? 0} objects → {destLabel(latest.destination || "cv-cloud")} · {fmtTime(latest.at)}
                            </span>
                          )}
                        </span>
                        <span className="faint" style={{ fontSize: 11 }}>{expanded ? "▲" : "▼"}</span>
                      </button>
                      {expanded && (
                        <div className="map-activity">
                          {isSyncing && (
                            <div className="row" style={{ gap: 8, fontSize: 12.5, alignItems: "center" }}>
                              <span className="spinner-dot" />
                              <span className="faint">
                                {m.is_agent ? "Collecting on desktop agent, then ingesting…" : "Syncing & ingesting…"}
                              </span>
                            </div>
                          )}
                          {evs.length === 0 && !isSyncing && (
                            <span className="faint" style={{ fontSize: 12 }}>No activity yet.</span>
                          )}
                          {evs.map((e, i) => (
                            <div key={i} className="row" style={{ gap: 6, fontSize: 12, flexWrap: "wrap", alignItems: "center" }}>
                              <Icon name={isApplianceDest(e.destination || "") ? "server" : "cloud"} size={12} />
                              <span className="faint">{destLabel(e.destination || "cv-cloud")}</span>
                              <span className="faint">· {e.object_count ?? 0} objects · {bytes(e.total_bytes ?? 0)}</span>
                              <Pill tone={e.status === "recoverable" ? "ok" : "warn"}>
                                {e.status === "recoverable" ? "recoverable" : "sealing"}
                              </Pill>
                              <span className="faint" style={{ marginLeft: "auto" }}>{fmtTime(e.at)}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })()}
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
