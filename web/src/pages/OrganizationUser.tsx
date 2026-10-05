import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, Stat, bytes, timeAgo, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { confirmDialog, formDialog, notify } from "../components/dialog";

interface Passkey { id: string; label: string; transport: string; created_at: string | null }
interface VaultUsage { id: string; name: string; object_count: number; protected_bytes: number }
interface UserDetail {
  id: string; email: string; display_name: string; first_name: string; last_name: string;
  phone: string; role: string; status: string; email_verified: boolean; is_you: boolean;
  is_platform_admin: boolean; created_at: string | null; last_login_at: string | null;
  permissions: { is_admin: boolean; is_owner: boolean; can_manage_org: boolean };
  passkeys: Passkey[];
  usage: { vault_count: number; object_count: number; protected_bytes: number; vaults: VaultUsage[] };
}
interface Activity { action: string; resource: string; category: string; severity: string; detail: any; created_at: string | null }

const ROLE_TONE: Record<string, "info" | "ok" | "warn"> = { owner: "ok", admin: "info", "security-admin": "info", member: "warn" };
const ROLE_LABEL: Record<string, string> = { owner: "Owner", admin: "Admin", member: "Member", "security-admin": "Security admin" };
const SEV_TONE: Record<string, "ok" | "warn" | "danger" | "info"> = { info: "info", notice: "info", warning: "warn", critical: "danger" };

export default function OrganizationUser() {
  const { id = "" } = useParams();
  const nav = useNavigate();
  const { me } = useAuth();
  const isOwner = me?.is_owner || me?.role === "owner";
  const [u, setU] = useState<UserDetail | null>(null);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [loaded, setLoaded] = useState(false);

  async function load() {
    try {
      const [d, a] = await Promise.all([
        api.get<UserDetail>(`/org/users/${id}`),
        api.get<Activity[]>(`/org/users/${id}/activity`),
      ]);
      setU(d); setActivity(a);
    } catch (e) {
      await notify({ title: "Couldn't load member", message: (e as ApiError).message, tone: "danger" });
    } finally { setLoaded(true); }
  }
  useEffect(() => { void load(); }, [id]);

  async function editProfile() {
    if (!u) return;
    const res = await formDialog({
      title: "Edit profile", confirmLabel: "Save",
      fields: [
        { name: "first_name", label: "First name", defaultValue: u.first_name },
        { name: "last_name", label: "Last name", defaultValue: u.last_name },
        { name: "display_name", label: "Display name", defaultValue: u.display_name },
      ],
    });
    if (!res) return;
    try { await api.put(`/org/users/${id}`, res); await load(); await notify({ message: "Profile updated", tone: "ok" }); }
    catch (e) { await notify({ title: "Couldn't update", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function changeEmail() {
    if (!u) return;
    const res = await formDialog({
      title: "Change email address",
      message: "A confirmation code is sent to the new address — it shows as unverified until confirmed.",
      confirmLabel: "Update email",
      fields: [{ name: "email", label: "New email", required: true, defaultValue: u.email, placeholder: "name@company.com" }],
    });
    if (!res || !res.email || res.email.trim().toLowerCase() === u.email.toLowerCase()) return;
    try {
      await api.put(`/org/users/${id}`, { email: res.email.trim() });
      await load();
      await notify({ title: "Email updated", message: "A confirmation email was sent to the new address.", tone: "ok" });
    } catch (e) { await notify({ title: "Couldn't change email", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function changeRole() {
    if (!u) return;
    const res = await formDialog({
      title: `Change ${u.display_name}'s role`, confirmLabel: "Save",
      fields: [{ name: "role", label: "Role", defaultValue: u.role, options: [
        { label: "Member — own data only", value: "member" },
        { label: "Admin — manage the organization", value: "admin" },
        ...(isOwner ? [{ label: "Owner — full control", value: "owner" }] : []),
      ] }],
    });
    if (!res || res.role === u.role) return;
    try { await api.put(`/org/users/${id}`, { role: res.role }); await load(); }
    catch (e) { await notify({ title: "Couldn't update role", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function toggleStatus() {
    if (!u) return;
    const next = u.status === "active" ? "suspended" : "active";
    try { await api.put(`/org/users/${id}`, { status: next }); await load(); }
    catch (e) { await notify({ title: "Couldn't update", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function resendVerification() {
    try {
      await api.post(`/org/users/${id}/resend-verification`, {});
      await notify({ message: "Confirmation email sent.", tone: "ok" });
    } catch (e) { await notify({ title: "Couldn't send", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function resetPasskeys() {
    if (!u) return;
    const ok = await confirmDialog({
      title: `Reset ${u.display_name}'s sign-in?`, tone: "danger", confirmLabel: "Reset sign-in",
      message: "This removes ALL of their passkeys and emails a fresh sign-in code so they re-enroll a device. Use it when a device is lost. This is audited.",
    });
    if (!ok) return;
    try { await api.post(`/org/users/${id}/reset-passkeys`, {}); await load(); await notify({ message: "Sign-in reset; a code was emailed.", tone: "ok" }); }
    catch (e) { await notify({ title: "Couldn't reset", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function removePasskey(p: Passkey) {
    const ok = await confirmDialog({ title: `Remove passkey "${p.label}"?`, tone: "warn", confirmLabel: "Remove",
      message: "That device will no longer be able to sign in." });
    if (!ok) return;
    try { await api.del(`/org/users/${id}/passkeys/${p.id}`); await load(); }
    catch (e) { await notify({ title: "Couldn't remove", message: (e as ApiError).message, tone: "danger" }); }
  }

  async function removeMember() {
    if (!u) return;
    const ok = await confirmDialog({ title: `Remove ${u.display_name}?`, tone: "danger", confirmLabel: "Remove member",
      message: "They lose access immediately. Their vault and keys remain and can be recovered by an admin." });
    if (!ok) return;
    try { await api.del(`/org/users/${id}`); nav("/organization"); }
    catch (e) { await notify({ title: "Couldn't remove", message: (e as ApiError).message, tone: "danger" }); }
  }

  if (!loaded) return <Loading label="Loading member…" />;
  if (!u) return <Card><div className="muted">Member not found.</div></Card>;

  const canEditOther = !u.is_you || isOwner;

  return (
    <div className="stack" style={{ gap: 16 }}>
      <button className="btn ghost sm" onClick={() => nav("/organization")} style={{ alignSelf: "flex-start" }}>
        ← Organization
      </button>

      {/* Header */}
      <Card>
        <div className="spread" style={{ alignItems: "flex-start" }}>
          <div className="row" style={{ gap: 14 }}>
            <div className="result-icon" style={{ width: 48, height: 48, fontSize: 18, background: "linear-gradient(135deg,#4f7cff,#35d0a5)" }}>
              {u.display_name.slice(0, 2).toUpperCase()}
            </div>
            <div className="stack" style={{ gap: 4 }}>
              <h2 style={{ margin: 0 }}>{u.display_name}{u.is_you && <span className="faint" style={{ fontWeight: 400, fontSize: 14 }}> · you</span>}</h2>
              <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
                <span className="faint">{u.email}</span>
                {u.email_verified
                  ? <Pill tone="ok" dot>verified</Pill>
                  : <Pill tone="warn" dot>unverified</Pill>}
                <Pill tone={ROLE_TONE[u.role] ?? "info"}>{ROLE_LABEL[u.role] ?? u.role}</Pill>
                {u.status !== "active" && <Pill tone="warn">suspended</Pill>}
                {u.passkeys.length === 0 && <Pill tone="warn">no passkey</Pill>}
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 6 }}>
            <button className="btn sm ghost" onClick={editProfile}><Icon name="edit" size={13} /> Edit</button>
            {canEditOther && !u.is_you && (
              <button className="btn sm ghost" onClick={toggleStatus}>{u.status === "active" ? "Suspend" : "Restore"}</button>
            )}
          </div>
        </div>
      </Card>

      {/* Usage */}
      <div className="stat-grid">
        <Stat label="Vaults" value={u.usage.vault_count} />
        <Stat label="Objects" value={u.usage.object_count.toLocaleString()} />
        <Stat label="Protected" value={bytes(u.usage.protected_bytes)} />
        <Stat label="Passkeys" value={u.passkeys.length} hint={u.passkeys.length ? "2FA enrolled" : "none"} />
        <Stat label="Last sign-in" value={u.last_login_at ? timeAgo(u.last_login_at) : "never"} />
      </div>

      {/* Account details + controls */}
      <div className="card-grid">
        <Card>
          <h3 style={{ marginTop: 0 }}>Account details</h3>
          <Row k="Display name" v={u.display_name} />
          <Row k="First name" v={u.first_name || "—"} />
          <Row k="Last name" v={u.last_name || "—"} />
          <Row k="Email" v={<span className="row" style={{ gap: 6 }}>{u.email}{u.email_verified ? <Pill tone="ok">verified</Pill> : <Pill tone="warn">unverified</Pill>}</span>} />
          <Row k="Role" v={ROLE_LABEL[u.role] ?? u.role} />
          <Row k="Status" v={u.status} />
          <Row k="Member since" v={u.created_at ? timeAgo(u.created_at) : "—"} />
          <div className="row" style={{ gap: 6, marginTop: 10, flexWrap: "wrap" }}>
            <button className="btn sm ghost" onClick={changeEmail}><Icon name="mail" size={13} /> Change email</button>
            {!u.email_verified && <button className="btn sm ghost" onClick={resendVerification}><Icon name="repeat" size={13} /> Resend confirmation</button>}
            {canEditOther && <button className="btn sm ghost" onClick={changeRole}><Icon name="shield" size={13} /> Change role</button>}
          </div>
        </Card>

        <Card>
          <h3 style={{ marginTop: 0 }}>Permissions</h3>
          <Perm on={u.permissions.is_owner} label="Owner — full organization control" />
          <Perm on={u.permissions.is_admin} label="Admin — manage members, appliances & keys" />
          <Perm on={true} label="Member — owns a private encrypted vault" />
          <Perm on={u.is_platform_admin} label="Platform administrator" />
          <div className="faint" style={{ fontSize: 12, marginTop: 10 }}>
            Members only ever access their own data. Admins manage the organization but never see another member's content.
          </div>
        </Card>
      </div>

      {/* Passkeys / sign-in (2FA) */}
      <Card>
        <div className="spread" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0 }}>Sign-in &amp; passkeys</h3>
          <button className="btn sm ghost" onClick={resetPasskeys}><Icon name="restore" size={13} /> Reset sign-in</button>
        </div>
        <div className="faint" style={{ fontSize: 12.5, marginBottom: 10 }}>
          Arkive uses passwordless passkeys (WebAuthn) — the device biometric/security key IS the second factor.
          Resetting removes every passkey and emails a fresh sign-in code.
        </div>
        {u.passkeys.length === 0 ? (
          <div className="muted">No passkeys enrolled — this member hasn't set up a device yet.</div>
        ) : u.passkeys.map((p) => (
          <div key={p.id} className="row" style={{ gap: 10, alignItems: "center", padding: "8px 0", borderTop: "1px solid var(--border-soft)" }}>
            <Icon name="key" size={15} />
            <div className="flex1">
              <div style={{ fontWeight: 600, fontSize: 13 }}>{p.label}</div>
              <div className="faint" style={{ fontSize: 11.5 }}>{p.transport} · added {p.created_at ? timeAgo(p.created_at) : "—"}</div>
            </div>
            <button className="btn sm ghost" onClick={() => removePasskey(p)}><Icon name="trash" size={13} /></button>
          </div>
        ))}
      </Card>

      {/* Activity */}
      <Card>
        <h3 style={{ marginTop: 0 }}>Account activity</h3>
        {activity.length === 0 ? <div className="muted">No recorded activity yet.</div> : (
          <div className="stack" style={{ gap: 0 }}>
            {activity.map((a, i) => (
              <div key={i} className="row" style={{ gap: 10, alignItems: "center", padding: "8px 0", borderTop: i ? "1px solid var(--border-soft)" : "none" }}>
                <Pill tone={SEV_TONE[a.severity] || "info"} dot>{a.category}</Pill>
                <div className="flex1" style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 13, fontFamily: "var(--mono)" }}>{a.action}</div>
                  {a.detail?.reason && <div className="faint" style={{ fontSize: 11.5 }}>{a.detail.reason}</div>}
                </div>
                <span className="faint" style={{ fontSize: 11.5, whiteSpace: "nowrap" }}>{a.created_at ? timeAgo(a.created_at) : ""}</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Danger zone */}
      {!u.is_you && (
        <Card style={{ borderColor: "var(--danger-c)" }}>
          <div className="spread">
            <div>
              <h3 style={{ margin: 0 }}>Remove member</h3>
              <div className="faint" style={{ fontSize: 12.5 }}>Revokes access immediately. Their vault + keys remain (recoverable by an admin).</div>
            </div>
            <button className="btn sm danger" onClick={removeMember}><Icon name="logout" size={13} /> Remove</button>
          </div>
        </Card>
      )}
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="row" style={{ gap: 10, padding: "5px 0", alignItems: "center" }}>
      <span className="faint" style={{ minWidth: 120, fontSize: 12.5 }}>{k}</span>
      <span style={{ fontSize: 13 }}>{v}</span>
    </div>
  );
}

function Perm({ on, label }: { on: boolean; label: string }) {
  return (
    <div className="row" style={{ gap: 8, padding: "5px 0", alignItems: "center" }}>
      <span style={{ display: "inline-flex", color: on ? "var(--ok)" : "var(--muted-c)" }}>
        <Icon name={on ? "check" : "x"} size={14} />
      </span>
      <span style={{ fontSize: 13, color: on ? "var(--text)" : "var(--muted-c)" }}>{label}</span>
    </div>
  );
}
