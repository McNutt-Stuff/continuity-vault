import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Card, Pill, bytes, timeAgo } from "../components/ui";
import { Icon, IconName } from "../components/Icon";

interface Result {
  object_id: string;
  snapshot_id: string;
  source_type: string;
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
  };
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

export default function Search() {
  const { me, stepUp } = useAuth();
  const [q, setQ] = useState("");
  const [source, setSource] = useState<string | null>(null);
  const [category, setCategory] = useState<string | null>(null);
  const [label, setLabel] = useState<string | null>(null);
  const [data, setData] = useState<SearchResp | null>(null);
  const [locked, setLocked] = useState(false);
  const [msg, setMsg] = useState("");

  async function retrieve(r: Result, loc: { destination: string; label: string }) {
    try {
      const res = await api.post<{ message: string }>("/search/retrieve", {
        snapshot_id: r.snapshot_id, object_id: r.object_id, destination: loc.destination,
      });
      setMsg(res.message);
      setTimeout(() => setMsg(""), 6000);
    } catch (e) {
      setMsg((e as ApiError).message);
      setTimeout(() => setMsg(""), 6000);
    }
  }

  async function run() {
    try {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      if (source) params.set("source_type", source);
      if (category) params.set("category", category);
      if (label) params.set("label", label);
      setData(await api.get<SearchResp>(`/search?${params.toString()}`));
      setLocked(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) setLocked(true);
    }
  }

  useEffect(() => {
    if (me?.passkey_verified) void run();
    else setLocked(true);
  }, [me?.passkey_verified, source, category, label]);

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
        <button className="btn accent" onClick={() => stepUp().then(run).catch((e) => alert(e.message))}>
          <Icon name="key" size={15} /> Unlock to search
        </button>
      </Card>
    );
  }

  return (
    <>
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

      {data && Object.keys(data.facets.category).length > 0 && (
        <div className="chips" style={{ marginBottom: 10 }}>
          <span className={`chip ${!category ? "active" : ""}`} onClick={() => setCategory(null)}>
            All types
          </span>
          {Object.entries(data.facets.category)
            .sort((a, b) => b[1] - a[1])
            .map(([cat, n]) => {
              const cm = CATEGORY_META[cat] ?? { icon: "database" as IconName, label: cat, color: "#9aa7bf" };
              return (
                <span key={cat} className={`chip ${category === cat ? "active" : ""}`}
                  style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                  onClick={() => setCategory(category === cat ? null : cat)}>
                  <span style={{ color: cm.color, display: "inline-flex" }}><Icon name={cm.icon} size={13} /></span>
                  {cm.label} · {n}
                </span>
              );
            })}
        </div>
      )}

      <div className="chips" style={{ marginBottom: 10 }}>
        <span className={`chip ${!source ? "active" : ""}`} onClick={() => setSource(null)}>
          All sources {data && `· ${data.total_indexed}`}
        </span>
        {data &&
          Object.entries(data.facets.source).map(([s, n]) => (
            <span key={s} className={`chip ${source === s ? "active" : ""}`} onClick={() => setSource(s)}>
              {SOURCE_META[s]?.label ?? s} · {n}
            </span>
          ))}
      </div>

      {data && Object.keys(data.facets.label).length > 0 && (
        <div className="chips" style={{ marginBottom: 18 }}>
          <span className="faint" style={{ fontSize: 11, alignSelf: "center", marginRight: 4 }}>Labels</span>
          <span className={`chip ${!label ? "active" : ""}`} onClick={() => setLabel(null)}>All</span>
          {Object.entries(data.facets.label)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 12)
            .map(([l, n]) => (
              <span key={l} className={`chip ${label === l ? "active" : ""}`} onClick={() => setLabel(l)}>
                {l} · {n}
              </span>
            ))}
        </div>
      )}

      {data && (
        <div className="muted" style={{ marginBottom: 10, fontSize: 13 }}>
          {data.count} result{data.count === 1 ? "" : "s"}
        </div>
      )}

      {data?.results.map((r) => {
        const sm = SOURCE_META[r.source_type] ?? { color: "#1a2234", icon: "database" as IconName, label: r.source_type };
        return (
          <div key={r.object_id} className="result-row">
            <div className="result-icon" style={{ background: sm.color }}>
              <Icon name={sm.icon} size={18} />
            </div>
            <div className="flex1">
              <div style={{ fontWeight: 600 }}>{r.title}</div>
              <div className="faint" style={{ fontSize: 12.5 }}>
                {r.preview || <span className="faint">metadata hidden (zero-knowledge vault)</span>}
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
              {r.locations && r.locations.length > 0 && (
                <div className="row" style={{ gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
                  <span className="faint" style={{ fontSize: 10.5 }}>Stored at</span>
                  {r.locations.map((loc) => (
                    <button
                      key={loc.destination}
                      className="btn sm ghost"
                      style={{ padding: "2px 8px", fontSize: 11 }}
                      title={`Retrieve from ${loc.label}`}
                      onClick={() => retrieve(r, loc)}
                    >
                      <Icon name={loc.destination.startsWith("appliance") ? "server" : "cloud"} size={12} />
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

      {msg && <div className="toast"><Icon name="check" size={15} /> {msg}</div>}
    </>
  );
}
