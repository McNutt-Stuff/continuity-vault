import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Card, Pill } from "../components/ui";
import { Icon } from "../components/Icon";

interface Account { id: string; connector_type: string; account_label: string; }
interface Vault { id: string; name: string; }
interface StorageTarget {
  id: string; kind: string; label: string; detail?: string;
  state?: string; online?: boolean;
}
interface Mapping {
  id: string; name: string; source_type: string; vault_id: string;
  vault_name: string | null; connector_account_id: string | null;
  account_label: string | null; sensitivity: string; destinations: string[];
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
  }
  useEffect(() => { void load(); }, []);

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
    if (!confirm(`Remove mapping "${m.name}"?`)) return;
    try { await api.del(`/collections/${m.id}`); flash("Mapping removed"); await load(); }
    catch (e) { flash((e as ApiError).message); }
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

      <Card>
        <h3 style={{ marginBottom: 12 }}>Mappings</h3>
        {mappings.length === 0 && <div className="muted">No mappings yet. Add one above.</div>}
        {mappings.map((m) => (
          <div key={m.id} className="result-row">
            <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
              <Icon name="database" size={17} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>
                {m.account_label ?? m.source_type} <span className="faint">→</span> {m.vault_name ?? m.vault_id}
              </div>
              <div className="row" style={{ gap: 6, marginTop: 4, flexWrap: "wrap" }}>
                <Pill tone="info">{m.source_type}</Pill>
                {(m.destinations || []).map((d) => (
                  <Pill key={d} tone={d.startsWith("appliance") ? "ok" : "info"}>{destLabel(d)}</Pill>
                ))}
                {m.sensitivity === "restricted" && <Pill tone="danger">restricted</Pill>}
              </div>
            </div>
            <button className="btn sm ghost" onClick={() => remove(m)}>Remove</button>
          </div>
        ))}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}
