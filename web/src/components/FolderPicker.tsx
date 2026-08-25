import { ReactNode, useEffect, useMemo, useState } from "react";
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
  // When true, a folder with subfolders can be marked "files only" (back up its
  // top-level files without recursing) — encoded as "flat:<path>" in the roots.
  allowFilesOnly?: boolean;
}

const FLAT = "flat:";

// A reusable folder-tree picker shared by every filesystem-style source (cloud
// Dropbox/OneDrive and the desktop-agent endpoint files). The data source is
// abstracted behind loadRoots/loadChildren so the same UI serves sync (cloud API)
// and async (agent command) backends.
export function FolderPicker(props: Props) {
  const [roots, setRoots] = useState<FolderNode[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [lazyKids, setLazyKids] = useState<Record<string, FolderNode[]>>({});
  const [loadingPaths, setLoadingPaths] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(
    new Set((props.initialSelected || []).filter((p) => !p.startsWith(FLAT))));
  const [flatSel, setFlatSel] = useState<Set<string>>(
    new Set((props.initialSelected || []).filter((p) => p.startsWith(FLAT)).map((p) => p.slice(FLAT.length))));
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
    const isSel = selected.has(path) || flatSel.has(path);
    if (isSel) {
      setSelected((cur) => { const n = new Set(cur); n.delete(path); return n; });
      setFlatSel((cur) => { const n = new Set(cur); n.delete(path); return n; });
    } else {
      setSelected((cur) => new Set(cur).add(path));  // default: include subfolders
    }
  }

  // Switch a selected folder between recursive (subfolders) and "files only".
  function toggleFlat(path: string) {
    if (flatSel.has(path)) {
      setFlatSel((cur) => { const n = new Set(cur); n.delete(path); return n; });
      setSelected((cur) => new Set(cur).add(path));
    } else {
      setSelected((cur) => { const n = new Set(cur); n.delete(path); return n; });
      setFlatSel((cur) => new Set(cur).add(path));
    }
  }

  async function save() {
    setSaving(true); setErr("");
    try {
      await props.onSave([...selected, ...[...flatSel].map((p) => FLAT + p)]);
    } catch (e) {
      setErr((e as Error)?.message || "Couldn't save"); setSaving(false);
    }
  }

  // Index every folder currently loaded in the tree (roots + lazily-expanded
  // children) so we can tell which of the operator's *saved* selections still
  // exist. Selected folders that were moved/deleted no longer appear anywhere in
  // the browsable tree — this lets us surface them so they can be un-selected.
  const loadedIndex = useMemo(() => {
    const loaded = new Set<string>();
    const expandedParents = new Set<string>();
    const nameByPath = new Map<string, string>();
    const walk = (nodes: FolderNode[]) => {
      for (const n of nodes) {
        loaded.add(n.path);
        nameByPath.set(n.path, n.name);
        const kids = lazyKids[n.path] ?? n.children;
        if (kids) { expandedParents.add(n.path); walk(kids); }
      }
    };
    walk(roots);
    for (const [p, kids] of Object.entries(lazyKids)) {
      expandedParents.add(p);
      for (const k of kids) { loaded.add(k.path); nameByPath.set(k.path, k.name); }
    }
    return { loaded, expandedParents, nameByPath, rootPaths: roots.map((r) => r.path) };
  }, [roots, lazyKids]);

  // "ok" = present in the tree, "gone" = confidently missing (its drive/parent is
  // loaded but it isn't there), "unknown" = parent not browsed yet.
  function availabilityOf(p: string): "ok" | "gone" | "unknown" {
    const { loaded, expandedParents, rootPaths } = loadedIndex;
    if (p === "__root__" || loaded.has(p)) return "ok";
    const underRoot = rootPaths.some((r) => p === r || p.startsWith(r.endsWith("/") ? r : r + "/"));
    if (rootPaths.length > 0 && !underRoot) return "gone";  // whole drive/volume/home is gone
    const par = p.replace(/\/[^/]*$/, "");
    if (expandedParents.has(par)) return "gone";  // parent browsed, folder absent → deleted
    return "unknown";
  }

  const selectedList = useMemo(() => {
    const items = [
      ...[...selected].map((p) => ({ path: p, flat: false })),
      ...[...flatSel].map((p) => ({ path: p, flat: true })),
    ];
    return items
      .map((it) => ({ ...it, avail: availabilityOf(it.path), name: loadedIndex.nameByPath.get(it.path) }))
      .sort((a, b) => a.path.localeCompare(b.path));
    /* eslint-disable-next-line react-hooks/exhaustive-deps */
  }, [selected, flatSel, loadedIndex]);

  function displayName(path: string, name?: string): string {
    if (path === "__root__") return "Files in the root folder";
    return name || path.replace(/\/+$/, "").split("/").pop() || path;
  }

  function renderNode(node: FolderNode, depth: number) {
    const isSel = selected.has(node.path) || flatSel.has(node.path);
    const isFlat = flatSel.has(node.path);
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
          {isSel && canExpand && props.allowFilesOnly && (
            <label className="row" style={{ gap: 3, marginLeft: 6, alignItems: "center", cursor: "pointer" }}
                   title="Back up only the files directly in this folder, not its subfolders">
              <input type="checkbox" checked={isFlat} onChange={() => toggleFlat(node.path)} />
              <span className="faint" style={{ fontSize: 10.5 }}>files only</span>
            </label>
          )}
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
          {selectedList.length > 0 && (
            <div style={{ marginTop: 10, border: "1px solid var(--border-soft)", borderRadius: 8, padding: 8 }}>
              <div className="faint" style={{ fontSize: 11, marginBottom: 6, textTransform: "uppercase", letterSpacing: 0.4 }}>
                Selected folders
              </div>
              <div className="stack" style={{ gap: 2 }}>
                {selectedList.map((it) => {
                  const gone = it.avail === "gone";
                  return (
                    <div key={(it.flat ? FLAT : "") + it.path} className="row"
                         style={{ gap: 6, alignItems: "center", padding: "2px 0", opacity: gone ? 0.85 : 1 }}>
                      <Icon name={gone ? "alert" : "database"} size={13} />
                      <span style={{ fontSize: 12.5, textDecoration: gone ? "line-through" : "none" }}>
                        {displayName(it.path, it.name)}
                      </span>
                      {it.flat && <span className="faint" style={{ fontSize: 10 }}>· files only</span>}
                      {gone && (
                        <span style={{ fontSize: 10.5, color: "var(--warn-c,#d19a30)" }}>(no longer available)</span>
                      )}
                      <span className="faint" style={{ fontSize: 10.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: 260 }}
                            title={it.path}>{it.path === "__root__" ? "" : it.path}</span>
                      <div style={{ flex: 1 }} />
                      <button className="btn ghost sm" style={{ padding: "0 6px", minWidth: 18 }}
                              title="Remove from selection" onClick={() => toggleSelect(it.path)}>×</button>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          {props.extra}
          <div className="faint" style={{ fontSize: 11, marginTop: 8 }}>
            {selected.size + flatSel.size} folder(s) selected · nothing selected backs up everything. Each item is encrypted before upload.
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
