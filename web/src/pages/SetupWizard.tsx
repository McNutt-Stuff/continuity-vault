import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Pill, bytes } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { DestIcon } from "../components/DestIcon";
import { notify, formDialog } from "../components/dialog";
import { AddStorageModal, ProviderSpec } from "./CloudStorage";

// ---- Shared types (loose; the wizard reuses the platform's own endpoints) --
interface TenantInfo {
  name: string; plan: string; tenant_type: string; can_admin: boolean;
  protection_options: string[];
  vaults: { id: string; name: string }[];
}
interface CatalogItem {
  type: string; displayName: string; icon: string; color: string; category: string;
  family: string; mode: "oauth" | "token"; configured: boolean; requiresAgent?: boolean;
}
interface Account { id: string; connector_type: string; account_label: string; auth_status: string; }
interface StorageInstance { id: string; name: string; provider: string; provider_display: string; status: string; enabled: boolean; }
interface StorageTarget { id: string; label: string; kind: string; provider?: string; }

const OPTIONS: { id: string; title: string; tagline: string; icon: IconName; color: string; badge?: string; note: string }[] = [
  { id: "cv-cloud", title: "Arkive Cloud", icon: "cloud", color: "#0559c9", badge: "Recommended",
    tagline: "Fully-managed, multi-region cloud — nothing to set up.",
    note: "We host and manage everything. Your data is encrypted with our quantum-safe cipher before it ever leaves Arkive." },
  { id: "customer-cloud", title: "Bring your own storage", icon: "database", color: "#f59e0b",
    tagline: "Your own AWS, Azure or Google Cloud bucket.",
    note: "Back up into infrastructure you own. We can auto-provision a dedicated, least-privilege bucket + keys for you." },
  { id: "appliance", title: "Hardware appliance", icon: "server", color: "#35d0a5",
    tagline: "An on-prem sealed appliance you can order.",
    note: "A tamper-sealed box on your own network for fully-offline, air-gapped recovery. We'll help you order one." },
];

const STEPS = ["Protection", "Sources", "Storage", "First backup", "Done"];
const STEP_KEY = "cv_setup_step";

export default function SetupWizard({ onDone }: { onDone: () => void }) {
  const { me, stepUp, refresh } = useAuth();
  const [step, setStep] = useState<number>(() => {
    const s = Number(localStorage.getItem(STEP_KEY));
    return s >= 1 && s <= 5 ? s : 1;
  });
  const [tenant, setTenant] = useState<TenantInfo | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => { localStorage.setItem(STEP_KEY, String(step)); }, [step]);
  async function loadTenant() {
    try { setTenant(await api.get<TenantInfo>("/tenant")); } catch { /* ignore */ }
  }
  useEffect(() => { void loadTenant(); }, []);

  // Returning from an OAuth source connect lands here (?connected=…) — jump to
  // the Sources step so the user sees their freshly-linked account.
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (p.get("connected") || p.get("error")) {
      window.history.replaceState({}, "", window.location.pathname);
      setStep((s) => Math.max(s, 2));
    }
  }, []);

  const canEditProtection = !!tenant && (tenant.tenant_type === "shared" || tenant.can_admin);

  async function finish() {
    setBusy(true);
    try {
      await api.post("/auth/complete-setup", {});
      localStorage.removeItem(STEP_KEY);
      await refresh();
      onDone();
    } catch (e) { await notify({ title: "Couldn't finish", message: (e as Error).message, tone: "danger" }); }
    finally { setBusy(false); }
  }
  async function skip() {
    setBusy(true);
    try { await api.post("/auth/complete-setup", {}); localStorage.removeItem(STEP_KEY); await refresh(); onDone(); }
    catch { /* ignore */ }
    finally { setBusy(false); }
  }

  return (
    <div className="wiz-backdrop">
      <div className="wiz-panel">
        <div className="wiz-head">
          <div className="row" style={{ gap: 10, alignItems: "center" }}>
            <div className="wiz-logo"><Icon name="shield" size={18} /></div>
            <div>
              <div style={{ fontWeight: 700, fontSize: 15 }}>Welcome to Arkive{me?.display_name ? `, ${me.display_name.split(" ")[0]}` : ""}</div>
              <div className="faint" style={{ fontSize: 12 }}>Let's get you protected in a few quick steps.</div>
            </div>
          </div>
          <button className="btn ghost sm" disabled={busy} onClick={() => void skip()}>Skip for now</button>
        </div>

        <div className="wiz-steps">
          {STEPS.map((label, i) => {
            const n = i + 1;
            return (
              <div key={label} className={`wiz-step ${n === step ? "active" : ""} ${n < step ? "done" : ""}`}>
                <span className="wiz-step-dot">{n < step ? <Icon name="check" size={12} /> : n}</span>
                <span className="wiz-step-label">{label}</span>
              </div>
            );
          })}
        </div>

        <div className="wiz-body">
          {step === 1 && <StepProtection tenant={tenant} canEdit={canEditProtection}
                                          onNext={() => setStep(2)} reload={loadTenant} />}
          {step === 2 && <StepSources me={me} stepUp={stepUp} onNext={() => setStep(3)} onBack={() => setStep(1)} />}
          {step === 3 && <StepStorage tenant={tenant} onNext={() => setStep(4)} onBack={() => setStep(2)} />}
          {step === 4 && <StepDataMap tenant={tenant} onNext={() => setStep(5)} onBack={() => setStep(3)} />}
          {step === 5 && <StepDone tenant={tenant} busy={busy} onBack={() => setStep(4)} onFinish={() => void finish()} />}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Step 1 — Protection level & storage options                                 //
// --------------------------------------------------------------------------- //
function StepProtection({ tenant, canEdit, onNext, reload }:
  { tenant: TenantInfo | null; canEdit: boolean; onNext: () => void; reload: () => Promise<void> }) {
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [init, setInit] = useState(false);

  useEffect(() => {
    if (tenant && !init) { setSel(new Set(tenant.protection_options || [])); setInit(true); }
  }, [tenant, init]);

  function toggle(id: string) {
    if (!canEdit) return;
    setSel((c) => { const n = new Set(c); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }
  async function next() {
    if (canEdit && sel.size === 0) { await notify({ title: "Pick at least one", message: "Choose how your data is protected to continue.", tone: "warn" }); return; }
    if (canEdit) {
      setSaving(true);
      try {
        await api.put("/billing/plan", { options: [...sel], licensed_tb: 1, appliance_plan: [] });
        await reload();
      } catch (e) { await notify({ title: "Couldn't save", message: (e as Error).message, tone: "danger" }); setSaving(false); return; }
      setSaving(false);
    }
    onNext();
  }

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <h2 style={{ margin: 0 }}>How would you like to protect your data?</h2>
        <div className="faint" style={{ fontSize: 13 }}>
          {canEdit
            ? "Choose one or more places to keep encrypted, recoverable copies. You can change this anytime in Protection Setup."
            : "Your organization has already chosen how data is protected. Here's what's enabled for you."}
        </div>
      </div>
      <div className="grid grid-3" style={{ gap: 12 }}>
        {OPTIONS.map((o) => {
          const on = sel.has(o.id);
          return (
            <div key={o.id} className={`wiz-opt ${on ? "on" : ""} ${!canEdit ? "ro" : ""}`} onClick={() => toggle(o.id)}>
              <div className="row" style={{ gap: 10, alignItems: "center", marginBottom: 8 }}>
                <div className="insight-card-ic" style={{ background: `${o.color}1e`, color: o.color, width: 38, height: 38 }}>
                  <Icon name={o.icon} size={20} />
                </div>
                <div className="flex1">
                  <div style={{ fontWeight: 700 }}>{o.title}</div>
                  <div className="faint" style={{ fontSize: 11.5 }}>{o.tagline}</div>
                </div>
                {on ? <Icon name="check" size={18} /> : <span className="wiz-check-empty" />}
              </div>
              <div className="faint" style={{ fontSize: 12, lineHeight: 1.45 }}>{o.note}</div>
              {o.badge && <div style={{ marginTop: 8 }}><Pill tone="ok">{o.badge}</Pill></div>}
            </div>
          );
        })}
      </div>
      <div className="wiz-foot">
        <div style={{ flex: 1 }} />
        <button className="btn primary" disabled={saving} onClick={() => void next()}>
          {saving ? "Saving…" : "Continue"} <Icon name="restore" size={14} />
        </button>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Step 2 — Add source(s)                                                       //
// --------------------------------------------------------------------------- //
function StepSources({ me, stepUp, onNext, onBack }:
  { me: ReturnType<typeof useAuth>["me"]; stepUp: () => Promise<void>; onNext: () => void; onBack: () => void }) {
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [q, setQ] = useState("");

  async function load() {
    try {
      const [cat, acc] = await Promise.all([
        api.get<CatalogItem[]>("/connectors/catalog"),
        api.get<Account[]>("/connectors/accounts"),
      ]);
      setCatalog(cat); setAccounts(acc.filter((a) => a.auth_status !== "unlinked"));
    } catch { /* ignore */ }
  }
  useEffect(() => { void load(); }, []);

  async function connect(c: CatalogItem) {
    if (c.requiresAgent) {
      await notify({ title: `${c.displayName} needs a desktop agent`, message: "Install the Arkive desktop agent on the device that holds this source, then add it from the Sources page.", tone: "info" });
      return;
    }
    if (!me?.passkey_verified) {
      try { await stepUp(); } catch (e) { await notify({ message: (e as Error).message, tone: "danger" }); return; }
    }
    if (c.mode === "oauth") {
      if (!c.configured) { await notify({ title: "Not available yet", message: `${c.displayName} needs to be configured by an administrator first.`, tone: "warn" }); return; }
      try {
        const res = await api.post<{ authorize_url: string }>(`/connectors/${c.type}/connect`, {});
        window.location.href = res.authorize_url;  // returns to the wizard via ?connected=
      } catch (e) { await notify({ title: "Couldn't start", message: (e as ApiError).message, tone: "danger" }); }
      return;
    }
    // Token-based sources (iCloud, 1Password Connect, generic token).
    let fields: { name: string; label: string; password?: boolean; required?: boolean; placeholder?: string; defaultValue?: string }[];
    if (c.type === "icloud") {
      fields = [
        { name: "username", label: "Apple ID (email)", required: true },
        { name: "token", label: "App-specific password", password: true, required: true },
        { name: "label", label: "Account label", defaultValue: `My ${c.displayName}` },
      ];
    } else if (c.type === "onepassword") {
      fields = [
        { name: "host", label: "Connect server URL", placeholder: "https://connect.example.com", required: true },
        { name: "token", label: "Connect token", password: true, required: true },
        { name: "label", label: "Account label", defaultValue: `My ${c.displayName}` },
      ];
    } else {
      fields = [
        { name: "token", label: "API token", password: true, required: true },
        { name: "label", label: "Account label", defaultValue: `My ${c.displayName}` },
      ];
    }
    const r = await formDialog({ title: `Connect ${c.displayName}`, confirmLabel: "Connect", fields });
    if (!r) return;
    try { await api.post(`/connectors/${c.type}/token`, r); await load(); await notify({ title: "Connected", message: `${c.displayName} is linked.`, tone: "ok" }); }
    catch (e) { await notify({ title: "Couldn't connect", message: (e as ApiError).message, tone: "danger" }); }
  }

  const ql = q.trim().toLowerCase();
  const shown = catalog.filter((c) => !ql || c.displayName.toLowerCase().includes(ql) || c.category.toLowerCase().includes(ql));

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <h2 style={{ margin: 0 }}>Connect the things you want to protect</h2>
        <div className="faint" style={{ fontSize: 13 }}>
          Add one or more sources — email, files, photos, passwords and more. Arkive keeps encrypted,
          searchable, recoverable copies. You can add more later from the Sources page.
        </div>
      </div>

      {accounts.length > 0 && (
        <div className="wiz-connected">
          <span className="faint" style={{ fontSize: 12 }}>Connected:</span>
          {accounts.map((a) => (
            <span key={a.id} className="dest-chip">
              <SourceIcon type={a.connector_type} size={14} fallback="database" /> {a.account_label}
            </span>
          ))}
        </div>
      )}

      <input className="input" placeholder="Search sources…" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="wiz-grid">
        {shown.map((c) => (
          <div key={c.type} className="dest-card" onClick={() => void connect(c)}>
            <div className="row" style={{ gap: 10, alignItems: "center" }}>
              <div className="insight-card-ic" style={{ background: `${c.color}1e`, color: c.color, width: 34, height: 34 }}>
                <SourceIcon type={c.type} size={19} fallback="database" />
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 650, fontSize: 13 }}>{c.displayName}</div>
                <div className="faint" style={{ fontSize: 11 }}>{c.category}</div>
              </div>
              {c.requiresAgent && <Icon name="user" size={13} />}
            </div>
          </div>
        ))}
        {shown.length === 0 && <div className="muted">No sources match “{q}”.</div>}
      </div>

      <div className="wiz-foot">
        <button className="btn ghost" onClick={onBack}>← Back</button>
        <div style={{ flex: 1 }} />
        <button className="btn ghost" onClick={onNext}>{accounts.length ? "Continue" : "Skip for now"}</button>
        {accounts.length > 0 && <button className="btn primary" onClick={onNext}>Continue <Icon name="restore" size={14} /></button>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Step 3 — Set up storage (BYOS auto-provision) / Arkive Cloud ready          //
// --------------------------------------------------------------------------- //
function StepStorage({ tenant, onNext, onBack }:
  { tenant: TenantInfo | null; onNext: () => void; onBack: () => void }) {
  const opts = tenant?.protection_options || [];
  const [instances, setInstances] = useState<StorageInstance[]>([]);
  const [providers, setProviders] = useState<ProviderSpec[]>([]);
  const [showAdd, setShowAdd] = useState(false);

  async function load() {
    try {
      const r = await api.get<{ providers: ProviderSpec[]; instances: StorageInstance[] }>("/storage");
      setProviders(r.providers || []); setInstances(r.instances || []);
    } catch { /* ignore */ }
  }
  useEffect(() => {
    void load();
    const t = setInterval(load, 6000);  // provisioning updates live
    return () => clearInterval(t);
  }, []);

  const wantsByos = opts.includes("customer-cloud");
  const wantsCloud = opts.includes("cv-cloud");
  const wantsAppliance = opts.includes("appliance");

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <h2 style={{ margin: 0 }}>Set up your storage</h2>
        <div className="faint" style={{ fontSize: 13 }}>Where your encrypted backups will land.</div>
      </div>

      {wantsCloud && (
        <div className="wiz-ready">
          <div className="insight-card-ic" style={{ background: "#0559c91e", color: "#0559c9", width: 40, height: 40 }}><Icon name="cloud" size={20} /></div>
          <div className="flex1">
            <div style={{ fontWeight: 700 }}>Arkive Cloud is ready</div>
            <div className="faint" style={{ fontSize: 12 }}>Nothing to configure — your managed, multi-region cloud is provisioned and standing by.</div>
          </div>
          <Pill tone="ok" dot>Ready</Pill>
        </div>
      )}

      {wantsByos && (
        <div className="stack" style={{ gap: 10 }}>
          <div className="spread">
            <div style={{ fontWeight: 700 }}>Your cloud storage</div>
            <button className="btn primary sm" onClick={() => setShowAdd(true)}><Icon name="link" size={13} /> Add storage</button>
          </div>
          {instances.length === 0 ? (
            <div className="wiz-empty">
              Connect your own AWS, Azure or Google Cloud bucket. We can <b>set it up for you</b> —
              creating a dedicated bucket plus separate least-privilege write & read keys automatically.
            </div>
          ) : instances.map((s) => {
            const provisioning = ["provisioning", "starting"].includes(s.status);
            return (
              <div key={s.id} className="wiz-ready">
                <div className="insight-card-ic" style={{ background: "var(--inset)", width: 40, height: 40 }}>
                  <SourceIcon type={s.provider} size={20} fallback="cloud" />
                </div>
                <div className="flex1">
                  <div style={{ fontWeight: 700 }}>{s.name}</div>
                  <div className="faint" style={{ fontSize: 12 }}>{s.provider_display}</div>
                </div>
                {provisioning ? <Pill tone="info"><span className="spinner-dot" /> Setting up</Pill>
                  : <Pill tone={s.status === "healthy" ? "ok" : "warn"} dot>{s.status === "healthy" ? "Ready" : s.status}</Pill>}
              </div>
            );
          })}
        </div>
      )}

      {wantsAppliance && (
        <div className="wiz-ready">
          <div className="insight-card-ic" style={{ background: "#35d0a51e", color: "#35d0a5", width: 40, height: 40 }}><Icon name="server" size={20} /></div>
          <div className="flex1">
            <div style={{ fontWeight: 700 }}>Hardware appliance</div>
            <div className="faint" style={{ fontSize: 12 }}>We'll help you order a sealed on-prem appliance. Once it arrives, activate it from the Appliances page and it'll appear as a storage target.</div>
          </div>
          <Pill tone="info">Order to come</Pill>
        </div>
      )}

      <div className="wiz-foot">
        <button className="btn ghost" onClick={onBack}>← Back</button>
        <div style={{ flex: 1 }} />
        <button className="btn primary" onClick={onNext}>Continue <Icon name="restore" size={14} /></button>
      </div>

      {showAdd && (
        <AddStorageModal providers={providers}
                         onClose={() => setShowAdd(false)}
                         onDone={() => { setShowAdd(false); void load(); }} />
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Step 4 — First data map + test sync                                         //
// --------------------------------------------------------------------------- //
interface JobRow { id: string; collection_id?: string; status: string; processed: number; total: number; message: string; }
interface ActEvent { collection_id?: string; object_count?: number; total_bytes?: number; status: string; }
function StepDataMap({ tenant, onNext, onBack }:
  { tenant: TenantInfo | null; onNext: () => void; onBack: () => void }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [targets, setTargets] = useState<StorageTarget[]>([]);
  const [srcId, setSrcId] = useState("");
  const [destId, setDestId] = useState("");
  const [phase, setPhase] = useState<"idle" | "creating" | "syncing" | "done" | "error">("idle");
  const [progress, setProgress] = useState<{ processed: number; total: number; message: string }>({ processed: 0, total: 0, message: "" });
  const [result, setResult] = useState<{ objects: number; bytes: number } | null>(null);
  const [collId, setCollId] = useState<string | null>(null);
  const jobId = useRef<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  async function load() {
    try {
      const [acc, tgt] = await Promise.all([
        api.get<Account[]>("/connectors/accounts"),
        api.get<StorageTarget[]>("/tenant/storage-targets"),
      ]);
      const live = acc.filter((a) => a.auth_status !== "unlinked");
      setAccounts(live);
      setTargets(tgt);
      if (!srcId && live[0]) setSrcId(live[0].id);
      if (!destId && tgt[0]) setDestId(tgt[0].id);
    } catch { /* ignore */ }
  }
  useEffect(() => { void load(); return () => { if (pollRef.current) clearInterval(pollRef.current); }; }, []);

  const src = accounts.find((a) => a.id === srcId);
  const vaultId = tenant?.vaults?.[0]?.id;

  async function start() {
    if (!src || !destId || !vaultId) return;
    setPhase("creating");
    try {
      const coll = await api.post<{ id: string }>("/collections", {
        vault_id: vaultId,
        name: src.account_label || src.connector_type,
        source_type: src.connector_type,
        connector_account_id: src.id,
        destinations: [destId],
      });
      setCollId(coll.id);
      const job = await api.post<{ job_id?: string }>(`/collections/${coll.id}/sync`, {});
      jobId.current = job.job_id || null;
      setPhase("syncing");
      poll(coll.id);
    } catch (e) {
      setPhase("error");
      await notify({ title: "Couldn't start the backup", message: (e as ApiError).message, tone: "danger" });
    }
  }

  function poll(cid: string) {
    if (pollRef.current) clearInterval(pollRef.current);
    let ticks = 0;
    pollRef.current = setInterval(async () => {
      ticks++;
      try {
        const act = await api.get<{ jobs: JobRow[]; events: ActEvent[] }>("/activity");
        const job = act.jobs.find((j) => j.id === jobId.current || j.collection_id === cid);
        if (job) setProgress({ processed: job.processed, total: job.total, message: job.message || "Backing up…" });
        const ev = act.events.find((e) => e.collection_id === cid && e.status === "recoverable");
        const jobGone = !job;
        if (ev || (jobGone && ticks > 2)) {
          if (pollRef.current) clearInterval(pollRef.current);
          setResult({ objects: ev?.object_count || progress.processed || 0, bytes: ev?.total_bytes || 0 });
          setPhase("done");
        }
      } catch { /* keep polling */ }
      if (ticks > 120) { if (pollRef.current) clearInterval(pollRef.current); setPhase("done"); }
    }, 2500);
  }

  const pct = progress.total > 0 ? Math.min(100, Math.round((progress.processed / progress.total) * 100)) : (phase === "syncing" ? 40 : 0);

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div>
        <h2 style={{ margin: 0 }}>Run your first backup</h2>
        <div className="faint" style={{ fontSize: 13 }}>Route a source to a storage destination and we'll run a test backup so you can see it work.</div>
      </div>

      {accounts.length === 0 ? (
        <div className="wiz-empty">
          You haven't connected a source yet. Go back a step to add one, or skip — you can set up your Data Map anytime.
        </div>
      ) : (
        <div className="grid grid-2" style={{ gap: 12 }}>
          <label className="stack" style={{ gap: 4 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Source</span>
            <select className="input" value={srcId} onChange={(e) => setSrcId(e.target.value)} disabled={phase !== "idle"}>
              {accounts.map((a) => <option key={a.id} value={a.id}>{a.account_label || a.connector_type}</option>)}
            </select>
          </label>
          <label className="stack" style={{ gap: 4 }}>
            <span className="faint" style={{ fontSize: 11.5 }}>Store it to</span>
            <select className="input" value={destId} onChange={(e) => setDestId(e.target.value)} disabled={phase !== "idle"}>
              {targets.length === 0 && <option value="">No destination — check Step 1</option>}
              {targets.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
            </select>
          </label>
        </div>
      )}

      {phase !== "idle" && (
        <div className="wiz-sync">
          <div className="row" style={{ gap: 10, alignItems: "center", marginBottom: 8 }}>
            {src && (brandForSource(src.connector_type)
              ? <BrandIcon name={brandForSource(src.connector_type)!} size={20} />
              : <SourceIcon type={src.connector_type} size={20} fallback="database" />)}
            <Icon name="restore" size={14} />
            <DestIcon dest={destId} provider={targets.find((t) => t.id === destId)?.provider} size={18} />
            <div className="flex1" />
            {phase === "done"
              ? <Pill tone="ok" dot>Complete</Pill>
              : phase === "error" ? <Pill tone="danger" dot>Failed</Pill>
                : <Pill tone="info"><span className="spinner-dot" /> {phase === "creating" ? "Preparing…" : "Backing up…"}</Pill>}
          </div>
          {(phase === "syncing" || phase === "creating") && (
            <>
              <div className="progress"><div style={{ width: `${pct}%`, height: "100%", background: "var(--brand)", transition: "width .4s" }} /></div>
              <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>
                {progress.message || "Encrypting and uploading your first snapshot…"}
                {progress.total > 0 ? ` · ${progress.processed}/${progress.total}` : ""}
              </div>
            </>
          )}
          {phase === "done" && (
            <div className="faint" style={{ fontSize: 12.5 }}>
              First recovery point created{result && result.objects ? ` — ${result.objects.toLocaleString()} item(s)${result.bytes ? `, ${bytes(result.bytes)}` : ""}` : ""}. It's now searchable and recoverable.
            </div>
          )}
        </div>
      )}

      <div className="wiz-foot">
        <button className="btn ghost" onClick={onBack} disabled={phase === "syncing" || phase === "creating"}>← Back</button>
        <div style={{ flex: 1 }} />
        {phase === "idle" && accounts.length > 0 && (
          <button className="btn primary" disabled={!src || !destId || !vaultId} onClick={() => void start()}>
            <Icon name="restore" size={14} /> Run test backup
          </button>
        )}
        {(phase === "done" || phase === "error" || accounts.length === 0) && (
          <button className="btn primary" onClick={onNext}>Continue <Icon name="restore" size={14} /></button>
        )}
        {phase === "idle" && accounts.length > 0 && <button className="btn ghost" onClick={onNext}>Skip</button>}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Step 5 — Finish                                                             //
// --------------------------------------------------------------------------- //
function StepDone({ tenant, busy, onBack, onFinish }:
  { tenant: TenantInfo | null; busy: boolean; onBack: () => void; onFinish: () => void }) {
  const next: { icon: IconName; title: string; body: string }[] = [
    { icon: "database", title: "Fine-tune your Data Map", body: "Add more sources, choose backup schedules and route data to different storage." },
    { icon: "search", title: "Search & recover anything", body: "Everything you back up is encrypted, searchable and recoverable from Unified Search." },
    { icon: "shield", title: "Review Protection Setup", body: "Adjust your storage tiers, licensed amount and see your value over time." },
    { icon: "clock", title: "Automatic backups", body: "Arkive keeps protecting you on a schedule — new items are captured as they appear." },
  ];
  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="stack" style={{ alignItems: "center", textAlign: "center", gap: 8, paddingTop: 6 }}>
        <div className="wiz-done-ic"><Icon name="check" size={30} /></div>
        <h2 style={{ margin: 0 }}>You're all set{tenant?.name ? `, welcome to ${tenant.name}` : ""}!</h2>
        <div className="faint" style={{ fontSize: 13, maxWidth: 520 }}>
          Your baseline protection is configured and your first backup is on its way. Here's what to explore next.
        </div>
      </div>
      <div className="grid grid-2" style={{ gap: 12 }}>
        {next.map((n) => (
          <div key={n.title} className="wiz-next">
            <div className="insight-card-ic" style={{ background: "var(--inset)", width: 36, height: 36 }}><Icon name={n.icon} size={18} /></div>
            <div>
              <div style={{ fontWeight: 650, fontSize: 13.5 }}>{n.title}</div>
              <div className="faint" style={{ fontSize: 12, lineHeight: 1.45 }}>{n.body}</div>
            </div>
          </div>
        ))}
      </div>
      <div className="wiz-foot">
        <button className="btn ghost" onClick={onBack} disabled={busy}>← Back</button>
        <div style={{ flex: 1 }} />
        <button className="btn primary" disabled={busy} onClick={onFinish}>
          {busy ? "Finishing…" : "Enter Arkive"} <Icon name="restore" size={14} />
        </button>
      </div>
    </div>
  );
}
