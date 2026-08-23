import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Card, Pill, bytes, serverDate, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { confirmDialog, notify } from "../components/dialog";
import { FolderPicker, FolderNode } from "../components/FolderPicker";

interface Account { id: string; connector_type: string; account_label: string; account_username?: string | null; }
interface Agent { id: string; name: string; hostname: string; collectors: string[]; }
interface Vault { id: string; name: string; }
interface StorageTarget {
  id: string; kind: string; label: string; detail?: string;
  state?: string; online?: boolean;
}
interface FileConfig {
  roots?: string[]; excludeExts?: string[]; maxSizeBytes?: number;
  excludeFolders?: string[]; includeSpamTrash?: boolean;
  includeCategories?: string[];
}
interface CatalogItem {
  type: string;
  capabilities?: { filterCategories?: { id: string; label: string }[]; browsable?: boolean };
}
interface Mapping {
  id: string; name: string; source_type: string; source_display: string;
  source_label: string; is_agent: boolean; vault_id: string; agent_id: string | null;
  vault_name: string | null; connector_account_id: string | null;
  account_label: string | null; account_username?: string | null; sensitivity: string; destinations: string[];
  index_fields: string[]; available_fields: string[];
  last_backup_at: string | null; last_object_count: number; last_recoverable: boolean;
  offpolicy_points: number;
  backup_interval_minutes: number | null; default_interval_minutes: number;
  last_backup_run_at: string | null;
  supports_since?: boolean; since_date?: string;
  is_picker?: boolean; reminder_days?: number;
  config?: FileConfig;
}
interface ActivityEvent {
  kind: string; collection_id?: string; source: string; source_type?: string;
  destination?: string; object_count?: number; total_bytes?: number;
  status: string; at?: string; command?: string;
}
interface Job {
  id: string; collection_id?: string; source: string; kind: string;
  status: string; processed: number; total: number; message: string;
}
interface Activity {
  in_flight: ActivityEvent[]; events: ActivityEvent[]; jobs: Job[];
  summary: { recent: number; pending: number; queued_agents: number; active_jobs: number };
}

export default function Mappings() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [targets, setTargets] = useState<StorageTarget[]>([]);
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [toast, setToast] = useState("");
  const [loaded, setLoaded] = useState(false);

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
  // -1 = use the global default, 0 = manual only, >0 = every N minutes.
  const [editInterval, setEditInterval] = useState<number>(-1);
  // Gmail: which folders to skip + whether to include Spam/Trash.
  const [editGmailExclude, setEditGmailExclude] = useState<string[]>([]);
  const [editGmailSpamTrash, setEditGmailSpamTrash] = useState<boolean>(false);
  const [editCategories, setEditCategories] = useState<string[]>([]);
  const [editSince, setEditSince] = useState<string>("");
  const [editReminder, setEditReminder] = useState<number>(3);
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  // Endpoint-files folder picker: which mapping/agent it's configuring.
  const [picker, setPicker] = useState<{ agentId: string; mappingId: string; initial: FileConfig } | null>(null);
  // Cloud folder picker (Dropbox / OneDrive / other browsable sources).
  const [cloudPicker, setCloudPicker] = useState<{ accountId: string; mappingId: string; label: string; sourceType: string; initial: FileConfig } | null>(null);
  const [activity, setActivity] = useState<Activity | null>(null);
  // collection_id -> epoch ms when a sync was triggered (live indicator).
  const [syncing, setSyncing] = useState<Record<string, number>>({});
  // collection_id -> whether the activity window is expanded.
  const [openActivity, setOpenActivity] = useState<Record<string, boolean>>({});

  async function load() {
    try {
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
      try { setCatalog(await api.get<CatalogItem[]>("/connectors/catalog")); } catch { /* ignore */ }
      try { setActivity(await api.get<Activity>("/activity")); } catch { /* ignore */ }
    } finally {
      setLoaded(true);
    }
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
        const activeJob = (activity.jobs || []).some((j) => j.collection_id === cid);
        const landed = activity.events.some(
          (e) => e.collection_id === cid && e.at && serverDate(e.at).getTime() >= ts - 2000);
        // A running job supersedes the local spinner; otherwise clear on a fresh
        // event or after a long timeout.
        const stale = Date.now() - ts > 180000;
        if (activeJob || landed || stale) { delete next[cid]; changed = true; }
      }
      return changed ? next : cur;
    });
  }, [activity]);

  function eventsFor(collectionId: string): ActivityEvent[] {
    return (activity?.events || []).filter((e) => e.collection_id === collectionId).slice(0, 40);
  }

  function jobFor(collectionId: string): Job | undefined {
    return (activity?.jobs || []).find((j) => j.collection_id === collectionId);
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
        const created = await api.post<Mapping>("/collections", {
          vault_id: vaultId,
          name: `${collectorLabel(collector)} (${agent?.hostname || agent?.name || "agent"})`,
          source_type: collector,
          agent_id: agentId,
          sensitivity: "restricted",
          destinations: dests,
        });
        await load();
        flash("Source added");
        // Endpoint files need a folder selection before anything is collected.
        if (collector === "endpoint_files") {
          setPicker({ agentId, mappingId: created.id, initial: created.config || {} });
        }
        return;
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
    setEditInterval(m.backup_interval_minutes == null ? -1 : m.backup_interval_minutes);
    setEditGmailExclude([...(m.config?.excludeFolders || [])]);
    setEditGmailSpamTrash(!!m.config?.includeSpamTrash);
    setEditCategories([...(m.config?.includeCategories || [])]);
    setEditSince(m.since_date || "");
    setEditReminder(m.reminder_days ?? 3);
  }

  function toggleEditDest(id: string) {
    setEditDests((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  function toggleEditField(id: string) {
    setEditFields((cur) => cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]);
  }

  function filterCatsFor(sourceType: string): { id: string; label: string }[] {
    return catalog.find((c) => c.type === sourceType)?.capabilities?.filterCategories || [];
  }

  function browsableFor(sourceType: string): boolean {
    return !!catalog.find((c) => c.type === sourceType)?.capabilities?.browsable;
  }

  async function saveRouting(m: Mapping) {
    if (editDests.length === 0) return flash("Pick at least one destination");
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const body: any = {
        destinations: editDests, index_fields: editFields,
        backup_interval_minutes: editInterval,
      };
      if (m.source_type === "gmail") {
        body.config = { ...(m.config || {}), excludeFolders: editGmailExclude, includeSpamTrash: editGmailSpamTrash };
      }
      if (filterCatsFor(m.source_type).length > 0) {
        body.config = { ...(m.config || {}), ...(body.config || {}), includeCategories: editCategories };
      }
      if (m.supports_since) {
        body.config = { ...(m.config || {}), ...(body.config || {}), sinceDate: editSince || "" };
      }
      if (m.is_picker) {
        body.config = { ...(m.config || {}), ...(body.config || {}), reminderDays: editReminder };
      }
      await api.put(`/collections/${m.id}`, body);
      setEditId(null);
      flash("Mapping updated");
      await load();
    } catch (e) {
      await notify({ title: "Couldn't update mapping", message: (e as ApiError).message, tone: "danger" });
    }
  }

  async function prune(m: Mapping) {
    const ok = await confirmDialog({
      title: "Prune off-policy recovery points",
      message: `Delete ${m.offpolicy_points} recovery point(s) stored where "${m.name}" no longer routes? They'll stop appearing as recovery locations. Immutable stored data ages out under retention.`,
      confirmLabel: "Prune copies",
    });
    if (!ok) return;
    try {
      const res = await api.post<{ pruned: number; destinations: string[] }>(`/collections/${m.id}/prune`, {});
      flash(res.pruned > 0
        ? `Pruned ${res.pruned} off-policy recovery point(s)${res.destinations.length ? ` from ${res.destinations.join(", ")}` : ""}`
        : "No off-policy recovery points to prune");
      await load();
    } catch (e) {
      await notify({ title: "Couldn't prune", message: (e as ApiError).message, tone: "danger" });
    }
  }

  async function syncNow(m: Mapping) {
    setSyncing((cur) => ({ ...cur, [m.id]: Date.now() }));
    setOpenActivity((cur) => ({ ...cur, [m.id]: true }));
    try {
      const res = await api.post<{ kind: string; queued_agents?: number; object_count?: number; job_id?: string }>(`/collections/${m.id}/sync`, {});
      if (res.kind === "agent") flash(`Queued sync on ${res.queued_agents} agent(s)`);
      else if (res.job_id) flash("Sync started — progress below");
      else flash(`Synced ${res.object_count ?? 0} objects`);
      await load();
    } catch (e) {
      setSyncing((cur) => { const n = { ...cur }; delete n[m.id]; return n; });
      await notify({ title: "Sync failed", message: (e as ApiError).message, tone: "danger" });
    }
  }

  if (!loaded && mappings.length === 0) return <Loading label="Loading data map…" />;

  return (
    <>
      {loaded && targets.length === 0 && (
        <Card style={{ marginBottom: 16, borderColor: "var(--warn)" }}>
          <div className="row" style={{ gap: 12, alignItems: "center" }}>
            <div className="result-icon" style={{ background: "var(--inset)", color: "var(--warn)" }}>
              <Icon name="alert" size={18} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>Choose where to protect your data first</div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                You haven't enabled any storage destinations. Pick one in <a href="/onboarding">Protection Setup</a> before mapping any data.
              </div>
            </div>
            <a className="btn primary sm" href="/onboarding"><Icon name="grid" size={13} /> Protection Setup</a>
          </div>
        </Card>
      )}
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
                    <option key={a.id} value={`acct:${a.id}`}>{a.account_label}{a.account_username && a.account_username !== a.account_label ? ` (${a.account_username})` : ""} · {a.connector_type}</option>
                  ))}
                </optgroup>
              )}
              {agents.some((a) => (a.collectors || []).length) && (
                <optgroup label="Desktop agents">
                  {agents.flatMap((a) => (a.collectors || []).map((c) => (
                    <option key={`${a.id}:${c}`} value={`agent:${a.id}:${c}`}>
                      {collectorLabel(c)} — {a.hostname || a.name}
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

      {activity && (activity.summary.recent > 0 || activity.summary.queued_agents > 0 || activity.summary.active_jobs > 0) && (
        <div className="row" style={{ gap: 8, marginBottom: 12, justifyContent: "flex-end" }}>
          {activity.summary.active_jobs > 0 && <Pill tone="warn">{activity.summary.active_jobs} running</Pill>}
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
              <div className="result-icon" style={{ background: brand ? "var(--inset)" : "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
                {brand ? <BrandIcon name={brand} size={18} /> : <Icon name="database" size={17} />}
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>
                  {m.source_label}{m.account_username && m.account_username !== m.source_label ? <span className="faint" style={{ fontWeight: 400 }}> ({m.account_username})</span> : null} <span className="faint">→</span> {m.vault_name ?? m.vault_id}
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
                    <Pill tone={m.backup_interval_minutes === 0 ? "warn" : "info"}>
                      <Icon name="clock" size={11} /> {scheduleLabel(m)}
                    </Pill>
                    {m.source_type === "endpoint_files" && (
                      <Pill tone={(m.config?.roots?.length || 0) > 0 ? "info" : "warn"}>
                        <Icon name="database" size={11} /> {m.config?.roots?.length || 0} folders
                      </Pill>
                    )}
                    {!m.is_agent && browsableFor(m.source_type) && (m.config?.roots?.length || 0) > 0 && (
                      <Pill tone="info">
                        <Icon name="database" size={11} /> {m.config?.roots?.length} folders
                      </Pill>
                    )}
                    {m.source_type === "gmail" && (m.config?.excludeFolders?.length || 0) > 0 && (
                      <Pill tone="warn">
                        <Icon name="mail" size={11} /> skipping {m.config?.excludeFolders?.length}
                      </Pill>
                    )}
                    {(m.index_fields && m.index_fields.length ? m.index_fields : m.available_fields)
                      .slice(0, 6).map((f) => (
                        <span key={f} className="chip" style={{ padding: "1px 8px", fontSize: 10.5 }}>{f}</span>
                      ))}
                    {m.sensitivity === "restricted" && <Pill tone="danger">restricted</Pill>}
                  </div>
                )}
                {!editing && (() => {
                  const evs = eventsFor(m.id);
                  const job = jobFor(m.id);
                  const isSyncing = !!syncing[m.id] || !!job;
                  const expanded = openActivity[m.id] || isSyncing;
                  const latest = evs[0];
                  const pct = job && job.total > 0 ? Math.min(100, (job.processed / job.total) * 100) : 0;
                  return (
                    <div style={{ marginTop: 8 }}>
                      <button
                        className="map-activity-toggle"
                        onClick={() => setOpenActivity((cur) => ({ ...cur, [m.id]: !expanded }))}
                      >
                        <span className="row" style={{ gap: 6, alignItems: "center" }}>
                          {isSyncing ? <span className="spinner-dot" /> : <Icon name="activity" size={13} />}
                          <span style={{ fontWeight: 600, fontSize: 12 }}>
                            {job ? (job.message || "Syncing…") : isSyncing ? "Syncing…" : "Activity"}
                          </span>
                          {!job && evs.length > 0 && (
                            <span className="faint" style={{ fontSize: 11.5 }}>
                              · {latest.object_count ?? 0} objects → {destLabel(latest.destination || "cv-cloud")} · {fmtTime(latest.at)}
                            </span>
                          )}
                          {!job && !isSyncing && evs.length === 0 && (
                            <span className="faint" style={{ fontSize: 11.5 }}>
                              · {m.last_backup_at ? `last sync ${fmtTime(m.last_backup_at)}` : "no activity yet"}
                            </span>
                          )}
                        </span>
                        <span className="faint" style={{ fontSize: 11 }}>{expanded ? "▲" : "▼"}</span>
                      </button>
                      {expanded && (
                        <div className="map-activity">
                          {job && (
                            <div className="stack" style={{ gap: 6 }}>
                              <div className="spread faint" style={{ fontSize: 12 }}>
                                <span>{job.message || "Working…"}</span>
                                {job.total > 0 && <span>{job.processed}/{job.total}</span>}
                              </div>
                              <div className="progress">
                                <span style={{ width: job.total > 0 ? `${pct}%` : "40%", opacity: job.total > 0 ? 1 : 0.5 }} />
                              </div>
                            </div>
                          )}
                          {!job && isSyncing && (
                            <div className="row" style={{ gap: 8, fontSize: 12.5, alignItems: "center" }}>
                              <span className="spinner-dot" />
                              <span className="faint">
                                {m.is_agent ? "Collecting on desktop agent, then ingesting…" : "Syncing & ingesting…"}
                              </span>
                            </div>
                          )}
                          {!job && evs.length === 0 && !isSyncing && (
                            <span className="faint" style={{ fontSize: 12 }}>No activity yet.</span>
                          )}
                          {evs.length > 0 && (
                            <>
                              <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: ".06em", margin: "2px 0 4px" }}>
                                Recent recovery points{evs.length >= 40 ? " (latest 40)" : ` (${evs.length})`}
                              </div>
                              <div style={{ maxHeight: 200, overflowY: "auto" }} className="stack">
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
                            </>
                          )}
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
                    <div className="stack" style={{ gap: 6 }}>
                      <label className="row" style={{ gap: 8, alignItems: "center", cursor: "pointer" }}>
                        <input type="checkbox"
                               checked={editInterval !== 0}
                               onChange={(e) => setEditInterval(e.target.checked ? -1 : 0)} />
                        <span className="faint" style={{ fontSize: 11.5 }}>Back up automatically on a schedule</span>
                      </label>
                      {editInterval !== 0 ? (
                        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                          <select style={{ padding: "5px 8px", borderRadius: 6 }}
                                  value={editInterval}
                                  onChange={(e) => setEditInterval(Number(e.target.value))}>
                            {INTERVAL_OPTIONS.filter((o) => o.value !== 0).map((o) => (
                              <option key={o.value} value={o.value}>
                                {o.value === -1 ? `Use default (${intervalText(m.default_interval_minutes)})` : o.label}
                              </option>
                            ))}
                          </select>
                          <span className="faint" style={{ fontSize: 11 }}>
                            Runs in the background {intervalText(editInterval < 0 ? m.default_interval_minutes : editInterval)}.
                          </span>
                        </div>
                      ) : (
                        <span className="faint" style={{ fontSize: 11 }}>
                          Automatic backups are off — this source only backs up when you click Sync/Back up now.
                        </span>
                      )}
                    </div>
                    {m.supports_since && (
                      <div className="stack" style={{ gap: 6 }}>
                        <span className="faint" style={{ fontSize: 11.5 }}>
                          Back up history from (leave blank = entire library)
                        </span>
                        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                          <input type="date" value={editSince}
                                 onChange={(e) => setEditSince(e.target.value)}
                                 style={{ padding: "5px 8px", borderRadius: 6 }} />
                          {editSince && (
                            <button className="btn ghost sm" onClick={() => setEditSince("")}>Clear</button>
                          )}
                          <span className="faint" style={{ fontSize: 11 }}>
                            {editSince
                              ? `Only items dated ${editSince} and newer are captured.`
                              : "Everything is crawled in the background (can take hours for large libraries)."}
                          </span>
                        </div>
                      </div>
                    )}
                    {m.is_picker && (
                      <div className="stack" style={{ gap: 6 }}>
                        <span className="faint" style={{ fontSize: 11.5 }}>
                          Remind me to add new photos every
                        </span>
                        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                          <select style={{ padding: "5px 8px", borderRadius: 6 }} value={editReminder}
                                  onChange={(e) => setEditReminder(Number(e.target.value))}>
                            {[0, 1, 3, 7, 14, 30].map((d) => (
                              <option key={d} value={d}>{d === 0 ? "Never" : `${d} day${d === 1 ? "" : "s"}`}</option>
                            ))}
                          </select>
                          <span className="faint" style={{ fontSize: 11 }}>
                            {editReminder === 0
                              ? "No reminders — back up photos manually from Sources."
                              : "You'll get an ‘Add new photos’ task on your dashboard on this cadence."}
                          </span>
                        </div>
                      </div>
                    )}
                    {m.source_type === "gmail" && (
                      <div className="stack" style={{ gap: 6 }}>
                        <span className="faint" style={{ fontSize: 11.5 }}>Skip folders (excluded from backup)</span>
                        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                          {GMAIL_FOLDERS.map((f) => (
                            <span
                              key={f.id}
                              className={`chip ${editGmailExclude.includes(f.id) ? "active" : ""}`}
                              onClick={() => setEditGmailExclude((cur) =>
                                cur.includes(f.id) ? cur.filter((x) => x !== f.id) : [...cur, f.id])}
                            >
                              {editGmailExclude.includes(f.id) && <Icon name="check" size={12} />} {f.label}
                            </span>
                          ))}
                        </div>
                        <label className="row" style={{ gap: 6, alignItems: "center", fontSize: 12 }}>
                          <input type="checkbox" checked={editGmailSpamTrash}
                                 onChange={(e) => setEditGmailSpamTrash(e.target.checked)} />
                          Also back up Spam &amp; Trash (Gmail excludes them by default)
                        </label>
                      </div>
                    )}
                    {m.source_type === "endpoint_files" && (
                      <div className="stack" style={{ gap: 6 }}>
                        <span className="faint" style={{ fontSize: 11.5 }}>Folders to back up</span>
                        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                          <button className="btn sm" disabled={!m.agent_id}
                                  onClick={() => m.agent_id && setPicker({ agentId: m.agent_id, mappingId: m.id, initial: m.config || {} })}>
                            <Icon name="database" size={13} /> Choose folders…
                          </button>
                          <span className="faint" style={{ fontSize: 11 }}>
                            {(m.config?.roots?.length || 0)} folder(s) selected
                            {m.config?.excludeExts?.length ? ` · excluding ${m.config.excludeExts.join(", ")}` : ""}
                          </span>
                        </div>
                      </div>
                    )}
                    {!m.is_agent && browsableFor(m.source_type) && (
                      <div className="stack" style={{ gap: 6 }}>
                        <span className="faint" style={{ fontSize: 11.5 }}>Folders to back up</span>
                        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                          <button className="btn sm" disabled={!m.connector_account_id}
                                  onClick={() => m.connector_account_id && setCloudPicker({
                                    accountId: m.connector_account_id, mappingId: m.id,
                                    label: m.source_display, sourceType: m.source_type, initial: m.config || {} })}>
                            <Icon name="database" size={13} /> Choose folders…
                          </button>
                          <span className="faint" style={{ fontSize: 11 }}>
                            {(m.config?.roots?.length || 0) > 0
                              ? `${m.config?.roots?.length} folder(s) selected`
                              : "backing up everything"}
                          </span>
                        </div>
                      </div>
                    )}
                    {filterCatsFor(m.source_type).length > 0 && (
                      <div className="stack" style={{ gap: 6 }}>
                        <span className="faint" style={{ fontSize: 11.5 }}>What to back up (all if none selected)</span>
                        <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                          {filterCatsFor(m.source_type).map((f) => (
                            <span
                              key={f.id}
                              className={`chip ${editCategories.includes(f.id) ? "active" : ""}`}
                              onClick={() => setEditCategories((cur) =>
                                cur.includes(f.id) ? cur.filter((x) => x !== f.id) : [...cur, f.id])}
                            >
                              {editCategories.includes(f.id) && <Icon name="check" size={12} />} {f.label}
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
                  {m.offpolicy_points > 0 && (
                    <button className="btn sm warn" onClick={() => prune(m)}
                            title="Delete recovery points stored where this source no longer routes">
                      Prune {m.offpolicy_points} off-policy
                    </button>
                  )}
                  <button className="btn sm ghost" onClick={() => remove(m)}>Remove</button>
                </>
              )}
            </div>
          );
        })}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
      {picker && (
        <FilePicker
          agentId={picker.agentId}
          mappingId={picker.mappingId}
          initial={picker.initial}
          onClose={() => setPicker(null)}
          onSaved={() => { flash("Folder selection saved"); void load(); }}
        />
      )}
      {cloudPicker && (
        <FolderPicker
          title={`Choose folders — ${cloudPicker.label}`}
          note="Only the folders you select are backed up. Nothing selected = everything. Pick “Files in the root folder” or tick “files only” to include a folder’s top-level files without every subfolder."
          initialSelected={cloudPicker.initial.roots || []}
          allowFilesOnly={["dropbox", "onedrive", "google_drive"].includes(cloudPicker.sourceType)}
          loadingLabel="loading your folders…"
          emptyLabel="No subfolders here. Selecting nothing backs up the whole account."
          loadRoots={async () => {
            const folders = await api.get<{ folders: FolderNode[] }>(
              `/connectors/accounts/${cloudPicker.accountId}/folders`).then((r) => r.folders);
            // File-storage sources have an addressable root: offer its top-level
            // files as a selectable item ("__root__"), separate from subfolders.
            const ROOT_TYPES = ["dropbox", "onedrive", "google_drive"];
            if (!ROOT_TYPES.includes(cloudPicker.sourceType)) return folders;
            return [{ path: "__root__", name: "Files in the root folder (not subfolders)", hasMore: false }, ...folders];
          }}
          loadChildren={(node) => api.get<{ folders: FolderNode[] }>(
            `/connectors/accounts/${cloudPicker.accountId}/folders?path=${encodeURIComponent(node.path)}`).then((r) => r.folders)}
          onClose={() => setCloudPicker(null)}
          onSave={async (roots) => {
            await api.put(`/collections/${cloudPicker.mappingId}`, { config: { ...cloudPicker.initial, roots } });
            setCloudPicker(null); flash("Folder selection saved"); void load();
          }}
        />
      )}
    </>
  );
}

function fmtTime(iso?: string | null): string {
  if (!iso) return "never";
  const d = (Date.now() - serverDate(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

const INTERVAL_OPTIONS: { value: number; label: string }[] = [
  { value: -1, label: "Use default" },
  { value: 0, label: "Manual only" },
  { value: 15, label: "Every 15 min" },
  { value: 30, label: "Every 30 min" },
  { value: 60, label: "Hourly" },
  { value: 360, label: "Every 6 hours" },
  { value: 720, label: "Every 12 hours" },
  { value: 1440, label: "Daily" },
];

function intervalText(minutes: number): string {
  if (minutes <= 0) return "manual only";
  if (minutes < 60) return `every ${minutes} min`;
  if (minutes < 1440) { const h = minutes / 60; return h === 1 ? "hourly" : `every ${h}h`; }
  const d = minutes / 1440; return d === 1 ? "daily" : `every ${d}d`;
}

function scheduleLabel(m: Mapping): string {
  if (m.backup_interval_minutes == null) return `Auto · ${intervalText(m.default_interval_minutes)}`;
  if (m.backup_interval_minutes === 0) return "Manual only";
  return `Auto · ${intervalText(m.backup_interval_minutes)}`;
}

function collectorLabel(c: string): string {
  return c === "onepassword" ? "1Password" : c === "endpoint_files" ? "Endpoint Files" : c;
}

// Gmail folders/labels the operator can skip (excluded via a Gmail search query).
const GMAIL_FOLDERS: { id: string; label: string }[] = [
  { id: "SPAM", label: "Spam" },
  { id: "TRASH", label: "Trash" },
  { id: "CATEGORY_PROMOTIONS", label: "Promotions" },
  { id: "CATEGORY_SOCIAL", label: "Social" },
  { id: "CATEGORY_UPDATES", label: "Updates" },
  { id: "CATEGORY_FORUMS", label: "Forums" },
];

interface FsNode {
  path: string; name: string; files?: number; bytes?: number;
  children?: FsNode[]; hasMore?: boolean; kind?: string; error?: string;
}
interface FsIndex {
  built_at?: string | null; roots?: FsNode[]; nodes?: number;
  building?: boolean; error?: string;
}

// Folder picker for the Endpoint Files source. The agent maintains a cached
// folder index (rebuilt in the background), so this loads the whole tree once
// and navigates it locally — no per-folder scan round trips.
// The endpoint-files picker now reuses the shared FolderPicker, providing the
// agent-specific data source (its async cached folder index + lazy fs-expand)
// plus the exclude-file-types / max-size controls.
function FilePicker({ agentId, mappingId, initial, onClose, onSaved }: {
  agentId: string; mappingId: string; initial: FileConfig;
  onClose: () => void; onSaved: () => void;
}) {
  const [excl, setExcl] = useState<string>((initial.excludeExts || []).join(", "));
  const [maxMb, setMaxMb] = useState<number>(Math.round((initial.maxSizeBytes || 100 * 1024 * 1024) / (1024 * 1024)));
  const [rebuilding, setRebuilding] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  async function loadRoots(): Promise<FolderNode[]> {
    // Serve the agent's cached index first; nudge + poll if it isn't ready yet.
    const first = await api.get<{ scan: FsIndex | null }>(`/agents/${agentId}/fs-scan`);
    const cached = first.scan?.roots;
    if (cached && cached.length) return cached as FolderNode[];
    await api.post(`/agents/${agentId}/fs-scan`, { rebuild: false });
    const deadline = Date.now() + 180000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2500));
      const res = await api.get<{ scan: FsIndex | null }>(`/agents/${agentId}/fs-scan`);
      const rs = res.scan?.roots;
      if (rs && rs.length) return rs as FolderNode[];
    }
    throw new Error("The agent hasn't reported a folder index yet. Make sure it's online and updated (it indexes on start), then Rescan drives.");
  }

  async function loadChildren(node: FolderNode): Promise<FolderNode[]> {
    const { request_id } = await api.post<{ request_id: string }>(`/agents/${agentId}/fs-expand`, { path: node.path });
    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 1500));
      const { expansion } = await api.get<{ expansion: { request_id: string; ok: boolean; result?: { children?: FsNode[] } } | null }>(
        `/agents/${agentId}/fs-expand?path=${encodeURIComponent(node.path)}`);
      if (expansion && expansion.request_id === request_id) {
        return (expansion.ok ? (expansion.result?.children || []) : []) as FolderNode[];
      }
    }
    return [];
  }

  async function rescan() {
    setRebuilding(true);
    try { await api.post(`/agents/${agentId}/fs-scan`, { rebuild: true }); } catch { /* ignore */ }
    // Give the agent a moment to rebuild, then remount the picker to re-poll.
    setTimeout(() => { setRebuilding(false); setReloadKey((k) => k + 1); }, 4000);
  }

  async function onSave(roots: string[]) {
    const config: FileConfig = {
      roots,
      excludeExts: excl.split(",").map((s) => s.trim().replace(/^\./, "").toLowerCase()).filter(Boolean),
      maxSizeBytes: Math.max(1, maxMb) * 1024 * 1024,
    };
    await api.put(`/collections/${mappingId}`, { config });
    onSaved();
    onClose();
  }

  return (
    <FolderPicker
      key={reloadKey}
      title="Choose folders to back up"
      note="Selecting a folder includes everything beneath it — tick “files only” to back up just a folder’s top-level files (no subfolders)."
      initialSelected={initial.roots || []}
      allowFilesOnly
      loadingLabel="loading the agent's folder index…"
      emptyLabel="No folder index yet. Make sure the agent is online and updated, then Rescan drives."
      loadRoots={loadRoots}
      loadChildren={loadChildren}
      onClose={onClose}
      onSave={onSave}
      headerAction={
        <button className="btn ghost sm" disabled={rebuilding} onClick={() => void rescan()}>
          {rebuilding ? <><span className="spinner-dot" /> Rebuilding…</> : "Rescan drives"}
        </button>
      }
      extra={
        <div className="row" style={{ gap: 12, marginTop: 12, flexWrap: "wrap" }}>
          <label className="stack" style={{ flex: 1, minWidth: 200 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Exclude file types (comma-separated)</span>
            <input className="input" value={excl} placeholder="mp4, iso, dmg" onChange={(e) => setExcl(e.target.value)} />
          </label>
          <label className="stack" style={{ width: 160 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Max file size (MB)</span>
            <input className="input" type="number" min={1} value={maxMb} onChange={(e) => setMaxMb(Number(e.target.value))} />
          </label>
        </div>
      }
    />
  );
}
