---
applyTo: "cloud/app/support_defaults.py"
description: "Keeping the Help Center (support docs) in sync with platform changes."
---

# Support documentation — package updates for admin review

The public Help Center is defined by `DEFAULT_SUPPORT_DOCS` / `DEFAULT_SUPPORT_SECTIONS`
in `cloud/app/support_defaults.py`. These are the platform-authored baseline; admins
publish and (optionally) edit them via **Admin → Documentation**.

## When you change the platform, update the docs
Whenever a change affects user-facing behavior (new source/connector, new page, renamed
feature, changed flow, new setting), update the matching page here in the SAME change:
- Edit the relevant `_doc(...)` `body`/`summary` (bodies are Markdown).
- Add a new `_doc(...)` for a genuinely new feature; add a section to
  `DEFAULT_SUPPORT_SECTIONS` only if none fits.
- Wire contextual help via `help_routes` (portal Help icon deep-links to the page).

## How it ships — a reviewable package, never a blind overwrite
The admin opens **Documentation → "Review & publish updates"**, which previews a package
(`POST /admin/support/seed-updates?preview=true`) grouped as:
- **New** — pages that don't exist yet,
- **Refreshed** — default pages whose baseline changed (with the changed field list + a
  content preview),
- **Your edit kept** — admin-customized pages that are PRESERVED unchanged.

Publishing (`POST /admin/support/seed-updates`) applies the package. The `baseline_hash`
on each `SupportDoc` guards admin edits: a page an admin changed is never clobbered
(unless `force`). There is **no separate "Seed defaults" step** — first-publish and
incremental updates both go through Review & publish updates, so we never overwrite once
seeded.

Published docs mirror to the Public Web Node on the next heartbeat (`heartbeat.py` →
`support.json`).

## Rules
- Keep `_doc(...)` spec keys limited to real `SupportDoc` columns (slug, title, section,
  section_order, nav_order, icon, summary, body, help_routes, published) — a stray key
  makes `SupportDoc(**spec)` raise.
- The publish endpoint is wrapped (rollback + `cv.support` logging) so any failure is
  surfaced in Platform Logs and returns a clear message — don't swallow errors here.
