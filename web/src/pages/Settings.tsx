import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { formDialog, notify } from "../components/dialog";
import { getTheme, applyTheme, Theme } from "../theme";

interface Tenant { name: string; plan: string; key_ownership_model: string; vaults: any[]; }
interface KeyInfo {
  vault_id: string; vault_name: string; provisioned: boolean; status: string;
  content_algorithm: string; signature_algorithm: string; recovery_kem: string | null;
  strength_bits: number; pq_hybrid: boolean; ownership_model: string | null; root_key_hash: string | null;
}

export default function Settings() {
  const { me, enrollPasskey, refresh } = useAuth();
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [keys, setKeys] = useState<KeyInfo[]>([]);
  const [toast, setToast] = useState("");
  const [theme, setThemeState] = useState<Theme>(getTheme());
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.get<Tenant>("/tenant").then(setTenant).catch(() => {}).finally(() => setLoaded(true));
    api.get<KeyInfo[]>("/tenant/keys").then(setKeys).catch(() => {});
  }, []);

  async function loadVaultsAndKeys() {
    const [t, k] = await Promise.all([
      api.get<Tenant>("/tenant").catch(() => null),
      api.get<KeyInfo[]>("/tenant/keys").catch(() => [] as KeyInfo[]),
    ]);
    if (t) setTenant(t);
    setKeys(k);
  }

  async function createVault() {
    const r = await formDialog({
      title: "New vault",
      message: "A vault is your own encrypted store with its own root key. You can route different sources into different vaults.",
      confirmLabel: "Create vault",
      fields: [
        { name: "name", label: "Vault name", required: true, placeholder: "e.g. Personal, Photos, Work" },
        { name: "key_ownership_model", label: "Key ownership", defaultValue: tenant?.key_ownership_model || "customer-managed",
          options: [
            { label: "Customer-managed — you hold the keys", value: "customer-managed" },
            { label: "Zero-knowledge — only you can decrypt", value: "zero-knowledge" },
          ] },
      ],
    });
    if (!r) return;
    try {
      await api.post("/tenant/vaults", r);
      await loadVaultsAndKeys();
      setToast("Vault created");
      setTimeout(() => setToast(""), 2500);
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Could not create vault", tone: "warn" });
    }
  }

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

      <NotificationSettings />

      <Card style={{ marginBottom: 16 }}>
        <h2 style={{ marginBottom: 4 }}>Your encryption keys</h2>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 14 }}>
          You hold the keys to your data. These live in your key broker record and are shown here so you can
          confirm their type, strength and status — the key material itself is never exposed.
        </div>
        {keys.map((k) => (
          <div key={k.vault_id} className="result-row">
            <div className="result-icon" style={{ background: "var(--bg-elev-2)" }}><Icon name="key" size={16} /></div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>{k.vault_name}</div>
              <div className="faint" style={{ fontSize: 12 }}>
                {k.content_algorithm} · {k.pq_hybrid ? "hybrid post-quantum" : "classical"} · {k.recovery_kem || "ML-KEM"} · {k.strength_bits}-bit
                {k.root_key_hash ? ` · ${k.root_key_hash.slice(0, 16)}…` : ""}
              </div>
            </div>
            <Pill tone={k.pq_hybrid ? "ok" : "info"} dot>{k.pq_hybrid ? "quantum-safe" : "classical"}</Pill>
            <Pill tone={k.status === "active" ? "ok" : "warn"} dot>{k.status}</Pill>
          </div>
        ))}
        {keys.length === 0 && <div className="muted" style={{ fontSize: 12.5 }}>No keys provisioned yet.</div>}
        <div className="divider" />
        <h3 style={{ marginBottom: 8 }}>How your keys are used</h3>
        <ul className="faint" style={{ fontSize: 12.5, margin: 0, paddingLeft: 18, lineHeight: 1.7 }}>
          <li>A unique root key encrypts your vault's contents with <b>AES-256-GCM</b>. Every vault has its own key.</li>
          <li>Your key is <b>wrapped</b> (never stored in the clear) and only unwrapped for operations you authorize by unlocking with your passkey.</li>
          <li><b>ML-KEM</b> protects recovery key exchange and <b>ML-DSA</b> signs appliance receipts — a hybrid, quantum-safe design.</li>
          <li>Arkive can't read your data: content stays encrypted end-to-end. Only you — or, for continuity, an authorized organization-admin key recovery — can unwrap it.</li>
        </ul>
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
              <Pill tone="ok" dot>active</Pill>
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
          <Row label="Organization" value={tenant?.name} />
          <Row label="Plan" value={tenant?.plan} />
          <Row label="Key ownership" value={tenant?.key_ownership_model} />
          <Row label="Your role" value={me?.role} />
          <div className="divider" />
          <div className="spread" style={{ marginBottom: 10 }}>
            <h3 style={{ margin: 0 }}>Your vaults</h3>
            <button className="btn primary sm" onClick={createVault}>
              <Icon name="shield" size={14} /> New vault
            </button>
          </div>
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
          {(!tenant?.vaults || tenant.vaults.length === 0) && (
            <div className="muted" style={{ fontSize: 12.5 }}>
              You don't have a vault yet. Create one to start protecting data.
            </div>
          )}
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

interface NotifType { key: string; label: string; icon: string; desc: string; }

function NotificationSettings() {
  const [types, setTypes] = useState<NotifType[]>([]);
  const [prefs, setPrefs] = useState<Record<string, boolean>>({});
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.get<{ types: NotifType[]; prefs: Record<string, boolean> }>("/me/notifications")
      .then((r) => { setTypes(r.types || []); setPrefs(r.prefs || {}); })
      .catch(() => {}).finally(() => setLoaded(true));
  }, []);

  async function toggle(key: string) {
    const prev = prefs;
    const next = { ...prefs, [key]: !prefs[key] };
    setPrefs(next);
    try { await api.put("/me/notifications", { prefs: { [key]: next[key] } }); }
    catch (e: any) { notify({ message: e.message || "Could not save", tone: "danger" }); setPrefs(prev); }
  }

  if (!loaded || types.length === 0) return null;
  return (
    <Card style={{ marginBottom: 16 }}>
      <h2 style={{ marginBottom: 4 }}>Email notifications</h2>
      <div className="muted" style={{ fontSize: 12.5, marginBottom: 6 }}>
        Choose which emails Arkive sends you. You can change these any time.
      </div>
      <div className="stack" style={{ gap: 0 }}>
        {types.map((t) => (
          <div key={t.key} className="spread"
               style={{ padding: "13px 0", borderTop: "1px solid var(--border-soft)", alignItems: "center", gap: 14 }}>
            <div className="row" style={{ gap: 12, alignItems: "flex-start", minWidth: 0 }}>
              <div className="result-icon" style={{ width: 34, height: 34, background: "var(--inset)", color: "var(--brand)", flexShrink: 0 }}>
                <Icon name={t.icon as IconName} size={17} />
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600, fontSize: 14 }}>{t.label}</div>
                <div className="faint" style={{ fontSize: 12.5 }}>{t.desc}</div>
              </div>
            </div>
            <Toggle on={!!prefs[t.key]} onClick={() => toggle(t.key)} />
          </div>
        ))}
      </div>
    </Card>
  );
}

export function Toggle({ on, onClick }: { on: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick} aria-pressed={on} title={on ? "On" : "Off"}
      style={{ width: 42, height: 24, borderRadius: 999, border: "none", cursor: "pointer", flexShrink: 0,
               background: on ? "var(--brand)" : "var(--border)", position: "relative", transition: "background .15s" }}>
      <span style={{ position: "absolute", top: 3, left: on ? 21 : 3, width: 18, height: 18, borderRadius: "50%",
                     background: "#fff", transition: "left .15s", boxShadow: "0 1px 2px rgba(0,0,0,.3)" }} />
    </button>
  );
}
