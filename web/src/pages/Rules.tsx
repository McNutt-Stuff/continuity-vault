import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { Card, Pill, Loading } from "../components/ui";
import { Icon } from "../components/Icon";
import { confirmDialog, notify } from "../components/dialog";

type Condition = { field: string; op: string; value?: string };
type Action = { type: string; value?: string; min_plan?: string };
interface Rule {
  id: string; name: string; description: string; enabled: boolean; priority: number;
  match: "all" | "any"; conditions: Condition[]; actions: Action[];
  collection_ids: string[]; source_types: string[]; min_plan: string;
  updated_at?: string | null;
}
interface Options {
  operators: { id: string; label: string }[];
  action_types: { id: string; label: string; needs_value: boolean; min_plan: string }[];
  field_suggestions: string[];
  plans: string[];
  plan: string;
  collections: { id: string; name: string; source_type: string }[];
}

const PLAN_RANK: Record<string, number> = { personal: 0, family: 1, business: 2 };

function blankRule(): Rule {
  return {
    id: "", name: "", description: "", enabled: true, priority: 100, match: "all",
    conditions: [{ field: "from", op: "contains", value: "" }],
    actions: [{ type: "label", value: "" }],
    collection_ids: [], source_types: [], min_plan: "personal",
  };
}

export default function Rules() {
  const [params, setParams] = useSearchParams();
  const [opts, setOpts] = useState<Options | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [sel, setSel] = useState<Rule | null>(null);
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
      if (!sel && r.rules.length) selectRule(r.rules[0]);
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

  function selectRule(r: Rule) { setSel(JSON.parse(JSON.stringify(r))); setDirty(false); }
  function edit<K extends keyof Rule>(k: K, v: Rule[K]) { setSel((s) => (s ? { ...s, [k]: v } : s)); setDirty(true); }

  async function save() {
    if (!sel) return;
    setSaving(true);
    try {
      const body = { ...sel };
      const saved = sel.id
        ? await api.put<Rule>(`/rules/${sel.id}`, body)
        : await api.post<Rule>("/rules", body);
      await load();
      setSel(saved); setDirty(false);
      await notify({ title: "Saved", message: `Rule “${saved.name}” saved.`, tone: "ok" });
    } catch (e) {
      await notify({ title: "Couldn't save", message: (e as Error).message, tone: "danger" });
    } finally { setSaving(false); }
  }

  async function remove(r: Rule) {
    if (!r.id) { setSel(null); return; }
    if (!(await confirmDialog({ title: "Delete rule", message: `Delete “${r.name}”? This can't be undone.`, confirmLabel: "Delete", danger: true }))) return;
    try { await api.del(`/rules/${r.id}`); if (sel?.id === r.id) setSel(null); await load(); }
    catch (e) { await notify({ title: "Couldn't delete", message: (e as Error).message, tone: "danger" }); }
  }

  async function toggle(r: Rule) {
    try { await api.post(`/rules/${r.id}/toggle`, {}); await load(); if (sel?.id === r.id) setSel({ ...sel, enabled: !sel.enabled }); }
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
            then label, restrict, obfuscate, or discard. Rules take precedence over the basic Data Map settings.
          </div>
        </div>
        <button className="btn primary" onClick={() => { setSel(blankRule()); setDirty(true); }}>
          <Icon name="plus" size={14} /> New rule
        </button>
      </div>

      {collFilter && (
        <div className="row" style={{ gap: 8, alignItems: "center" }}>
          <Pill tone="info"><Icon name="database" size={11} /> Scoped to one Data Map source</Pill>
          <button className="btn ghost sm" onClick={() => setParams({})}>Show all rules</button>
        </div>
      )}

      <div className="rules-2pane">
        {/* Left: rule list */}
        <div className="rules-list">
          {shown.length === 0 && <div className="muted" style={{ padding: 14 }}>No rules yet. Create one to get started.</div>}
          {shown.map((r) => (
            <button key={r.id} className={`rule-row ${sel?.id === r.id ? "active" : ""}`} onClick={() => selectRule(r)}>
              <span className={`rule-dot ${r.enabled ? "on" : ""}`} />
              <span className="flex1" style={{ minWidth: 0 }}>
                <div className="rule-name">{r.name || "Untitled rule"}</div>
                <div className="faint rule-sub">
                  {r.conditions.length} condition{r.conditions.length === 1 ? "" : "s"} · {r.actions.length} action{r.actions.length === 1 ? "" : "s"}
                </div>
              </span>
              {r.min_plan !== "personal" && <Pill tone="info">{r.min_plan}</Pill>}
              <span className="rule-toggle" onClick={(e) => { e.stopPropagation(); void toggle(r); }}
                    title={r.enabled ? "Enabled" : "Disabled"}>
                <span className={`switch ${r.enabled ? "on" : ""}`}><span className="knob" /></span>
              </span>
            </button>
          ))}
        </div>

        {/* Right: editor */}
        <div className="rules-editor">
          {!sel ? (
            <div className="muted" style={{ padding: 20 }}>Select a rule, or create a new one.</div>
          ) : (
            <RuleEditor
              key={sel.id || "new"}
              rule={sel} opts={opts} planRank={planRank}
              onChange={edit} onSave={save} onDelete={() => remove(sel)}
              saving={saving} dirty={dirty}
            />
          )}
        </div>
      </div>
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
        <span className="faint" style={{ fontSize: 11.5 }}>Applies to (leave empty = every source)</span>
        <div className="row" style={{ gap: 6, flexWrap: "wrap" }}>
          {opts.collections.map((c) => {
            const on = rule.collection_ids.includes(c.id);
            return (
              <button key={c.id} className={`chip ${on ? "active" : ""}`}
                      onClick={() => onChange("collection_ids", on ? rule.collection_ids.filter((x) => x !== c.id) : [...rule.collection_ids, c.id])}>
                {on && <Icon name="check" size={12} />} {c.name}
              </button>
            );
          })}
          {opts.collections.length === 0 && <span className="faint" style={{ fontSize: 12 }}>No Data Map sources yet.</span>}
        </div>
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
              <input className="input sm" list="rule-fields" value={c.field} placeholder="field (e.g. from, meta.folder)"
                     onChange={(e) => setCond(i, { field: e.target.value })} />
              <select className="input sm" value={c.op} onChange={(e) => setCond(i, { op: e.target.value })}>
                {opts.operators.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
              {!["exists", "not_exists"].includes(c.op) && (
                <input className="input sm" value={c.value || ""} placeholder="value"
                       onChange={(e) => setCond(i, { value: e.target.value })} />
              )}
              <button className="btn ghost sm" onClick={() => onChange("conditions", rule.conditions.filter((_, j) => j !== i))}
                      disabled={rule.conditions.length <= 1} title="Remove"><Icon name="x" size={13} /></button>
            </div>
          ))}
          <datalist id="rule-fields">{opts.field_suggestions.map((f) => <option key={f} value={f} />)}</datalist>
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
                {locked && <Pill tone="warn" title="Not included in your plan"><Icon name="lock" size={10} /> {m?.min_plan}</Pill>}
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
