import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, timeAgo } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { confirmDialog, formDialog, notify } from "../components/dialog";

interface CatalogItem {
  type: string;
  displayName: string;
  authType: string;
  icon: string;
  color: string;
  docTypes: string[];
  mode: "oauth" | "token";
  configured: boolean;
  requiresAgent?: boolean;
  setup: string[];
}
interface Account {
  id: string;
  connector_type: string;
  account_label: string;
  auth_status: string;
  last_sync_at: string | null;
}
interface Vault { id: string; name: string; }
interface Appliance { id: string; name: string; serial: string; state: string; }

export default function Connectors() {
  const { me, stepUp } = useAuth();
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [appliances, setAppliances] = useState<Appliance[]>([]);
  const [dest, setDest] = useState<string>("cv-cloud");
  const [setup, setSetup] = useState<CatalogItem | null>(null);
  const [toast, setToast] = useState("");

  async function load() {
    setCatalog(await api.get<CatalogItem[]>("/connectors/catalog"));
    setAccounts(await api.get<Account[]>("/connectors/accounts"));
    const t = await api.get<{ vaults: Vault[] }>("/tenant");
    setVaults(t.vaults);
    try { setAppliances(await api.get<Appliance[]>("/appliances")); } catch { /* ignore */ }
  }

  function destinations(): string[] {
    if (dest === "appliance") return ["appliance"];
    if (dest === "both") return ["cv-cloud", "appliance"];
    return ["cv-cloud"];
  }

  useEffect(() => {
    void load();
    // Handle the return from an OAuth consent redirect.
    const p = new URLSearchParams(window.location.search);
    if (p.get("connected")) {
      flash(`${p.get("connected")} connected`);
      window.history.replaceState({}, "", "/connectors");
    } else if (p.get("error")) {
      flash(`Connection failed: ${p.get("error")}`);
      window.history.replaceState({}, "", "/connectors");
    }
  }, []);

  function flash(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 3500);
  }

  async function connect(c: CatalogItem) {
    // Agent-collected sources (e.g. 1Password) are gathered by a local desktop
    // agent, not a cloud pull — route the user to agent setup instead.
    if (c.requiresAgent) {
      setSetup(c);
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

  async function backup(a: Account) {
    const vault = vaults[0];
    if (!vault) return notify({ message: "No vault is available to store this backup.", tone: "warn" });
    // Prefer an existing Data Map mapping for this source so the sync routes to
    // the destinations configured there; only fall back to a page-level choice
    // when the source has not been mapped yet.
    let collId: string | null = null;
    let routedDests: string[] | null = null;
    try {
      const mappings = await api.get<Array<{ id: string; connector_account_id: string | null; destinations: string[] }>>("/collections");
      const existing = mappings.find((m) => m.connector_account_id === a.id);
      if (existing) { collId = existing.id; routedDests = existing.destinations; }
    } catch { /* fall back to creating one */ }
    if (!collId) {
      const dests = destinations();
      const coll = await api.post<{ id: string }>("/collections", {
        vault_id: vault.id,
        name: a.account_label,
        source_type: a.connector_type,
        connector_account_id: a.id,
        destinations: dests,
      });
      collId = coll.id;
      routedDests = dests;
    }
    try {
      const res = await api.post<{ object_count: number }>(
        `/collections/${collId}/backup`, {});
      flash(`Backed up ${res.object_count} objects from ${a.account_label} → ${(routedDests || ["cv-cloud"]).join(", ")}`);
    } catch (e) {
      await notify({ title: "Backup failed", message: (e as ApiError).message, tone: "danger" });
    }
    await load();
  }

  async function unlink(a: Account) {
    const ok = await confirmDialog({
      title: "Unlink source",
      message: `Unlink ${a.account_label}? New backups will stop for this source. Existing recovery points are kept.`,
      confirmLabel: "Unlink",
    });
    if (!ok) return;
    try {
      await api.del(`/connectors/accounts/${a.id}`);
      flash("Unlinked");
      await load();
    } catch (e) {
      await notify({ title: "Couldn't unlink", message: (e as ApiError).message, tone: "danger" });
    }
  }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>Connect a source</h2>
        <div className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
          You authorize each service through its own consent screen. Data is encrypted before
          it leaves the connector environment. Tokens are stored encrypted at rest.
        </div>
        <div className="grid grid-3">
          {catalog.map((c) => (
            <div key={c.type} className="dest-card" onClick={() => connect(c)}>
              <div className="spread" style={{ marginBottom: 10 }}>
                <div className="row">
                  <div className="result-icon" style={{ background: c.color, width: 34, height: 34 }}>
                    <Icon name={c.icon as IconName} size={17} />
                  </div>
                  <div>
                    <div style={{ fontWeight: 650 }}>{c.displayName}</div>
                    <div className="faint" style={{ fontSize: 11.5 }}>{c.mode === "oauth" ? "OAuth" : "Token"}</div>
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
      </Card>

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
          <label className="row" style={{ gap: 8, fontSize: 12.5 }}>
            <span className="faint">Back up to</span>
            <select className="input sm" value={dest} onChange={(e) => setDest(e.target.value)}>
              <option value="cv-cloud">Cloud</option>
              {appliances.length > 0 && <option value="appliance">Appliance</option>}
              {appliances.length > 0 && <option value="both">Cloud + Appliance</option>}
            </select>
          </label>
        </div>
        {accounts.length === 0 && <div className="muted">No sources linked yet.</div>}
        {accounts.map((a) => {
          const c = catalog.find((x) => x.type === a.connector_type);
          return (
            <div key={a.id} className="result-row">
              <div className="result-icon" style={{ background: c?.color ?? "#1a2234" }}>
                <Icon name={(c?.icon as IconName) ?? "database"} size={17} />
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>{a.account_label}</div>
                <div className="faint" style={{ fontSize: 12 }}>
                  {c?.displayName ?? a.connector_type} · last sync {timeAgo(a.last_sync_at)}
                </div>
              </div>
              <Pill tone={a.auth_status === "linked" ? "ok" : "warn"}>{a.auth_status}</Pill>
              <button className="btn sm primary" onClick={() => backup(a)}>Back up now</button>
              <button className="btn sm ghost" onClick={() => unlink(a)}>Unlink</button>
            </div>
          );
        })}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}
