---
applyTo: "cloud/app/models.py,cloud/app/db.py"
description: "Adding or changing SQLAlchemy models and database migrations."
---

# Models & database migrations

- **A new column on an EXISTING table requires TWO edits:**
  1. Add the `Column(...)` to the model in `models.py`.
  2. Add `"ALTER TABLE <table> ADD COLUMN IF NOT EXISTS <col> <TYPE> [DEFAULT ...]"` to the
     `statements` list in `db.py` `_apply_additive_migrations`.
  Shipping only the model change → prod 500s `column ... does not exist`, because
  `Base.metadata.create_all` only CREATES NEW TABLES.
- **A whole new table** is auto-created by `create_all` — NO `db.py` change needed.
- Keep additive migrations **cheap and metadata-only** (constant DEFAULT, no table rewrite). NEVER run a
  full-table UPDATE/DELETE or a big-table index build here — it blocks startup → the deploy health check
  times out → rollback. Do heavy one-time work in `workers/scheduler` (background, `CREATE INDEX CONCURRENTLY`).
- **Naive UTC everywhere.** `Column(DateTime)` is TIMESTAMP WITHOUT TIME ZONE on Postgres. Default with a
  naive-UTC `_now`. Compare against `datetime.now(timezone.utc).replace(tzinfo=None)`, never a tz-aware value.
- Primary keys are `String` UUIDs (`default=_uuid`). FKs are `ForeignKey("table.id")`; index hot-queried FKs.
- Big optional JSON/text columns on hot-queried tables must be `deferred()` so routine queries don't load
  them (see `DesktopAgent.last_scan`). Never store unbounded per-row JSON on a hot table.
- New model that a federated node produces and the portal must show → add it to the node→CP push
  (`workers/node_replication._push` + `api/node_sync.py` PushPayload + handler), keyed by a stable
  business key when the row id differs per DB.
