# Microsoft 365 Managed Integration Implementation Roadmap

> Tracks delivery of the M365 Managed Integration spec
> (`ARKIVE_M365_MANAGED_INTEGRATION_SPEC.md`) and the packaged-integration
> architecture. Update this file as slices land.

## Current summary
- Overall status: In progress (Phase 0 + architecture foundation)
- Current phase: Phase 1 — Platform primitives (started)
- Last updated: 2026-09-13
- Owner: (assign)

## Repository findings (gap analysis)
| Area | Existing component/path | Reuse decision | Gap |
|---|---|---|---|
| Integration registry | `cloud/app/integrations/base.py`, `__init__.py` | Reuse + extend | Was a flat registry; now auto-discovers self-contained packages |
| Integration spec/metadata | `IntegrationSpec` | Extended | Added version/status/plans/capabilities/ownership_models/managed/workspace/feature_flag |
| Integration API | `cloud/app/api/integrations.py` | Reuse | Monolithic; per-package control-plane modules to be split out in later phases |
| Entitlement | `cloud/app/features.py` (`resolve`), `security.require_feature` | Reuse | Added `m365_managed_integration` flag + plan gate in catalog/create |
| Plan | `Tenant.plan` | Reuse | Plan-based capability gate applied to specs (`plans`, `min_plan`) |
| Sources model | `Collection`, `ConnectorAccount` | Extend later | No `ownership_type=organization`, no managed sources/credentials yet |
| Nodes / federation | `Node`, `api/node_proxy.py`, `workers/node_replication.py` | Reuse | Need signed desired-state envelope + node validation for M365 commands |
| Credentials | `credstore` | Reuse | Need managed-credential references (node-only, never reusable in CP) |
| Docs | `support_defaults.py` | Reuse | Added `integration-microsoft365` stub |
| Frontend integrations | `web/src/pages/Integrations.tsx` | Reuse | Catalog now shows managed/plan/preview state; M365 workspace UI TBD |

## Decisions and ADRs
| Decision | ADR/path | Status | Consequence |
|---|---|---|---|
| Integrations are self-contained packages, auto-discovered | this doc / code | Implemented | New integration = new dir under `cloud/app/integrations/`, no core edits |
| M365 ships gated (`status=coming_soon`, plan+flag) until backend lands | code | Implemented | Card visible but setup blocked server-side (fail-closed) |
| Default Entra app = Arkive multi-tenant; Enterprise = customer-owned | spec §16 | Proposed | No per-customer secret in default flow |
| Payloads/plaintext-index/reusable creds/private keys never in control plane | spec §15/§17 | Invariant | Enforced in all later slices |

## Milestones
| ID | Milestone | Status | Exit evidence |
|---|---|---|---|
| P0 | Repo discovery + roadmap + gap analysis | Done | This file |
| P1a | Packaged-integration SDK/registry + auto-discovery | Done | `integrations/__init__._discover`, `ubiquiti/` + `microsoft365/` packages |
| P1b | Entitlement (plan + flag) + fail-closed create guard | Done | `_spec_entitlement`, create 403/409 |
| P1c | Managed credential/source/org-source/mapping/rule models | Todo | Migrations + tests |
| P1d | Desired-state federation envelope + node validation | Todo | Signed command + node reject tests |
| P2 | M365 connection + Entra identity (OAuth, discovery, mapping) | Todo | Pilot tenant connect/scope/map |
| P3 | Core managed protection (Exchange/OneDrive) | Todo | End-to-end recovery test |
| P4 | Organization collaboration (SharePoint/Teams) | Todo | Design-partner validation |
| P5 | Compliance packs + security-source evidence | Todo | Evidence/privacy review |
| P6 | Broader Microsoft business sources + customer-owned app | Todo | Per-module gates |

## Requirements traceability (initial)
| Requirement/AC | Code | Tests | Docs | Status | Notes |
|---|---|---|---|---|---|
| ENT-001 (no P/F access to managed APIs) | `_spec_entitlement`, create guard | Todo | roadmap | Partial | Catalog + create gated; full managed API surface pending |
| ENT-002 (downgrade suspends, not deletes) | — | Todo | — | Todo | Needs managed objects + downgrade handler |
| PKG-001 (package failure isolation) | `_discover` try/except per package | Todo | — | Partial | One bad package can't break others |
| OAUTH-001/002 | — | — | — | Todo | Phase 2 |
| FED-001 | — | — | — | Todo | Phase 1d |
| DATA-001 | design invariant | — | — | Todo | Enforced as slices land |

## Remaining work
- [ ] P1c data model (spec §19) + additive migrations
- [ ] P1d federation desired-state envelope + node-side validation
- [ ] P2 OAuth admin-consent flow (Arkive multi-tenant app), Entra discovery, identity mapping, org-management Microsoft indicator
- [ ] P3 managed sources + mapping/rule compiler + Exchange/OneDrive collectors (customer node)
- [ ] P4 organization sources (SharePoint/Teams), custodians
- [ ] P5 compliance pack engine + Microsoft evidence collectors
- [ ] P6 broader sources + enterprise customer-owned app + direct restore

## Known gaps and deviations
| Requirement | Gap/deviation | Reason | Resolution plan |
|---|---|---|---|
| Full M365 collection | Not implemented | Multi-phase; foundation first | Phases 2–6 |
| Per-package control-plane API modules | API still in `api/integrations.py` | Avoid destabilizing core in one pass | Extract per package during Phase 2 |

## Validation commands
```text
python3 -m py_compile cloud/app/integrations/**/*.py cloud/app/api/integrations.py cloud/app/features.py
# web: tsc -b && vite build   (run on the build host)
```

## Release readiness (M365 GA — all Todo)
- [ ] Security review  - [ ] Privacy review  - [ ] Threat model
- [ ] Tenant-isolation tests  - [ ] Plan-entitlement regression tests
- [ ] Scale/throttling tests  - [ ] Upgrade/rollback tests  - [ ] Recovery drill
- [ ] Support documentation  - [ ] Operations runbooks  - [ ] Feature flag + rollback
