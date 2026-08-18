import { useEffect, useState } from "react";
import { api, ApiError, getToken } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, bytes, timeAgo } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { notify } from "../components/dialog";

interface Recovered {
  id: string; object_id: string; title: string; doc_type: string;
  source_type: string; mime: string; size_bytes: number; location: string;
  expires_in_seconds: number; viewed: boolean;
}
interface RetrieveResp {
  status: string; message: string; recovered_id?: string; title?: string;
  mime?: string; doc_type?: string; size_bytes?: number;
  content_b64?: string | null; filename?: string;
}
interface Viewing { item: Recovered; kind: "text" | "image" | "binary"; text?: string; url?: string; }

interface Result {
  object_id: string;
  snapshot_id: string;
  source_type: string;
  source_label: string;
  source_display: string;
  doc_type: string;
  category: string;
  sensitivity: string;
  title: string;
  preview: string;
  meta: Record<string, unknown>;
  labels: string[];
  size_bytes: number;
  modified_at: string | null;
  locations?: { destination: string; label: string; recoverable: boolean }[];
}
interface SearchResp {
  count: number;
  total_indexed: number;
  results: Result[];
  facets: {
    source: Record<string, number>;
    type: Record<string, number>;
    category: Record<string, number>;
    label: Record<string, number>;
    attributes: Record<string, Record<string, number>>;
  };
  source_display: Record<string, string>;
}

const CATEGORY_META: Record<string, { icon: IconName; label: string; color: string }> = {
  credential: { icon: "key", label: "Credentials", color: "#f5a623" },
  message: { icon: "mail", label: "Messages", color: "#ea4335" },
  contact: { icon: "user", label: "Contacts", color: "#35d0a5" },
  document: { icon: "file", label: "Documents", color: "#4f7cff" },
  media: { icon: "image", label: "Media", color: "#7a5cff" },
  file: { icon: "database", label: "Files", color: "#9aa7bf" },
  calendar: { icon: "calendar", label: "Calendar", color: "#0078d4" },
  note: { icon: "note", label: "Notes", color: "#35d0a5" },
  identity: { icon: "shield", label: "Identity & Legal", color: "#f2545b" },
  record: { icon: "database", label: "Records", color: "#7a5cff" },
};

const SOURCE_META: Record<string, { color: string; icon: IconName; label: string }> = {
  gmail: { color: "#ea4335", icon: "mail", label: "Gmail" },
  outlook: { color: "#0078d4", icon: "mail", label: "Outlook" },
  onedrive: { color: "#0078d4", icon: "cloud", label: "OneDrive" },
  dropbox: { color: "#0061ff", icon: "cloud", label: "Dropbox" },
  icloud: { color: "#3693f3", icon: "cloud", label: "iCloud" },
  onepassword: { color: "#0364d3", icon: "key", label: "1Password" },
  custom: { color: "#7a5cff", icon: "database", label: "Custom" },
};

function downloadB64(b64: string, filename: string) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([bytes]));
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

function safeName(title: string, docType: string): string {
  const base = (title || "recovered").replace(/[^\w.\- ]+/g, "_").slice(0, 80).trim() || "recovered";
  if (/\.[a-z0-9]{2,5}$/i.test(base)) return base;
  return base + (docType === "email" ? ".eml" : "");
}

function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"]; const i = Math.floor(Math.log(n) / Math.log(1024));
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${u[i]}`;
}

function fmtCountdown(s: number): string {
  if (s <= 0) return "expired";
  const m = Math.floor(s / 60), sec = s % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

export default function Search() {
  const { me, stepUp } = useAuth();
  const [q, setQ] = useState("");
  const [source, setSource] = useState<string | null>(null);
  const [category, setCategory] = useState<string | null>(null);
  const [label, setLabel] = useState<string | null>(null);
  const [attr, setAttr] = useState<string | null>(null);
  const [attrKey, setAttrKey] = useState("");
  const [data, setData] = useState<SearchResp | null>(null);
  const [locked, setLocked] = useState(false);
  const [msg, setMsg] = useState("");
  const [recovered, setRecovered] = useState<Recovered[]>([]);
  const [viewing, setViewing] = useState<Viewing | null>(null);

  async function loadRecovered() {
    try { setRecovered((await api.get<{ items: Recovered[] }>("/recovered")).items); }
    catch { /* ignore */ }
  }
  useEffect(() => {
    void loadRecovered();
    const t = setInterval(loadRecovered, 5000);  // keep the countdown fresh
    return () => clearInterval(t);
  }, []);

  async function retrieve(r: Result, loc: { destination: string; label: string }) {
    try {
      const res = await api.post<RetrieveResp>("/search/retrieve", {
        snapshot_id: r.snapshot_id, object_id: r.object_id, destination: loc.destination,
      });
      if (res.status === "recovered" && res.recovered_id) {
        setMsg(res.message);
        await loadRecovered();
        await openViewer({
          id: res.recovered_id, object_id: r.object_id, title: res.title || r.title,
          doc_type: res.doc_type || r.doc_type, source_type: r.source_type,
          mime: res.mime || "application/octet-stream", size_bytes: res.size_bytes || 0,
          location: loc.label, expires_in_seconds: 1800, viewed: false,
        });
      } else if (res.status === "client-encrypted" && res.content_b64) {
        downloadB64(res.content_b64, safeName(res.filename || r.title, r.doc_type));
        setMsg(res.message);
      } else {
        setMsg(res.message);
      }
      setTimeout(() => setMsg(""), 6000);
    } catch (e) {
      setMsg((e as ApiError).message);
      setTimeout(() => setMsg(""), 6000);
    }
  }

  // Fetch the staged plaintext (raw) and open it in the viewer.
  async function openViewer(item: Recovered) {
    try {
      const res = await fetch(`/api/recovered/${item.id}/content`, {
        headers: { Authorization: `Bearer ${getToken() ?? ""}` },
      });
      if (!res.ok) { setMsg(res.status === 410 ? "Recovery window expired" : "Could not open item"); return; }
      const blob = await res.blob();
      const mime = item.mime || blob.type;
      if (mime.startsWith("image/")) {
        setViewing({ item, kind: "image", url: URL.createObjectURL(blob) });
      } else if (mime.startsWith("text/") || mime === "application/json"
                 || mime === "message/rfc822" || mime === "application/xml") {
        setViewing({ item, kind: "text", text: await blob.text(), url: URL.createObjectURL(blob) });
      } else {
        setViewing({ item, kind: "binary", url: URL.createObjectURL(blob) });
      }
      await loadRecovered();
    } catch { setMsg("Could not open item"); }
  }

  function closeViewer() {
    if (viewing?.url) URL.revokeObjectURL(viewing.url);
    setViewing(null);
  }

  async function destroyRecovered(id: string) {
    try { await api.del(`/recovered/${id}`); } catch { /* ignore */ }
    if (viewing?.item.id === id) closeViewer();
    await loadRecovered();
  }

  // Pick the best copy to recover from: cloud first (direct download), then any
  // recoverable copy, then the first known location, else default to the cloud.
  function bestLocation(r: Result): { destination: string; label: string } {
    const locs = r.locations || [];
    return (locs.find((l) => l.destination === "cv-cloud")
      || locs.find((l) => l.destination === "customer-s3")
      || locs.find((l) => l.recoverable)
      || locs[0]
      || { destination: "cv-cloud", label: "Arkive Cloud" });
  }

  async function recover(r: Result) {
    await retrieve(r, bestLocation(r));
  }

  async function run() {
    try {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      if (source) params.set("source_type", source);
      if (category) params.set("category", category);
      if (label) params.set("label", label);
      if (attr) params.set("attr", attr);
      setData(await api.get<SearchResp>(`/search?${params.toString()}`));
      setLocked(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) setLocked(true);
    }
  }

  useEffect(() => {
    if (me?.passkey_verified) void run();
    else setLocked(true);
  }, [me?.passkey_verified, source, category, label, attr]);

  if (locked) {
    return (
      <Card>
        <div className="lock-banner" style={{ marginBottom: 16 }}>
          <Icon name="lock" />
          <div>
            <div style={{ fontWeight: 600 }}>Unified search is locked</div>
            <div className="faint" style={{ fontSize: 12 }}>
              Searching your protected data requires passkey / hardware-token verification.
            </div>
          </div>
        </div>
        <button className="btn accent" onClick={() => stepUp().then(run).catch((e) => notify({ message: e.message, tone: "danger" }))}>
          <Icon name="key" size={15} /> Unlock to search
        </button>
      </Card>
    );
  }

  return (
    <>
      {recovered.length > 0 && (
        <Card style={{ marginBottom: 12, borderColor: "var(--warn)" }}>
          <div className="spread" style={{ marginBottom: 8 }}>
            <h3 style={{ margin: 0 }}><Icon name="restore" size={15} /> Recovered items</h3>
            <span className="faint" style={{ fontSize: 12 }}>brought out of storage · auto-destroyed on expiry</span>
          </div>
          {recovered.map((it) => (
            <div key={it.id} className="result-row" style={{ padding: "8px 0" }}>
              <div className="result-icon" style={{ width: 30, height: 30, background: "#0e1524" }}>
                <Icon name={brandForSource(it.source_type) ? "database" : "file"} size={14} />
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600, fontSize: 13 }}>{it.title}</div>
                <div className="faint" style={{ fontSize: 11.5 }}>
                  {it.location} · {bytes(it.size_bytes)} · <span style={{ color: it.expires_in_seconds < 120 ? "var(--danger-c)" : undefined }}>expires in {fmtCountdown(it.expires_in_seconds)}</span>
                </div>
              </div>
              <button className="btn sm primary" onClick={() => openViewer(it)}>View</button>
              <a className="btn sm ghost" href={`/api/recovered/${it.id}/content`} download={it.title}
                 onClick={(e) => { e.preventDefault(); void fetch(`/api/recovered/${it.id}/content`, { headers: { Authorization: `Bearer ${getToken() ?? ""}` } }).then((r) => r.blob()).then((b) => { const u = URL.createObjectURL(b); const a = document.createElement("a"); a.href = u; a.download = it.title; a.click(); URL.revokeObjectURL(u); }); }}>
                Download
              </a>
              <button className="btn sm danger" onClick={() => destroyRecovered(it.id)}>Destroy</button>
            </div>
          ))}
        </Card>
      )}

      <div className="search-bar" style={{ marginBottom: 16 }}>
        <Icon name="search" />
        <input
          autoFocus
          value={q}
          placeholder="Search titles, contents & metadata — sender, path, tags, party…"
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && run()}
        />
        <button className="btn primary sm" onClick={run}>Search</button>
      </div>

      {data && (() => {
        const cats = Object.entries(data.facets.category).sort((a, b) => b[1] - a[1]);
        const srcs = Object.entries(data.facets.source).sort((a, b) => b[1] - a[1]);
        const labels = Object.entries(data.facets.label).sort((a, b) => b[1] - a[1]);
        const attrKeys = Object.keys(data.facets.attributes || {});
        const attrVals = attrKey ? data.facets.attributes?.[attrKey] : undefined;
        const attrValue = attr && attr.startsWith(`${attrKey}:`) ? attr.slice(attrKey.length + 1) : "";
        const srcLabel = source ? (data.source_display?.[source] ?? SOURCE_META[source]?.label ?? source) : "";
        const catLabel = category ? (CATEGORY_META[category]?.label ?? category) : "";
        const hasFilters = !!(source || category || label || attr);
        function clearAll() { setSource(null); setCategory(null); setLabel(null); setAttr(null); setAttrKey(""); }
        return (
          <>
            <div className="filter-bar">
              <label className="filter-select">
                <span>Source</span>
                <select value={source ?? ""} onChange={(e) => { setAttr(null); setAttrKey(""); setSource(e.target.value || null); }}>
                  <option value="">All sources ({data.total_indexed})</option>
                  {srcs.map(([s, n]) => (
                    <option key={s} value={s}>{data.source_display?.[s] ?? SOURCE_META[s]?.label ?? s} ({n})</option>
                  ))}
                </select>
              </label>
              {cats.length > 0 && (
                <label className="filter-select">
                  <span>Type</span>
                  <select value={category ?? ""} onChange={(e) => setCategory(e.target.value || null)}>
                    <option value="">All types</option>
                    {cats.map(([c, n]) => (
                      <option key={c} value={c}>{CATEGORY_META[c]?.label ?? c} ({n})</option>
                    ))}
                  </select>
                </label>
              )}
              {labels.length > 0 && (
                <label className="filter-select">
                  <span>Label</span>
                  <select value={label ?? ""} onChange={(e) => setLabel(e.target.value || null)}>
                    <option value="">All labels</option>
                    {labels.map(([l, n]) => <option key={l} value={l}>{l} ({n})</option>)}
                  </select>
                </label>
              )}
              {attrKeys.length > 0 && (
                <label className="filter-select">
                  <span>Attribute</span>
                  <select value={attrKey} onChange={(e) => { setAttrKey(e.target.value); setAttr(null); }}>
                    <option value="">Choose…</option>
                    {attrKeys.map((k) => <option key={k} value={k}>{k}</option>)}
                  </select>
                </label>
              )}
              {attrKey && attrVals && (
                <label className="filter-select">
                  <span style={{ textTransform: "capitalize" }}>{attrKey}</span>
                  <select value={attrValue}
                    onChange={(e) => setAttr(e.target.value ? `${attrKey}:${e.target.value}` : null)}>
                    <option value="">Any</option>
                    {Object.entries(attrVals).map(([v, n]) => <option key={v} value={v}>{v} ({n})</option>)}
                  </select>
                </label>
              )}
              {hasFilters && (
                <button className="btn ghost sm" style={{ alignSelf: "flex-end" }} onClick={clearAll}>Clear all</button>
              )}
            </div>

            {hasFilters && (
              <div className="active-filters">
                {source && <span className="filter-chip">{srcLabel}<button onClick={() => { setSource(null); setAttr(null); setAttrKey(""); }}>×</button></span>}
                {category && <span className="filter-chip">{catLabel}<button onClick={() => setCategory(null)}>×</button></span>}
                {label && <span className="filter-chip">Label: {label}<button onClick={() => setLabel(null)}>×</button></span>}
                {attr && <span className="filter-chip">{attr.replace(":", ": ")}<button onClick={() => setAttr(null)}>×</button></span>}
              </div>
            )}
          </>
        );
      })()}

      {data && (
        <div className="muted" style={{ marginBottom: 10, fontSize: 13 }}>
          {data.count} result{data.count === 1 ? "" : "s"}
        </div>
      )}

      {data?.results.map((r) => {
        const sm = SOURCE_META[r.source_type] ?? { color: "#1a2234", icon: "database" as IconName, label: r.source_type };
        const brand = brandForSource(r.source_type);
        return (
          <div key={`${r.source_type}:${r.object_id}`} className="result-row">
            <div className="result-icon" style={{ background: brand ? "#0e1524" : sm.color }}>
              {brand ? <BrandIcon name={brand} size={20} /> : <Icon name={sm.icon} size={18} />}
            </div>
            <div className="flex1">
              <div className="row" style={{ gap: 8, alignItems: "center" }}>
                <span style={{ fontWeight: 600 }}>{r.title}</span>
                <span className="src-tag" style={{ borderColor: sm.color, color: sm.color }}>
                  {brand ? <BrandIcon name={brand} size={11} /> : <Icon name={sm.icon} size={11} />}
                  {r.source_label || r.source_display || sm.label}
                </span>
              </div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                {r.preview || <span className="faint">no indexed metadata for this object</span>}
              </div>
              {r.labels && r.labels.length > 0 && (
                <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                  {r.labels.slice(0, 4).map((l) => (
                    <span
                      key={l}
                      className={`chip ${label === l ? "active" : ""}`}
                      style={{ padding: "2px 8px", fontSize: 11 }}
                      onClick={() => setLabel(l)}
                    >
                      {l}
                    </span>
                  ))}
                </div>
              )}
            </div>
            <div className="stack" style={{ alignItems: "flex-end", gap: 6 }}>
              <div className="row" style={{ gap: 6 }}>
                {r.sensitivity === "restricted" && <Pill tone="danger">restricted</Pill>}
                <Pill tone="info">{CATEGORY_META[r.category]?.label ?? r.category}</Pill>
                <span className="faint" style={{ fontSize: 11 }}>{r.doc_type}</span>
              </div>
              <div className="faint" style={{ fontSize: 11 }}>
                {bytes(r.size_bytes)} · {timeAgo(r.modified_at)}
              </div>
              <button className="btn sm primary" style={{ padding: "3px 12px" }} onClick={() => recover(r)}>
                <Icon name="restore" size={13} /> Recover
              </button>
              {r.locations && r.locations.length > 0 && (
                <div className="row" style={{ gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
                  <span className="faint" style={{ fontSize: 10.5 }}>from</span>
                  {r.locations.map((loc) => (
                    <button
                      key={loc.destination}
                      className="btn sm ghost"
                      style={{ padding: "2px 8px", fontSize: 11 }}
                      title={`Recover from ${loc.label}`}
                      onClick={() => retrieve(r, loc)}
                    >
                      <Icon name={/^(appliance|store:)/.test(loc.destination) ? "server" : "cloud"} size={12} />
                      {loc.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        );
      })}

      {data && data.results.length === 0 && (
        <Card><div className="muted">No matches. Try a different query or add more sources.</div></Card>
      )}

      {viewing && (
        <div className="modal-backdrop" onClick={closeViewer}>
          <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div>
                <h3 style={{ margin: 0 }}>{viewing.item.title}</h3>
                <div className="faint" style={{ fontSize: 12 }}>
                  {viewing.item.mime} · {bytes(viewing.item.size_bytes)} · from {viewing.item.location}
                </div>
              </div>
              <button className="btn ghost sm" onClick={closeViewer}><Icon name="logout" size={14} /></button>
            </div>
            <div className="modal-body">
              {viewing.kind === "text" && (
                <pre className="log-pane" style={{ maxHeight: "60vh" }}>{viewing.text}</pre>
              )}
              {viewing.kind === "image" && (
                <img src={viewing.url} alt={viewing.item.title} style={{ maxWidth: "100%", borderRadius: 8 }} />
              )}
              {viewing.kind === "binary" && (
                <div className="muted" style={{ padding: 16 }}>
                  This item type can't be previewed. Use Download to save it.
                </div>
              )}
            </div>
            <div className="modal-foot">
              <a className="btn ghost sm" href={viewing.url} download={viewing.item.title}>Download</a>
              <div style={{ flex: 1 }} />
              <button className="btn danger sm" onClick={() => destroyRecovered(viewing.item.id)}>Destroy now</button>
            </div>
          </div>
        </div>
      )}

      {msg && <div className="toast"><Icon name="check" size={15} /> {msg}</div>}
    </>
  );
}
