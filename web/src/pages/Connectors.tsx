import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, timeAgo } from "../components/ui";
import { Icon, IconName } from "../components/Icon";

interface CatalogItem {
  type: string;
  displayName: string;
  authType: string;
  scopes: string[];
  icon: string;
  color: string;
  docTypes: string[];
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
  const { me, unlock } = useAuth();
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [toast, setToast] = useState("");

  async function load() {
    setCatalog(await api.get<CatalogItem[]>("/connectors/catalog"));
    setAccounts(await api.get<Account[]>("/connectors/accounts"));
    const t = await api.get<{ vaults: Vault[] }>("/tenant");
    setVaults(t.vaults);
  }
  useEffect(() => { void load(); }, []);

  async function link(c: CatalogItem) {
    if (!me?.passkey_verified) {
      await unlock().catch((e) => alert(e.message));
    }
    const label = prompt(`Account label for ${c.displayName}`, `My ${c.displayName}`);
    if (!label) return;
    try {
      await api.post("/connectors/link", { connector_type: c.type, account_label: label });
      setToast(`${c.displayName} linked`);
      await load();
    } catch (e) {
      alert((e as ApiError).message);
    }
    setTimeout(() => setToast(""), 2500);
  }

  async function backup(a: Account) {
    const vault = vaults[0];
    if (!vault) return alert("No vault available");
    // Create a collection for this source then run a backup to cloud+appliance.
    const coll = await api.post<{ id: string }>("/collections", {
      vault_id: vault.id,
      name: a.account_label,
      source_type: a.connector_type,
      connector_account_id: a.id,
      destinations: ["cv-cloud"],
    });
    const res = await api.post<{ object_count: number; total_bytes: number }>(
      `/collections/${coll.id}/backup`,
      { destinations: ["cv-cloud"] }
    );
    setToast(`Backed up ${res.object_count} objects from ${a.account_label}`);
    await load();
    setTimeout(() => setToast(""), 3000);
  }

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>Connect a source</h2>
        <div className="muted" style={{ marginBottom: 16, fontSize: 13 }}>
          Sync workers continuously capture and encrypt data from your services. Content is
          encrypted before it leaves the connector environment.
        </div>
        <div className="grid grid-3">
          {catalog.map((c) => (
            <div key={c.type} className="dest-card" onClick={() => link(c)}>
              <div className="row" style={{ marginBottom: 10 }}>
                <div className="result-icon" style={{ background: c.color, width: 34, height: 34 }}>
                  <Icon name={c.icon as IconName} size={17} />
                </div>
                <div>
                  <div style={{ fontWeight: 650 }}>{c.displayName}</div>
                  <div className="faint" style={{ fontSize: 11.5 }}>{c.authType}</div>
                </div>
              </div>
              <div className="faint" style={{ fontSize: 12 }}>{c.docTypes.join(" · ")}</div>
            </div>
          ))}
        </div>
      </Card>

      <Card>
        <h2 style={{ marginBottom: 12 }}>Linked accounts</h2>
        {accounts.length === 0 && <div className="muted">No sources linked yet.</div>}
        {accounts.map((a) => (
          <div key={a.id} className="result-row">
            <div className="result-icon" style={{ background: catalog.find(c => c.type === a.connector_type)?.color ?? "#1a2234" }}>
              <Icon name={(catalog.find(c => c.type === a.connector_type)?.icon as IconName) ?? "database"} size={17} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>{a.account_label}</div>
              <div className="faint" style={{ fontSize: 12 }}>
                {a.connector_type} · last sync {timeAgo(a.last_sync_at)}
              </div>
            </div>
            <Pill tone={a.auth_status === "linked" ? "ok" : "warn"}>{a.auth_status}</Pill>
            <button className="btn sm primary" onClick={() => backup(a)}>Back up now</button>
          </div>
        ))}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}
