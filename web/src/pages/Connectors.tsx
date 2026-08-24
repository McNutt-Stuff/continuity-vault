import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, timeAgo, Loading, bytes } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { confirmDialog, formDialog, notify, promptDialog, stepsDialog } from "../components/dialog";
import { Menu, MenuEntry } from "../components/Menu";
import { PhotoPickerModal } from "../components/PhotoPicker";

interface PostConnect {
  title?: string;
  message?: string;
  steps: string[];
  appUrl?: string;
  linkLabel?: string;
}
interface CatalogItem {
  type: string;
  displayName: string;
  authType: string;
  icon: string;
  color: string;
  family: string;
  category: string;
  docTypes: string[];
  mode: "oauth" | "token";
  configured: boolean;
  requiresAgent?: boolean;
  setup: string[];
  postConnect?: PostConnect | null;
}
interface Account {
  id: string;
  connector_type: string;
  account_label: string;
  account_username?: string | null;
  auth_status: string;
  active?: boolean;
  last_sync_at: string | null;
  last_object_count?: number | null;
  protected_bytes?: number | null;
  last_error?: string | null;
  last_error_at?: string | null;
  needs_reauth?: boolean;
  has_error?: boolean;
}
interface Vault { id: string; name: string; }
interface Agent { id: string; name: string; hostname: string; collectors: string[]; enabled_collectors?: string[] }

export default function Connectors() {
  const { me, stepUp } = useAuth();
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [setup, setSetup] = useState<CatalogItem | null>(null);
  const [toast, setToast] = useState("");
  const [query, setQuery] = useState("");
  const [groupBy, setGroupBy] = useState<"category" | "family">("category");
  const [photoPicker, setPhotoPicker] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [showAdd, setShowAdd] = useState(false);

  async function load() {
    try {
      setCatalog(await api.get<CatalogItem[]>("/connectors/catalog"));
      setAccounts(await api.get<Account[]>("/connectors/accounts"));
      setAgents(await api.get<Agent[]>("/agents").catch(() => [] as Agent[]));
      const t = await api.get<{ vaults: Vault[] }>("/tenant");
      setVaults(t.vaults);
    } finally {
      setLoaded(true);
    }
  }

  useEffect(() => {
    void load();
    // Handle the return from an OAuth consent redirect.
    const p = new URLSearchParams(window.location.search);
    const connected = p.get("connected");
    const acct = p.get("account");
    const isNew = p.get("new") === "1";
    if (connected) {
      window.history.replaceState({}, "", "/connectors");
      if (acct && isNew) void postConnect(connected, acct);
      else flash(`${connected} connected`);
    } else if (p.get("error")) {
      flash(`Connection failed: ${p.get("error")}`);
      window.history.replaceState({}, "", "/connectors");
    }
  }, []);

  function flash(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 3500);
  }

  // After a fresh OAuth link: name the source, then run any reusable extra steps
  // the source declares (e.g. GitHub app install for private-repo access).
  async function postConnect(type: string, accountId: string) {
    let item: CatalogItem | undefined;
    let account: Account | undefined;
    try {
      const [cat, accts] = await Promise.all([
        api.get<CatalogItem[]>("/connectors/catalog"),
        api.get<Account[]>("/connectors/accounts"),
      ]);
      item = cat.find((c) => c.type === type);
      account = accts.find((a) => a.id === accountId);
    } catch { /* fall back to defaults below */ }
    const displayName = item?.displayName ?? type;
    const name = await promptDialog({
      title: `Name your ${displayName} source`,
      message: account?.account_username ? `Linked account: ${account.account_username}` : undefined,
      label: "Display name",
      defaultValue: account?.account_label ?? displayName,
      confirmLabel: "Save",
    });
    const trimmed = name?.trim();
    if (trimmed && trimmed !== account?.account_label) {
      try {
        await api.put(`/connectors/accounts/${accountId}`, { account_label: trimmed });
      } catch (e) {
        await notify({ title: "Couldn't rename", message: (e as ApiError).message, tone: "danger" });
      }
    }
    if (item?.postConnect) {
      await stepsDialog({
        title: item.postConnect.title ?? `Finish setting up ${displayName}`,
        message: item.postConnect.message,
        steps: item.postConnect.steps,
        linkUrl: item.postConnect.appUrl,
        linkLabel: item.postConnect.linkLabel,
        confirmLabel: "Done",
      });
    }
    flash(`${displayName} connected`);
    await load();
  }

  async function connect(c: CatalogItem) {
    // Agent-collected sources (e.g. 1Password): pick which linked desktop agent
    // collects it and create the agent-bound source. Only show install
    // instructions when no agent is linked yet.
    if (c.requiresAgent) {
      // Only agents that have this collector ENABLED can serve it.
      const eligible = agents.filter((a) => (a.enabled_collectors || []).includes(c.type));
      if (eligible.length === 0) {
        const hasIt = agents.some((a) => (a.collectors || []).includes(c.type));
        return notify({
          title: hasIt ? `Enable ${c.displayName} first` : `No agent can collect ${c.displayName}`,
          message: hasIt
            ? `This collector is turned off on your agent(s). Enable it under Agents → Collectors, then add the source.`
            : `Install the Arkive desktop agent on the device that has ${c.displayName}, then enable the collector under Agents.`,
          tone: "warn",
        });
      }
      const pool = eligible;
      const vault = vaults[0];
      if (!vault) return notify({ message: "No vault is available to store this source.", tone: "warn" });
      const res = await formDialog({
        title: `Collect ${c.displayName} with a desktop agent`,
        message: "Choose the agent on the device where this source lives. It collects locally and pushes encrypted data to the vault.",
        confirmLabel: "Add source",
        fields: [{
          name: "agent", label: "Desktop agent", required: true,
          options: pool.map((a) => ({
            label: `${a.hostname || a.name}`,
            value: a.id,
          })),
        }],
      });
      if (!res || !res.agent) return;
      const agent = pool.find((a) => a.id === res.agent);
      try {
        await api.post("/collections", {
          vault_id: vault.id,
          name: `${c.type} (${agent?.hostname || agent?.name || "agent"})`,
          source_type: c.type,
          agent_id: res.agent,
          sensitivity: "restricted",
          destinations: ["cv-cloud"],
        });
        flash(`${c.displayName} added — route & sync it in the Data Map`);
        await load();
      } catch (e) {
        await notify({ title: "Couldn't add source", message: (e as ApiError).message, tone: "danger" });
      }
      return;
    }
    if (!me?.passkey_verified) {
      try { await stepUp(); } catch (e) { return notify({ message: (e as Error).message, tone: "danger" }); }
    }
    if (c.mode === "oauth" && !c.configured) {
      setSetup(c);
      return;
    }
    if (c.mode === "token") {
      let result: Record<string, string> | null;
      if (c.type === "onepassword") {
        result = await formDialog({
          title: `Connect ${c.displayName}`,
          message: "Enter your 1Password Connect server details.",
          fields: [
            { name: "host", label: "Connect server URL (host)", placeholder: "https://connect.example.com", required: true },
            { name: "token", label: "Connect token", password: true, required: true },
            { name: "label", label: "Account label", defaultValue: `My ${c.displayName}` },
          ],
        });
      } else if (c.type === "icloud") {
        result = await formDialog({
          title: `Connect ${c.displayName}`,
          message: "Use an app-specific password from appleid.apple.com.",
          fields: [
            { name: "username", label: "Apple ID (email)", required: true },
            { name: "token", label: "App-specific password", password: true, required: true },
            { name: "label", label: "Account label" },
          ],
        });
      } else {
        result = await formDialog({
          title: `Connect ${c.displayName}`,
          fields: [
            { name: "token", label: `${c.displayName} token`, password: true, required: true },
            { name: "label", label: "Account label", defaultValue: `My ${c.displayName}` },
          ],
        });
      }
      if (!result || !result.token) return;
      const label = result.label?.trim() || result.username || `My ${c.displayName}`;
      try {
        await api.post(`/connectors/${c.type}/token`, {
          account_label: label, token: result.token,
          username: result.username, host: result.host,
        });
        flash(`${c.displayName} connected`);
        await load();
      } catch (e) {
        await notify({ title: "Couldn't connect", message: (e as ApiError).message, tone: "danger" });
      }
      return;
    }
    // OAuth: get the provider consent URL and redirect the browser to it.
    try {
      const res = await api.post<{ authorize_url: string }>(`/connectors/${c.type}/connect`, {});
      window.location.href = res.authorize_url;
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 400) setSetup(c);
      else await notify({ title: "Couldn't start authorization", message: err.message, tone: "danger" });
    }
  }

  async function reconnect(a: Account) {
    try {
      const res = await api.post<{ authorize_url?: string }>(`/connectors/${a.connector_type}/connect`, { account_id: a.id });
      if (res.authorize_url) { window.location.href = res.authorize_url; return; }
      await notify({ title: "Re-add this source", message: "This source is linked with a token — remove and add it again to re-authorize.", tone: "warn" });
    } catch (e) {
      await notify({ title: "Couldn't start re-authorization", message: (e as ApiError).message, tone: "danger" });
    }
  }

  async function backup(a: Account) {
    const vault = vaults[0];
    if (!vault) return notify({ message: "No vault is available to store this backup.", tone: "warn" });
    // Prefer an existing Data Map mapping for this source so the sync routes to
    // the destinations configured there; only create a cloud-default mapping when
    // the source has not been mapped yet (routing is managed in the Data Map).
    let collId: string | null = null;
    try {
      const mappings = await api.get<Array<{ id: string; connector_account_id: string | null }>>("/collections");
      const existing = mappings.find((m) => m.connector_account_id === a.id);
      if (existing) collId = existing.id;
    } catch { /* fall back to creating one */ }
    if (!collId) {
      const coll = await api.post<{ id: string }>("/collections", {
        vault_id: vault.id,
        name: a.account_label,
        source_type: a.connector_type,
        connector_account_id: a.id,
        destinations: ["cv-cloud"],
      });
      collId = coll.id;
    }
    try {
      const res = await api.post<{ job_id?: string; object_count?: number }>(
        `/collections/${collId}/backup`, {});
      if (res.job_id) flash(`Backup started for ${a.account_label} — see Activity for progress`);
      else flash(`Backed up ${res.object_count ?? 0} objects from ${a.account_label}`);
    } catch (e) {
      await notify({ title: "Backup failed", message: (e as ApiError).message, tone: "danger" });
    }
    await load();
  }

  async function unlink(a: Account) {
    const ok = await confirmDialog({
      title: "Deactivate source",
      message: `Deactivate ${a.account_label}? Syncing stops and no new data is captured. Your existing data is kept and searchable — you can re-link later, or purge it permanently.`,
      confirmLabel: "Deactivate",
    });
    if (!ok) return;
    try {
      await api.del(`/connectors/accounts/${a.id}`);
      flash("Source deactivated");
      await load();
    } catch (e) {
      await notify({ title: "Couldn't deactivate", message: (e as ApiError).message, tone: "danger" });
    }
  }

  async function reactivate(a: Account) {
    try {
      await api.post(`/connectors/accounts/${a.id}/reactivate`, {});
      flash("Source re-linked");
      await load();
    } catch (e) {
      await notify({ title: "Couldn't re-link", message: (e as ApiError).message, tone: "danger" });
    }
  }

  async function rename(a: Account) {
    const name = await promptDialog({
      title: "Rename source",
      label: "Display name",
      message: a.account_username ? `Linked account: ${a.account_username}` : undefined,
      defaultValue: a.account_label,
      confirmLabel: "Save",
    });
    if (name == null) return;
    const trimmed = name.trim();
    if (!trimmed || trimmed === a.account_label) return;
    try {
      await api.put(`/connectors/accounts/${a.id}`, { account_label: trimmed });
      flash("Source renamed");
      await load();
    } catch (e) {
      await notify({ title: "Couldn't rename", message: (e as ApiError).message, tone: "danger" });
    }
  }

  async function purge(a: Account) {
    let targets: { active: boolean; destinations: { id: string; label: string; recovery_points: number; bytes: number }[] };
    try {
      targets = await api.get(`/connectors/accounts/${a.id}/purge-targets`);
    } catch (e) {
      await notify({ title: "Couldn't load purge options", message: (e as ApiError).message, tone: "danger" });
      return;
    }
    // The data mapping must be disabled/removed first.
    if (targets.active) {
      await notify({
        title: "Deactivate the source first", tone: "warn",
        message: `Disable ${a.account_label}'s data mapping by deactivating the source before you can purge its data.`,
      });
      return;
    }
    const opts = [
      { label: "Everywhere — remove this source completely", value: "all" },
      ...targets.destinations.map((d) => ({
        label: `${d.label} — ${bytes(d.bytes)} · ${d.recovery_points} recovery point${d.recovery_points === 1 ? "" : "s"}`,
        value: d.id,
      })),
    ];
    const sel = await formDialog({
      title: `Purge ${a.account_label}`,
      message: "Choose where to permanently delete this source's data from. This is irreversible.",
      confirmLabel: "Continue",
      fields: [{ name: "where", label: "Purge from", defaultValue: "all", options: opts }],
    });
    if (!sel) return;
    const where = sel.where;
    const scope = where === "all"
      ? "everywhere — the source will be removed completely"
      : (opts.find((o) => o.value === where)?.label ?? where);
    const ok2 = await confirmDialog({
      title: "This cannot be undone",
      message: `Once purged, ${a.account_label}'s data is NOT recoverable. Purge from ${scope}?`,
      tone: "danger", confirmLabel: "Purge permanently",
    });
    if (!ok2) return;
    try {
      const r = await api.post<{ documents?: number; recovery_points?: number; removed?: boolean }>(
        `/connectors/accounts/${a.id}/purge`, { destinations: where === "all" ? ["all"] : [where] });
      flash(r.removed
        ? `Purged — source removed, ${(r.recovery_points || 0).toLocaleString()} recovery points deleted`
        : `Purged ${(r.recovery_points || 0).toLocaleString()} recovery point${(r.recovery_points || 0) === 1 ? "" : "s"}`);
      await load();
    } catch (e) {
      await notify({ title: "Couldn't purge", message: (e as ApiError).message, tone: "danger" });
    }
  }

  // Group the catalog by functional type or provider family so the page stays
  // organized as more sources are added.
  const TYPE_ORDER = ["Email", "Files & Storage", "Photos", "Social", "Contacts", "Calendar", "Passwords", "Other"];
  const filtered = catalog.filter((c) => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return c.displayName.toLowerCase().includes(q) || (c.family || "").toLowerCase().includes(q)
      || (c.category || "").toLowerCase().includes(q) || c.type.includes(q);
  });
  const groups = new Map<string, CatalogItem[]>();
  for (const c of filtered) {
    const key = (groupBy === "family" ? c.family : c.category) || "Other";
    const arr = groups.get(key);
    if (arr) arr.push(c); else groups.set(key, [c]);
  }
  const groupKeys = [...groups.keys()].sort((a, b) => {
    if (groupBy === "category") {
      const ia = TYPE_ORDER.indexOf(a), ib = TYPE_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
    }
    return a.localeCompare(b);
  });

  if (!loaded && catalog.length === 0) return <Loading label="Loading sources…" />;

  return (
    <>
      <div className="spread" style={{ marginBottom: 16, alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <div>
          <h2 style={{ margin: 0 }}>Sources</h2>
          <div className="muted" style={{ fontSize: 13, maxWidth: 560 }}>
            The services Arkive backs up. Add one, then route it to a vault in the <a href="/mappings">Data Map</a>.
          </div>
        </div>
        <button className="btn primary" onClick={() => { setQuery(""); setShowAdd(true); }}>
          <Icon name="link" size={15} /> Add source
        </button>
      </div>

      {showAdd && (
        <div className="modal-backdrop" onClick={() => setShowAdd(false)}>
          <div className="modal-panel" style={{ width: "min(920px, 100%)" }} onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div>
                <h3 style={{ margin: 0 }}>Connect a source</h3>
                <div className="faint" style={{ fontSize: 12, maxWidth: 520 }}>
                  You authorize each service through its own consent screen. Data is encrypted before it
                  leaves the connector environment; tokens are stored encrypted at rest.
                </div>
              </div>
              <button className="btn ghost sm" onClick={() => setShowAdd(false)}>Close</button>
            </div>
            <div className="modal-body" style={{ maxHeight: "72vh", overflow: "auto" }}>
              <div className="row" style={{ gap: 8, marginBottom: 14, flexWrap: "wrap" }}>
                <input className="input sm" placeholder="Search sources…" value={query}
                       onChange={(e) => setQuery(e.target.value)} style={{ flex: 1, minWidth: 180 }} />
                {(["category", "family"] as const).map((g) => (
                  <button key={g} className={`btn sm ${groupBy === g ? "primary" : "ghost"}`}
                          onClick={() => setGroupBy(g)}>
                    {g === "category" ? "By type" : "By family"}
                  </button>
                ))}
              </div>
              <div className="stack" style={{ gap: 18 }}>
                {groupKeys.map((gk) => (
                  <div key={gk}>
                    <div className="row" style={{ gap: 8, marginBottom: 10, alignItems: "center" }}>
                      <div className="nav-section" style={{ padding: 0 }}>{gk}</div>
                      <span className="faint" style={{ fontSize: 11 }}>{groups.get(gk)!.length}</span>
                    </div>
                    <div className="grid grid-3">
                      {groups.get(gk)!.map((c) => (
                        <div key={c.type} className="dest-card" onClick={() => { setShowAdd(false); void connect(c); }}>
                          <div className="spread" style={{ marginBottom: 10 }}>
                            <div className="row">
                              <div className="result-icon" style={{ background: brandForSource(c.type) ? "var(--inset)" : c.color, width: 34, height: 34 }}>
                                {brandForSource(c.type)
                                  ? <BrandIcon name={brandForSource(c.type)!} size={19} />
                                  : <Icon name={c.icon as IconName} size={17} />}
                              </div>
                              <div>
                                <div style={{ fontWeight: 650 }}>{c.displayName}</div>
                                <div className="faint" style={{ fontSize: 11.5 }}>{groupBy === "category" ? c.family : c.category}</div>
                              </div>
                            </div>
                            {c.requiresAgent
                              ? <Pill tone="info">Desktop agent</Pill>
                              : c.mode === "oauth" && !c.configured
                              ? <Pill tone="warn">Needs setup</Pill>
                              : <Pill tone="ok">Ready</Pill>}
                          </div>
                          <div className="faint" style={{ fontSize: 12 }}>{c.docTypes.join(" · ")}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
                {groupKeys.length === 0 && <div className="muted">No sources match “{query}”.</div>}
              </div>
            </div>
          </div>
        </div>
      )}

      {setup && (
        <Card style={{ marginBottom: 16, borderColor: "var(--warn)" }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h3>Set up {setup.displayName}</h3>
            <button className="btn ghost sm" onClick={() => setSetup(null)}>Close</button>
          </div>
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 10 }}>
            {setup.requiresAgent
              ? `${setup.displayName} is collected by a local Arkive desktop agent (it uses the native app/CLI on the device). Install an agent, then it appears here automatically.`
              : "This provider needs an OAuth app configured on the server before it can be connected."}
          </div>
          <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.7 }}>
            {setup.setup.map((s, i) => <li key={i}>{s}</li>)}
          </ol>
          {setup.requiresAgent && (
            <button className="btn primary sm" style={{ marginTop: 12 }}
                    onClick={() => window.location.assign("/agents")}>
              <Icon name="user" size={14} /> Go to Desktop Agents
            </button>
          )}
        </Card>
      )}

      <Card>
        <div className="spread" style={{ marginBottom: 12 }}>
          <h2>Linked accounts</h2>
          <span className="faint" style={{ fontSize: 12 }}>
            Routing is managed in <a href="/mappings">Data Map</a>
          </span>
        </div>
        {accounts.length === 0 && <div className="muted">No sources linked yet.</div>}
        {accounts.map((a) => {
          const c = catalog.find((x) => x.type === a.connector_type);
          const inactive = a.active === false;
          const err = !inactive && !!(a.needs_reauth || a.has_error);
          const canPurge = me?.features?.purge_enabled !== false;
          return (
            <div key={a.id} className="result-row" style={inactive ? { opacity: 0.72 } : err ? { borderLeft: "3px solid var(--warn)" } : undefined}>
              <div className="result-icon" style={{ background: brandForSource(a.connector_type) ? "var(--inset)" : (c?.color ?? "var(--bg-elev-2)") }}>
                {brandForSource(a.connector_type)
                  ? <BrandIcon name={brandForSource(a.connector_type)!} size={18} />
                  : <Icon name={(c?.icon as IconName) ?? "database"} size={17} />}
              </div>
              <div className="flex1">
                <div className="row" style={{ gap: 6, alignItems: "baseline" }}>
                  <span style={{ fontWeight: 600 }}>{a.account_label}</span>
                  {a.account_username && a.account_username !== a.account_label && (
                    <span className="faint" style={{ fontSize: 12 }}>({a.account_username})</span>
                  )}
                </div>
                <div className="faint" style={{ fontSize: 12 }}>
                  {c?.displayName ?? a.connector_type} · last sync {timeAgo(a.last_sync_at)}
                  {a.protected_bytes != null && a.protected_bytes > 0
                    ? ` · ${bytes(a.protected_bytes)} protected`
                    : (a.last_object_count != null
                        ? ` · ${a.last_object_count.toLocaleString()} object${a.last_object_count === 1 ? "" : "s"} collected`
                        : "")}
                </div>
                {err && (
                  <div style={{ fontSize: 12, color: "var(--warn)", marginTop: 3, display: "flex", gap: 6, alignItems: "center" }}>
                    <Icon name="alert" size={11} />
                    {a.needs_reauth ? "Needs re-authorization" : "Last sync failed"}
                    {a.last_error ? ` — ${a.last_error.slice(0, 140)}` : ""}
                  </div>
                )}
                {inactive && (
                  <div className="faint" style={{ fontSize: 12, marginTop: 3 }}>
                    Deactivated — data retained. Re-link to resume syncing, or remove it to delete it permanently.
                  </div>
                )}
              </div>
              {inactive ? (
                <>
                  <Pill tone="warn">Deactivated</Pill>
                  <button className="btn sm primary" onClick={() => reactivate(a)}><Icon name="link" size={13} /> Re-link</button>
                  <Menu items={([
                    { label: "Rename source", icon: "edit", onClick: () => rename(a) },
                    ...(c?.mode === "oauth" ? [{ label: "Re-authorize", icon: "key", onClick: () => reconnect(a) }] : []),
                    ...(canPurge ? ["divider", { label: "Remove source", icon: "trash", danger: true, onClick: () => purge(a) }] : []),
                  ] as MenuEntry[])} />
                </>
              ) : (
                <>
                  <Pill tone={err ? "warn" : "ok"}>{a.needs_reauth ? "Reconnect needed" : err ? "Issue" : "Healthy"}</Pill>
                  {a.needs_reauth
                    ? <button className="btn sm primary" onClick={() => reconnect(a)}><Icon name="key" size={13} /> Reconnect</button>
                    : (a.connector_type === "google_photos"
                        ? <button className="btn sm primary" onClick={() => setPhotoPicker(a.id)}>Add photos</button>
                        : <button className="btn sm primary" onClick={() => backup(a)}>Back up now</button>)}
                  <Menu items={([
                    { label: "Rename source", icon: "edit", onClick: () => rename(a) },
                    ...(c?.mode === "oauth" && !a.needs_reauth ? [{ label: "Re-authorize", icon: "key", onClick: () => reconnect(a) }] : []),
                    { label: "Deactivate", icon: "link", onClick: () => unlink(a) },
                    ...(canPurge ? ["divider", { label: "Remove source", icon: "trash", danger: true, disabled: true, onClick: () => purge(a) }] : []),
                  ] as MenuEntry[])} />
                </>
              )}
            </div>
          );
        })}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
      {photoPicker && (
        <PhotoPickerModal accountId={photoPicker} onClose={() => setPhotoPicker(null)}
                          onStarted={() => flash("Photo import started — see Activity")} />
      )}
    </>
  );
}
