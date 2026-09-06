import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { loadSupport, slugForRoute, SupportContent, DocNavItem } from "../support";
import { renderDoc, planLabel } from "../md";
import { site } from "../content";

export default function Support() {
  const { slug } = useParams();
  const [params] = useSearchParams();
  const nav = useNavigate();
  const [content, setContent] = useState<SupportContent | null>(null);
  const [q, setQ] = useState("");
  const [openNav, setOpenNav] = useState<Set<string>>(new Set());

  useEffect(() => {
    let alive = true;
    loadSupport().then((c) => {
      if (!alive) return;
      setContent(c);
      // Contextual deep-link from the portal Help icon: /support?for=/search
      const forRoute = params.get("for");
      if (!slug && forRoute) {
        const s = slugForRoute(c, forRoute);
        if (s) nav(`/support/${s}`, { replace: true });
      }
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activeSlug = slug || content?.tree?.[0]?.docs?.[0]?.slug || "";
  const doc = content && activeSlug ? content.docs[activeSlug] : undefined;

  // Slugs of the active page's ancestors — auto-expanded so the nav opens to it.
  const activeTrail = useMemo(() => {
    const trail = new Set<string>();
    if (!content) return trail;
    const walk = (nodes: any[], parents: string[]): boolean => {
      for (const n of nodes) {
        if (n.slug === activeSlug) { parents.forEach((p) => trail.add(p)); return true; }
        if (n.children?.length && walk(n.children, [...parents, n.slug])) return true;
      }
      return false;
    };
    content.tree.forEach((s) => walk(s.docs, []));
    return trail;
  }, [content, activeSlug]);

  const filteredTree = useMemo(() => {
    if (!content) return [];
    const needle = q.trim().toLowerCase();
    if (!needle) return content.tree;
    // Search flattens the hierarchy so a matching sub-page still surfaces.
    const flatten = (nodes: any[]): any[] =>
      nodes.flatMap((n) => [n, ...(n.children ? flatten(n.children) : [])]);
    const match = (d: any) =>
      d.title.toLowerCase().includes(needle) || (d.summary || "").toLowerCase().includes(needle);
    return content.tree
      .map((s) => ({ ...s, docs: flatten(s.docs).filter(match).map((d) => ({ ...d, children: [] })) }))
      .filter((s) => s.docs.length > 0);
  }, [content, q]);

  // Keep in-app navigation for links to other docs inside rendered markdown.
  function onContentClick(e: React.MouseEvent) {
    const a = (e.target as HTMLElement).closest("a");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (href.startsWith("/support")) {
      e.preventDefault();
      nav(href);
    }
  }

  if (!content) {
    return (
      <div className="support-shell">
        <div className="support-main">
          <p className="muted">Loading documentation…</p>
        </div>
      </div>
    );
  }

  const empty = content.tree.length === 0;

  function renderNav(d: DocNavItem, depth: number) {
    const kids = d.children || [];
    const hasKids = kids.length > 0;
    const open = hasKids && (openNav.has(d.slug) || activeTrail.has(d.slug));
    return (
      <div key={d.slug} className="support-nav-node">
        <div className="support-nav-row" style={{ paddingLeft: depth * 12 }}>
          {hasKids ? (
            <button
              className="support-nav-caret"
              aria-label={open ? "Collapse" : "Expand"}
              onClick={() =>
                setOpenNav((s) => {
                  const n = new Set(s);
                  n.has(d.slug) ? n.delete(d.slug) : n.add(d.slug);
                  return n;
                })
              }
            >
              {open ? "▾" : "▸"}
            </button>
          ) : (
            <span className="support-nav-caret-spacer" />
          )}
          <Link
            to={`/support/${d.slug}`}
            className={`support-nav-link ${d.slug === activeSlug ? "active" : ""}`}
          >
            {d.title}
            {d.required_plan && (
              <span className="plan-gate-badge" data-plan={d.required_plan}
                    style={{ marginLeft: 6, fontSize: 9.5, padding: "1px 6px" }}>
                {planLabel(d.required_plan)}
              </span>
            )}
          </Link>
        </div>
        {hasKids && open && kids.map((c) => renderNav(c, depth + 1))}
      </div>
    );
  }

  return (
    <div className="support-shell">
      <aside className="support-side">
        <div className="support-side-head">
          <Link to="/support" className="support-home">
            <span className="support-home-mark">?</span> Help Center
          </Link>
        </div>
        <input
          className="support-search"
          placeholder="Search the docs…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <nav className="support-nav">
          {filteredTree.map((s) => (
            <div key={s.section} className="support-nav-group">
              <div className="support-nav-title">{s.section}</div>
              {s.docs.map((d) => renderNav(d, 0))}
            </div>
          ))}
          {empty && <div className="muted" style={{ padding: "8px 4px" }}>No documentation yet.</div>}
        </nav>
      </aside>

      <div className="support-main">
        {empty ? (
          <div className="support-article">
            <h1>Help Center</h1>
            <p className="muted">
              Documentation is being published. Please check back shortly, or{" "}
              <Link to="/contact">contact us</Link>.
            </p>
          </div>
        ) : doc ? (
          <article className="support-article">
            <div className="support-crumbs">
              <Link to="/support">Help Center</Link> <span>›</span> <span>{doc.section}</span>
            </div>
            <h1>{doc.title}</h1>
            {doc.required_plan && (
              <div style={{ margin: "0 0 14px" }}>
                <span className="plan-gate-badge" data-plan={doc.required_plan}>
                  {planLabel(doc.required_plan)} plan &amp; above
                </span>
              </div>
            )}
            {doc.summary && <p className="support-lead">{doc.summary}</p>}
            <div
              className="support-body"
              onClick={onContentClick}
              dangerouslySetInnerHTML={{ __html: renderDoc(doc.body) }}
            />
            {doc.updated_at && (
              <div className="support-updated">
                Last updated {new Date(doc.updated_at).toLocaleDateString()}
              </div>
            )}
            <div className="support-foot-cta">
              <span>Still need help?</span>
              <a className="btn primary" href={`${site.appUrl}/support/tickets`}>
                Open a support ticket
              </a>
            </div>
          </article>
        ) : (
          <div className="support-article">
            <h1>Not found</h1>
            <p className="muted">
              That page doesn't exist. <Link to="/support">Back to the Help Center</Link>.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
