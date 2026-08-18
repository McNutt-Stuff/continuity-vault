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
  title: string;
  preview: string;
  meta: Record<string, unknown>;
  labels: string[];
  size_bytes: number;
  modified_at: string | null;
}
interface SearchResp {
  count: number;
  total_indexed: number;
  results: Result[];
  facets: { source: Record<string, number>; type: Record<string, number>; label: Record<string, number> };
}

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
  const [label, setLabel] = useState<string | null>(null);
  const [data, setData] = useState<SearchResp | null>(null);
  const [locked, setLocked] = useState(false);

  async function run() {
    try {
      const params = new URLSearchParams();
      if (q) params.set("q", q);
      if (source) params.set("source_type", source);
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
  }, [me?.passkey_verified, source, label]);

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
              <Pill tone="info">{r.doc_type}</Pill>
              <div className="faint" style={{ fontSize: 11 }}>
                {bytes(r.size_bytes)} · {timeAgo(r.modified_at)}
              </div>
            </div>
          </div>
        );
      })}

      {data && data.results.length === 0 && (
        <Card><div className="muted">No matches. Try a different query or add more sources.</div></Card>
      )}
    </>
  );
}
