---
applyTo: "web/src/pages/Admin.tsx"
description: "Uniformity rules for the platform admin console pages (Admin.tsx sections)."
---

# Admin console pages — uniform structure

The admin console (`web/src/pages/Admin.tsx`) is one file of section components, each rendered by the
`Admin()` switch and listed in `ADMIN_SECTIONS`. Every section MUST follow the same shell so the console feels
like one product. Reuse the existing primitives — never invent a new card/table/modal/toast style.

## Page shell (every section)
- **Header row** at the top, OUTSIDE any card: a left block with an `<h2>` title + a `.muted` one-line
  description, and the section's PRIMARY action on the right, in a `spread`:
  ```tsx
  <div className="spread" style={{ marginBottom: 16, alignItems: "flex-start", gap: 12, flexWrap: "wrap" }}>
    <div>
      <h2 style={{ margin: "0 0 2px" }}>Section title</h2>
      <div className="muted" style={{ fontSize: 12.5, maxWidth: 640 }}>One-line what/why.</div>
    </div>
    <button className="btn primary sm" onClick={openNew}><Icon name="…" size={13} /> New thing</button>
  </div>
  ```
  Use `<h2>` for the page title; use uppercase `.faint` labels (`textTransform:"uppercase", letterSpacing:".06em"`)
  for sub-sections inside cards.
- **Content** goes in `<Card>` (from `components/ui`). Stat tiles use `<Stat>` in a `grid grid-4` /
  `grid` with `minmax(...)`; a secondary "Refresh" is a `btn ghost sm` with `<Icon name="repeat" />`.
- **Toast** at the very end: `const [toast,setToast]=useState("")`, a `flash(m)` helper
  (`setToast(m); setTimeout(()=>setToast(""), 3000)`), and `{toast && <div className="toast"><Icon name="check" size={15}/> {toast}</div>}`.
- **Loading / empty:** while `data===null` render `<Card><div className="muted">Loading…</div></Card>`; an empty
  table shows a single `<tr><td colSpan={N} className="muted">Nothing yet — …</td></tr>`.

## Buttons
- Primary action: `btn primary sm`. Secondary/neutral: `btn ghost sm`. Destructive: `btn danger sm`.
- Do NOT scatter multiple `+ Foo` primary buttons in a header. One primary "New …" button that opens a modal.
  If the thing has several types, the modal picks the type first (see below).
- Row actions live in the LAST table cell, right-aligned: `style={{ textAlign:"right", whiteSpace:"nowrap" }}`,
  ordered Test → Edit → Delete, separated by `{" "}`.

## Tables
- Use `<table className="table">`. Status/kind values render as `<Pill>` (`tone` = ok|warn|danger|info). Pass
  `dot` to `Pill` ONLY for genuine liveness/health (online/offline), never for informational chips.
- Timestamps via `timeAgo(...)` / `fmtAbsolute(...)`; bytes via `bytes(...)` (all from `components/ui`).

## Create / edit — use a modal
- Prefer a modal over an inline draft panel. Build it with the shared modal CSS:
  `modal-backdrop > modal-panel > (`.spread` header) + `modal-body` + `modal-foot``. The backdrop closes on
  click; `stopPropagation` on the panel. Header has the title + a `btn ghost sm` close (`<Icon name="x" size={14}/>`).
  `modal-foot` holds Cancel (`btn ghost sm`) + the primary Save/Create (`btn primary sm`), right-aligned.
- **Multi-type creates (e.g. service objects): pick the type first, then collect details in the SAME modal.**
  Keep a `picking` boolean + a `draft` object: "New" → `setPicking(true)` shows a grid of type cards
  (`<button className="card">`), choosing one builds the draft (`setPicking(false)`); the details form then
  renders in the modal body. A "Back" button (`marginRight:"auto"`) returns to the picker on new (not edit).
- For SIMPLE forms with a fixed field set, prefer `formDialog(...)` from `components/dialog.tsx` instead of a
  hand-built modal. Field layout inside a modal uses the `Field` helper + `grid grid-2`.

## Dialogs & safety
- NEVER use native `alert/confirm/prompt`. Use `notify / confirmDialog / promptDialog / formDialog / stepsDialog`
  from `components/dialog.tsx`.
- Every destructive action (`delete`, disable, teardown) MUST go through `confirmDialog({ tone: "danger", … })`.

## Data & API
- Load with the `api` client (`api.get/post/put/del`); wrap in `try/catch` and surface failures via `flash` or
  `notify({ tone:"danger" })`. Re-`load()` after a successful mutation.
- Gate platform-only sections on `me.is_platform_admin`; the router + `ADMIN_SECTIONS` already assume it.

## Icons
- Use `<Icon name=… />` with a valid `IconName`; brand/source icons come from the shared registry
  (`brandForSource`, `SourceIcon`/`BrandIcon`) — never a per-page icon set. Reuse the section's own nav icon
  (from `ADMIN_SECTIONS`) for its primary "New" button where it reads well.

When adding a NEW admin section, copy an existing modern section (e.g. `ServiceObjectsAdmin`,
`ConfigObjectsAdmin`, `BillingAdmin`) rather than a legacy inline-panel one, and register it in both
`ADMIN_SECTIONS` and the `Admin()` switch.
