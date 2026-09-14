---
applyTo: "cloud/app/integrations/**/*.py"
description: "Managed, admin-governed integrations & org sources (Microsoft 365: SharePoint, Teams, Exchange, OneDrive)."
---

# Managed integrations & organization sources

Managed integrations (Microsoft 365) are **admin-governed** and **app-only** (no per-employee sign-in): an
admin grants Microsoft consent once, Arkive discovers Entra identities + org resources, and the node/CP
collects each user's + the org's data with the app-only Graph token. Config is **control-plane authoritative**
and replicated DOWN to the assigned node; discovery/collection RESULTS (identities, `ManagedSource`,
`last_collected_at`) run where the instance lives and replicate UP for the portal.

## Managed sources are FIRST-CLASS but not user-connectable
A managed source type (e.g. `sharepoint`, `teams`) must look and feel like every other source EVERYWHERE it
appears, while being collected app-only rather than added from the Sources menu:
- **Hidden from the connectable catalog:** add the type to `api/connectors.py` `_MANAGED_ONLY` (dropped from
  `/connectors/catalog`) AND give it `_SOURCE_FAMILY` / `_SOURCE_TYPE` entries so it still categorizes right.
- **Brand icon in BOTH registries + the asset** (so overview, dashboards, insights, reports AND notification
  emails render the real mark): `web/public/source-icons/<type>.svg`, `web/src/components/sourceIcons.ts`
  (`SYNCED_SOURCE_ICONS`), `cloud/app/source_icons.py` (`BRAND_ICON_TYPES`), and `scripts/sync_source_icons.py`.
  Email icons resolve through `source_icons` (`notifications._source_icon_url`) — never a private per-file set.
- **Its OWN Help Center page** under `_SOURCE_PAGES` in `support_defaults.py` via `_source_doc(...)`, flagged
  `required_plan="business"`, describing it as a managed source collected via the Microsoft 365 integration
  (not connectable from the Sources menu). Business/Enterprise gating is enforced server-side too.
- **Register the connector** (`connectors/registry.py`, `auth_type="managed"`, streaming/delta) so its content
  is searchable/recoverable like any source, even though `fetch_objects` is a no-op (collection is app-only).

## User vs org workloads
`collect._WORKLOADS` maps each workload to a `source_type` + `scope`: **user** workloads (exchange, onedrive,
teams_chat) provision one `ManagedSource` per mapped identity into that user's vault; **org** workloads
(sharepoint, teams) discover sites/teams with the Graph token and provision org `ManagedSource`s
(`owner_user_id=None`) into the org vault. Org discovery needs the token, so it runs in the collect path
(`provision_org_sources`), gated by the workload being in the managed profile — a workload the admin didn't
select is never provisioned.

## Observability is MANDATORY — a managed backup is NEVER silent
Every managed collection must be reproducible from the admin **Platform Logs** and visible in the **activity
trail** + **audit ledger**. A silent collect (no counts, no dates, nothing in logs) is a bug.
- Record a result via `collect.audit_cycle(db, inst, objects=, sources=, trigger=)` (action `m365.collected`,
  `category="activity"`) from BOTH the CP `collect-now` thread and the node collect cycle — it dual-writes to
  Platform Logs + the customer activity trail.
- `collect_source` logs an INFO line per source **with the object count**; the collect-now/worker paths log a
  per-instance total; provisioning logs how many sites/teams were **discovered** per workload AND logs when NO
  org workload is in the profile (the usual reason SharePoint/Teams never appear). Log via `cv.integrations.m365.*`
  loggers so the in-process sink captures them on the CP and every node.
- Admin actions (`connect`, consent, `collection_toggled`, `collect_now`, remove) `audit.record(...)` with
  `category="admin"`. Warn (don't swallow) when there's no app token — that means consent/permissions aren't
  effective yet.

## Consent is NOT binary — surface permission health + a re-consent path
`consent_state="granted"` does NOT mean the app-only token carries the granted **Application** roles (a
Delegated-only app returns `roles=[]` → 403 on every call). So:
- On a 403-with-no-roles, `discovery.run_discovery` sets `cred.meta.needs_consent/permissions_ok=false` (and
  clears them + stamps `last_checked_at` on a successful run); `_status_view` exposes `last_error`,
  `permissions_ok`, `needs_consent`, `last_checked_at`.
- The UI must offer **Re-request admin consent** (re-run `oauth/start`, prefilled tenant) AND **Re-check**
  (re-run discovery) even when consent is already "granted", plus a "more consent needed" banner. `_instance_view`
  overlays live managed data (consent, identities, mapped, sources) so the integration card shows real state,
  and `_instance_health` treats identity-based integrations as healthy when identities > 0 (not "empty").

## Federation
Node-hosted tenants: config replicates CP→node; the node worker (`worker.run_due`) discovers + collects and
`node_replication` pushes `ManagedSource` + `ExternalIdentity` back UP (keyed by `updated_at`, so
`last_collected_at` changes propagate). The CP-authoritative M365 API is NOT proxied to the node (see
`api/node_proxy.py`); its config is authored on the CP. Never build the OAuth redirect from a node domain.

## Governance / rules — managed sources use the MAIN rules engine
Managed sources are real `Collection`s, so they are governed by the **main compliance rules engine**
(`rules_engine` + `api/rules.py`), NOT a separate system — a rule binds to a managed source by its
`collection_id` or by `source_type`, and is enforced at ingest in `sync_worker.ingest_objects` like any other
source. When you add a governance surface for managed sources, wire it to the main engine:
- `/rules/options` marks each collection `managed`/`workload`/`instance_id` so the builder can group them; the
  Rules page lists managed sources under a "Microsoft 365 (managed sources)" group.
- `GET /integrations/microsoft365/compliance-rules` returns the rules that apply to an instance's managed
  collections (unscoped, or matching a managed `collection_id`/`source_type`); the workspace's "Compliance
  rules" card renders them and deep-links to the Rules tab.
- The `ManagedRule`/`ManagedRuleVersion`/`ManagedRuleAssignment` models under `microsoft365/` are a SEPARATE,
  not-yet-enforced org-policy framework — do NOT route ingest-time governance through them; use the main engine.

