import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, timeAgo } from "../components/ui";
import { Icon, IconName } from "../components/Icon";

interface CatalogItem {
  type: string;
  displayName: string;
  authType: string;
  icon: string;
  color: string;
  docTypes: string[];
  mode: "oauth" | "token";
  configured: boolean;
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

export default function Connectors() {
  const { me, stepUp } = useAuth();
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [setup, setSetup] = useState<CatalogItem | null>(null);
  const [toast, setToast] = useState("");

  async function load() {
    setCatalog(await api.get<CatalogItem[]>("/connectors/catalog"));
    setAccounts(await api.get<Account[]>("/connectors/accounts"));
    const t = await api.get<{ vaults: Vault[] }>("/tenant");
    setVaults(t.vaults);
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
    if (!me?.passkey_verified) {
      try { await stepUp(); } catch (e) { return alert((e as Error).message); }
    }
    if (c.mode === "oauth" && !c.configured) {
      setSetup(c);
      return;
    }
    if (c.mode === "token") {
      let token = "";
      let username: string | undefined;
      let host: string | undefined;
      if (c.type === "onepassword") {
        host = prompt("1Password Connect server URL (host)") ?? undefined;
        token = prompt("1Password Connect token") ?? "";
      } else if (c.type === "icloud") {
        username = prompt("Apple ID (email)") ?? undefined;
        token = prompt("App-specific password") ?? "";
      } else {
        token = prompt(`Paste your ${c.displayName} token`) ?? "";
      }
      if (!token) return;
      const label = prompt("Account label", username || `My ${c.displayName}`) ?? c.displayName;
      try {
        await api.post(`/connectors/${c.type}/token`, { account_label: label, token, username, host });
        flash(`${c.displayName} connected`);
        await load();
      } catch (e) { alert((e as ApiError).message); }
      return;
    }
    // OAuth: get the provider consent URL and redirect the browser to it.
    try {
      const res = await api.post<{ authorize_url: string }>(`/connectors/${c.type}/connect`, {});
      window.location.href = res.authorize_url;
    } catch (e) {
      const err = e as ApiError;
      if (err.status === 400) setSetup(c);
      else alert(err.message);
    }
  }

  async function backup(a: Account) {
    const vault = vaults[0];
    if (!vault) return alert("No vault available");
    const coll = await api.post<{ id: string }>("/collections", {
      vault_id: vault.id,
      name: a.account_label,
      source_type: a.connector_type,
      connector_account_id: a.id,
      destinations: ["cv-cloud"],
    });
    try {
      const res = await api.post<{ object_count: number }>(
        `/collections/${coll.id}/backup`, { destinations: ["cv-cloud"] });
      flash(`Backed up ${res.object_count} objects from ${a.account_label}`);
    } catch (e) {
      alert(`Backup failed: ${(e as ApiError).message}`);
    }
    await load();
  }

  async function unlink(a: Account) {
    if (!confirm(`Unlink ${a.account_label}?`)) return;
    try {
      await api.del(`/connectors/accounts/${a.id}`);
      flash("Unlinked");
      await load();
    } catch (e) { alert((e as ApiError).message); }
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
                {c.mode === "oauth" && !c.configured
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
            This provider needs an OAuth app configured on the server before it can be connected.
          </div>
          <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.7 }}>
            {setup.setup.map((s, i) => <li key={i}>{s}</li>)}
          </ol>
        </Card>
      )}

      <Card>
        <h2 style={{ marginBottom: 12 }}>Linked accounts</h2>
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
