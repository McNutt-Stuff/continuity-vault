import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, Loading, serverDate } from "../components/ui";
import { Icon } from "../components/Icon";
import { SourceIcon } from "../components/SourceIcon";
import { confirmDialog, notify } from "../components/dialog";

type Condition = { field: string; op: string; value?: string };
type Action = { type: string; value?: string; min_plan?: string };
interface Rule {
  id: string; name: string; description: string; enabled: boolean; priority: number;
  match: "all" | "any"; conditions: Condition[]; actions: Action[];
  collection_ids: string[]; source_types: string[]; min_plan: string;
  hit_count?: number; last_match_at?: string | null;
  updated_at?: string | null;
}
interface Options {
  operators: { id: string; label: string }[];
  action_types: { id: string; label: string; needs_value: boolean; min_plan: string }[];
  field_suggestions: string[];
  plans: string[];
  plan: string;
  collections: { id: string; name: string; source_type: string; managed?: boolean; workload?: string; instance_id?: string }[];
}

const WORKLOAD_LABELS: Record<string, string> = {
  exchange: "Exchange Online", onedrive: "OneDrive", sharepoint: "SharePoint",
  teams: "Teams channels", teams_chat: "Teams chats",
};

type Coll = { id: string; name: string; source_type: string; managed?: boolean; workload?: string; instance_id?: string };

const SOURCE_TYPE_LABEL: Record<string, string> = {
  outlook: "Outlook / Exchange", onedrive: "OneDrive", sharepoint: "SharePoint",
  teams: "Teams", gmail: "Gmail", exchange: "Exchange Online",
};
function stLabel(st: string): string {
  return SOURCE_TYPE_LABEL[st] || st.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
// Icon type for a collection — managed sources brand by workload, others by source_type.
function collIcon(c: Coll): string {
  if (c.managed && c.workload) return c.workload === "teams_chat" ? "teams" : c.workload;
  return c.source_type;
}

// Rule scope selector: pick "every source", whole source TYPES (broad, future-proof),
// and/or specific sources via a searchable list — friendly even with many sources.
function ScopePicker({ collections, collectionIds, sourceTypes, onChange }: {
  collections: Coll[];
  collectionIds: string[];
  sourceTypes: string[];
  onChange: (field: "collection_ids" | "source_types", value: string[]) => void;
}) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);

  // Distinct source types present, with a friendly label + count.
  const types = useMemo(() => {
    const m = new Map<string, number>();
    for (const c of collections) m.set(c.source_type, (m.get(c.source_type) || 0) + 1);
    return [...m.entries()].map(([st, n]) => ({ st, n })).sort((a, b) => b.n - a.n);
  }, [collections]);

  const byId = useMemo(() => new Map(collections.map((c) => [c.id, c])), [collections]);
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q
      ? collections.filter((c) => c.name.toLowerCase().includes(q)
          || stLabel(c.source_type).toLowerCase().includes(q)
          || (c.workload && WORKLOAD_LABELS[c.workload]?.toLowerCase().includes(q)))
      : collections;
    return [...list].sort((a, b) => a.name.localeCompare(b.name));
  }, [collections, query]);

  const toggleType = (st: string) =>
    onChange("source_types", sourceTypes.includes(st)
      ? sourceTypes.filter((x) => x !== st) : [...sourceTypes, st]);
  const toggleColl = (id: string) =>
    onChange("collection_ids", collectionIds.includes(id)
      ? collectionIds.filter((x) => x !== id) : [...collectionIds, id]);

  const nothing = collectionIds.length === 0 && sourceTypes.length === 0;
  // Only show the specific list when searching, expanded, or the set is small.
  const showList = expanded || query.trim().length > 0 || collections.length <= 8;

  return (
    <div className="stack" style={{ gap: 8 }}>
      <span className="faint" style={{ fontSize: 11.5 }}>Applies to</span>

      {/* Summary of the current scope, with removable chips. */}
      <div className="row" style={{ gap: 6, flexWrap: "wrap", alignItems: "center" }}>
        {nothing && <Pill tone="info"><Icon name="check" size={11} /> Every source</Pill>}
        {sourceTypes.map((st) => (
          <button key={`t-${st}`} className="chip active" onClick={() => toggleType(st)}
                  title="Remove — all sources of this type">
            <SourceIcon type={st} size={12} /> All {stLabel(st)} <Icon name="x" size={10} />
          </button>
        ))}
        {collectionIds.map((id) => {
          const c = byId.get(id);
          return (
            <button key={`c-${id}`} className="chip active" onClick={() => toggleColl(id)} title="Remove">
              {c ? <SourceIcon type={collIcon(c)} size={12} /> : null} {c ? c.name : id} <Icon name="x" size={10} />
            </button>
          );
        })}
      </div>

      {/* Quick: whole source types (covers current + future sources of that type). */}
      {types.length > 0 && (
        <div className="stack" style={{ gap: 4 }}>
          <span className="faint" style={{ fontSize: 11 }}>By source type</span>
          <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
            {types.map(({ st, n }) => {
              const on = sourceTypes.includes(st);
              return (
                <button key={st} className={`chip ${on ? "active" : ""}`} onClick={() => toggleType(st)}>
                  <SourceIcon type={st} size={12} /> {stLabel(st)} <span className="faint">· {n}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Specific sources — searchable list, so many sources stay manageable. */}
      <div className="stack" style={{ gap: 4 }}>
        <div className="spread" style={{ alignItems: "center" }}>
          <span className="faint" style={{ fontSize: 11 }}>Specific sources</span>
          {collections.length > 8 && !showList && (
            <button className="btn ghost sm" onClick={() => setExpanded(true)}>Choose sources…</button>
          )}
        </div>
        {showList && (
          <>
            <input className="input sm" placeholder={`Search ${collections.length} sources…`}
                   value={query} onChange={(e) => setQuery(e.target.value)} />
            {collections.length === 0 ? (
              <span className="faint" style={{ fontSize: 12 }}>No Data Map sources yet.</span>
            ) : (
              <div style={{ maxHeight: 200, overflowY: "auto", border: "1px solid var(--border-soft)",
                   borderRadius: 8 }}>
                {filtered.map((c) => {
                  const on = collectionIds.includes(c.id);
                  const covered = sourceTypes.includes(c.source_type);
                  return (
                    <label key={c.id} className="row" style={{ gap: 8, alignItems: "center", padding: "6px 10px",
                           borderBottom: "1px solid var(--border-soft)", cursor: covered ? "default" : "pointer",
                           opacity: covered ? 0.55 : 1 }}
                           title={covered ? `Already covered by "All ${stLabel(c.source_type)}"` : undefined}>
                      <input type="checkbox" checked={on || covered} disabled={covered}
                             onChange={() => toggleColl(c.id)} />
                      <SourceIcon type={collIcon(c)} size={15} />
                      <span style={{ fontSize: 12.5, fontWeight: 600, flex: 1 }}>{c.name}</span>
                      <span className="faint" style={{ fontSize: 11 }}>
                        {c.managed ? WORKLOAD_LABELS[c.workload || ""] || c.workload : stLabel(c.source_type)}
                      </span>
                    </label>
                  );
                })}
                {filtered.length === 0 && (
                  <div className="faint" style={{ fontSize: 12, padding: "8px 10px" }}>No sources match “{query}”.</div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

const PLAN_RANK: Record<string, number> = { personal: 0, family: 1, business: 2 };

// A compact dropdown mirroring the unified-search filter menu (fs-* styling), so
// the rule builder reads like the search filters instead of raw inputs.
function RuleSelect({ value, options, onPick, placeholder, allowCustom, width }: {
  value: string;
  options: { value: string; label: string }[];
  onPick: (v: string) => void;
  placeholder?: string;
  allowCustom?: boolean;
  width?: number;
}) {
  const [open, setOpen] = useState(false);
  const cur = options.find((o) => o.value === value);
  return (
    <div className="filter-select" style={{ position: "relative", minWidth: width || 130 }}>
      <button type="button" className={`fs-trigger ${value ? "on" : ""}`} onClick={() => setOpen((o) => !o)}>
        <span className="fs-trigger-label">{cur ? cur.label : (value || placeholder || "—")}</span>
        <span className="fs-caret">▾</span>
      </button>
      {open && (
        <>
          <div className="fs-overlay" onClick={() => setOpen(false)} />
          <div className="fs-menu" style={{ minWidth: width || 180 }}>
            {allowCustom && (
              <input className="fs-input" autoFocus placeholder="Type a field…" defaultValue={value}
                     onKeyDown={(e) => { if (e.key === "Enter") { onPick((e.target as HTMLInputElement).value.trim()); setOpen(false); } }}
                     onBlur={(e) => { const v = e.target.value.trim(); if (v && v !== value) onPick(v); }} />
            )}
            {options.map((o) => (
              <button key={o.value} className="fs-opt" onClick={() => { onPick(o.value); setOpen(false); }}>
                <span className="fs-opt-label">{o.label}</span>
                {value === o.value && <Icon name="check" size={11} />}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function blankRule(): Rule {
  return {
    id: "", name: "", description: "", enabled: true, priority: 100, match: "all",
    conditions: [{ field: "from", op: "contains", value: "" }],
    actions: [{ type: "label", value: "" }],
    collection_ids: [], source_types: [], min_plan: "personal",
  };
}

// --- Firewall-style summary cells --------------------------------------------
const ACTION_TONE: Record<string, "ok" | "info" | "warn" | "danger"> = {
  discard: "danger", restrict: "warn", obfuscate: "warn", no_index: "info",
  index: "info", label: "ok", tag: "ok",
};
function actionLabel(a: Action, opts: Options | null): string {
  const meta = opts?.action_types.find((t) => t.id === a.type);
  const base = meta?.label || a.type;
  return a.value ? `${base}: ${a.value}` : base;
}
function condText(c: Condition, opts: Options | null): string {
  const op = opts?.operators.find((o) => o.id === c.op)?.label || c.op;
  const noVal = ["exists", "not_exists"].includes(c.op);
  return `${c.field || "field"} ${op}${noVal ? "" : ` “${c.value || ""}”`}`;
}
function fmtHits(n?: number): string {
  const v = n || 0;
  return v >= 1000 ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k` : String(v);
}
function timeAgoShort(iso?: string | null): string {
  if (!iso) return "—";
  const d = (Date.now() - serverDate(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}

function ScopeCell({ rule, opts }: { rule: Rule; opts: Options | null }) {
  if (!rule.source_types.length && !rule.collection_ids.length)
    return <span className="faint" style={{ fontSize: 12 }}>Every source</span>;
  const byId = new Map((opts?.collections || []).map((c) => [c.id, c]));
  return (
    <div className="row" style={{ gap: 4, flexWrap: "wrap" }}>
      {rule.source_types.map((st) => (
        <span key={`t-${st}`} className="chip" style={{ padding: "1px 7px", fontSize: 10.5 }}>
          <SourceIcon type={st} size={11} /> All {stLabel(st)}
        </span>
      ))}
      {rule.collection_ids.slice(0, 3).map((id) => {
        const c = byId.get(id);
        return (
          <span key={`c-${id}`} className="chip" style={{ padding: "1px 7px", fontSize: 10.5 }}>
            {c ? <SourceIcon type={collIcon(c)} size={11} /> : null} {c ? c.name : id}
          </span>
        );
      })}
      {rule.collection_ids.length > 3 && (
        <span className="faint" style={{ fontSize: 11 }}>+{rule.collection_ids.length - 3}</span>
      )}
    </div>
  );
}

function LogicCell({ rule, opts }: { rule: Rule; opts: Options | null }) {
  const conds = rule.conditions || [];
  const join = rule.match === "any" ? " OR " : " AND ";
  const condStr = conds.slice(0, 2).map((c) => condText(c, opts)).join(join)
    + (conds.length > 2 ? ` ${join.trim()} +${conds.length - 2} more` : "");
  return (
    <div className="stack" style={{ gap: 4 }}>
      <div style={{ fontSize: 11.5 }}>
        <span className="rule-kw" style={{ fontSize: 9.5, padding: "0 5px", marginRight: 6 }}>IF</span>
        <span className="faint">{condStr || "—"}</span>
      </div>
      <div className="row" style={{ gap: 4, flexWrap: "wrap", alignItems: "center" }}>
        <span className="rule-kw then" style={{ fontSize: 9.5, padding: "0 5px", marginRight: 2 }}>THEN</span>
        {(rule.actions || []).map((a, i) => (
          <Pill key={i} tone={ACTION_TONE[a.type] || "info"}>{actionLabel(a, opts)}</Pill>
        ))}
        {(rule.actions || []).length === 0 && <span className="faint" style={{ fontSize: 11 }}>no actions</span>}
      </div>
    </div>
  );
}

export default function Rules() {
  const [params, setParams] = useSearchParams();
  const [opts, setOpts] = useState<Options | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [editing, setEditing] = useState<Rule | null>(null);  // rule open in the edit modal
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const collFilter = params.get("collection");

  async function load() {
    setLoading(true);
    try {
      const [o, r] = await Promise.all([
        api.get<Options>("/rules/options"),
        api.get<{ rules: Rule[] }>("/rules"),
      ]);
      setOpts(o);
      setRules(r.rules);
    } catch {
      setOpts(null);
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => { void load(); /* eslint-disable-next-line */ }, []);

  const planRank = PLAN_RANK[opts?.plan || "personal"] ?? 2;
  const shown = useMemo(
    () => (collFilter ? rules.filter((r) => !r.collection_ids.length || r.collection_ids.includes(collFilter)) : rules),
    [rules, collFilter]);

  function openEdit(r: Rule) { setEditing(JSON.parse(JSON.stringify(r))); setDirty(false); }
  function openNew() { setEditing(blankRule()); setDirty(true); }
  function editField<K extends keyof Rule>(k: K, v: Rule[K]) { setEditing((s) => (s ? { ...s, [k]: v } : s)); setDirty(true); }

  async function save() {
    if (!editing) return;
    setSaving(true);
    try {
      const body = { ...editing };
      const saved = editing.id
        ? await api.put<Rule>(`/rules/${editing.id}`, body)
        : await api.post<Rule>("/rules", body);
      setEditing(null); setDirty(false);
      await load();
      await notify({ title: "Saved", message: `Rule “${saved.name}” saved.`, tone: "ok" });
    } catch (e) {
      await notify({ title: "Couldn't save", message: (e as Error).message, tone: "danger" });
    } finally { setSaving(false); }
  }

  async function remove(r: Rule) {
    if (!r.id) { setEditing(null); return; }
    if (!(await confirmDialog({ title: "Delete rule", message: `Delete “${r.name}”? This can't be undone.`, confirmLabel: "Delete", tone: "danger" }))) return;
    try { await api.del(`/rules/${r.id}`); setEditing(null); await load(); }
    catch (e) { await notify({ title: "Couldn't delete", message: (e as Error).message, tone: "danger" }); }
  }

  async function toggle(r: Rule) {
    try { await api.post(`/rules/${r.id}/toggle`, {}); await load(); }
    catch (e) { await notify({ title: "Couldn't update", message: (e as Error).message, tone: "danger" }); }
  }

  if (loading) return <Loading label="Loading rules…" />;
  if (!opts) return <Card><div className="muted">The Rules engine isn't available for this account.</div></Card>;

  return (
    <div className="stack" style={{ gap: 14 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h2 style={{ margin: 0 }}>Rules</h2>
          <div className="faint" style={{ fontSize: 12.5, maxWidth: 640 }}>
            Declarative logic evaluated the moment data is ingested — match on any source attribute,
            then label, restrict, obfuscate, or discard. Evaluated top‑to‑bottom by priority; a discard stops the rest.
          </div>
        </div>
        <button className="btn primary" onClick={openNew}>
          <Icon name="plus" size={14} /> New rule
        </button>
      </div>

      {collFilter && (
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <Pill tone="info"><Icon name="database" size={11} /> Scoped to one Data Map source</Pill>
          <button className="btn ghost sm" onClick={() => setParams({})}>Show all rules</button>
        </div>
      )}

      <Card style={{ padding: 0, overflow: "hidden" }}>
        {shown.length === 0 ? (
          <div className="muted" style={{ padding: 18 }}>No rules yet. Create one to get started.</div>
        ) : (
          <table className="rules-table">
            <thead>
              <tr>
                <th style={{ width: 44, textAlign: "center" }}>#</th>
                <th style={{ width: 60 }}>On</th>
                <th>Rule</th>
                <th>Applies to</th>
                <th>Logic</th>
                <th style={{ width: 70, textAlign: "right" }}>Hits</th>
                <th style={{ width: 90 }}>Last match</th>
                <th style={{ width: 90 }}></th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.id} className="rules-trow" onClick={() => openEdit(r)}>
                  <td style={{ textAlign: "center", fontVariantNumeric: "tabular-nums" }} className="faint">{r.priority}</td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <span className="rule-toggle" onClick={() => void toggle(r)} title={r.enabled ? "Enabled" : "Disabled"}>
                      <span className={`switch ${r.enabled ? "on" : ""}`}><span className="knob" /></span>
                    </span>
                  </td>
                  <td>
                    <div style={{ fontWeight: 600, fontSize: 13 }}>{r.name || "Untitled rule"}</div>
                    {r.description && <div className="faint" style={{ fontSize: 11.5 }}>{r.description}</div>}
                    {r.min_plan !== "personal" && <Pill tone="info">{r.min_plan}</Pill>}
                  </td>
                  <td><ScopeCell rule={r} opts={opts} /></td>
                  <td><LogicCell rule={r} opts={opts} /></td>
                  <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums", fontWeight: 600 }}
                      title={`${r.hit_count || 0} matches`}>{fmtHits(r.hit_count)}</td>
                  <td className="faint" style={{ fontSize: 11.5 }} title={r.last_match_at || ""}>{timeAgoShort(r.last_match_at)}</td>
                  <td onClick={(e) => e.stopPropagation()} style={{ textAlign: "right" }}>
                    <button className="btn ghost sm" onClick={() => openEdit(r)} title="Edit"><Icon name="edit" size={13} /></button>
                    <button className="btn ghost sm danger" onClick={() => void remove(r)} title="Delete"><Icon name="trash" size={13} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {editing && (
        <div className="modal-backdrop" onClick={() => (!dirty || confirm("Discard unsaved changes?")) && setEditing(null)}>
          <div className="modal-panel rules-modal" onClick={(e) => e.stopPropagation()}>
            <div className="spread" style={{ alignItems: "center", marginBottom: 12 }}>
              <h3 style={{ margin: 0 }}>{editing.id ? "Edit rule" : "New rule"}</h3>
              <button className="btn ghost sm" onClick={() => (!dirty || confirm("Discard unsaved changes?")) && setEditing(null)}>
                <Icon name="x" size={15} />
              </button>
            </div>
            <div className="modal-body">
              <RuleEditor
                rule={editing} opts={opts} planRank={planRank}
                onChange={editField} onSave={save} onDelete={() => remove(editing)}
                saving={saving} dirty={dirty}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function RuleEditor({ rule, opts, planRank, onChange, onSave, onDelete, saving, dirty }: {
  rule: Rule; opts: Options; planRank: number;
  onChange: <K extends keyof Rule>(k: K, v: Rule[K]) => void;
  onSave: () => void; onDelete: () => void; saving: boolean; dirty: boolean;
}) {
  const setCond = (i: number, patch: Partial<Condition>) =>
    onChange("conditions", rule.conditions.map((c, j) => (j === i ? { ...c, ...patch } : c)));
  const setAct = (i: number, patch: Partial<Action>) =>
    onChange("actions", rule.actions.map((a, j) => (j === i ? { ...a, ...patch } : a)));
  const actMeta = (t: string) => opts.action_types.find((a) => a.id === t);

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="row" style={{ gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
        <label className="stack flex1" style={{ gap: 4, minWidth: 200 }}>
          <span className="faint" style={{ fontSize: 11.5 }}>Rule name</span>
          <input className="input" value={rule.name} placeholder="e.g. Flag Rob's mail"
                 onChange={(e) => onChange("name", e.target.value)} />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="faint" style={{ fontSize: 11.5 }}>Priority</span>
          <input className="input" type="number" style={{ width: 90 }} value={rule.priority}
                 onChange={(e) => onChange("priority", parseInt(e.target.value || "100", 10))} />
        </label>
        <label className="stack" style={{ gap: 4 }}>
          <span className="faint" style={{ fontSize: 11.5 }}>Requires plan</span>
          <select className="input" value={rule.min_plan} onChange={(e) => onChange("min_plan", e.target.value)}>
            {opts.plans.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
      </div>
      <label className="stack" style={{ gap: 4 }}>
        <span className="faint" style={{ fontSize: 11.5 }}>Description (optional)</span>
        <input className="input" value={rule.description} placeholder="What this rule is for"
               onChange={(e) => onChange("description", e.target.value)} />
      </label>

      {/* Scope */}
      <div className="stack" style={{ gap: 6 }}>
        <ScopePicker collections={opts.collections} collectionIds={rule.collection_ids}
                     sourceTypes={rule.source_types}
                     onChange={(field, value) => onChange(field, value)} />
      </div>

      {/* IF */}
      <div className="rule-block">
        <div className="rule-block-head">
          <span className="rule-kw">IF</span>
          <div className="seg">
            <button className={rule.match === "all" ? "on" : ""} onClick={() => onChange("match", "all")}>match ALL</button>
            <button className={rule.match === "any" ? "on" : ""} onClick={() => onChange("match", "any")}>match ANY</button>
          </div>
        </div>
        <div className="stack" style={{ gap: 8 }}>
          {rule.conditions.map((c, i) => (
            <div key={i} className="rule-cond">
              <RuleSelect value={c.field} placeholder="field" allowCustom width={150}
                          options={opts.field_suggestions.map((f) => ({ value: f, label: f }))}
                          onPick={(v) => setCond(i, { field: v })} />
              <RuleSelect value={c.op} placeholder="is" width={130}
                          options={opts.operators.map((o) => ({ value: o.id, label: o.label }))}
                          onPick={(v) => setCond(i, { op: v })} />
              {!["exists", "not_exists"].includes(c.op) && (
                <input className="input sm" value={c.value || ""} placeholder="value"
                       onChange={(e) => setCond(i, { value: e.target.value })} />
              )}
              <button className="btn ghost sm" onClick={() => onChange("conditions", rule.conditions.filter((_, j) => j !== i))}
                      disabled={rule.conditions.length <= 1} title="Remove"><Icon name="x" size={13} /></button>
            </div>
          ))}
          <button className="btn ghost sm" style={{ alignSelf: "flex-start" }}
                  onClick={() => onChange("conditions", [...rule.conditions, { field: "", op: "contains", value: "" }])}>
            <Icon name="plus" size={13} /> Add condition
          </button>
        </div>
      </div>

      {/* THEN */}
      <div className="rule-block">
        <div className="rule-block-head"><span className="rule-kw then">THEN</span></div>
        <div className="stack" style={{ gap: 8 }}>
          {rule.actions.map((a, i) => {
            const m = actMeta(a.type);
            const locked = m ? (PLAN_RANK[m.min_plan] ?? 0) > planRank : false;
            return (
              <div key={i} className={`rule-cond ${locked ? "locked" : ""}`}>
                <select className="input sm" value={a.type} onChange={(e) => setAct(i, { type: e.target.value })}>
                  {opts.action_types.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
                </select>
                {m?.needs_value && (
                  <input className="input sm" value={a.value || ""} placeholder="label / tag value"
                         onChange={(e) => setAct(i, { value: e.target.value })} />
                )}
                {locked && <span title="Not included in your plan"><Pill tone="warn"><Icon name="lock" size={10} /> {m?.min_plan}</Pill></span>}
                <button className="btn ghost sm" onClick={() => onChange("actions", rule.actions.filter((_, j) => j !== i))}
                        disabled={rule.actions.length <= 1} title="Remove"><Icon name="x" size={13} /></button>
              </div>
            );
          })}
          <button className="btn ghost sm" style={{ alignSelf: "flex-start" }}
                  onClick={() => onChange("actions", [...rule.actions, { type: "label", value: "" }])}>
            <Icon name="plus" size={13} /> Add action
          </button>
        </div>
      </div>

      <RuleTester collectionIds={rule.collection_ids} />

      <div className="row" style={{ gap: 8, justifyContent: "space-between", borderTop: "1px solid var(--border-soft)", paddingTop: 12 }}>
        <button className="btn ghost sm danger" onClick={onDelete}><Icon name="trash" size={13} /> Delete</button>
        <div className="row" style={{ gap: 8 }}>
          <label className="row" style={{ gap: 6, alignItems: "center", fontSize: 12.5 }}>
            <input type="checkbox" checked={rule.enabled} onChange={(e) => onChange("enabled", e.target.checked)} /> Enabled
          </label>
          <button className="btn primary" onClick={onSave} disabled={saving || !dirty}>
            <Icon name="check" size={14} /> {saving ? "Saving…" : "Save rule"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Live evaluation preview against the tenant's saved rules.
function RuleTester({ collectionIds }: { collectionIds: string[] }) {
  const [open, setOpen] = useState(false);
  const [f, setF] = useState({ source_type: "gmail", doc_type: "email", title: "", from: "", folder: "" });
  const [res, setRes] = useState<{ matched: { name: string; actions: string[] }[]; restricted: boolean; obfuscate: boolean; no_index: boolean; discard: boolean; add_labels: string[] } | null>(null);

  async function run() {
    try {
      const r = await api.post<typeof res & object>("/rules/preview", {
        doc_type: f.doc_type, source_type: f.source_type, title: f.title,
        labels: [], meta: { from: f.from, folder: f.folder },
        collection_id: collectionIds[0] || null,
      });
      setRes(r as never);
    } catch (e) { await notify({ title: "Test failed", message: (e as Error).message, tone: "danger" }); }
  }

  return (
    <div className="rule-block">
      <button className="rule-block-head asbtn" onClick={() => setOpen((o) => !o)}>
        <span className="rule-kw test">TEST</span>
        <span className="faint" style={{ fontSize: 12 }}>Check which rules match a sample object</span>
        <Icon name={open ? "moon" : "sun"} size={13} />
      </button>
      {open && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="rule-cond">
            <input className="input sm" value={f.from} placeholder="from" onChange={(e) => setF({ ...f, from: e.target.value })} />
            <input className="input sm" value={f.title} placeholder="title/subject" onChange={(e) => setF({ ...f, title: e.target.value })} />
            <input className="input sm" value={f.folder} placeholder="folder" onChange={(e) => setF({ ...f, folder: e.target.value })} />
            <button className="btn sm primary" onClick={run}>Test</button>
          </div>
          {res && (
            res.matched.length === 0
              ? <div className="faint" style={{ fontSize: 12.5 }}>No rules match this sample.</div>
              : <div className="row" style={{ gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                  {res.matched.map((m) => <Pill key={m.name} tone="ok">{m.name}: {m.actions.join(", ")}</Pill>)}
                  {res.discard && <Pill tone="danger">would discard</Pill>}
                  {res.restricted && <Pill tone="warn">restricted</Pill>}
                  {res.add_labels.map((l) => <span key={l} className="chip active">{l}</span>)}
                </div>
          )}
        </div>
      )}
    </div>
  );
}
