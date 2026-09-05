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
- **All logs MUST be viewable from the control plane (one place).** Logs are a first-class product surface,
  not an afterthought. Everything (cloud, nodes, appliances, endpoint agents, connectors/integrations, auth,
  user actions, audits) funnels into the unified `log_entries` table via `cloud/app/logsink.py`:
  (1) app loggers (`cv.*`/`arkive.*`) are captured by the in-process sink + flusher (runs on the CP AND every
  customer-tenant node); (2) appliance/agent `recent_logs` are ingested on heartbeat (`logsink.ingest_device_logs`);
  (3) `audit.record` dual-writes a LogEntry so auth/user-actions/audits appear too. Customer-tenant nodes PUSH
  their `log_entries` to the CP every ~30s inside `workers/node_replication._push` — the `logs_cursor` advances
  ONLY on confirmed delivery, so an undeliverable batch is retried whole and never dropped. The admin
  **Platform Logs** page (`GET /api/admin/logs` + `/logs/facets`) is the one-stop viewer (unified-search-styled
  filters, default warn/error, tenant/node/appliance scoping, drill-down from node/appliance cards). When you
  add a new component or failure path, make sure its logs reach this store (log via a `cv.*` logger, or emit
  via `logsink.emit(...)`), and record `Node.last_log_push_at` visibility. Prune keeps info/debug 7d, warn+ 30d.
- **Granular error detail is a STANDARD for sources/connectors/integrations.** Every source/connector/
  integration failure must be triageable from the admin **Platform Logs** WITHOUT shell access: record the
  HTTP status code, the provider's error code/reason (`invalid_grant`, `quotaExceeded`, …), a bounded
  response-body snippet (~200 chars), and the operation context (endpoint, account, folder/cursor/object id) —
  never the full payload, never secrets (tokens/passwords/`Authorization`). RAISE errors that preserve
  `.response` (`raise … from exc`) so `sync_worker._normalize_sync_error` captures the status+snippet, and
  route them through `sync_worker._record_sync_error` (→ `last_error`/`fail_count`/needs-reauth +
  tenant-attributed audit LogEntry + `source_problem` notification). When emitting an event yourself, pass
  `audit.record(..., detail={type, account, error, reason/code, <op context>})` — those keys are folded into
  the log message and the full detail into `meta`. Details: `.github/instructions/connectors.instructions.md`
  → “Logging & error detail — STANDARD”. A bare `except: return []` or a terse `"failed"` log is a bug.
- **Durable, resumable writes — NEVER silently lose a backup/sync/write.** Every write to a destination
  (cv-cloud, customer-s3, byos, appliance store, appliance vault) MUST either succeed, or be recorded for
  automatic retry AND allow a manual re-try after the issue is resolved. Use the durable queue
  (`cloud/app/queue_registry.py` → `enqueue`/`resolve`/`retry`, drained with backoff by
  `workers/queue.py`). A destination that fails at ISSUE time is enqueued by `sync_worker.ingest_objects`;
  a destination that ACCEPTS then fails (e.g. an appliance ingest that errors in `command-result`) must
  also `enqueue` a retry. VERIFY every write landed (byte-size check after write; the appliance vault and
  `LocalFsDestination` do this) — a truncated/failed write must raise, never be sealed as recoverable.
  Never mark a snapshot recoverable unless its bytes are confirmed written.


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
