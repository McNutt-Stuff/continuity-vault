---
applyTo: "cloud/app/workers/**/*.py"
description: "Background workers: scheduler, jobs, replication, pruning, index replication."
---

# Background workers

- The **scheduler** (`scheduler.py`) is a daemon loop; each step is wrapped in try/except so one failure
  can't kill the thread. Add new periodic work as its own `try/except` in the loop calling a dedicated
  function. Interval-gate expensive work (module-level `_last_*` timestamp) so idle cycles are cheap.
- **Run on the assigned node.** In a federated fleet each server's DB only holds its own tenants' data.
  Work that reads/writes a tenant's index/data must run where that tenant lives: the assigned node for
  `tenant.node_id`, the control plane for unassigned tenants. Filter scopes by node ownership
  (see `index_replication._scopes`); don't process another node's tenants.
- **Never block startup.** Heavy one-time jobs (big-table backfills, index builds) run here in the
  background (`CREATE INDEX CONCURRENTLY` via a worker AUTOCOMMIT connection), NOT in `db.py`.
- **Sessions:** use `with SessionLocal() as db:`; commit per unit of work; `db.rollback()` on error before
  continuing. Don't hold a transaction open across a long external call (idle-in-transaction blocks autovacuum).
- **Node→CP propagation:** results a node produces (receipts, documents, jobs, index replicas, insights) are
  pushed to the CP in `node_replication._push`; the CP applies them in `api/node_sync.py`. Add new pushed
  models to both, guarded by a `valid_tenants` check so an orphan row can't abort the whole push.
- **Pruning** (`pruning.py`) bounds high-churn tables; NEVER prune `audit_events` (hash-chained) or
  `search_documents`/`snapshot_receipts` (recovery/history). Free big TOASTed JSON payloads on state
  transition, not via a recurring `col::text <> '{}'` predicate (that detoasts the whole table).
- **Memory:** stream rows (`.yield_per`) and ingest in bounded batches for anything that could be large.- **Durable retry — no write is ever silently lost.** A destination write that fails is recorded in the
  durable queue (`queue_registry.enqueue`) and retried with exponential backoff by `workers/queue.py`
  (`run_due` re-runs the source backup to the single failed destination); success calls `resolve`, and a
  user can force a retry via `queue_registry.retry` after fixing the cause. `sync_worker.ingest_objects`
  enqueues issue-time failures and `resolve`s on success. When adding a new write path (a new destination
  kind, or an async accept-then-write like appliance ingest), it MUST enqueue on failure and resolve on
  confirmed success. VERIFY the bytes landed (size check) before treating a write as successful.
- **Source-failure detail is a STANDARD** (troubleshoot from admin Platform Logs). A connector/source sync
  failure MUST run through `sync_worker._record_sync_error` (sets `last_error`/`last_error_at`/`fail_count`,
  needs-reauth on auth errors, and `audit.record`s a tenant-attributed LogEntry that feeds `source_problem`
  notifications). `_normalize_sync_error` captures the exception + HTTP status + a bounded response snippet
  from `exc.response`, so RAISE errors that preserve `.response` (`raise … from exc`) rather than restringing
  them. When you record a source event directly, pass `audit.record(..., detail={type, account, error,
  reason/code, <op context>})` — `audit.record` folds those into the Platform-Logs message + carries the full
  detail in `meta`. Never swallow, never log secrets. See `connectors.instructions.md` → “Logging & error
  detail — STANDARD”.
