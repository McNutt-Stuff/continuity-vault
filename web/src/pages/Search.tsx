import { useEffect, useRef, useState } from "react";
import { api, ApiError, getToken } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, bytes, timeAgo, fmtAbsolute, Loading } from "../components/ui";
import { Icon, IconName } from "../components/Icon";
import { BrandIcon, brandForSource } from "../components/BrandIcon";
import { notify } from "../components/dialog";

interface Recovered {
  id: string; object_id: string; title: string; doc_type: string;
  source_type: string; mime: string; size_bytes: number; location: string;
  expires_in_seconds: number; viewed: boolean;
  version?: number | null; version_created_at?: string | null;
}
interface RetrieveResp {
  status: string; message: string; recovered_id?: string; title?: string;
  mime?: string; doc_type?: string; size_bytes?: number;
  content_b64?: string | null; filename?: string;
  async?: boolean; command_id?: string; appliance_name?: string;
}
interface RecoveredUnit {
  object_id: string; recovered_id: string; title: string; mime: string;
  size_bytes: number; doc_type: string; source_type: string;
}
interface RetrieveStatus {
  status: string; command_status: string; recovered: RecoveredUnit[];
  error?: string | null; message?: string | null;
}
interface Retrieving {
  commandId: string; title: string; location: string; stage: string; error?: string;
}
interface EmailView {
  from?: string; to?: string; cc?: string; subject?: string; date?: string;
  html?: string; text?: string;
}
interface OpField { label: string; value: string; concealed: boolean; kind: string; }
interface OnePasswordView {
  title: string; category: string; vault?: string; updatedAt?: string;
  fields: OpField[]; urls: { label?: string; href: string }[]; notes?: string;
}
interface NoteView { title?: string; notebook?: string; tags?: string[]; content?: string }
interface Viewing {
  item: Recovered;
  kind: "text" | "image" | "binary" | "email" | "onepassword" | "pdf" | "audio" | "video" | "note";
  text?: string; url?: string; email?: EmailView; onePassword?: OnePasswordView; note?: NoteView;
}

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
  first_ingested_at: string | null;
  locations?: { destination: string; label: string; recoverable: boolean }[];
  versions?: { version: number; snapshot_id: string; size_bytes: number; created_at: string | null; is_current: boolean }[];
  version_count?: number;
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
  social: { icon: "activity", label: "Social", color: "#c56cf0" },
  contact: { icon: "user", label: "Contacts", color: "#35d0a5" },
  document: { icon: "file", label: "Documents", color: "#4f7cff" },
  image: { icon: "image", label: "Images", color: "#35d0a5" },
  media: { icon: "activity", label: "Video & Audio", color: "#7a5cff" },
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
  endpoint_files: { color: "#7a5cff", icon: "file", label: "Endpoint Files" },
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

// --- Recovered-content parsers (email MIME + 1Password item) --------------

function safeDecode(bytes: Uint8Array, charset?: string): string {
  try { return new TextDecoder(charset || "utf-8").decode(bytes); }
  catch { return new TextDecoder("utf-8").decode(bytes); }
}

function decodeQuotedPrintable(s: string, charset?: string): string {
  const noSoft = s.replace(/=\r?\n/g, "");
  const out: number[] = [];
  for (let i = 0; i < noSoft.length; i++) {
    const ch = noSoft[i];
    if (ch === "=" && /^[0-9A-Fa-f]{2}$/.test(noSoft.substr(i + 1, 2))) {
      out.push(parseInt(noSoft.substr(i + 1, 2), 16)); i += 2;
    } else out.push(ch.charCodeAt(0));
  }
  return safeDecode(Uint8Array.from(out), charset);
}

function decodeBase64(s: string, charset?: string): string {
  try {
    const bin = atob(s.replace(/\s+/g, ""));
    return safeDecode(Uint8Array.from(bin, (c) => c.charCodeAt(0)), charset);
  } catch { return s; }
}

function decodeTransfer(body: string, encoding: string, charset?: string): string {
  const enc = (encoding || "").toLowerCase();
  if (enc === "base64") return decodeBase64(body, charset);
  if (enc === "quoted-printable") return decodeQuotedPrintable(body, charset);
  return body;
}

// Decode RFC 2047 encoded-words in header values (=?utf-8?B?...?= / ?Q?...?=).
function decodeMimeWords(s?: string): string | undefined {
  if (!s) return s;
  return s.replace(/=\?([^?]+)\?([BQ])\?([^?]*)\?=/gi, (_m, charset: string, enc: string, txt: string) =>
    enc.toUpperCase() === "B"
      ? decodeBase64(txt, charset)
      : decodeQuotedPrintable(txt.replace(/_/g, " "), charset));
}

function splitHeadersBody(raw: string): [string, string] {
  const m = raw.match(/\r?\n\r?\n/);
  if (!m || m.index === undefined) return [raw, ""];
  return [raw.slice(0, m.index), raw.slice(m.index + m[0].length)];
}

function parseHeaders(head: string): Record<string, string> {
  const unfolded: string[] = [];
  for (const line of head.split(/\r?\n/)) {
    if (/^\s/.test(line) && unfolded.length) unfolded[unfolded.length - 1] += " " + line.trim();
    else unfolded.push(line);
  }
  const out: Record<string, string> = {};
  for (const l of unfolded) {
    const m = l.match(/^([^:]+):\s?(.*)$/);
    if (m) out[m[1].toLowerCase()] = m[2];
  }
  return out;
}

function ctParam(ct: string, name: string): string | undefined {
  const m = ct.match(new RegExp(`${name}="?([^";]+)"?`, "i"));
  return m ? m[1] : undefined;
}

function splitMimeParts(body: string, boundary: string): string[] {
  const parts: string[] = [];
  for (let seg of body.split("--" + boundary)) {
    if (seg.startsWith("--")) continue;              // closing delimiter
    seg = seg.replace(/^\r?\n/, "");
    if (seg.trim()) parts.push(seg);
  }
  return parts;
}

function findMimeBodies(raw: string): { html?: string; text?: string } {
  const [head, body] = splitHeadersBody(raw);
  const headers = parseHeaders(head);
  const ct = headers["content-type"] || "text/plain";
  if (/^multipart\//i.test(ct)) {
    const boundary = ctParam(ct, "boundary");
    if (!boundary) return {};
    let html: string | undefined, text: string | undefined;
    for (const p of splitMimeParts(body, boundary)) {
      const found = findMimeBodies(p);
      if (found.html && !html) html = found.html;
      if (found.text && !text) text = found.text;
    }
    return { html, text };
  }
  const charset = ctParam(ct, "charset");
  const decoded = decodeTransfer(body, headers["content-transfer-encoding"] || "", charset);
  if (/text\/html/i.test(ct)) return { html: decoded };
  if (/text\/plain/i.test(ct)) return { text: decoded };
  return {};
}

function parseEmail(raw: string): EmailView {
  const [head] = splitHeadersBody(raw);
  const h = parseHeaders(head);
  const bodies = findMimeBodies(raw);
  return {
    from: decodeMimeWords(h["from"]),
    to: decodeMimeWords(h["to"]),
    cc: decodeMimeWords(h["cc"]),
    subject: decodeMimeWords(h["subject"]),
    date: h["date"],
    html: bodies.html,
    text: bodies.text,
  };
}

function prettyOpCategory(c: string): string {
  return (c || "Item").split("_").map((w) => w.charAt(0) + w.slice(1).toLowerCase()).join(" ");
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function parse1Password(d: any): OnePasswordView {
  const fields: OpField[] = [];
  let notes: string | undefined;
  for (const f of (d.fields || [])) {
    const value = f.value;
    if (value === undefined || value === null || value === "") continue;
    if (f.purpose === "NOTES" || f.id === "notesPlain") { notes = String(value); continue; }
    fields.push({
      label: f.label || f.id || "field",
      value: String(value),
      concealed: f.type === "CONCEALED" || f.purpose === "PASSWORD",
      kind: String(f.type || "").toLowerCase(),
    });
  }
  return {
    title: d.title || "(untitled)",
    category: prettyOpCategory(d.category || ""),
    vault: (d.vault || {}).name,
    updatedAt: d.updated_at,
    fields,
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    urls: (d.urls || []).filter((u: any) => u.href).map((u: any) => ({ label: u.label, href: u.href })),
    notes,
  };
}

function isOnePassword(item: Recovered): boolean {
  return item.source_type === "onepassword"
    || ["login", "password", "secret", "note", "identity", "api_key", "credit_card"].includes(item.doc_type);
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
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState("");
  const [recovered, setRecovered] = useState<Recovered[]>([]);
  const [viewing, setViewing] = useState<Viewing | null>(null);
  const [retrieving, setRetrieving] = useState<Retrieving | null>(null);
  const [versionsFor, setVersionsFor] = useState<Result | null>(null);
  const pollRef = useRef(0);

  async function loadRecovered() {
    try { setRecovered((await api.get<{ items: Recovered[] }>("/recovered")).items); }
    catch { /* ignore */ }
  }
  useEffect(() => {
    void loadRecovered();
    const t = setInterval(loadRecovered, 5000);  // keep the countdown fresh
    return () => clearInterval(t);
  }, []);

  async function retrieve(r: Result, loc: { destination: string; label: string }, snapshotOverride?: string) {
    try {
      const res = await api.post<RetrieveResp>("/search/retrieve", {
        snapshot_id: snapshotOverride || r.snapshot_id, object_id: r.object_id, destination: loc.destination,
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
      } else if (res.async && res.command_id) {
        // Appliance-stored: the appliance must unseal, retrieve and re-seal. Show a
        // live progress modal and poll until the item is decrypted and staged.
        setRetrieving({ commandId: res.command_id, title: r.title, location: loc.label, stage: "requested" });
        void pollRetrieveStatus(res.command_id, { title: r.title, location: loc.label, objectId: r.object_id });
      } else if (res.status === "client-encrypted" && res.content_b64) {
        downloadB64(res.content_b64, safeName(res.filename || r.title, r.doc_type));
        setMsg(res.message);
        setTimeout(() => setMsg(""), 6000);
      } else {
        setMsg(res.message);
        setTimeout(() => setMsg(""), 6000);
      }
    } catch (e) {
      setMsg((e as ApiError).message);
      setTimeout(() => setMsg(""), 6000);
    }
  }

  // Poll an appliance recovery command until it re-seals and the cloud stages the
  // decrypted item, then open it. Superseded/cancelled when pollRef changes.
  async function pollRetrieveStatus(commandId: string,
                                    ctx: { title: string; location: string; objectId: string }) {
    const token = ++pollRef.current;
    const deadline = Date.now() + 3 * 60 * 1000;
    while (pollRef.current === token && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 2000));
      if (pollRef.current !== token) return;
      let s: RetrieveStatus;
      try { s = await api.get<RetrieveStatus>(`/search/retrieve-status/${commandId}`); }
      catch { continue; }
      if (pollRef.current !== token) return;
      setRetrieving((cur) => (cur && cur.commandId === commandId
        ? { ...cur, stage: s.status, error: s.error || undefined } : cur));
      if (s.status === "ready" && s.recovered.length) {
        const rec = s.recovered.find((x) => x.object_id === ctx.objectId) || s.recovered[0];
        pollRef.current++;  // stop this loop
        setRetrieving(null);
        await loadRecovered();
        await openViewer({
          id: rec.recovered_id, object_id: rec.object_id, title: rec.title,
          doc_type: rec.doc_type, source_type: rec.source_type,
          mime: rec.mime, size_bytes: rec.size_bytes,
          location: ctx.location, expires_in_seconds: 1800, viewed: false,
        });
        return;
      }
      if (s.status === "unavailable" || s.status === "failed") {
        setRetrieving((cur) => (cur && cur.commandId === commandId
          ? { ...cur, stage: s.status, error: s.error || s.message || undefined } : cur));
        return;
      }
    }
    if (pollRef.current === token) {
      setRetrieving((cur) => (cur && cur.commandId === commandId
        ? { ...cur, stage: "failed", error: "Timed out waiting for the appliance." } : cur));
    }
  }

  function cancelRetrieving() {
    pollRef.current++;  // invalidate the active poll; item still stages in the tray
    setRetrieving(null);
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
      const url = URL.createObjectURL(blob);
      if (mime.startsWith("image/")) {
        setViewing({ item, kind: "image", url });
        await loadRecovered();
        return;
      }
      if (mime === "application/pdf") {
        setViewing({ item, kind: "pdf", url });
        await loadRecovered();
        return;
      }
      if (mime.startsWith("audio/")) {
        setViewing({ item, kind: "audio", url });
        await loadRecovered();
        return;
      }
      if (mime.startsWith("video/")) {
        setViewing({ item, kind: "video", url });
        await loadRecovered();
        return;
      }
      const text = await blob.text();
      const oversized = text.trimStart().startsWith("{") && text.includes("content_exceeds_cap");
      // 1Password items: parse the op JSON into a structured credential display.
      if (!oversized && isOnePassword(item)) {
        try {
          setViewing({ item, kind: "onepassword", onePassword: parse1Password(JSON.parse(text)), url });
          await loadRecovered();
          return;
        } catch { /* fall through to generic rendering */ }
      }
      // Notes (e.g. Evernote): render the note body rather than raw JSON.
      if (!oversized && item.doc_type === "note") {
        try {
          const n = JSON.parse(text);
          setViewing({ item, kind: "note", note: n, url });
          await loadRecovered();
          return;
        } catch { /* fall through to generic rendering */ }
      }
      // Email: parse the RFC822 MIME and render its HTML (or text) body.
      if (!oversized && (mime === "message/rfc822" || item.doc_type === "email")) {
        setViewing({ item, kind: "email", email: parseEmail(text), url });
        await loadRecovered();
        return;
      }
      if (mime.startsWith("text/") || mime === "application/json"
          || mime === "message/rfc822" || mime === "application/xml") {
        setViewing({ item, kind: "text", text, url });
      } else {
        setViewing({ item, kind: "binary", url });
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
    setLoading(true);
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
      if (e instanceof ApiError && e.status === 403) {
        setLocked(true);
      } else {
        // Don't fail silently — show why search didn't return.
        setMsg((e as ApiError).message || "Search failed");
        setTimeout(() => setMsg(""), 6000);
      }
    } finally {
      setLoading(false);
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
              <div className="result-icon" style={{ width: 30, height: 30, background: "var(--inset)" }}>
                {brandForSource(it.source_type)
                  ? <BrandIcon name={brandForSource(it.source_type)!} size={16} />
                  : <Icon name="file" size={14} />}
              </div>
              <div className="flex1">
                <div style={{ fontWeight: 600, fontSize: 13 }}>
                  {it.title}
                  {it.version != null && (
                    <span className="faint" style={{ fontWeight: 400, fontSize: 11.5 }}> · v{it.version}</span>
                  )}
                </div>
                <div className="faint" style={{ fontSize: 11.5 }}>
                  {it.location} · {bytes(it.size_bytes)}
                  {it.version_created_at ? ` · version from ${timeAgo(it.version_created_at)}` : ""}
                   · <span style={{ color: it.expires_in_seconds < 120 ? "var(--danger-c)" : undefined }}>expires in {fmtCountdown(it.expires_in_seconds)}</span>
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

      {loading && <Loading label="Searching your protected data…" />}
      {!loading && data && data.results.length === 0 && (
        <Card><div className="loading-state muted">
          {data.total_indexed === 0
            ? "Nothing is indexed yet — run a backup, then search."
            : `No matches${q ? ` for “${q}”` : ""}. Search covers titles and indexed metadata (sender, path, tags, folder…), not encrypted file contents.`}
        </div></Card>
      )}

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
        const sm = SOURCE_META[r.source_type] ?? { color: "var(--bg-elev-2)", icon: "database" as IconName, label: r.source_type };
        const brand = brandForSource(r.source_type);
        return (
          <div key={`${r.source_type}:${r.object_id}`} className="result-row">
            <div className="result-icon" style={{ background: brand ? "var(--inset)" : sm.color }}>
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
                {(r.version_count ?? 0) > 1 && (
                  <button className="btn sm ghost" style={{ padding: "1px 8px", fontSize: 11 }}
                          title="View version history" onClick={() => setVersionsFor(r)}>
                    <Icon name="clock" size={11} /> {r.version_count} versions
                  </button>
                )}
                <Pill tone="info">{CATEGORY_META[r.category]?.label ?? r.category}</Pill>
                <span className="faint" style={{ fontSize: 11 }}>{r.doc_type}</span>
              </div>
              <div className="faint" style={{ fontSize: 11 }} title={`First ingested ${fmtAbsolute(r.first_ingested_at)}`}>
                {bytes(r.size_bytes)} · ingested {timeAgo(r.first_ingested_at)}
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
              {viewing.kind === "email" && viewing.email && (
                <EmailCard data={viewing.email} />
              )}
              {viewing.kind === "onepassword" && viewing.onePassword && (
                <OnePasswordCard data={viewing.onePassword} />
              )}
              {viewing.kind === "image" && (
                <img src={viewing.url} alt={viewing.item.title} style={{ maxWidth: "100%", borderRadius: 8 }} />
              )}
              {viewing.kind === "pdf" && (
                <iframe src={viewing.url} title={viewing.item.title}
                        style={{ width: "100%", height: "70vh", border: "none", borderRadius: 8, background: "#fff" }} />
              )}
              {viewing.kind === "audio" && (
                <audio controls src={viewing.url} style={{ width: "100%" }} />
              )}
              {viewing.kind === "video" && (
                <video controls src={viewing.url} style={{ maxWidth: "100%", maxHeight: "70vh", borderRadius: 8 }} />
              )}
              {viewing.kind === "note" && viewing.note && (
                <div>
                  {(viewing.note.notebook || viewing.note.tags?.length) && (
                    <div className="faint" style={{ fontSize: 12, marginBottom: 10 }}>
                      {viewing.note.notebook ? `📓 ${viewing.note.notebook}` : ""}
                      {viewing.note.tags?.length ? `  ·  ${viewing.note.tags.join(", ")}` : ""}
                    </div>
                  )}
                  <pre className="log-pane" style={{ maxHeight: "60vh", whiteSpace: "pre-wrap" }}>
                    {viewing.note.content || "(empty note)"}
                  </pre>
                </div>
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

      {retrieving && (() => {
        const steps = [
          "Request sent to the appliance",
          "Appliance unsealing & retrieving from sealed storage",
          "Decrypting & opening the item",
        ];
        const order: Record<string, number> = {
          requested: 0, awaiting_approval: 1, retrieving: 1,
          ready: 3, unavailable: 3, failed: 3,
        };
        const failed = retrieving.stage === "failed" || retrieving.stage === "unavailable";
        const active = order[retrieving.stage] ?? 0;
        return (
          <div className="modal-backdrop" onClick={cancelRetrieving}>
            <div className="modal-panel" style={{ maxWidth: 460 }} onClick={(e) => e.stopPropagation()}>
              <div className="spread">
                <div>
                  <h3 style={{ margin: 0 }}><Icon name="restore" size={15} /> Recovering from appliance</h3>
                  <div className="faint" style={{ fontSize: 12 }}>{retrieving.title} · {retrieving.location}</div>
                </div>
                <button className="btn ghost sm" onClick={cancelRetrieving}><Icon name="logout" size={14} /></button>
              </div>
              <div className="modal-body">
                <div style={{ display: "grid", gap: 14, padding: "8px 4px" }}>
                  {steps.map((label, i) => {
                    const done = active > i && !failed;
                    const isActive = active === i && !failed;
                    return (
                      <div key={i} className="flex" style={{ gap: 10, alignItems: "center", opacity: done || isActive ? 1 : 0.45 }}>
                        <span style={{
                          width: 24, height: 24, borderRadius: 12, flex: "none",
                          display: "grid", placeItems: "center", color: "#fff", fontSize: 12,
                          background: done ? "var(--ok, #35d0a5)" : isActive ? "var(--accent, #4f7cff)" : "#1b2436",
                        }}>
                          {done ? <Icon name="check" size={12} /> : isActive ? <span className="spinner-dot" /> : i + 1}
                        </span>
                        <span style={{ fontSize: 13 }}>{label}</span>
                      </div>
                    );
                  })}
                  {retrieving.stage === "awaiting_approval" && (
                    <div className="muted" style={{ fontSize: 12.5 }}>
                      Waiting for someone to approve the recovery on the appliance's physical panel.
                    </div>
                  )}
                  {failed && (
                    <div style={{ color: "var(--danger-c, #f2545b)", fontSize: 12.5 }}>
                      {retrieving.error || (retrieving.stage === "unavailable"
                        ? "The appliance returned no recoverable content for this item."
                        : "Recovery failed.")}
                    </div>
                  )}
                </div>
              </div>
              <div className="modal-foot">
                <div style={{ flex: 1 }} />
                <button className="btn ghost sm" onClick={cancelRetrieving}>
                  {failed ? "Close" : "Run in background"}
                </button>
              </div>
            </div>
          </div>
        );
      })()}

      {versionsFor && (
        <div className="modal-backdrop" onClick={() => setVersionsFor(null)}>
          <div className="modal-panel" style={{ maxWidth: 560 }} onClick={(e) => e.stopPropagation()}>
            <div className="spread">
              <div>
                <h3 style={{ margin: 0 }}>Version history</h3>
                <div className="faint" style={{ fontSize: 12 }}>{versionsFor.title}</div>
              </div>
              <button className="btn ghost sm" onClick={() => setVersionsFor(null)}><Icon name="logout" size={14} /></button>
            </div>
            <div className="modal-body">
              <div className="faint" style={{ fontSize: 12, marginBottom: 8 }}>
                Each change is kept as an immutable version — recover any point in time.
              </div>
              {(versionsFor.versions || []).map((v) => (
                <div key={v.version} className="result-row" style={{ padding: "8px 0" }}>
                  <div className="result-icon" style={{ width: 30, height: 30, background: "var(--inset)" }}>
                    <Icon name="clock" size={14} />
                  </div>
                  <div className="flex1">
                    <div style={{ fontWeight: 600, fontSize: 13 }}>
                      v{v.version}{v.is_current ? <span className="faint" style={{ fontWeight: 400 }}> · current</span> : ""}
                    </div>
                    <div className="faint" style={{ fontSize: 11.5 }} title={fmtAbsolute(v.created_at)}>
                      {bytes(v.size_bytes)} · {timeAgo(v.created_at)}
                    </div>
                  </div>
                  <button className="btn sm primary" onClick={() => { const r = versionsFor; setVersionsFor(null); void retrieve(r, bestLocation(r), v.snapshot_id); }}>
                    <Icon name="restore" size={13} /> Recover
                  </button>
                </div>
              ))}
            </div>
            <div className="modal-foot">
              <div style={{ flex: 1 }} />
              <button className="btn ghost sm" onClick={() => setVersionsFor(null)}>Close</button>
            </div>
          </div>
        </div>
      )}

      {msg && <div className="toast"><Icon name="check" size={15} /> {msg}</div>}
    </>
  );
}

// Rendered email: decoded headers + the HTML body in a locked-down iframe (no
// scripts, no form submission) so recovered mail displays as intended but safely.
function EmailCard({ data }: { data: EmailView }) {
  const [showText, setShowText] = useState(false);
  const hasHtml = !!data.html;
  const rows: [string, string | undefined][] = [
    ["From", data.from], ["To", data.to], ["Cc", data.cc], ["Date", data.date],
  ];
  return (
    <div>
      <div style={{ border: "1px solid var(--border,#22304a)", borderRadius: 8, padding: "10px 12px", marginBottom: 10 }}>
        {data.subject && <div style={{ fontWeight: 700, fontSize: 15, marginBottom: 6 }}>{data.subject}</div>}
        <div style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "2px 10px", fontSize: 12.5 }}>
          {rows.filter(([, v]) => v).map(([k, v]) => (
            <div key={k} style={{ display: "contents" }}>
              <span className="faint">{k}</span>
              <span style={{ overflowWrap: "anywhere" }}>{v}</span>
            </div>
          ))}
        </div>
      </div>
      {hasHtml && data.text && (
        <div className="row" style={{ gap: 8, marginBottom: 8 }}>
          <button className={`chip ${!showText ? "active" : ""}`} onClick={() => setShowText(false)}>HTML</button>
          <button className={`chip ${showText ? "active" : ""}`} onClick={() => setShowText(true)}>Plain text</button>
        </div>
      )}
      {hasHtml && !showText ? (
        <iframe
          title="email"
          sandbox=""
          srcDoc={data.html}
          style={{ width: "100%", height: "58vh", border: "1px solid var(--border,#22304a)",
                   borderRadius: 8, background: "#fff" }}
        />
      ) : (data.text || data.html) ? (
        <pre className="log-pane" style={{ maxHeight: "58vh", whiteSpace: "pre-wrap" }}>
          {data.text ?? data.html}
        </pre>
      ) : (
        <div className="muted" style={{ padding: 16 }}>This message has no readable body.</div>
      )}
    </div>
  );
}

// Rendered 1Password item: fields laid out like the 1Password app, with
// concealed values masked behind a reveal toggle and copy-to-clipboard.
function OnePasswordCard({ data }: { data: OnePasswordView }) {
  const [revealed, setRevealed] = useState<Record<number, boolean>>({});
  const [copied, setCopied] = useState<string>("");
  async function copy(value: string, tag: string) {
    try { await navigator.clipboard.writeText(value); setCopied(tag); setTimeout(() => setCopied(""), 1500); }
    catch { /* clipboard blocked */ }
  }
  return (
    <div style={{ maxHeight: "62vh", overflow: "auto" }}>
      <div className="row" style={{ gap: 10, alignItems: "center", marginBottom: 12 }}>
        <div style={{ width: 38, height: 38, borderRadius: 9, display: "grid", placeItems: "center",
                      background: "#0364d3" }}>
          <BrandIcon name="onepassword" size={20} />
        </div>
        <div>
          <div style={{ fontWeight: 700, fontSize: 15 }}>{data.title}</div>
          <div className="faint" style={{ fontSize: 12 }}>
            {data.category}{data.vault ? ` · ${data.vault}` : ""}
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gap: 8 }}>
        {data.fields.map((f, i) => {
          const show = !!revealed[i] || !f.concealed;
          const tag = `f${i}`;
          return (
            <div key={i} style={{ border: "1px solid var(--border,#22304a)", borderRadius: 8, padding: "8px 10px" }}>
              <div className="faint" style={{ fontSize: 11, textTransform: "capitalize", marginBottom: 2 }}>{f.label}</div>
              <div className="row" style={{ gap: 8, alignItems: "center" }}>
                <span style={{ fontFamily: f.concealed ? "monospace" : undefined, overflowWrap: "anywhere", flex: 1 }}>
                  {show ? f.value : "•".repeat(Math.min(12, f.value.length || 8))}
                </span>
                {f.concealed && (
                  <button className="btn ghost sm" title={show ? "Hide" : "Reveal"}
                          onClick={() => setRevealed((c) => ({ ...c, [i]: !c[i] }))}>
                    <Icon name={show ? "lock" : "key"} size={13} />
                  </button>
                )}
                <button className="btn ghost sm" title="Copy" onClick={() => copy(f.value, tag)}>
                  <Icon name={copied === tag ? "check" : "file"} size={13} />
                </button>
              </div>
            </div>
          );
        })}

        {data.urls.length > 0 && (
          <div style={{ border: "1px solid var(--border,#22304a)", borderRadius: 8, padding: "8px 10px" }}>
            <div className="faint" style={{ fontSize: 11, marginBottom: 4 }}>Websites</div>
            {data.urls.map((u, i) => (
              <div key={i}>
                <a href={u.href} target="_blank" rel="noreferrer noopener" style={{ color: "var(--accent,#4f7cff)", overflowWrap: "anywhere" }}>
                  {u.href}
                </a>
              </div>
            ))}
          </div>
        )}

        {data.notes && (
          <div style={{ border: "1px solid var(--border,#22304a)", borderRadius: 8, padding: "8px 10px" }}>
            <div className="faint" style={{ fontSize: 11, marginBottom: 4 }}>Notes</div>
            <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontFamily: "inherit", fontSize: 13 }}>{data.notes}</pre>
          </div>
        )}
      </div>

      {data.updatedAt && (
        <div className="faint" style={{ fontSize: 11, marginTop: 10 }}>Last modified {data.updatedAt}</div>
      )}
    </div>
  );
}

