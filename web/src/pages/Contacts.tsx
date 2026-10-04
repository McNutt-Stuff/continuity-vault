import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Card, Loading, Pill, timeAgo } from "../components/ui";
import { Icon } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { notify, confirmDialog } from "../components/dialog";

// ---- Types -----------------------------------------------------------------
interface Stats {
  by_source?: Record<string, number>;
  by_direction?: { in?: number; out?: number };
  by_month?: { m: string; count: number }[];
  bytes?: number;
  top_source?: string;
  identity_count?: number;
}
interface Contact {
  id: string; display_name: string; nickname?: string;
  given_name?: string; family_name?: string;
  primary_email?: string; primary_phone?: string; avatar_url?: string;
  circle: string; pinned_circle?: string; relationship?: string;
  labels: string[]; starred: boolean; hidden?: boolean;
  interaction_count: number; last_interaction_at?: string | null;
  first_interaction_at?: string | null; source_types: string[];
  stats: Stats; notes?: string; details?: Record<string, Record<string, string>>;
  identities?: Identity[];
}
interface Identity {
  id: string; kind: string; value: string; raw_value: string; label: string;
  source_type: string; link_method: string; confirmed: boolean; confidence: number;
}
interface Overview {
  total: number; by_circle: Record<string, number>;
  by_source: Record<string, number>; by_relationship: Record<string, number>;
  pending_suggestions: number; top_contacts: Contact[];
  prefs: { relationships: string[]; labels: string[]; auto_link: boolean };
}
interface Exchange {
  source_type: string; doc_type: string; object_id: string; title: string;
  preview: string; direction: string; modified_at?: string | null; size_bytes: number;
}
interface Suggestion {
  id: string; kind: string; reason: string; confidence: number;
  contact?: Contact | null; merge_contact?: Contact | null;
  identity?: { kind: string; value: string; raw: string; source: string } | null;
}
interface GraphNode {
  id: string; name: string; circle: string; relationship?: string;
  labels?: string[]; interaction_count?: number; weight?: number; me?: boolean; starred?: boolean;
}
interface GraphData { nodes: GraphNode[]; edges: { source: string; target: string; weight: number; circle: string }[]; }

// ---- Circle metadata (widest → tightest) -----------------------------------
const CIRCLES = [
  { key: "inner", label: "Inner circle", color: "var(--brand)", ring: 1 },
  { key: "close", label: "Close", color: "var(--brand-2)", ring: 2 },
  { key: "active", label: "Active", color: "var(--accent)", ring: 3 },
  { key: "acquaintance", label: "Acquaintances", color: "var(--text-dim)", ring: 4 },
  { key: "dormant", label: "Dormant", color: "var(--text-faint)", ring: 5 },
];
const CIRCLE_META: Record<string, { label: string; color: string }> =
  Object.fromEntries(CIRCLES.map((c) => [c.key, { label: c.label, color: c.color }]));

function initials(name: string): string {
  const p = (name || "?").trim().split(/\s+/);
  return ((p[0]?.[0] || "") + (p.length > 1 ? p[p.length - 1][0] : "")).toUpperCase() || "?";
}
function avatarColor(name: string): string {
  let h = 0;
  for (let i = 0; i < (name || "").length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
  return `hsl(${h} 45% 42%)`;
}

// ---- Avatar ----------------------------------------------------------------
function Avatar({ name, size = 38, starred }: { name: string; size?: number; starred?: boolean }) {
  return (
    <div style={{ position: "relative", width: size, height: size, flexShrink: 0 }}>
      <div style={{
        width: size, height: size, borderRadius: "50%", background: avatarColor(name),
        color: "#fff", display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: size * 0.4, fontWeight: 700,
      }}>{initials(name)}</div>
      {starred && <span style={{ position: "absolute", bottom: -2, right: -2, fontSize: size * 0.34 }}>★</span>}
    </div>
  );
}

// ---- Main ------------------------------------------------------------------
export default function Contacts() {
  const [params, setParams] = useSearchParams();
  const [tab, setTab] = useState<"people" | "circles" | "suggestions">("people");
  const [ov, setOv] = useState<Overview | null>(null);
  const [contacts, setContacts] = useState<Contact[] | null>(null);
  const [q, setQ] = useState("");
  const [circle, setCircle] = useState("");
  const [relationship, setRelationship] = useState("");
  const [sort, setSort] = useState("circle");
  const [starredOnly, setStarredOnly] = useState(false);
  const [selected, setSelected] = useState<string | null>(params.get("c"));
  const [fullId, setFullId] = useState<string | null>(params.get("full"));
  const [busy, setBusy] = useState(false);
  const [showSettings, setShowSettings] = useState(false);

  async function loadOverview() {
    try { setOv(await api.get<Overview>("/contacts/overview")); } catch { /* flag off */ }
  }
  async function loadList() {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (circle) p.set("circle", circle);
    if (relationship) p.set("relationship", relationship);
    if (starredOnly) p.set("starred", "true");
    p.set("sort", sort);
    try {
      const r = await api.get<{ contacts: Contact[] }>(`/contacts?${p.toString()}`);
      setContacts(r.contacts);
    } catch { setContacts([]); }
  }
  useEffect(() => { void loadOverview(); }, []);
  useEffect(() => { void loadList(); }, [q, circle, relationship, sort, starredOnly]);
  useEffect(() => { if (selected) setParams({ c: selected }); else setParams({}); }, [selected]);

  async function rebuild() {
    setBusy(true);
    try {
      const r = await api.post<{ contacts: number; overview?: Overview; page?: Contact[] }>("/contacts/rebuild");
      // Seed straight from the rebuild response (computed on the node that just
      // built them) so the UI shows the result immediately; the CP replica the
      // normal reads use only catches up on the next replication push (~30s).
      if (r.overview) setOv(r.overview);
      if (r.page) setContacts(r.page);
      else { await loadOverview(); await loadList(); }
      notify({ title: "Contacts rebuilt", message: `Linked ${r.contacts} ${r.contacts === 1 ? "person" : "people"} across your sources.`, tone: "ok" });
      // Reconcile against the CP replica once it has replicated (keeps filters live).
      window.setTimeout(() => { void loadOverview(); void loadList(); }, 35000);
    } catch (e) {
      const err = e as { status?: number; message?: string };
      const msg = err.status === 404
        ? "Unified Contacts isn't available on your node yet — it may still be updating. Please try again shortly."
        : (err.message || "The rebuild request failed.");
      notify({ title: "Couldn't rebuild contacts", message: msg, tone: "danger" });
    } finally { setBusy(false); }
  }

  const grouped = useMemo(() => {
    const m: Record<string, Contact[]> = {};
    for (const c of contacts || []) (m[c.circle] ||= []).push(c);
    return m;
  }, [contacts]);

  useEffect(() => {
    const p: Record<string, string> = {};
    if (fullId) p.full = fullId; else if (selected) p.c = selected;
    setParams(p);
  }, [fullId]);

  if (fullId) return (
    <ContactFull id={fullId} relationships={ov?.prefs.relationships || []} labels={ov?.prefs.labels || []}
                 onBack={() => setFullId(null)}
                 onChanged={() => { loadList(); loadOverview(); }} />
  );

  return (
    <div>
      <div className="spread" style={{ alignItems: "flex-start", marginBottom: 14, gap: 12, flexWrap: "wrap" }}>
        <div>
          <h1 style={{ margin: "0 0 2px" }}>My Circles</h1>
          <div className="faint" style={{ fontSize: 13 }}>
            A unified view of the people in your life — deduced across every source and linked to your exchanges.
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn ghost sm" onClick={() => setShowSettings(true)}>
            <Icon name="gear" size={13} /> Customize
          </button>
          <button className="btn ghost sm" onClick={rebuild} disabled={busy}>
            <span style={busy ? { display: "inline-block", animation: "spin 1s linear infinite" } : undefined}>
              <Icon name="repeat" size={13} />
            </span> {busy ? "Rebuilding…" : "Rebuild"}
          </button>
        </div>
      </div>

      {showSettings && <ContactsSettings onClose={() => { setShowSettings(false); loadOverview(); }} />}

      {ov && (
        <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(150px,1fr))", marginBottom: 14 }}>
          <MiniStat label="People" value={ov.total} />
          <MiniStat label="Inner circle" value={ov.by_circle.inner || 0} tint="var(--brand)" />
          <MiniStat label="Close" value={ov.by_circle.close || 0} tint="var(--brand-2)" />
          <MiniStat label="Sources linked" value={Object.keys(ov.by_source).length} />
          <MiniStat label="Suggestions" value={ov.pending_suggestions}
                    onClick={() => setTab("suggestions")} tint={ov.pending_suggestions ? "var(--warn)" : undefined} />
        </div>
      )}

      <div className="row" style={{ gap: 6, marginBottom: 14, borderBottom: "1px solid var(--border)" }}>
        {([["people", "People"], ["circles", "Circles map"], ["suggestions", "Suggestions"]] as const).map(([k, l]) => (
          <button key={k} onClick={() => setTab(k)}
                  className="btn ghost sm"
                  style={{ borderRadius: 0, borderBottom: tab === k ? "2px solid var(--brand)" : "2px solid transparent",
                           color: tab === k ? "var(--text)" : "var(--text-dim)", fontWeight: tab === k ? 600 : 400 }}>
            {l}{k === "suggestions" && ov?.pending_suggestions ? ` (${ov.pending_suggestions})` : ""}
          </button>
        ))}
      </div>

      {tab === "people" && (
        <div style={{ display: "grid", gridTemplateColumns: selected ? "minmax(320px, 1fr) minmax(360px, 1.3fr)" : "1fr", gap: 14, alignItems: "start" }}>
          <div>
            <div className="filter-toolbar" style={{ marginBottom: 14 }}>
              <div className="search-bar" style={{ padding: "9px 13px" }}>
                <Icon name="search" size={16} />
                <input placeholder="Search people by name, email or phone…" value={q}
                       onChange={(e) => setQ(e.target.value)} />
                {q && <button className="filter-bar-clear" title="Clear" onClick={() => setQ("")}>×</button>}
              </div>
              <div className="filter-bar">
                <label className="filter-select">
                  <span>Circle</span>
                  <select value={circle} onChange={(e) => setCircle(e.target.value)}>
                    <option value="">All circles</option>
                    {CIRCLES.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
                  </select>
                </label>
                <label className="filter-select">
                  <span>Relationship</span>
                  <select value={relationship} onChange={(e) => setRelationship(e.target.value)}>
                    <option value="">All relationships</option>
                    {(ov?.prefs.relationships || []).map((r) => <option key={r} value={r}>{r}</option>)}
                  </select>
                </label>
                <label className="filter-select">
                  <span>Sort by</span>
                  <select value={sort} onChange={(e) => setSort(e.target.value)}>
                    <option value="circle">Closeness</option>
                    <option value="frequency">Most contacted</option>
                    <option value="recent">Recent</option>
                    <option value="name">Name</option>
                  </select>
                </label>
                <button className="filter-chip"
                        onClick={() => setStarredOnly((v) => !v)}
                        style={{ cursor: "pointer", alignSelf: "flex-end", padding: "6px 12px",
                                 borderColor: starredOnly ? "var(--brand)" : "var(--border)",
                                 background: starredOnly ? "rgba(79,124,255,.1)" : "transparent",
                                 color: starredOnly ? "var(--text)" : "var(--text-dim)" }}>
                  ★ Starred
                </button>
              </div>
            </div>
            {contacts === null ? <Loading label="Loading your people…" />
              : contacts.length === 0 ? (
                <Card><div className="muted" style={{ padding: "16px 4px" }}>
                  No contacts yet. Connect contact + message sources, then Rebuild.
                </div></Card>
              ) : sort === "circle" ? (
                CIRCLES.filter((c) => (grouped[c.key] || []).length).map((c) => (
                  <div key={c.key} style={{ marginBottom: 14 }}>
                    <div className="row" style={{ gap: 7, marginBottom: 6, alignItems: "center" }}>
                      <span style={{ width: 8, height: 8, borderRadius: "50%", background: c.color }} />
                      <b style={{ fontSize: 12.5 }}>{c.label}</b>
                      <span className="faint" style={{ fontSize: 11.5 }}>{grouped[c.key].length}</span>
                    </div>
                    <div className="stack" style={{ gap: 6 }}>
                      {grouped[c.key].map((p) => <ContactRow key={p.id} c={p} active={selected === p.id} onClick={() => setSelected(p.id)} />)}
                    </div>
                  </div>
                ))
              ) : (
                <div className="stack" style={{ gap: 6 }}>
                  {contacts.map((p) => <ContactRow key={p.id} c={p} active={selected === p.id} onClick={() => setSelected(p.id)} />)}
                </div>
              )}
          </div>
          {selected && (
            <ContactDetail id={selected} relationships={ov?.prefs.relationships || []}
                           labels={ov?.prefs.labels || []}
                           onClose={() => setSelected(null)} onChanged={() => { loadList(); loadOverview(); }}
                           onExpand={() => setFullId(selected)} />
          )}
        </div>
      )}

      {tab === "circles" && <CirclesMap onSelect={(id) => { setTab("people"); setSelected(id); }} />}
      {tab === "suggestions" && <Suggestions onChanged={() => { loadOverview(); loadList(); }} />}
    </div>
  );
}

function MiniStat({ label, value, tint, onClick }: { label: string; value: number; tint?: string; onClick?: () => void }) {
  return (
    <Card onClick={onClick} style={{ cursor: onClick ? "pointer" : undefined, padding: "12px 14px", marginTop: 0 }}>
      <div style={{ fontSize: 22, fontWeight: 700, color: tint }}>{value.toLocaleString()}</div>
      <div className="faint" style={{ fontSize: 11.5 }}>{label}</div>
    </Card>
  );
}

function ContactRow({ c, active, onClick }: { c: Contact; active: boolean; onClick: () => void }) {
  return (
    <div onClick={onClick} className="card" style={{
      display: "flex", gap: 10, alignItems: "center", padding: "8px 11px", cursor: "pointer", marginTop: 0,
      background: active ? "var(--hover)" : undefined, borderColor: active ? "var(--brand)" : undefined,
    }}>
      <Avatar name={c.display_name} starred={c.starred} />
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="row" style={{ gap: 6, alignItems: "center", minWidth: 0 }}>
          <span style={{ fontWeight: 600, fontSize: 13, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.display_name}</span>
          {c.relationship && <Pill tone="info">{c.relationship}</Pill>}
        </div>
        <div className="faint" style={{ fontSize: 11.5, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
          {c.interaction_count.toLocaleString()} interactions
          {c.last_interaction_at ? ` · ${timeAgo(c.last_interaction_at)}` : ""}
        </div>
      </div>
      <div className="row" style={{ gap: 3 }}>
        {(c.source_types || []).slice(0, 4).map((s) => <SourceIcon key={s} type={s} size={15} />)}
      </div>
    </div>
  );
}

// ---- Detail ----------------------------------------------------------------
function ContactDetail({ id, relationships, labels, onClose, onChanged, onExpand }:
  { id: string; relationships: string[]; labels: string[]; onClose: () => void; onChanged: () => void; onExpand?: () => void }) {
  const [c, setC] = useState<Contact | null>(null);
  const [exchanges, setExchanges] = useState<Exchange[] | null>(null);
  const [exTotal, setExTotal] = useState(0);
  const [editDetails, setEditDetails] = useState(false);
  const [merging, setMerging] = useState(false);

  async function load() {
    try {
      const d = await api.get<Contact>(`/contacts/${id}`);
      setC(d);
    } catch { setC(null); }
  }
  async function loadExchanges() {
    try {
      const r = await api.get<{ total: number; items: Exchange[] }>(`/contacts/${id}/exchanges?limit=40`);
      setExchanges(r.items); setExTotal(r.total);
    } catch { setExchanges([]); }
  }
  useEffect(() => { setC(null); setExchanges(null); void load(); void loadExchanges(); }, [id]);

  async function patch(body: Partial<Contact>) {
    try {
      const d = await api.put<Contact>(`/contacts/${id}`, body);
      setC((cur) => cur ? { ...cur, ...d } : d);
      onChanged();
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Couldn't save the change.", tone: "danger" });
    }
  }

  if (!c) return <Card><Loading label="Loading contact…" card={false} /></Card>;

  const dir = c.stats?.by_direction || {};
  const bySource = Object.entries(c.stats?.by_source || {}).sort((a, b) => b[1] - a[1]);
  const months = c.stats?.by_month || [];
  const maxMonth = Math.max(1, ...months.map((m) => m.count));

  return (
    <Card style={{ position: "sticky", top: 12, maxHeight: "calc(100vh - 24px)", overflowY: "auto" }}>
      <div className="spread" style={{ alignItems: "flex-start", marginBottom: 12 }}>
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <Avatar name={c.display_name} size={52} />
          <div>
            <div className="row" style={{ gap: 8, alignItems: "center" }}>
              <h2 style={{ margin: 0, fontSize: 20 }}>{c.display_name}</h2>
              <button className="btn ghost sm" title="Star"
                      onClick={() => patch({ starred: !c.starred })}
                      style={{ color: c.starred ? "var(--warn)" : "var(--text-faint)" }}>★</button>
            </div>
            <div className="faint" style={{ fontSize: 12 }}>
              <span style={{ color: CIRCLE_META[c.circle]?.color }}>● </span>
              {CIRCLE_META[c.circle]?.label}
              {c.pinned_circle ? " (pinned)" : ""}
              {" · "}{c.interaction_count.toLocaleString()} interactions
            </div>
          </div>
        </div>
        <div className="row" style={{ gap: 6 }}>
          {onExpand && <button className="btn ghost sm" title="Open full profile" onClick={onExpand}>Full profile</button>}
          <button className="btn ghost sm" onClick={onClose}><Icon name="x" size={14} /></button>
        </div>
      </div>

      {/* Relationship + circle + labels */}
      <div className="row" style={{ gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <select className="input sm" value={c.relationship || ""} onChange={(e) => patch({ relationship: e.target.value })}>
          <option value="">— relationship —</option>
          {relationships.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
        <select className="input sm" value={c.pinned_circle || ""} onChange={(e) => patch({ pinned_circle: e.target.value })}
                title="Pin to a circle (overrides the computed tier)">
          <option value="">Auto circle</option>
          {CIRCLES.map((x) => <option key={x.key} value={x.key}>Pin: {x.label}</option>)}
        </select>
        <button className="btn ghost sm" onClick={() => patch({ hidden: !c.hidden })}>
          {c.hidden ? "Unhide" : "Hide"}
        </button>
        <button className="btn ghost sm" title="Merge / link into another contact" onClick={() => setMerging((v) => !v)}>
          <Icon name="repeat" size={12} /> Merge
        </button>
      </div>
      {merging && <MergeInto currentId={id} currentName={c.display_name}
                             onMerged={() => { onChanged(); onClose(); }}
                             onCancel={() => setMerging(false)} />}
      <LabelEditor current={c.labels || []} palette={labels} onChange={(l) => patch({ labels: l })} />

      {/* Stats */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 8, margin: "14px 0" }}>
        <StatBox label="Sent" value={dir.out || 0} />
        <StatBox label="Received" value={dir.in || 0} />
        <StatBox label="Identifiers" value={c.stats?.identity_count || (c.identities?.length || 0)} />
      </div>

      {/* Timeline */}
      {months.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="faint" style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 6 }}>Interaction timeline</div>
          <div className="row" style={{ gap: 2, alignItems: "flex-end", height: 48 }}>
            {months.slice(-24).map((m) => (
              <div key={m.m} title={`${m.m}: ${m.count}`} style={{
                flex: 1, height: `${Math.max(4, (m.count / maxMonth) * 100)}%`,
                background: "var(--brand)", borderRadius: 2, minWidth: 3,
              }} />
            ))}
          </div>
        </div>
      )}

      {/* Methods / sources */}
      {bySource.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="faint" style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 6 }}>How you connect</div>
          <div className="stack" style={{ gap: 5 }}>
            {bySource.map(([s, n]) => (
              <div key={s} className="row" style={{ gap: 8, alignItems: "center" }}>
                <SourceIcon type={s} size={15} />
                <span style={{ fontSize: 12.5, flex: 1 }}>{s}</span>
                <span className="faint" style={{ fontSize: 12 }}>{n.toLocaleString()}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Identities */}
      <Identities contactId={id} identities={c.identities || []}
                  onChanged={(updated) => { if (updated) { setC(updated); onChanged(); } else void load(); }} />

      {/* Rich details */}
      <div className="spread" style={{ margin: "16px 0 6px", alignItems: "center" }}>
        <div className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>Details</div>
        <button className="btn ghost sm" onClick={() => setEditDetails((v) => !v)}>
          <Icon name="edit" size={12} /> {editDetails ? "Done" : "Edit"}
        </button>
      </div>
      <DetailsSections details={c.details || {}} editing={editDetails}
                       onChange={(d) => patch({ details: d })} />

      {/* Notes */}
      <div className="faint" style={{ fontSize: 11.5, fontWeight: 600, margin: "14px 0 5px" }}>Notes</div>
      <textarea className="input" rows={2} defaultValue={c.notes || ""} placeholder="Private notes about this person…"
                onBlur={(e) => { if (e.target.value !== (c.notes || "")) patch({ notes: e.target.value }); }} />

      {/* Exchanges → unified search */}
      <div className="spread" style={{ margin: "16px 0 6px", alignItems: "center" }}>
        <div className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>
          Exchanges {exTotal ? `(${exTotal})` : ""}
        </div>
        <a className="btn ghost sm" href={`/search?q=${encodeURIComponent(c.primary_email || c.display_name)}`}>
          <Icon name="search" size={12} /> Open in Search
        </a>
      </div>
      {exchanges === null ? <Loading label="Loading exchanges…" card={false} />
        : exchanges.length === 0 ? <div className="muted" style={{ fontSize: 12 }}>No exchanges indexed yet.</div>
          : (
            <div className="stack" style={{ gap: 5 }}>
              {exchanges.map((x, i) => (
                <div key={i} className="card" style={{ padding: "7px 10px", marginTop: 0 }}>
                  <div className="row" style={{ gap: 7, alignItems: "center" }}>
                    <SourceIcon type={x.source_type} size={14} />
                    <span style={{ fontSize: 10, color: x.direction === "out" ? "var(--brand)" : "var(--ok)" }}>
                      {x.direction === "out" ? "↑" : x.direction === "in" ? "↓" : "·"}
                    </span>
                    <span style={{ fontSize: 12.5, flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {x.title || "(untitled)"}
                    </span>
                    <span className="faint" style={{ fontSize: 11 }}>{x.modified_at ? timeAgo(x.modified_at) : ""}</span>
                  </div>
                  {x.preview && <div className="faint" style={{ fontSize: 11.5, marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{x.preview}</div>}
                </div>
              ))}
            </div>
          )}
    </Card>
  );
}

// ---- Full-page contact profile ---------------------------------------------
function ContactFull({ id, relationships, labels, onBack, onChanged }:
  { id: string; relationships: string[]; labels: string[]; onBack: () => void; onChanged: () => void }) {
  const [c, setC] = useState<Contact | null>(null);
  const [exchanges, setExchanges] = useState<Exchange[] | null>(null);
  const [exTotal, setExTotal] = useState(0);
  const [exLimit, setExLimit] = useState(60);
  const [editDetails, setEditDetails] = useState(false);
  const [merging, setMerging] = useState(false);

  async function load() {
    try { setC(await api.get<Contact>(`/contacts/${id}`)); } catch { setC(null); }
  }
  async function loadExchanges() {
    try {
      const r = await api.get<{ total: number; items: Exchange[] }>(`/contacts/${id}/exchanges?limit=${exLimit}`);
      setExchanges(r.items); setExTotal(r.total);
    } catch { setExchanges([]); }
  }
  useEffect(() => { setC(null); void load(); }, [id]);
  useEffect(() => { void loadExchanges(); }, [id, exLimit]);

  async function patch(body: Partial<Contact>) {
    try {
      const d = await api.put<Contact>(`/contacts/${id}`, body);
      setC((cur) => cur ? { ...cur, ...d } : d);
      onChanged();
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Couldn't save the change.", tone: "danger" });
    }
  }

  if (!c) return (
    <div>
      <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 12 }}>← Back to My Circles</button>
      <Card><Loading label="Loading profile…" card={false} /></Card>
    </div>
  );

  const dir = c.stats?.by_direction || {};
  const bySource = Object.entries(c.stats?.by_source || {}).sort((a, b) => b[1] - a[1]);
  const months = c.stats?.by_month || [];
  const maxMonth = Math.max(1, ...months.map((m) => m.count));

  return (
    <div>
      <button className="btn ghost sm" onClick={onBack} style={{ marginBottom: 12 }}>← Back to My Circles</button>

      {/* Header */}
      <Card style={{ marginBottom: 14 }}>
        <div className="spread" style={{ alignItems: "flex-start", flexWrap: "wrap", gap: 12 }}>
          <div className="row" style={{ gap: 16, alignItems: "center" }}>
            <Avatar name={c.display_name} size={72} starred={c.starred} />
            <div>
              <div className="row" style={{ gap: 10, alignItems: "center" }}>
                <h1 style={{ margin: 0, fontSize: 26 }}>{c.display_name}</h1>
                <button className="btn ghost sm" title="Star" onClick={() => patch({ starred: !c.starred })}
                        style={{ color: c.starred ? "var(--warn)" : "var(--text-faint)", fontSize: 18 }}>★</button>
              </div>
              {c.nickname && <div className="faint" style={{ fontSize: 13 }}>“{c.nickname}”</div>}
              <div className="faint" style={{ fontSize: 13, marginTop: 2 }}>
                <span style={{ color: CIRCLE_META[c.circle]?.color }}>● </span>
                {CIRCLE_META[c.circle]?.label}{c.pinned_circle ? " (pinned)" : ""}
                {c.relationship ? ` · ${c.relationship}` : ""}
                {c.primary_email ? ` · ${c.primary_email}` : ""}
                {c.primary_phone ? ` · ${c.primary_phone}` : ""}
              </div>
            </div>
          </div>
          <a className="btn ghost sm" href={`/search?q=${encodeURIComponent(c.primary_email || c.display_name)}`}>
            <Icon name="search" size={13} /> Open in Search
          </a>
        </div>

        {/* Controls */}
        <div className="row" style={{ gap: 8, flexWrap: "wrap", marginTop: 14 }}>
          <select className="input sm" value={c.relationship || ""} onChange={(e) => patch({ relationship: e.target.value })}>
            <option value="">— relationship —</option>
            {relationships.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <select className="input sm" value={c.pinned_circle || ""} onChange={(e) => patch({ pinned_circle: e.target.value })}
                  title="Pin to a circle (overrides the computed tier)">
            <option value="">Auto circle</option>
            {CIRCLES.map((x) => <option key={x.key} value={x.key}>Pin: {x.label}</option>)}
          </select>
          <button className="btn ghost sm" onClick={() => patch({ hidden: !c.hidden })}>{c.hidden ? "Unhide" : "Hide"}</button>
          <button className="btn ghost sm" onClick={() => setMerging((v) => !v)}><Icon name="repeat" size={12} /> Merge</button>
        </div>
        {merging && <div style={{ marginTop: 10 }}>
          <MergeInto currentId={id} currentName={c.display_name}
                     onMerged={() => { onChanged(); onBack(); }} onCancel={() => setMerging(false)} />
        </div>}
        <div style={{ marginTop: 10 }}>
          <LabelEditor current={c.labels || []} palette={labels} onChange={(l) => patch({ labels: l })} />
        </div>
      </Card>

      {/* Stat row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px,1fr))", gap: 10, marginBottom: 14 }}>
        <StatBox label="Interactions" value={c.interaction_count} />
        <StatBox label="Sent" value={dir.out || 0} />
        <StatBox label="Received" value={dir.in || 0} />
        <StatBox label="Identifiers" value={c.stats?.identity_count || (c.identities?.length || 0)} />
        <StatBox label="Sources" value={bySource.length} />
      </div>
      <div className="faint" style={{ fontSize: 12, marginBottom: 14 }}>
        {c.first_interaction_at ? `First seen ${timeAgo(c.first_interaction_at)}` : ""}
        {c.first_interaction_at && c.last_interaction_at ? " · " : ""}
        {c.last_interaction_at ? `last ${timeAgo(c.last_interaction_at)}` : ""}
      </div>

      {/* Two-column body */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(300px, 1fr) minmax(320px, 1.2fr)", gap: 14, alignItems: "start" }}>
        <div className="stack" style={{ gap: 14 }}>
          {months.length > 0 && (
            <Card>
              <div className="faint" style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 8 }}>Interaction timeline</div>
              <div className="row" style={{ gap: 2, alignItems: "flex-end", height: 70 }}>
                {months.slice(-36).map((m) => (
                  <div key={m.m} title={`${m.m}: ${m.count}`} style={{
                    flex: 1, height: `${Math.max(4, (m.count / maxMonth) * 100)}%`,
                    background: "var(--brand)", borderRadius: 2, minWidth: 3,
                  }} />
                ))}
              </div>
            </Card>
          )}
          {bySource.length > 0 && (
            <Card>
              <div className="faint" style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 8 }}>How you connect</div>
              <div className="stack" style={{ gap: 6 }}>
                {bySource.map(([s, n]) => (
                  <div key={s} className="row" style={{ gap: 8, alignItems: "center" }}>
                    <SourceIcon type={s} size={16} />
                    <span style={{ fontSize: 13, flex: 1 }}>{s}</span>
                    <span className="faint" style={{ fontSize: 12.5 }}>{n.toLocaleString()}</span>
                  </div>
                ))}
              </div>
            </Card>
          )}
          <Card>
            <Identities contactId={id} identities={c.identities || []}
                        onChanged={(updated) => { if (updated) { setC(updated); onChanged(); } else void load(); }} />
          </Card>
          <Card>
            <div className="spread" style={{ marginBottom: 6, alignItems: "center" }}>
              <div className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>Details</div>
              <button className="btn ghost sm" onClick={() => setEditDetails((v) => !v)}>
                <Icon name="edit" size={12} /> {editDetails ? "Done" : "Edit"}
              </button>
            </div>
            <DetailsSections details={c.details || {}} editing={editDetails} onChange={(d) => patch({ details: d })} />
            <div className="faint" style={{ fontSize: 11.5, fontWeight: 600, margin: "14px 0 5px" }}>Notes</div>
            <textarea className="input" rows={3} defaultValue={c.notes || ""} placeholder="Private notes about this person…"
                      onBlur={(e) => { if (e.target.value !== (c.notes || "")) patch({ notes: e.target.value }); }} />
          </Card>
        </div>

        <Card>
          <div className="spread" style={{ marginBottom: 8, alignItems: "center" }}>
            <div className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>Exchanges {exTotal ? `(${exTotal})` : ""}</div>
          </div>
          {exchanges === null ? <Loading label="Loading exchanges…" card={false} />
            : exchanges.length === 0 ? <div className="muted" style={{ fontSize: 12.5 }}>No exchanges indexed yet.</div>
              : (
                <div className="stack" style={{ gap: 5 }}>
                  {exchanges.map((x, i) => (
                    <div key={i} className="card" style={{ padding: "8px 11px", marginTop: 0 }}>
                      <div className="row" style={{ gap: 7, alignItems: "center" }}>
                        <SourceIcon type={x.source_type} size={14} />
                        <span style={{ fontSize: 10.5, color: x.direction === "out" ? "var(--brand)" : x.direction === "in" ? "var(--ok)" : "var(--text-faint)" }}>
                          {x.direction === "out" ? "↑ sent" : x.direction === "in" ? "↓ received" : "·"}
                        </span>
                        <span style={{ fontSize: 13, flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                          {x.title || "(untitled)"}
                        </span>
                        <span className="faint" style={{ fontSize: 11 }}>{x.modified_at ? timeAgo(x.modified_at) : ""}</span>
                      </div>
                      {x.preview && <div className="faint" style={{ fontSize: 11.5, marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{x.preview}</div>}
                    </div>
                  ))}
                  {exchanges.length < exTotal && (
                    <button className="btn ghost sm" style={{ marginTop: 6 }} onClick={() => setExLimit((n) => n + 60)}>
                      Load more ({exTotal - exchanges.length} more)
                    </button>
                  )}
                </div>
              )}
        </Card>
      </div>
    </div>
  );
}

// ---- Merge / manual link ---------------------------------------------------
function MergeInto({ currentId, currentName, onMerged, onCancel }:
  { currentId: string; currentName: string; onMerged: () => void; onCancel: () => void }) {
  const [q, setQ] = useState("");
  const [results, setResults] = useState<Contact[]>([]);
  useEffect(() => {
    const t = setTimeout(() => {
      const p = new URLSearchParams({ sort: "frequency", limit: "20" });
      if (q) p.set("q", q);
      api.get<{ contacts: Contact[] }>(`/contacts?${p.toString()}`)
        .then((r) => setResults(r.contacts.filter((x) => x.id !== currentId)))
        .catch(() => setResults([]));
    }, 220);
    return () => clearTimeout(t);
  }, [q, currentId]);
  async function doMerge(target: Contact) {
    const ok = await confirmDialog({
      title: "Merge contacts?",
      message: `Combine “${currentName}” into “${target.display_name}”. All identifiers (emails, phone numbers, handles) and history move under ${target.display_name}. This can't be undone.`,
      confirmLabel: "Merge", tone: "warn",
    });
    if (!ok) return;
    try {
      await api.post("/contacts/merge", { primary_id: target.id, other_id: currentId });
      notify({ title: "Contacts merged", message: `Linked into ${target.display_name}.`, tone: "ok" });
      onMerged();
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Couldn't merge the contacts.", tone: "danger" });
    }
  }
  return (
    <div className="card" style={{ padding: "10px 12px", marginTop: 0, marginBottom: 12, background: "var(--inset)" }}>
      <div className="spread" style={{ marginBottom: 6 }}>
        <span className="faint" style={{ fontSize: 11.5, fontWeight: 600 }}>Link this person into another contact</span>
        <button className="btn ghost sm" onClick={onCancel}><Icon name="x" size={12} /></button>
      </div>
      <input className="input sm" autoFocus placeholder="Search people by name, email or phone…"
             value={q} onChange={(e) => setQ(e.target.value)} style={{ width: "100%" }} />
      <div className="stack" style={{ gap: 3, marginTop: 7, maxHeight: 230, overflow: "auto" }}>
        {results.map((r) => (
          <div key={r.id} onClick={() => doMerge(r)} className="row"
               style={{ gap: 8, alignItems: "center", padding: "5px 7px", borderRadius: 6, cursor: "pointer" }}
               onMouseEnter={(e) => (e.currentTarget.style.background = "var(--hover)")}
               onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
            <Avatar name={r.display_name} size={26} />
            <span style={{ fontSize: 12.5, flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{r.display_name}</span>
            <span className="faint" style={{ fontSize: 11 }}>{r.interaction_count.toLocaleString()}</span>
          </div>
        ))}
        {results.length === 0 && <div className="muted" style={{ fontSize: 12, padding: "4px 2px" }}>No matches.</div>}
      </div>
    </div>
  );
}

function StatBox({ label, value }: { label: string; value: number }) {
  return (
    <div className="card" style={{ padding: "9px 11px", marginTop: 0, textAlign: "center" }}>
      <div style={{ fontSize: 18, fontWeight: 700 }}>{value.toLocaleString()}</div>
      <div className="faint" style={{ fontSize: 10.5 }}>{label}</div>
    </div>
  );
}

function LabelEditor({ current, palette, onChange }: { current: string[]; palette: string[]; onChange: (l: string[]) => void }) {
  const [adding, setAdding] = useState("");
  const toggle = (l: string) => onChange(current.includes(l) ? current.filter((x) => x !== l) : [...current, l]);
  return (
    <div className="row" style={{ gap: 5, flexWrap: "wrap", alignItems: "center" }}>
      {current.map((l) => (
        <span key={l} onClick={() => toggle(l)} title="Remove"
              style={{ cursor: "pointer", fontSize: 11, padding: "2px 8px", borderRadius: 10, background: "var(--brand)", color: "#fff" }}>{l} ✕</span>
      ))}
      {palette.filter((l) => !current.includes(l)).map((l) => (
        <span key={l} onClick={() => toggle(l)}
              style={{ cursor: "pointer", fontSize: 11, padding: "2px 8px", borderRadius: 10, background: "var(--inset)", color: "var(--text-dim)" }}>+ {l}</span>
      ))}
      <input className="input sm" placeholder="+ label" value={adding} style={{ width: 80 }}
             onChange={(e) => setAdding(e.target.value)}
             onKeyDown={(e) => { if (e.key === "Enter" && adding.trim()) { onChange([...current, adding.trim()]); setAdding(""); } }} />
    </div>
  );
}

function DetailsSections({ details, editing, onChange }:
  { details: Record<string, Record<string, string>>; editing: boolean; onChange: (d: Record<string, Record<string, string>>) => void }) {
  const SECTIONS = ["personal", "business", "intimate", "custom"];
  const setField = (sec: string, k: string, v: string) => {
    const d = { ...details, [sec]: { ...(details[sec] || {}) } };
    if (v) d[sec][k] = v; else delete d[sec][k];
    onChange(d);
  };
  const [newKey, setNewKey] = useState<Record<string, string>>({});
  return (
    <div className="stack" style={{ gap: 10 }}>
      {SECTIONS.map((sec) => {
        const fields = Object.entries(details[sec] || {});
        if (!editing && fields.length === 0) return null;
        return (
          <div key={sec} className="card" style={{ padding: "9px 11px", marginTop: 0 }}>
            <div className="faint" style={{ fontSize: 11, fontWeight: 600, textTransform: "capitalize", marginBottom: 5 }}>{sec}</div>
            <div className="stack" style={{ gap: 4 }}>
              {fields.map(([k, v]) => (
                <div key={k} className="row" style={{ gap: 7, alignItems: "center" }}>
                  <span className="faint" style={{ fontSize: 11.5, width: 90, textTransform: "capitalize" }}>{k}</span>
                  {editing ? (
                    <input className="input sm" defaultValue={v} style={{ flex: 1 }}
                           onBlur={(e) => setField(sec, k, e.target.value)} />
                  ) : <span style={{ fontSize: 12.5 }}>{v}</span>}
                </div>
              ))}
              {editing && (
                <div className="row" style={{ gap: 6, marginTop: 2 }}>
                  <input className="input sm" placeholder="field" style={{ width: 90 }}
                         value={newKey[sec] || ""} onChange={(e) => setNewKey({ ...newKey, [sec]: e.target.value })} />
                  <input className="input sm" placeholder="value" style={{ flex: 1 }}
                         onKeyDown={(e) => {
                           const key = (newKey[sec] || "").trim();
                           if (e.key === "Enter" && key) {
                             setField(sec, key, (e.target as HTMLInputElement).value);
                             (e.target as HTMLInputElement).value = "";
                             setNewKey({ ...newKey, [sec]: "" });
                           }
                         }} />
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Identities({ contactId, identities, onChanged }:
  { contactId: string; identities: Identity[]; onChanged: (updated?: Contact) => void }) {
  const [kind, setKind] = useState("email");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  async function add() {
    if (!value.trim() || busy) return;
    setBusy(true);
    try {
      const updated = await api.post<Contact>(`/contacts/${contactId}/identities`, { kind, value: value.trim() });
      setValue(""); onChanged(updated);
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Couldn't add that identifier — check the format.", tone: "danger" });
    } finally { setBusy(false); }
  }
  async function remove(iid: string) {
    try {
      const updated = await api.del<Contact>(`/contacts/${contactId}/identities/${iid}`);
      onChanged(updated);
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Couldn't unlink that identifier.", tone: "danger" });
    }
  }
  return (
    <div>
      <div className="faint" style={{ fontSize: 11.5, fontWeight: 600, margin: "4px 0 6px" }}>Linked identifiers</div>
      <div className="stack" style={{ gap: 4 }}>
        {identities.map((i) => (
          <div key={i.id} className="row" style={{ gap: 7, alignItems: "center" }}>
            <Pill tone={i.link_method === "manual" ? "ok" : i.link_method === "suggested" ? "warn" : "info"}>{i.kind}</Pill>
            <span style={{ fontSize: 12.5, flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
              {i.raw_value || i.value}
            </span>
            {i.source_type && <SourceIcon type={i.source_type} size={14} />}
            <span className="faint" style={{ fontSize: 10 }}>{i.link_method}</span>
            <button className="btn ghost sm" onClick={() => remove(i.id)} title="Unlink"><Icon name="trash" size={11} /></button>
          </div>
        ))}
        {identities.length === 0 && <div className="muted" style={{ fontSize: 12 }}>No identifiers linked yet.</div>}
      </div>
      <div className="row" style={{ gap: 6, marginTop: 7 }}>
        <select className="input sm" value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: 90 }}>
          <option value="email">email</option>
          <option value="phone">phone</option>
          <option value="handle">handle</option>
        </select>
        <input className="input sm" placeholder="Link an identifier…" value={value} style={{ flex: 1 }}
               onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") add(); }} />
        <button className="btn sm" onClick={add} disabled={busy}><Icon name="plus" size={12} /></button>
      </div>
    </div>
  );
}

// ---- Circles map -----------------------------------------------------------
function CirclesMap({ onSelect }: { onSelect: (id: string) => void }) {
  const [g, setG] = useState<GraphData | null>(null);
  const [circle, setCircle] = useState("");
  const [within, setWithin] = useState(0);   // days; 0 = all time
  const [limit, setLimit] = useState(80);
  const [zoom, setZoom] = useState(1);
  useEffect(() => {
    const p = new URLSearchParams({ limit: String(limit) });
    if (circle) p.set("circle", circle);
    if (within) p.set("within_days", String(within));
    setG(null);
    api.get<GraphData>(`/contacts/graph?${p.toString()}`).then(setG).catch(() => setG({ nodes: [], edges: [] }));
  }, [circle, within, limit]);
  const controls = (
    <div className="filter-bar" style={{ marginBottom: 12 }}>
      <label className="filter-select">
        <span>Circle</span>
        <select value={circle} onChange={(e) => setCircle(e.target.value)}>
          <option value="">All circles</option>
          {CIRCLES.map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
        </select>
      </label>
      <label className="filter-select">
        <span>Active within</span>
        <select value={within} onChange={(e) => setWithin(Number(e.target.value))}>
          <option value={0}>Any time</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
          <option value={365}>Last year</option>
          <option value={1095}>Last 3 years</option>
        </select>
      </label>
      <label className="filter-select">
        <span>Show up to</span>
        <select value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
          <option value={40}>40 people</option>
          <option value={80}>80 people</option>
          <option value={150}>150 people</option>
          <option value={300}>300 people</option>
        </select>
      </label>
    </div>
  );
  if (!g) return <Card>{controls}<Loading label="Drawing your circles…" card={false} /></Card>;
  const peopleCount = g.nodes.filter((n) => !n.me).length;
  // Base ring radii; the zoom slider scales the whole canvas so a crowded ring
  // can be spread out (the container scrolls when zoomed past the viewport).
  const BR = [70, 140, 210, 280, 300].map((r) => r * zoom);
  const W = 760 * zoom, H = 620 * zoom, cx = W / 2, cy = H / 2;
  const ringFor: Record<string, number> = { inner: BR[0], close: BR[1], active: BR[2], acquaintance: BR[3], dormant: BR[4] };
  const nodeScale = Math.min(1.5, Math.max(0.85, zoom));
  // When filtered to a single circle, spread everyone across the ring evenly on one band.
  const byCircle: Record<string, GraphNode[]> = {};
  for (const n of g.nodes) if (!n.me) (byCircle[n.circle] ||= []).push(n);
  const pos: Record<string, { x: number; y: number }> = { me: { x: cx, y: cy } };
  for (const [circ, list] of Object.entries(byCircle)) {
    const r = ringFor[circ] ?? 300;
    list.forEach((n, i) => {
      const a = (i / list.length) * Math.PI * 2 - Math.PI / 2;
      pos[n.id] = { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
    });
  }
  return (
    <Card>
      {controls}
      {peopleCount === 0 ? (
        <div className="muted" style={{ fontSize: 13, padding: "24px 0", textAlign: "center" }}>
          No people match these filters. Widen the time window or pick another circle.
        </div>
      ) : (
      <>
      <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 8, justifyContent: "flex-end" }}>
        <span className="faint" style={{ fontSize: 11.5 }}>Zoom</span>
        <button className="btn ghost sm" title="Zoom out" onClick={() => setZoom((z) => Math.max(0.6, +(z - 0.2).toFixed(2)))}>–</button>
        <input type="range" min={0.6} max={2.4} step={0.1} value={zoom}
               onChange={(e) => setZoom(Number(e.target.value))} style={{ width: 160 }} />
        <button className="btn ghost sm" title="Zoom in" onClick={() => setZoom((z) => Math.min(2.4, +(z + 0.2).toFixed(2)))}>+</button>
        <button className="btn ghost sm" onClick={() => setZoom(1)}>Reset</button>
      </div>
      <div style={{ overflow: "auto", maxHeight: "72vh" }}>
        <svg width={W} height={H} style={{ maxWidth: "none", display: "block" }}>
          {CIRCLES.slice(0, 5).map((c, i) => (
            <circle key={c.key} cx={cx} cy={cy} r={BR[i]}
                    fill="none" stroke="var(--border)" strokeWidth={1} strokeDasharray="3 4" />
          ))}
          {g.edges.map((e, i) => {
            const a = pos[e.source], b = pos[e.target];
            if (!a || !b) return null;
            return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                         stroke={CIRCLE_META[e.circle]?.color || "var(--border)"}
                         strokeOpacity={0.15 + e.weight * 0.5} strokeWidth={0.5 + e.weight * 2.5} />;
          })}
          {g.nodes.map((n) => {
            const p = pos[n.id]; if (!p) return null;
            if (n.me) return (
              <g key="me">
                <circle cx={p.x} cy={p.y} r={26 * nodeScale} fill="var(--brand)" />
                <text x={p.x} y={p.y + 4} textAnchor="middle" fill="#fff" fontSize={12 * nodeScale} fontWeight={700}>You</text>
              </g>
            );
            const r = (9 + (n.weight || 0) * 12) * nodeScale;
            return (
              <g key={n.id} style={{ cursor: "pointer" }} onClick={() => onSelect(n.id)}>
                <circle cx={p.x} cy={p.y} r={r} fill={avatarColor(n.name)} stroke={n.starred ? "var(--warn)" : "transparent"} strokeWidth={2} />
                <text x={p.x} y={p.y + r + 11} textAnchor="middle" fill="var(--text-dim)" fontSize={10 * nodeScale}>
                  {n.name.length > 16 ? n.name.slice(0, 15) + "…" : n.name}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      </>
      )}
      <div className="spread" style={{ marginTop: 8, flexWrap: "wrap", gap: 10 }}>
        <div className="row" style={{ gap: 14, flexWrap: "wrap" }}>
          {CIRCLES.map((c) => (
            <span key={c.key} className="row" style={{ gap: 5, alignItems: "center", fontSize: 11.5 }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: c.color }} /> {c.label}
            </span>
          ))}
        </div>
        <span className="faint" style={{ fontSize: 11.5 }}>{peopleCount} shown</span>
      </div>
    </Card>
  );
}

// ---- Suggestions -----------------------------------------------------------
function Suggestions({ onChanged }: { onChanged: () => void }) {
  const [list, setList] = useState<Suggestion[] | null>(null);
  async function load() { try { setList((await api.get<{ suggestions: Suggestion[] }>("/contacts/suggestions")).suggestions); } catch { setList([]); } }
  useEffect(() => { void load(); }, []);
  async function act(id: string, action: "accept" | "dismiss") {
    try {
      await api.post(`/contacts/suggestions/${id}/${action}`); await load(); onChanged();
    } catch (e) {
      notify({ message: (e as { message?: string }).message || "Couldn't update the suggestion.", tone: "danger" });
    }
  }
  if (list === null) return <Loading label="Loading suggestions…" />;
  if (list.length === 0) return <Card><div className="muted" style={{ padding: "16px 4px" }}>No suggestions right now — your contacts look well-linked.</div></Card>;
  return (
    <div className="stack" style={{ gap: 8 }}>
      {list.map((s) => (
        <Card key={s.id}>
          <div className="spread" style={{ alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <div className="row" style={{ gap: 10, alignItems: "center", minWidth: 0 }}>
              <Pill tone="warn">{s.kind === "merge" ? "Merge" : "Link"}</Pill>
              {s.contact && <Avatar name={s.contact.display_name} size={30} />}
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 600 }}>
                  {s.kind === "merge"
                    ? <>Merge <b>{s.merge_contact?.display_name}</b> into <b>{s.contact?.display_name}</b></>
                    : <>Link {s.identity?.raw} to <b>{s.contact?.display_name}</b></>}
                </div>
                <div className="faint" style={{ fontSize: 11.5 }}>{s.reason}</div>
              </div>
            </div>
            <div className="row" style={{ gap: 6 }}>
              <button className="btn primary sm" onClick={() => act(s.id, "accept")}><Icon name="check" size={12} /> Accept</button>
              <button className="btn ghost sm" onClick={() => act(s.id, "dismiss")}>Dismiss</button>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}

// ---- Settings / customization ----------------------------------------------
function ContactsSettings({ onClose }: { onClose: () => void }) {
  const [relationships, setRelationships] = useState<string[]>([]);
  const [labels, setLabels] = useState<string[]>([]);
  const [autoLink, setAutoLink] = useState(true);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    api.get<{ prefs: { relationships: string[]; labels: string[]; auto_link: boolean } }>("/contacts/settings")
      .then((r) => { setRelationships(r.prefs.relationships); setLabels(r.prefs.labels); setAutoLink(r.prefs.auto_link); setLoaded(true); })
      .catch(() => setLoaded(true));
  }, []);
  async function save() {
    await api.put("/contacts/settings", { relationships, labels, auto_link: autoLink });
    onClose();
  }
  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)", zIndex: 50, display: "flex", alignItems: "center", justifyContent: "center", padding: 20 }}
         onClick={onClose}>
      <Card style={{ maxWidth: 460, width: "100%" }}>
        <div onClick={(e) => e.stopPropagation()}>
          <div className="spread" style={{ marginBottom: 12, alignItems: "center" }}>
            <h2 style={{ margin: 0, fontSize: 18 }}>Customize contacts</h2>
            <button className="btn ghost sm" onClick={onClose}><Icon name="x" size={14} /></button>
          </div>
          {!loaded ? <Loading label="Loading…" card={false} /> : (
            <div className="stack" style={{ gap: 14 }}>
              <TagList title="Relationship types" value={relationships} onChange={setRelationships} />
              <TagList title="Label palette" value={labels} onChange={setLabels} />
              <label className="row" style={{ gap: 8, fontSize: 13, alignItems: "center" }}>
                <input type="checkbox" checked={autoLink} onChange={(e) => setAutoLink(e.target.checked)} />
                Auto-link identifiers that clearly belong to the same person
              </label>
              <div className="row" style={{ gap: 8 }}>
                <button className="btn primary sm" onClick={save}><Icon name="check" size={12} /> Save</button>
                <button className="btn ghost sm" onClick={onClose}>Cancel</button>
              </div>
            </div>
          )}
        </div>
      </Card>
    </div>
  );
}

function TagList({ title, value, onChange }: { title: string; value: string[]; onChange: (v: string[]) => void }) {
  const [adding, setAdding] = useState("");
  return (
    <div>
      <div className="faint" style={{ fontSize: 11.5, fontWeight: 600, marginBottom: 6 }}>{title}</div>
      <div className="row" style={{ gap: 5, flexWrap: "wrap", alignItems: "center" }}>
        {value.map((t) => (
          <span key={t} onClick={() => onChange(value.filter((x) => x !== t))} title="Remove"
                style={{ cursor: "pointer", fontSize: 11.5, padding: "3px 9px", borderRadius: 10, background: "var(--inset)" }}>{t} ✕</span>
        ))}
        <input className="input sm" placeholder="+ add" value={adding} style={{ width: 90 }}
               onChange={(e) => setAdding(e.target.value)}
               onKeyDown={(e) => { if (e.key === "Enter" && adding.trim() && !value.includes(adding.trim())) { onChange([...value, adding.trim()]); setAdding(""); } }} />
      </div>
    </div>
  );
}
