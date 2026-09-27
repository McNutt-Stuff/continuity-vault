# Arkive Signal Platform + AI Compliance — Implementation Map

> Living document. Tracks how the two-phase spec
> (`ARKIVE_SIGNAL_AI_TWO_PHASE_SPEC.md`) maps onto the ACTUAL Arkive repository —
> what is reused, extended, refactored, or newly created, and in which phase.

## Non-negotiable principle
**COLLECT ONCE · NORMALIZE ONCE · CORRELATE ONCE · REUSE EVERYWHERE.**
Signals are normalized from data the existing collectors already retrieve — never a
second vendor API call. Signals are shared Arkive infrastructure, not Compliance- or
AI-owned data (see ADR `docs/adr/0001-signals-are-shared-infrastructure.md`).

## Capability → component map

| Capability | Current component / path | Reuse / Extend / New | Phase |
|---|---|---|---|
| Signal store | NEW `cloud/app/models.py` (`Signal`, `SignalDefinition`, `SignalProviderHealth`) | New | 1 |
| Signal engine (emit/dedup/freshness/confidence) | NEW `cloud/app/signals/` | New | 1 |
| Signal taxonomy (categories/severity) | NEW `cloud/app/signals/taxonomy.py` | New | 1 |
| Provider framework | NEW `cloud/app/signals/providers/` on the existing integration registry patterns | New (reuses integration conventions) | 1 |
| Arkive-native signals (protection/recovery/source/node health) | `models.py` Collection/SnapshotReceipt/Appliance/Node + `api/appliances.py` telemetry | Reuse → normalize | 1 |
| M365 identity/device/OAuth signals | `cloud/app/integrations/microsoft365/` (existing discovery) | Reuse → normalize | 1 |
| Ubiquiti network/device/client signals | `cloud/app/integrations/ubiquiti/` + `NetworkClient/NetworkApp/NetworkSample` | Reuse → normalize | 1 |
| Endpoint posture + app inventory | `desktop-agent/agent/` telemetry + `api/agents.py` | Extend (telemetry) → normalize | 1 |
| Appliance/node operational signals | `api/appliances.py` `_sync_storage_telemetry`, `Node` | Reuse → normalize | 1 |
| Entity correlation + asset reconciliation | `User`/`DesktopAgent`/`NetworkClient`/M365 identities | New service over existing entities | 1 |
| Protection-gap analysis | Collection/SnapshotReceipt + Insights | New over existing | 1 |
| Findings | NEW generic `Finding` (Signals→Findings) | New (generalized; Compliance can adopt) | 1 |
| Evidence | `cloud/app/compliance/` evidence primitives | Reuse/link | 1/2 |
| Coverage | NEW derived from provider health + entity counts | New | 1 |
| Provider health | NEW `SignalProviderHealth` + integration `last_*` | New + reuse | 1 |
| Signals Explorer UI | `web/src/pages/` + `components/ui.tsx` | New page | 1 |
| Feature flag | `cloud/app/features.py` FLAGS/LABELS | Extend | 1 |
| RBAC | `cloud/app/security.py` roles | Extend (signals.read via role/flag) | 1 |
| Audit | `cloud/app/audit.py` `record()` | Reuse | 1 |
| Notifications | `notifications.py` / `admin_notifications.py` | Reuse | 1 |
| Federation | `workers/node_replication.py` `_PULL_ORDER` + `api/node_sync.py` push | Extend (signals push node→CP) | 1 |
| Docs | `cloud/app/support_defaults.py` | Extend | 1 |
| Marketing | `site/src/` | Extend | 1/2 |
| Compliance consumption | `cloud/app/compliance/providers.py` | Extend (a signal evidence provider) | 1→2 |
| AI catalog / detection / inventory / MCP / governance | NEW `cloud/app/signals/ai/` (Phase 2) | New on Phase 1 signals | 2 |
| AI entitlement + `AI_COMPONENT` flag | `features.py` + entitlements | Extend | 2 |

## Build order (Phase 1)
1. Docs: this map + ADR.
2. Models: `Signal`, `SignalDefinition`, `SignalProviderHealth`, `Finding` (+ db migrations).
3. Engine: taxonomy, fingerprint/dedup, freshness, confidence, `emit()`.
4. Provider framework + registry + capabilities.
5. Providers: Arkive-native, endpoint, appliance/node, M365, Ubiquiti (normalize existing data).
6. Ingestion + query API (`/signals`) + provider-health API.
7. Feature flag + RBAC + audit + notifications.
8. Entity correlation + asset reconciliation + protection-gap analysis.
9. Signals Explorer UI + integration-card capabilities + Insights wiring.
10. Coverage + provider-health UI.
11. Compliance consumption (signal evidence provider).
12. Docs (Help Center) + marketing + tests.

## Regression guardrails
Existing backups/restores/sources/federation/M365/Ubiquiti/endpoint/Compliance/
dashboards/licensing/billing/admin must keep working. New signal collection is
additive to existing sync — no duplicate polling, no new credentials. New permissions
degrade gracefully (missing scope surfaced, existing features preserved).
