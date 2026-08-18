import { useEffect, useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, timeAgo } from "../components/ui";
import { Icon } from "../components/Icon";

interface Agent {
  id: string; name: string; hostname: string; platform: string; version: string;
  state: string; collectors: string[]; config: any; telemetry: any;
  last_heartbeat_at: string | null; last_collection_at: string | null;
}

export default function Agents() {
  const { me, stepUp } = useAuth();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [code, setCode] = useState<string | null>(null);
  const [toast, setToast] = useState("");

  async function load() {
    setAgents(await api.get<Agent[]>("/agents"));
  }
  useEffect(() => {
    void load();
    const t = setInterval(load, 8000);
    return () => clearInterval(t);
  }, []);

  function flash(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(""), 3000);
  }

  async function newCode() {
    const res = await api.post<{ code: string }>("/agents/linking-code", {
      name: "Mac Agent",
      collectors: ["onepassword"],
    });
    setCode(res.code);
  }

  async function downloadInstaller() {
    if (!me?.passkey_verified) {
      try { await stepUp(); } catch (e) { return alert((e as Error).message); }
    }
    const res = await api.post<{ filename: string; script: string }>("/agents/installer", {
      name: "Mac Agent", collectors: ["onepassword"], destinations: ["cv-cloud"],
    });
    const blob = new Blob([res.script], { type: "text/x-shellscript" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = res.filename;
    a.click();
    URL.revokeObjectURL(url);
    flash("Installer downloaded — run it on your Mac to link this agent");
  }

  async function command(a: Agent, type: string) {
    await api.post(`/agents/${a.id}/command`, { type, params: {} });
    flash(`${type} queued for ${a.name}`);
  }

  return (
    <div className="grid grid-2" style={{ alignItems: "start" }}>
      <div>
        <Card style={{ marginBottom: 16 }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h2>Install a desktop agent</h2>
            <div className="row">
              <button className="btn sm" onClick={newCode}>
                <Icon name="link" size={14} /> Linking code
              </button>
              <button className="btn primary sm" onClick={downloadInstaller}>
                <Icon name="logout" size={14} /> Download Mac installer
              </button>
            </div>
          </div>
          <div className="muted" style={{ fontSize: 13 }}>
            Desktop agents collect locally with native tools (like the 1Password CLI) and push
            <b> client-side–encrypted</b> data to the platform — the cloud never sees plaintext.
            Download the installer (linking code baked in), run it on a Mac, and it installs a
            background menu-bar app bundled with the 1Password CLI.
          </div>
          {code && (
            <>
              <div className="card" style={{ marginTop: 14, textAlign: "center", background: "#0e1421" }}>
                <div className="faint" style={{ fontSize: 12 }}>Agent linking code (valid 15 min)</div>
                <div className="mono" style={{ fontSize: 26, letterSpacing: 2, margin: "8px 0" }}>{code}</div>
              </div>
              <div className="mono faint" style={{ fontSize: 11, marginTop: 10, wordBreak: "break-all" }}>
                ARKIVE_LINKING_CODE={code} ./installers/desktop-agent-install-macos.sh
              </div>
            </>
          )}
        </Card>

        {agents.map((a) => (
          <div key={a.id} className="dest-card" style={{ marginBottom: 12 }}>
            <div className="spread">
              <div className="row">
                <div className="result-icon" style={{ background: "linear-gradient(135deg,#7a5cff,#4f7cff)", width: 36, height: 36 }}>
                  <Icon name="user" size={18} />
                </div>
                <div>
                  <div style={{ fontWeight: 650 }}>{a.name}</div>
                  <div className="faint mono" style={{ fontSize: 11 }}>{a.hostname} · v{a.version}</div>
                </div>
              </div>
              {a.telemetry?.op_available === false
                ? <Pill tone="warn">op CLI missing</Pill>
                : a.telemetry?.op_auth === "unauthenticated"
                ? <Pill tone="warn">1Password not signed in</Pill>
                : <Pill tone="ok">healthy</Pill>}
            </div>
            <div className="grid grid-3" style={{ marginTop: 12 }}>
              <Info label="Heartbeat" value={timeAgo(a.last_heartbeat_at)} />
              <Info label="Last collection" value={timeAgo(a.last_collection_at)} />
              <Info label="Collectors" value={(a.collectors || []).join(", ") || "—"} />
              <Info label="Local IP" value={a.telemetry?.local_ip || "—"} />
              <Info label="Local user" value={a.telemetry?.local_user || "—"} />
              <Info label="1Password" value={a.telemetry?.op_auth || "—"} />
            </div>
            <div className="row" style={{ marginTop: 12, gap: 8, flexWrap: "wrap" }}>
              <button className="btn sm primary" onClick={() => command(a, "collect")}>Collect now</button>
              <button className="btn sm" onClick={() => command(a, "update")}>Update</button>
              <button className="btn sm" onClick={() => command(a, "reconfigure")}>Reconfigure</button>
            </div>
            {Array.isArray(a.telemetry?.recent_logs) && a.telemetry.recent_logs.length > 0 && (
              <details style={{ marginTop: 12 }}>
                <summary className="faint" style={{ cursor: "pointer", fontSize: 12 }}>
                  Recent logs ({a.telemetry.recent_logs.length})
                </summary>
                <pre className="mono" style={{ fontSize: 11, maxHeight: 220, overflow: "auto",
                     background: "rgba(0,0,0,0.25)", padding: 10, borderRadius: 8, marginTop: 8 }}>
                  {a.telemetry.recent_logs.join("\n")}
                </pre>
              </details>
            )}
          </div>
        ))}
        {agents.length === 0 && <Card><div className="muted">No desktop agents linked yet.</div></Card>}
      </div>

      <Card>
        <h3 style={{ marginBottom: 10 }}>How it works</h3>
        <ol style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.8 }}>
          <li>Generate a linking code and run the macOS installer on the endpoint.</li>
          <li>The agent registers (like an appliance), receives its config, and starts.</li>
          <li>It collects via the 1Password CLI and pushes encrypted data to the cloud
              (or directly to an appliance).</li>
          <li>It reports telemetry on a heartbeat and self-updates on the <b>Update</b> command.</li>
        </ol>
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="stack">
      <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
      <div style={{ fontWeight: 600, fontSize: 13 }}>{value}</div>
    </div>
  );
}
