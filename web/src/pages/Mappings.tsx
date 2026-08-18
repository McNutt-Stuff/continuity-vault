import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { Card, Pill } from "../components/ui";
import { Icon } from "../components/Icon";

interface Account { id: string; connector_type: string; account_label: string; }
interface Vault { id: string; name: string; }
interface Appliance { id: string; name: string; serial: string; state: string; }
interface Mapping {
  id: string; name: string; source_type: string; vault_id: string;
  vault_name: string | null; connector_account_id: string | null;
  account_label: string | null; sensitivity: string; destinations: string[];
}

const DEST_LABEL: Record<string, string> = {
  "cv-cloud": "Cloud", "customer-s3": "Customer S3", appliance: "Appliance",
};

export default function Mappings() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [vaults, setVaults] = useState<Vault[]>([]);
  const [appliances, setAppliances] = useState<Appliance[]>([]);
  const [mappings, setMappings] = useState<Mapping[]>([]);
  const [toast, setToast] = useState("");

  // New-mapping form
  const [accountId, setAccountId] = useState("");
  const [vaultId, setVaultId] = useState("");
  const [dests, setDests] = useState<string[]>(["cv-cloud"]);

  async function load() {
    const [acc, tenant, coll] = await Promise.all([
      api.get<Account[]>("/connectors/accounts"),
      api.get<{ vaults: Vault[] }>("/tenant"),
      api.get<Mapping[]>("/collections"),
    ]);
    setAccounts(acc);
    setVaults(tenant.vaults);
    setMappings(coll);
    try { setAppliances(await api.get<Appliance[]>("/appliances")); } catch { /* ignore */ }
    if (!vaultId && tenant.vaults[0]) setVaultId(tenant.vaults[0].id);
    if (!accountId && acc[0]) setAccountId(acc[0].id);
  }
  useEffect(() => { void load(); }, []);

  function flash(m: string) { setToast(m); setTimeout(() => setToast(""), 3000); }

  function toggleDest(d: string) {
    setDests((cur) => cur.includes(d) ? cur.filter((x) => x !== d) : [...cur, d]);
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

  async function backup(m: Mapping) {
    try {
      const res = await api.post<{ object_count: number }>(`/collections/${m.id}/backup`, {});
      flash(`Backed up ${res.object_count} objects (${m.destinations.map((d) => DEST_LABEL[d] ?? d).join(", ")})`);
      await load();
    } catch (e) { flash(`Backup failed: ${(e as ApiError).message}`); }
  }

  const destOptions = ["cv-cloud", ...(appliances.length ? ["appliance"] : []), "customer-s3"];

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>Map sources to vaults</h2>
        <div className="muted" style={{ fontSize: 13, marginBottom: 14 }}>
          Route each source into one or more vaults, and choose where each mapping stores its
          data (cloud, an appliance, or customer S3). A source can feed many vaults, and a vault
          can hold many sources — configure the many-to-many layout that fits your context.
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
        <div className="row" style={{ gap: 8, marginTop: 12, flexWrap: "wrap" }}>
          <span className="faint" style={{ fontSize: 11.5, alignSelf: "center" }}>Destinations</span>
          {destOptions.map((d) => (
            <span
              key={d}
              className={`chip ${dests.includes(d) ? "active" : ""}`}
              onClick={() => toggleDest(d)}
            >
              {DEST_LABEL[d] ?? d}
            </span>
          ))}
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
                  <Pill key={d} tone={d === "appliance" ? "ok" : "info"}>{DEST_LABEL[d] ?? d}</Pill>
                ))}
                {m.sensitivity === "restricted" && <Pill tone="danger">restricted</Pill>}
              </div>
            </div>
            <button className="btn sm primary" onClick={() => backup(m)}>Back up now</button>
            <button className="btn sm ghost" onClick={() => remove(m)}>Remove</button>
          </div>
        ))}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}
