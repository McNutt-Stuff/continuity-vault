import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, Pill } from "../components/ui";
import { Icon } from "../components/Icon";

interface Tenant { name: string; plan: string; key_ownership_model: string; vaults: any[]; }

export default function Settings() {
  const { me, enrollPasskey, refresh } = useAuth();
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [toast, setToast] = useState("");

  useEffect(() => { api.get<Tenant>("/tenant").then(setTenant).catch(() => {}); }, []);

  async function addPasskey(transport: string) {
    await enrollPasskey(transport === "internal" ? "This device" : "Security key", transport);
    await refresh();
    setToast("Passkey enrolled");
    setTimeout(() => setToast(""), 2500);
  }

  return (
    <>
      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <Card>
          <h2 style={{ marginBottom: 12 }}>Identity & unlock</h2>
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
            Passkeys and hardware tokens unlock the interfaces where you access protected data.
          </div>
          {me?.passkeys.map((p) => (
            <div key={p.id} className="result-row">
              <div className="result-icon" style={{ background: "#1a2234" }}><Icon name="key" size={16} /></div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>{p.label}</div>
                <div className="faint" style={{ fontSize: 12 }}>{p.transport}</div>
              </div>
              <Pill tone="ok">active</Pill>
            </div>
          ))}
          <div className="row" style={{ marginTop: 12, gap: 8 }}>
            <button className="btn sm" onClick={() => addPasskey("internal")}>
              <Icon name="key" size={14} /> Add device passkey
            </button>
            <button className="btn sm" onClick={() => addPasskey("usb")}>
              <Icon name="lock" size={14} /> Add hardware token
            </button>
          </div>
        </Card>

        <Card>
          <h2 style={{ marginBottom: 12 }}>Organization</h2>
          <Row label="Tenant" value={tenant?.name} />
          <Row label="Plan" value={tenant?.plan} />
          <Row label="Key ownership" value={tenant?.key_ownership_model} />
          <Row label="Your role" value={me?.role} />
          <div className="divider" />
          <h3 style={{ marginBottom: 10 }}>Vaults</h3>
          {tenant?.vaults.map((v) => (
            <div key={v.id} className="result-row">
              <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
                <Icon name="shield" size={16} />
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>{v.name}</div>
                <div className="faint mono" style={{ fontSize: 11 }}>{v.crypto_profile_id}</div>
              </div>
              <Pill tone="info">{v.key_ownership_model}</Pill>
            </div>
          ))}
        </Card>
      </div>
      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </>
  );
}

function Row({ label, value }: { label: string; value?: string }) {
  return (
    <div className="spread" style={{ padding: "9px 0", borderBottom: "1px solid var(--border-soft)" }}>
      <span className="faint" style={{ fontSize: 12.5 }}>{label}</span>
      <span style={{ fontWeight: 600 }}>{value ?? "—"}</span>
    </div>
  );
}
