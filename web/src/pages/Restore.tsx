import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, timeAgo, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { notify } from "../components/dialog";

interface Snapshot { id: string; snapshot_id: string; destination: string; object_count: number; recoverable: boolean; }
interface Restore {
  id: string; snapshot_id: string; destination: string; status: string;
  purpose: string; approvals: number; required_approvals: number; created_at: string;
}

export default function RestorePage() {
  const { me, stepUp } = useAuth();
  const [snaps, setSnaps] = useState<Snapshot[]>([]);
  const [restores, setRestores] = useState<Restore[]>([]);
  const [toast, setToast] = useState("");
  const [loaded, setLoaded] = useState(false);

  async function load() {
    try {
      setSnaps((await api.get<Snapshot[]>("/snapshots")).filter((s) => s.recoverable));
      setRestores(await api.get<Restore[]>("/restore"));
    } finally {
      setLoaded(true);
    }
  }
  useEffect(() => { void load(); }, []);

  async function request(s: Snapshot) {
    if (!me?.passkey_verified) await stepUp().catch((e) => notify({ message: e.message, tone: "danger" }));
    try {
      await api.post("/restore", {
        snapshot_id: s.snapshot_id,
        object_ids: [],
        destination: "download",
        purpose: "User-initiated recovery",
      });
      setToast("Restore requested — awaiting approval");
      await load();
    } catch (e) { await notify({ title: "Couldn't request restore", message: (e as ApiError).message, tone: "danger" }); }
    setTimeout(() => setToast(""), 2500);
  }

  async function act(r: Restore, action: "approve" | "execute") {
    try {
      const res = await api.post<{ status: string; note?: string }>(`/restore/${r.id}/${action}`);
      setToast(res.note ?? `Restore ${res.status}`);
      await load();
    } catch (e) { await notify({ title: "Restore action failed", message: (e as ApiError).message, tone: "danger" }); }
    setTimeout(() => setToast(""), 3500);
  }

  if (!loaded && snaps.length === 0 && restores.length === 0) return <Loading label="Loading recovery…" />;

  return (
    <div className="grid grid-2" style={{ alignItems: "start" }}>
      <Card>
        <h2 style={{ marginBottom: 12 }}>Start a recovery</h2>
        <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
          Recovery requires passkey step-up, an approval quorum, and — for appliance vaults —
          local physical approval before unseal.
        </div>
        {snaps.map((s) => (
          <div key={s.id} className="result-row">
            <div className="result-icon" style={{ background: "var(--bg-elev-2)" }}><Icon name="restore" size={17} /></div>
            <div className="flex1">
              <div className="mono">{s.snapshot_id.slice(0, 14)}…</div>
              <div className="faint" style={{ fontSize: 12 }}>{s.object_count} objects · {s.destination}</div>
            </div>
            <button className="btn sm primary" onClick={() => request(s)}>Recover</button>
          </div>
        ))}
        {snaps.length === 0 && <div className="muted">No verified recovery points available.</div>}
      </Card>

      <Card>
        <h2 style={{ marginBottom: 12 }}>Restore requests</h2>
        {restores.map((r) => (
          <div key={r.id} className="result-row" style={{ flexWrap: "wrap" }}>
            <div className="flex1">
              <div className="mono">{r.snapshot_id.slice(0, 12)}…</div>
              <div className="faint" style={{ fontSize: 12 }}>
                {r.destination} · {r.approvals}/{r.required_approvals} approvals · {timeAgo(r.created_at)}
              </div>
            </div>
            <StatusPill status={r.status} />
            <div className="row" style={{ gap: 6 }}>
              {r.status === "pending-approval" && (
                <button className="btn sm" onClick={() => act(r, "approve")}>Approve</button>
              )}
              {r.status === "approved" && (
                <button className="btn sm accent" onClick={() => act(r, "execute")}>Execute</button>
              )}
            </div>
          </div>
        ))}
        {restores.length === 0 && <div className="muted">No restore requests.</div>}
      </Card>

      {toast && <div className="toast"><Icon name="check" size={15} /> {toast}</div>}
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  if (status === "completed") return <Pill tone="ok">Completed</Pill>;
  if (status === "approved") return <Pill tone="info">Approved</Pill>;
  if (status.includes("recovery-window")) return <Pill tone="warn">Awaiting local approval</Pill>;
  return <Pill tone="warn">Pending approval</Pill>;
}
