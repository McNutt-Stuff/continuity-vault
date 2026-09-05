# Continuity Vault / Arkive — Copilot instructions

Arkive is a quantum-safe backup platform: a **cloud** control plane + customer-tenant
nodes (FastAPI + Postgres + React portal), a **public marketing site**, a **desktop
agent** (macOS endpoint collector), and an **on-prem appliance agent**. Data is
client/server-encrypted; storage holds only ciphertext.

## Golden rules (apply everywhere)
- **Implement, then verify.** After editing, run a syntax/type check (`python3 -c "import ast; ast.parse(...)"`
  for Python; `get_errors` for TS). Web builds server-side — local "Cannot find module 'react'" TS errors
  are false positives.
- **New DB column on an EXISTING table needs BOTH** the model change AND an additive
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...` in `cloud/app/db.py` `_apply_additive_migrations`.
  `Base.metadata.create_all` ONLY creates NEW tables — it never alters existing ones. New TABLES need no ALTER.
- **Never** put heavy data migrations (full-table UPDATE/DELETE, index builds on big tables) in the
  synchronous startup path (`db.py`). Those go in a background worker (see `workers/scheduler._ensure_perf_indexes`).
- **Postgres DateTime columns are naive.** Comparing a stored value to `datetime.now(timezone.utc)` raises.
  Use naive UTC helpers: `datetime.now(timezone.utc).replace(tzinfo=None)`. SQLite (dev) hides this.
- **Federation-aware.** Customer nodes run their OWN Postgres + search index and replicate to the CP over
  HTTPS. File operations (search/retrieve/recovered/restore/fs) are proxied CP→node by `api/node_proxy.py`.
  Anything that reads/writes a tenant's data or index must work on the node that owns the tenant.
- **Secrets never go through the model.** Don't log credentials. Don't route passwords through tools.
- **Ensure logging is verbose at every level** enasure logs for appliances, endpoints and nodes are detailed and catch info, debug error and warnings and save to the right place. 

## Deploy loop
- Push, then `sudo /opt/arkive-src/updater/git-update.sh cloud` on the control plane. A failing WEB build
  silently rolls back the whole deploy (including backend fixes) — keep TS building.
- Customer nodes + public-web self-update from the CP bundle; appliances + desktop agents self-update from
  their bundles. Only systemd unit / plist changes need an installer re-run.

## Where things live
- `cloud/app/api/*.py` — routers (double-dot relative imports: `from ..models import X`).
- `cloud/app/workers/*.py` — scheduler, jobs, replication, pruning, index replication.
- `cloud/app/connectors/*` — source connectors (registry + live fetchers + oauth).
- `cloud/app/models.py` — SQLAlchemy models; `db.py` — engine + additive migrations.
- `web/src` — customer portal (React+Vite). `site/src` — public marketing site.
- `desktop-agent/agent` — macOS endpoint agent. `appliance/agent` — on-prem appliance agent.
- `infra/systemd` — unit files (must be listed in the relevant bundle to ship).

## Debugging production
- Debug API (key-gated, read-only SQL): `python3 scripts/arkive_debug.py --base https://vault.arkive.life --key dbg_... query`
  or curl `POST /api/debug/query` with `X-Debug-Key`. Write SQL to a file and use `curl --data @file`
  (shell single-quotes break inline `-d`).

## Component-specific rules
See `.github/instructions/*.instructions.md` for cloud API, models/db, connectors, workers,
desktop agent, appliance agent, and the web frontends. Read the one matching the files you're editing.
