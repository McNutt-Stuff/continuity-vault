import { ReactNode, useEffect, useState } from "react";
import { Icon } from "./Icon";
import { bytes } from "./ui";

export interface FolderNode {
  path: string;
  name: string;
  files?: number;
  bytes?: number;
  children?: FolderNode[];
  hasMore?: boolean;
}

interface Props {
  title: string;
  note?: string;
  initialSelected?: string[];
  // Load the top-level folders (may poll internally, e.g. for an agent index).
  loadRoots: () => Promise<FolderNode[]>;
  // Load a folder's immediate child folders (lazy expansion).
  loadChildren: (node: FolderNode) => Promise<FolderNode[]>;
  onSave: (roots: string[]) => Promise<void>;
  onClose: () => void;
  // Optional left-aligned footer control (e.g. "Rescan drives").
  headerAction?: ReactNode;
  // Optional extra body controls (e.g. exclude file types / max size).
  extra?: ReactNode;
  loadingLabel?: string;
  emptyLabel?: string;
  saveLabel?: string;
}

// A reusable folder-tree picker shared by every filesystem-style source (cloud
// Dropbox/OneDrive and the desktop-agent endpoint files). The data source is
// abstracted behind loadRoots/loadChildren so the same UI serves sync (cloud API)
// and async (agent command) backends.
export function FolderPicker(props: Props) {
  const [roots, setRoots] = useState<FolderNode[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [lazyKids, setLazyKids] = useState<Record<string, FolderNode[]>>({});
  const [loadingPaths, setLoadingPaths] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set(props.initialSelected || []));
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  async function reload() {
    setLoading(true); setErr("");
    try {
      setRoots(await props.loadRoots());
    } catch (e) {
      setErr((e as Error)?.message || "Couldn't load folders");
    }
    setLoading(false);
  }
  useEffect(() => { void reload(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  async function toggleExpand(node: FolderNode) {
    const path = node.path;
    const willExpand = !expanded.has(path);
    setExpanded((cur) => { const n = new Set(cur); n.has(path) ? n.delete(path) : n.add(path); return n; });
    if (willExpand && node.hasMore && !lazyKids[path] && !loadingPaths.has(path)) {
      setLoadingPaths((s) => new Set(s).add(path));
      try {
        setLazyKids((m) => ({ ...m, [path]: [] }));  // placeholder so spinner shows
        const kids = await props.loadChildren(node);
        setLazyKids((m) => ({ ...m, [path]: kids }));
      } catch {
        setLazyKids((m) => ({ ...m, [path]: [] }));
      }
      setLoadingPaths((s) => { const n = new Set(s); n.delete(path); return n; });
    }
  }

  function toggleSelect(path: string) {
    setSelected((cur) => { const n = new Set(cur); n.has(path) ? n.delete(path) : n.add(path); return n; });
  }

  async function save() {
    setSaving(true); setErr("");
    try {
      await props.onSave([...selected]);
    } catch (e) {
      setErr((e as Error)?.message || "Couldn't save"); setSaving(false);
    }
  }

  function renderNode(node: FolderNode, depth: number) {
    const isSel = selected.has(node.path);
    const isExp = expanded.has(node.path);
    const kids = lazyKids[node.path] ?? (node.children || []);
    const isLoading = loadingPaths.has(node.path);
    const canExpand = kids.length > 0 || node.hasMore;
    return (
      <div key={node.path}>
        <div className="row" style={{ gap: 6, alignItems: "center", padding: "3px 0", paddingLeft: depth * 16 }}>
          {canExpand ? (
            <button className="btn ghost sm" style={{ padding: "0 4px", minWidth: 18 }} onClick={() => void toggleExpand(node)}>
              {isExp ? "▾" : "▸"}
            </button>
          ) : <span style={{ width: 18 }} />}
          <input type="checkbox" checked={isSel} onChange={() => toggleSelect(node.path)} />
          <Icon name="database" size={13} />
          <span style={{ fontSize: 12.5 }}>{node.name}</span>
          {(node.files || 0) > 0 && (
            <span className="faint" style={{ fontSize: 10.5 }}>· {node.files} files{node.bytes ? ` · ${bytes(node.bytes)}` : ""}</span>
          )}
        </div>
        {isExp && (
          <div>
            {kids.map((c) => renderNode(c, depth + 1))}
            {kids.length === 0 && isLoading && (
              <div className="faint" style={{ paddingLeft: (depth + 1) * 16 + 24, fontSize: 11 }}>
                <span className="spinner-dot" /> loading subfolders…
              </div>
            )}
            {kids.length === 0 && !isLoading && node.hasMore && (
              <div className="faint" style={{ paddingLeft: (depth + 1) * 16 + 24, fontSize: 11 }}>
                selecting this folder backs up everything beneath it
              </div>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="modal-backdrop" onClick={props.onClose}>
      <div className="modal-panel" style={{ maxWidth: 640 }} onClick={(e) => e.stopPropagation()}>
        <div className="spread">
          <div>
            <h3 style={{ margin: 0 }}>{props.title}</h3>
            {props.note && <div className="faint" style={{ fontSize: 12 }}>{props.note}</div>}
          </div>
          <button className="btn ghost sm" onClick={props.onClose}><Icon name="logout" size={14} /></button>
        </div>
        <div className="modal-body">
          {err && <div style={{ color: "var(--danger-c,#f2545b)", fontSize: 12, marginBottom: 8 }}>{err}</div>}
          <div style={{ maxHeight: "44vh", overflow: "auto", border: "1px solid var(--border-soft)", borderRadius: 8, padding: 8 }}>
            {loading && roots.length === 0 && (
              <div className="faint"><span className="spinner-dot" /> {props.loadingLabel || "loading folders…"}</div>
            )}
            {roots.map((r) => renderNode(r, 0))}
            {!loading && roots.length === 0 && <div className="muted">{props.emptyLabel || "No folders found."}</div>}
          </div>
          {props.extra}
          <div className="faint" style={{ fontSize: 11, marginTop: 8 }}>
            {selected.size} folder(s) selected · nothing selected backs up everything. Each item is encrypted before upload.
          </div>
        </div>
        <div className="modal-foot">
          {props.headerAction}
          <div style={{ flex: 1 }} />
          <button className="btn sm" onClick={props.onClose}>Cancel</button>
          <button className="btn primary sm" disabled={saving} onClick={() => void save()}>
            {saving ? "Saving…" : (props.saveLabel || "Save selection")}
          </button>
        </div>
      </div>
    </div>
  );
}
