import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, bytes, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { confirmDialog, formDialog, notify } from "../components/dialog";

interface OrgSummary {
  id: string; name: string; plan: string; key_ownership_model: string;
  counts: { users: number; admins: number; vaults: number; appliances: number };
}
interface Member {
  id: string; email: string; display_name: string; role: string; status: string;
  email_verified: boolean; has_passkey: boolean;
  vault_count: number; object_count: number; protected_bytes: number;
}
interface Assignment { user_id: string; display_name: string; email: string; role: string; can_manage: boolean; }
interface OrgAppliance {
  id: string; name: string; model: string; serial: string; state: string; online: boolean;
  location_label: string; capacity_bytes: number; used_bytes: number; assignments: Assignment[];
}
interface KeyRow {
  vault_id: string; vault_name: string; owner_user_id: string | null;
  owner_name: string; owner_email: string | null;
  provisioned: boolean; status: string; ownership_model: string | null;
  content_algorithm: string; signature_algorithm: string; recovery_kem: string | null;
  strength_bits: number; pq_hybrid: boolean; root_key_hash: string | null;
}

const ROLE_TONE: Record<string, "info" | "ok" | "warn"> = { owner: "ok", admin: "info", member: "warn" };
const ROLE_LABEL: Record<string, string> = { owner: "Owner", admin: "Admin", member: "Member", "security-admin": "Admin" };
const roleLabel = (r: string) => ROLE_LABEL[r] ?? r;

type Tab = "members" | "appliances" | "keys";

export default function Organization() {
  const { me, stepUp, refresh } = useAuth();
  const [tab, setTab] = useState<Tab>("members");
  const [summary, setSummary] = useState<OrgSummary | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [appliances, setAppliances] = useState<OrgAppliance[]>([]);
  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [loaded, setLoaded] = useState(false);

  const isOwner = me?.is_owner || me?.role === "owner";

  async function load() {
    try {
      const [s, u, a, k] = await Promise.all([
        api.get<OrgSummary>("/org"),
        api.get<Member[]>("/org/users"),
        api.get<OrgAppliance[]>("/org/appliances"),
        api.get<KeyRow[]>("/org/keys"),
      ]);
      setSummary(s); setMembers(u); setAppliances(a); setKeys(k);
    } catch { /* ignore */ } finally { setLoaded(true); }
  }
  useEffect(() => { void load(); }, []);

  async function addMember() {
    const res = await formDialog({
      title: "Add a member",
      message: "They'll get a sign-in code by email and their own encrypted vault.",
      confirmLabel: "Add member",
      fields: [
        { name: "display_name", label: "Full name", required: true, placeholder: "Jamie Doe" },
        { name: "email", label: "Email", required: true, placeholder: "jamie@company.com" },
        { name: "role", label: "Role", options: [
          { label: "Member — own data only", value: "member" },
          { label: "Admin — manage the organization", value: "admin" },
          ...(isOwner ? [{ label: "Owner — full control", value: "owner" }] : []),
        ] },
      ],
    });
    if (!res) return;
    try {
      const out = await api.post<{ invite?: { dev_code?: string } }>("/org/users", {
        email: res.email, display_name: res.display_name, role: res.role || "member",
      });
      await load();
      const dev = out.invite?.dev_code;
      await notify({ title: "Member added",
        message: dev ? `Invite sent. Dev sign-in code: ${dev}` : "An invite with a sign-in code was emailed.",
        tone: "ok" });
    } catch (e) {
      await notify({ title: "Couldn't add member", message: (e as ApiError).message, tone: "danger" });
    }
  }

  async function changeRole(m: Member) {
    const res = await formDialog({
      title: `Change ${m.display_name}'s role`,
      confirmLabel: "Save",
      fields: [{ name: "role", label: "Role", defaultValue: m.role, options: [
        { label: "Member — own data only", value: "member" },
        { label: "Admin — manage the organization", value: "admin" },
        ...(isOwner ? [{ label: "Owner — full control", value: "owner" }] : []),
      ] }],
    });
    if (!res || res.role === m.role) return;
    try {
      await api.put(`/org/users/${m.id}`, { role: res.role });
      await load();
    } catch (e) { await notify({ title: "Couldn't update role", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function toggleStatus(m: Member) {
    const next = m.status === "active" ? "suspended" : "active";
    try { await api.put(`/org/users/${m.id}`, { status: next }); await load(); }
    catch (e) { await notify({ title: "Couldn't update", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function removeMember(m: Member) {
    const ok = await confirmDialog({
      title: `Remove ${m.display_name}?`, tone: "danger", confirmLabel: "Remove member",
      message: "They lose access immediately. Their vault and its keys remain and can be recovered by an admin.",
    });
    if (!ok) return;
    try { await api.del(`/org/users/${m.id}`); await load(); }
    catch (e) { await notify({ title: "Couldn't remove member", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function assign(a: OrgAppliance) {
    const unassigned = members.filter((m) => !a.assignments.some((x) => x.user_id === m.id));
    if (unassigned.length === 0) { await notify({ message: "Every member is already assigned to this appliance." }); return; }
    const res = await formDialog({
      title: `Assign a member to ${a.name}`,
      confirmLabel: "Assign",
      fields: [
        { name: "user_id", label: "Member", required: true,
          options: unassigned.map((m) => ({ label: `${m.display_name} (${roleLabel(m.role)})`, value: m.id })) },
        { name: "can_manage", label: "Access level", defaultValue: "view", options: [
          { label: "View only", value: "view" },
          { label: "Can manage this appliance", value: "manage" },
        ] },
      ],
    });
    if (!res) return;
    try {
      await api.post(`/org/appliances/${a.id}/assignments`, {
        user_id: res.user_id, can_manage: res.can_manage === "manage",
      });
      await load();
    } catch (e) { await notify({ title: "Couldn't assign", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function unassign(a: OrgAppliance, m: Assignment) {
    const ok = await confirmDialog({ title: `Remove ${m.display_name} from ${a.name}?`, confirmLabel: "Remove", tone: "warn",
      message: "They'll no longer see this appliance." });
    if (!ok) return;
    try { await api.del(`/org/appliances/${a.id}/assignments/${m.user_id}`); await load(); }
    catch (e) { await notify({ title: "Couldn't update", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function recoverKey(k: KeyRow) {
    if (!me?.passkey_verified) {
      await notify({ title: "Unlock required", message: "Unlock with your passkey before recovering a key.", tone: "warn" });
      try { await stepUp(); await refresh(); } catch { return; }
    }
    const ok = await confirmDialog({
      title: `Recover ${k.owner_name}'s key?`, tone: "danger", confirmLabel: "Recover key",
      message: `This authorized recovery of ${k.owner_name}'s vault key is written to the audit log. Use it for a lost key or end-of-life continuity.`,
    });
    if (!ok) return;
    try {
      const res = await api.post<{ root_key_hash: string }>(`/org/keys/${k.vault_id}/recover`, {});
      await notify({ title: "Key recovered",
        message: `Recovery authorized and audited. Key fingerprint: ${res.root_key_hash?.slice(0, 24)}…`, tone: "ok" });
    } catch (e) { await notify({ title: "Couldn't recover key", message: (e as ApiError).message, tone: "danger" }); }
  }

  if (!loaded && !summary) return <Loading label="Loading your organization…" />;

  return (
    <>
      <Card style={{ marginBottom: 16 }}>
        <div className="spread">
          <div className="stack">
            <div className="muted" style={{ fontSize: 12 }}>Organization</div>
            <h2 style={{ margin: 0 }}>{summary?.name}</h2>
            <div className="row" style={{ gap: 8, marginTop: 4 }}>
              <Pill tone="info">{summary?.plan}</Pill>
              <Pill tone="info"><Icon name="key" size={12} /> {summary?.key_ownership_model}</Pill>
            </div>
          </div>
          <div className="row" style={{ gap: 20 }}>
            <Metric label="Members" value={summary?.counts.users ?? 0} />
            <Metric label="Admins" value={summary?.counts.admins ?? 0} />
            <Metric label="Vaults" value={summary?.counts.vaults ?? 0} />
            <Metric label="Appliances" value={summary?.counts.appliances ?? 0} />
          </div>
        </div>
      </Card>

      <div className="row" style={{ gap: 8, marginBottom: 14 }}>
        {([["members", "Members", "user"], ["appliances", "Appliances", "server"], ["keys", "Encryption keys", "key"]] as const)
          .map(([id, label, icon]) => (
            <button key={id} className={`btn sm ${tab === id ? "primary" : "ghost"}`} onClick={() => setTab(id)}>
              <Icon name={icon} size={14} /> {label}
            </button>
          ))}
      </div>

      {tab === "members" && (
        <Card>
          <div className="spread" style={{ marginBottom: 12 }}>
            <h3 style={{ margin: 0 }}>Members</h3>
            <button className="btn sm primary" onClick={addMember}><Icon name="user" size={14} /> Add member</button>
          </div>
          {members.map((m) => (
            <div key={m.id} className="result-row">
              <div className="result-icon" style={{ background: "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
                {m.display_name.slice(0, 2).toUpperCase()}
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>
                  {m.display_name}{m.id === me?.user_id && <span className="faint" style={{ fontWeight: 400 }}> · you</span>}
                </div>
                <div className="faint" style={{ fontSize: 12 }}>
                  {m.email} · {m.vault_count} vault{m.vault_count === 1 ? "" : "s"} · {m.object_count.toLocaleString()} objects · {bytes(m.protected_bytes)}
                </div>
              </div>
              <Pill tone={ROLE_TONE[m.role] ?? "info"}>{roleLabel(m.role)}</Pill>
              {m.status !== "active" && <Pill tone="warn">suspended</Pill>}
              {!m.has_passkey && <Pill tone="warn">no passkey</Pill>}
              <button className="btn sm ghost" onClick={() => changeRole(m)}>Role</button>
              {m.id !== me?.user_id && (
                <>
                  <button className="btn sm ghost" onClick={() => toggleStatus(m)}>
                    {m.status === "active" ? "Suspend" : "Restore"}
                  </button>
                  <button className="btn sm ghost" onClick={() => removeMember(m)}><Icon name="logout" size={13} /></button>
                </>
              )}
            </div>
          ))}
          {members.length === 0 && <div className="muted">No members yet.</div>}
        </Card>
      )}

      {tab === "appliances" && (
        <Card>
          <h3 style={{ marginBottom: 4 }}>Appliances</h3>
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
            Assign appliances to members. Members see only appliances assigned to them and, unless given manage rights, can view but not change them.
          </div>
          {appliances.map((a) => (
            <div key={a.id} className="result-row" style={{ alignItems: "flex-start" }}>
              <div className="result-icon" style={{ background: "var(--bg-elev-2)" }}><Icon name="server" size={17} /></div>
              <div className="flex1">
                <div className="spread">
                  <div style={{ fontWeight: 600 }}>{a.name} <span className="faint" style={{ fontWeight: 400 }}>· {a.model}</span></div>
                  <button className="btn sm ghost" onClick={() => assign(a)}><Icon name="user" size={13} /> Assign member</button>
                </div>
                <div className="faint" style={{ fontSize: 12, margin: "2px 0 8px" }}>
                  {a.serial} · <Pill tone={a.online ? "ok" : "warn"} dot>{a.online ? "online" : "offline"}</Pill> · {bytes(a.used_bytes)} of {bytes(a.capacity_bytes)}
                </div>
                <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
                  {a.assignments.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No members assigned.</span>}
                  {a.assignments.map((m) => (
                    <span key={m.user_id} className="chip" style={{ padding: "2px 8px", fontSize: 11.5 }}>
                      {m.display_name}
                      {m.can_manage && <span className="faint"> · manages</span>}
                      <span onClick={() => unassign(a, m)} style={{ cursor: "pointer", marginLeft: 6 }} title="Remove">✕</span>
                    </span>
                  ))}
                </div>
              </div>
            </div>
          ))}
          {appliances.length === 0 && <div className="muted">No appliances in this organization yet.</div>}
        </Card>
      )}

      {tab === "keys" && (
        <Card>
          <h3 style={{ marginBottom: 4 }}>Encryption keys</h3>
          <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
            Every member holds their own keys. As an org admin you can see each key's type, strength and status — never the key itself —
            and, for a lost key or end-of-life continuity, perform an audited recovery.
          </div>
          {keys.map((k) => (
            <div key={k.vault_id} className="result-row">
              <div className="result-icon" style={{ background: "var(--bg-elev-2)" }}><Icon name="key" size={16} /></div>
              <div className="flex1">
                <div style={{ fontWeight: 600 }}>{k.owner_name} <span className="faint" style={{ fontWeight: 400 }}>· {k.vault_name}</span></div>
                <div className="faint" style={{ fontSize: 12 }}>
                  {k.content_algorithm} · {k.pq_hybrid ? "hybrid PQC" : "classical"} · {k.recovery_kem || "ML-KEM"} · {k.strength_bits}-bit
                  {k.root_key_hash ? ` · ${k.root_key_hash.slice(0, 16)}…` : ""}
                </div>
              </div>
              <Pill tone={k.status === "active" ? "ok" : "warn"} dot>{k.status}</Pill>
              <button className="btn sm ghost" onClick={() => recoverKey(k)} disabled={!k.provisioned}>
                <Icon name="restore" size={13} /> Recover
              </button>
            </div>
          ))}
          {keys.length === 0 && <div className="muted">No keys provisioned yet.</div>}
        </Card>
      )}
    </>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="stack" style={{ alignItems: "center" }}>
      <div style={{ fontSize: 22, fontWeight: 700 }}>{value}</div>
      <div className="faint" style={{ fontSize: 11 }}>{label}</div>
    </div>
  );
}
