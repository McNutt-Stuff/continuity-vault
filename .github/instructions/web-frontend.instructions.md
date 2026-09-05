---
applyTo: "web/src/**,site/src/**"
description: "React frontends — the customer portal (web) and public marketing site (site)."
---

# Web frontends (portal + public site)

- **Local TS errors are false positives.** `web/` and `site/` build server-side during deploy; the editor
  often shows "Cannot find module 'react'" / JSX errors because node_modules isn't installed locally. Verify
  logic by reading; don't chase those. `tsconfig` has `noUnusedLocals/Parameters=false`.
- **No native dialogs.** Use `web/src/components/dialog.tsx`: `notify / confirmDialog / promptDialog /
  formDialog / stepsDialog` (promise-based). `<DialogHost/>` is mounted once in `App.tsx`.
- **API:** use the `api` client (`api.get/post/put`); it injects the bearer token and triggers a global 401 →
  Login redirect. Public calls (e.g. `/api/site`) can use raw `fetch` with `credentials:"omit"`.
- **Source/brand icons** go through the single registry `web/src/components/sourceIcons.ts`
  (`brandForSource`, `resolveIconType`) and `SourceIcon`/`BrandIcon`. Aliases (e.g. `outlook_local→outlook`)
  live there and in the backend mirror `cloud/app/source_icons.py`. Never hardcode a per-page icon set.
- **Time:** API datetimes are naive UTC; append `Z` before `new Date(...)` (`ui.serverDate` / `fmtAbsolute`).
  Online/liveness checks compare against a `<90s` window.
- **Feature/plan gating:** gate nav + routes on `me.features.<flag>` and `me.can_admin` / plan, matching the
  backend `require_feature` / role checks.
- **Health badges:** pass `dot` to `Pill` only for genuine status badges (online/health/version), not
  informational chips.
- **Public site (`site/`)** has NO backend of its own — content comes from the CP `/api/site` (mirrored to
  `/site.json` + inlined as `window.__ARKIVE_CMS__` by the node heartbeat). Read config (e.g. analytics id)
  from that payload; fall back to bundled defaults offline.
- **Admin** (`web/src/pages/Admin.tsx`) is large — reuse `FilterBar`, `Menu`, `charts.tsx`, terminal-log CSS,
  and node-style tabbed detail patterns rather than inventing new ones.
