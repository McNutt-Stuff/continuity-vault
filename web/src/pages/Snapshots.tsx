import { Fragment, useEffect, useState } from "react";
import { api } from "../api";
import { Card, Pill, bytes, timeAgo } from "../components/ui";
import { DestIcon } from "../components/DestIcon";
import { SourceIcon } from "../components/SourceIcon";
import { Icon } from "../components/Icon";

interface Snapshot {
  id: string; snapshot_id: string; vault_id: string; collection_id: string;
  collection_name: string; source_type: string;
  destination: string; destination_label: string; object_count: number; total_bytes: number;
  manifest_hash: string; recoverable: boolean; created_at: string;
}

interface SnapObject {
  object_id: string; title: string; source_type: string; doc_type: string;
  size_bytes: number; content_hash: string | null; manifest_hash: string | null;
}
interface SnapDetail extends Snapshot {
  integrity: {
    signed: boolean; hash_alg: string; algorithms: string[]; retention_class: string;
    manifest_object_count: number | null; verified: boolean;
    seal: { isolation_state: string; integrity_result: string; commit_timestamp: string; appliance_id: string } | null;
  };
  objects: SnapObject[]; objects_truncated: boolean;
}

const short = (h?: string | null, n = 12) => (h ? h.slice(0, n) + "…" : "—");

export default function Snapshots() {
  const [rows, setRows] = useState<Snapshot[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, SnapDetail>>({});
  const [loading, setLoading] = useState<string | null>(null);

  useEffect(() => { api.get<Snapshot[]>("/snapshots").then(setRows).catch(() => {}); }, []);

  function toggle(id: string) {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    if (!detail[id]) {
      setLoading(id);
      api.get<SnapDetail>(`/snapshots/${id}`)
        .then((d) => setDetail((m) => ({ ...m, [id]: d })))
        .catch(() => {})
        .finally(() => setLoading(null));
    }
  }

  return (
    <Card>
      <div className="spread" style={{ marginBottom: 8 }}>
        <h2>Recovery-point inventory</h2>
        <span className="muted">{rows.length} recovery points</span>
      </div>
      <div className="muted" style={{ fontSize: 12.5, marginBottom: 12 }}>
        Each recovery point carries a hybrid-signed (ML-DSA + Ed25519) manifest. A point is
        only marked <b>recoverable</b> after its destination confirms and signs the commit.
        Expand a row to see the data it secured and its integrity evidence.
      </div>
      <table className="table">
        <thead>
          <tr>
            <th style={{ width: 24 }}></th>
            <th>Snapshot</th><th>Destination</th><th>Source</th><th>Objects</th><th>Size</th>
            <th>Manifest</th><th>Status</th><th>Created</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => {
            const d = detail[s.id];
            const isOpen = open === s.id;
            return (
              <Fragment key={s.id}>
                <tr onClick={() => toggle(s.id)} style={{ cursor: "pointer" }}>
                  <td style={{ color: "var(--muted)", fontSize: 11 }}>
                    <span style={{ display: "inline-block", transition: "transform .12s", transform: isOpen ? "rotate(90deg)" : "none" }}>▸</span>
                  </td>
                  <td className="mono">{s.snapshot_id.slice(0, 12)}…</td>
                  <td>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <DestIcon dest={s.destination} size={13} />
                      {s.destination_label || s.destination}
                    </span>
                  </td>
                  <td>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      {s.source_type && <SourceIcon type={s.source_type} size={13} />}
                      <span className="faint">{s.collection_name || s.source_type || "—"}</span>
                    </span>
                  </td>
                  <td>{s.object_count}</td>
                  <td>{bytes(s.total_bytes)}</td>
                  <td className="mono faint">{short(s.manifest_hash, 10)}</td>
                  <td>{s.recoverable ? <Pill tone="ok" dot>Recoverable</Pill> : <Pill tone="warn" dot>Pending</Pill>}</td>
                  <td className="faint">{timeAgo(s.created_at)}</td>
                </tr>
                {isOpen && (
                  <tr>
                    <td colSpan={9} style={{ background: "var(--bg-subtle, rgba(0,0,0,0.02))", padding: 0 }}>
                      {loading === s.id && !d && <div className="muted" style={{ padding: 14 }}>Loading…</div>}
                      {d && <Detail d={d} />}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && <div className="muted" style={{ marginTop: 12 }}>No recovery points yet.</div>}
    </Card>
  );
}

function Detail({ d }: { d: SnapDetail }) {
  const it = d.integrity;
  return (
    <div style={{ padding: 14, display: "grid", gap: 14 }}>
      {/* Integrity validation */}
      <div>
        <div style={{ fontWeight: 600, marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
          <Icon name="shield" size={14} /> Integrity validation
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center", marginBottom: 8 }}>
          {it.verified
            ? <Pill tone="ok" dot>Secured &amp; verified</Pill>
            : <Pill tone="warn" dot>Awaiting commit signature</Pill>}
          {it.signed && <Pill tone="info">Hybrid-signed</Pill>}
          {it.algorithms.map((a) => <Pill key={a} tone="info">{a}</Pill>)}
          {it.seal?.integrity_result &&
            <Pill tone={it.seal.integrity_result === "verified" || it.seal.integrity_result === "ok" ? "ok" : "warn"}>
              Seal: {it.seal.integrity_result}
            </Pill>}
        </div>
        <div className="faint" style={{ fontSize: 12, display: "grid", gap: 3 }}>
          <div>Manifest hash <span className="mono">{d.manifest_hash}</span> {it.hash_alg && `(${it.hash_alg})`}</div>
          {it.retention_class && <div>Retention class: {it.retention_class}</div>}
          {it.seal?.isolation_state && <div>Appliance isolation: {it.seal.isolation_state}</div>}
          {it.seal?.commit_timestamp && <div>Committed: {it.seal.commit_timestamp}</div>}
          <div>Stored to <b>{d.destination_label}</b></div>
        </div>
      </div>

      {/* Secured data */}
      <div>
        <div style={{ fontWeight: 600, marginBottom: 8 }}>
          Secured data <span className="faint" style={{ fontWeight: 400 }}>
            · {d.object_count.toLocaleString()} object{d.object_count === 1 ? "" : "s"} · {bytes(d.total_bytes)}
          </span>
        </div>
        {d.objects.length === 0
          ? <div className="muted" style={{ fontSize: 12.5 }}>
              No searchable index rows for this point (content-only or zero-knowledge vault). The signed
              manifest still secured {d.object_count.toLocaleString()} object(s).
            </div>
          : <table className="table" style={{ fontSize: 12.5 }}>
              <thead>
                <tr><th>Item</th><th>Type</th><th>Size</th><th>Content hash</th></tr>
              </thead>
              <tbody>
                {d.objects.map((o) => (
                  <tr key={o.object_id}>
                    <td>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                        {o.source_type && <SourceIcon type={o.source_type} size={12} />}
                        <span style={{ maxWidth: 380, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", display: "inline-block" }}>
                          {o.title || o.object_id}
                        </span>
                      </span>
                    </td>
                    <td className="faint">{o.doc_type || "—"}</td>
                    <td>{o.size_bytes ? bytes(o.size_bytes) : "—"}</td>
                    <td className="mono faint" title={o.content_hash || ""}>{short(o.content_hash, 12)}</td>
                  </tr>
                ))}
              </tbody>
            </table>}
        {d.objects_truncated &&
          <div className="faint" style={{ fontSize: 12, marginTop: 6 }}>Showing the first 1,000 objects.</div>}
      </div>
    </div>
  );
}

