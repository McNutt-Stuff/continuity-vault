---
applyTo: "cloud/app/placement.py,cloud/app/api/node_sync.py,cloud/app/api/node_proxy.py,cloud/app/workers/node_replication.py,cloud/app/services.py"
description: "Federation topology + active/passive tenant HA — standby replication, switchover, auto-failover, maintenance window."
---

# Federation & tenant HA (active / passive)

Arkive is federated: the **control plane (CP)** holds the fleet-wide catalog (tenants, users, nodes,
billing, config) and every **customer-tenant node** runs its OWN Postgres + search index and holds the
tenant's data, keys and storage credentials. Nodes replicate to the CP over HTTPS. Object BYTES live in
shared cloud storage (both nodes read the same bucket) or on appliances/agents (which re-target their node
on the next heartbeat) — the node DB holds *metadata* (receipts, search docs, config, keys), not the bytes.

## Topology model

```
                    ┌──────────────── Control Plane (authoritative catalog) ────────────────┐
                    │  Tenant.node_id = ACTIVE node   Tenant.standby_node_id = PASSIVE node  │
                    └───────┬───────────────────────────────────────────────┬───────────────┘
             pull (config, keys, standby data)                 push (data: receipts, search docs, logs)
                    ▼                                                         ▲
        ┌───────────────────────┐   warm replication (config+keys+       ┌───────────────────────┐
        │  Node A  (ACTIVE)     │   receipts+search index via cursors)    │  Node B  (STANDBY)    │
        │  runs workers,        │ ──────────────────────────────────────► │  warm passive replica │
        │  serves file ops      │                                         │  scheduler skips it   │
        └───────────┬───────────┘                                         └───────────────────────┘
                    │ heartbeat returns node_url (services.tenant_node_url → Tenant.node_id)
                    ▼
        appliances / desktop agents  ── retarget to the ACTIVE node automatically each heartbeat
```

- **`Tenant.node_id`** = the ACTIVE node (runs the tenant's scheduler/collectors, serves its file ops).
- **`Tenant.standby_node_id`** = a warm PASSIVE replica (nullable; most tenants have none → all HA code
  is a no-op). Kept in sync automatically.
- **`Tenant.placement_state`** = `""` | `"switching"` (brief window during a switchover) | `"degraded"`.
- **`Tenant.switchover_at`** = last switchover time (anti-flap cooldown + grace-window clearing).

New columns on the existing `tenants` table → they have BOTH a model field AND an additive
`ALTER TABLE tenants ADD COLUMN IF NOT EXISTS …` in `db.py::_apply_additive_migrations`.

## Warm-standby replication

The standby holds everything it needs to become active INSTANTLY except the object bytes (which are in
shared storage or on devices). Replication is additive on top of the existing pull/push:

- **`api/node_sync.py::pull`** — the CP builds a node's bundle. It now selects **active** tenants
  (`node_id == node.id`) AND **standby** tenants (`standby_node_id == node.id AND node_id != node.id`).
  Config (tenants/users/vaults/keys/…) is sent for BOTH sets so the standby is warm. Additionally it sends
  incremental **`standby_receipts`** (`SnapshotReceipt`) + **`standby_documents`** (`SearchDocument`) for the
  standby tenants, filtered by two independent cursors (`standby_rcpt_cursor` / `standby_doc_cursor`),
  ordered `created_at asc`, `LIMIT 3000` each, with a `standby_more` flag to page.
- **`workers/node_replication.py::_pull`** — the node sends its `standby_*_cursor`s in the pull body, applies
  config, then upserts the `standby_receipts`/`standby_documents` (each in a savepoint) and advances the
  cursors in `_read_state()` only on success.
- **`workers/node_replication.py::_push`** — a node pushes ONLY data it OWNS (`tenant_id in owned_tids`
  where `owned_tids = Tenant.node_id == self_node.id`). This stops a standby node from echoing another
  tenant's replicated receipts/docs back to the CP. No-op for non-HA nodes.
- **Scheduler auto-skips standbys**: the standby's scheduler filters `Tenant.node_id == self`, so a standby
  tenant (whose `node_id` points at the OTHER node) is never double-collected. This is why the design is safe.

## Truthful readiness (never a false "in sync")

A pull happening is NOT proof the data applied. The standby node reports its ACTUAL apply health back on the
next pull (`NodeIdent.standby_report`): per-tenant `pending` (rows that failed to apply) + `caught_up` (the
receipt/index stream had no more pages). The CP records it via `node_sync._record_standby_report`:

- **`Tenant.standby_synced_at`** — last time the standby confirmed a CLEAN, caught-up apply.
- **`Tenant.standby_pending`** — rows still failing to apply (>0 = the replica is INCOMPLETE).

`placement.is_standby_ready(tenant)` = `standby_synced_at is not None AND standby_pending == 0`. This gates
everything: `can_switch` refuses a manual switchover to an unready replica, and auto-failover **skips** an
unready standby (the object bytes are safe in shared storage, so switching to an empty/partial index would
only show the customer a broken view). `set_standby` clears readiness so a freshly-assigned standby shows
"syncing" until the node confirms. The admin UI (topology pair + TenantDetail + node detail) shows the real
state — `in sync` only when ready, else `syncing · N pending`. **Never surface "synced" from `last_sync_at`
alone** — that only means the node pulled, not that the tenant's rows landed.

## Switchover (`placement.py`)

`switchover(A→B)` is a **metadata flip** — no data movement, because the standby is warm:

```
node_id = B (was standby)     standby_node_id = A (old active)     placement_state = "switching"
```

- The old active becomes the new warm standby → a tenant can ping-pong between two nodes freely.
- `set_standby(db, tenant, node_id, actor=…)` validates the node is an existing `customer-tenant` with an
  endpoint and ≠ the active node. Clearing = pass `None`.
- `can_switch(tenant)` guards: must have a distinct standby and not be inside the anti-flap cooldown
  (`SWITCH_COOLDOWN_SECONDS = 300`). `force=True` (auto-failover) skips the cooldown but still requires a
  standby.
- Everything is **audited** (`tenant.standby_set`, `tenant.switchover`, `category="admin"`,
  `severity="warning"`) → visible in Platform Logs.

**Admin API** (`api/admin.py`): `PUT /admin/tenants/{id}/standby` (`{node_id|null}`) and
`POST /admin/tenants/{id}/switchover`. `_tenant_view` exposes `standby_node` (with `online`),
`standby_node_id`, `placement_state`, `switchover_at`. UI: TenantDetail → Settings → "High availability".

## Device / endpoint / appliance retargeting

No client config change is needed. Appliance + desktop-agent heartbeats return
`services.tenant_node_url(db, tenant_id)` which reads `Tenant.node_id`. Because switchover flips `node_id`,
every device **retargets to the new active node on its next heartbeat** — signalling, commands, backup and
ingest all follow automatically. Portal file ops are proxied CP→node by the same lookup.

## Maintenance window (seamless customer experience)

While `placement.is_switching(tenant)` is true (≤ `SWITCH_GRACE_SECONDS = 45`), `api/node_proxy.py`
returns a **`503 {"maintenance": true, "retry_after": …}`** for proxied file ops instead of routing
mid-migration. The portal (`web/src/api.ts::setOnMaintenance` → `auth.tsx`) shows a brief, auto-dismissing
"Brief maintenance" dialog rather than an error. The scheduler clears the switching state once the grace
window elapses (`placement.clear_stale_switching`).

## Automatic failover

`workers/scheduler.py::_check_node_health` detects offline nodes (heartbeat gap ≥ `_NODE_OFFLINE_SECONDS`).
For each tenant on a downed node, `_auto_failover_node` promotes the standby **only when**:

1. the tenant has the **`ha_auto_failover`** feature flag enabled (`Tenant.feature_flags`, OFF by default —
   manual switchover always works regardless), AND
2. the standby node is itself healthy (recent heartbeat).

`switchover(..., force=True, reason="node-offline")` flips `node_id`; because the tenant's `node_id` now
points at the standby, it is naturally **one-shot per episode** and never flaps. If the old node returns it
comes back as the (warm) standby — there is no automatic switch-back.

## Rules when touching this area

- **Additive + no-op by default.** A tenant with no standby must behave exactly as before. Never assume a
  standby exists.
- **DateTime is naive UTC** — use `placement._now()` (`datetime.now(timezone.utc).replace(tzinfo=None)`),
  never compare a stored value to an aware `datetime`.
- **Authorize/serve on the node that owns the tenant.** Anything reading/writing a tenant's data or index
  must run on its ACTIVE node (`Tenant.node_id`). The standby only receives replicated metadata.
- **Never echo replicated data.** A node pushes only `Tenant.node_id == self` data (`owned_tids`). Adding a
  new replicated entity to the standby bundle? Scope its push the same way.
- **Log to Platform Logs.** Placement/failover events log via `cv.placement` / `cv.nodeproxy` and audit
  records — a switchover or a skipped failover must be triageable from the CP without shell access.
- **Feature-flag unattended actions.** Automatic failover is gated by `ha_auto_failover`; manual admin
  actions are not. Never auto-move a tenant a customer/admin didn't opt into.
