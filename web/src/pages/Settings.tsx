import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { getTheme, applyTheme, Theme } from "../theme";

interface Tenant { name: string; plan: string; key_ownership_model: string; vaults: any[]; }

export default function Settings() {
  const { me, enrollPasskey, refresh } = useAuth();
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [toast, setToast] = useState("");
  const [theme, setThemeState] = useState<Theme>(getTheme());
  const [loaded, setLoaded] = useState(false);

  useEffect(() => { api.get<Tenant>("/tenant").then(setTenant).catch(() => {}).finally(() => setLoaded(true)); }, []);

  function pickTheme(t: Theme) { applyTheme(t); setThemeState(t); }

  async function addPasskey(transport: string) {
    await enrollPasskey(transport === "internal" ? "This device" : "Security key");
    await refresh();
    setToast("Passkey enrolled");
    setTimeout(() => setToast(""), 2500);
  }

  if (!loaded && !tenant) return <Loading label="Loading settings…" />;

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>Appearance</h2>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
          Choose how Arkive looks on this device.
        </div>
        <div className="row" style={{ gap: 12 }}>
          {([
            { id: "dark" as Theme, label: "Dark", icon: "moon" as const },
            { id: "light" as Theme, label: "Light", icon: "sun" as const },
          ]).map((opt) => {
            const on = theme === opt.id;
            return (
              <div key={opt.id} onClick={() => pickTheme(opt.id)}
                   style={{ cursor: "pointer", flex: "0 0 150px", border: `1.5px solid ${on ? "var(--brand)" : "var(--border-soft)"}`,
                            borderRadius: 12, padding: 14, background: on ? "rgba(79,124,255,.08)" : "var(--inset)", transition: "all .12s" }}>
                <div className="spread" style={{ marginBottom: 10 }}>
                  <Icon name={opt.icon} size={16} />
                  {on && <Icon name="check" size={15} />}
                </div>
                {/* Mini preview swatch */}
                <div style={{ display: "flex", gap: 6, marginBottom: 10 }}>
                  <span style={{ width: 30, height: 20, borderRadius: 4, background: opt.id === "light" ? "#f4f6fb" : "#0b0f17", border: "1px solid var(--border-soft)" }} />
                  <span style={{ width: 30, height: 20, borderRadius: 4, background: opt.id === "light" ? "#ffffff" : "#141b2b", border: "1px solid var(--border-soft)" }} />
                  <span style={{ width: 20, height: 20, borderRadius: 4, background: "#4f7cff" }} />
                </div>
                <div style={{ fontWeight: 600, fontSize: 13 }}>{opt.label}</div>
              </div>
            );
          })}
        </div>
      </Card>

      <div className="grid grid-2" style={{ alignItems: "start" }}>
        <Card>
          <h2 style={{ marginBottom: 12 }}>Identity & unlock</h2>
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
            Passkeys and hardware tokens unlock the interfaces where you access protected data.
          </div>
          {me?.passkeys.map((p) => (
            <div key={p.id} className="result-row">
              <div className="result-icon" style={{ background: "var(--bg-elev-2)" }}><Icon name="key" size={16} /></div>
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
