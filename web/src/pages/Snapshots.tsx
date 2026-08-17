import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, Pill, bytes, timeAgo } from "../components/ui";

interface Snapshot {
  id: string; snapshot_id: string; vault_id: string; collection_id: string;
  destination: string; object_count: number; total_bytes: number;
  manifest_hash: string; recoverable: boolean; created_at: string;
}

export default function Snapshots() {
  const [rows, setRows] = useState<Snapshot[]>([]);
  useEffect(() => { api.get<Snapshot[]>("/snapshots").then(setRows).catch(() => {}); }, []);

  return (
    <Card>
      <div className="spread" style={{ marginBottom: 8 }}>
        <h2>Recovery-point inventory</h2>
        <span className="muted">{rows.length} snapshots</span>
      </div>
      <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
        Each recovery point carries a hybrid-signed manifest. A snapshot is only marked
        recoverable after its destination confirms and signs the commit.
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>Snapshot</th><th>Destination</th><th>Objects</th><th>Size</th>
            <th>Manifest</th><th>Status</th><th>Created</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id}>
              <td className="mono">{s.snapshot_id.slice(0, 12)}…</td>
              <td><Pill tone="info">{s.destination}</Pill></td>
              <td>{s.object_count}</td>
              <td>{bytes(s.total_bytes)}</td>
              <td className="mono faint">{s.manifest_hash.slice(0, 10)}…</td>
              <td>{s.recoverable ? <Pill tone="ok">Recoverable</Pill> : <Pill tone="warn">Pending</Pill>}</td>
              <td className="faint">{timeAgo(s.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <div className="muted" style={{ marginTop: 12 }}>No recovery points yet.</div>}
    </Card>
  );
}
