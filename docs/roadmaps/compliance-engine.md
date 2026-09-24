# Compliance engine — roadmap

> Platform-level, capability-driven compliance posture that Arkive core + its
> integrations drive. Feature-flagged (`compliance_enabled`), Business/Enterprise,
> org-admin. Code: `cloud/app/compliance/`, `cloud/app/api/compliance.py`,
> `web/src/pages/Compliance.tsx`. Extension rules: `.github/instructions/compliance.instructions.md`.

## Delivered (preview)
- Capability registry (22 capabilities — incl. conditional access, DLP, external
  sharing control, data residency, version history, offsite copy, air-gapped copy)
  + frameworks **NIST CSF 2.0, CIS v8, HIPAA, ISO/IEC 27001:2022, SOC 2, GDPR**.
- Evidence providers: **Arkive core** (encryption, immutability, backup coverage,
  recovery, access control, MFA/passkeys, audit logging, monitoring, retention,
  legal hold, classification/minimization from rules, inventory, incident response)
  and the **Microsoft 365 driver** (managed-source coverage/health).
- **Reusable posture signals** (`compliance.signals` + `register_refresher` +
  generic `integration_signals` provider): integrations record live posture that the
  engine surfaces without framework changes. **M365 posture collector**
  (`microsoft365/posture.py`) evidences MFA registration, conditional access,
  external sharing and data residency from Graph; DLP is tracked as manual
  attestation. Missing Graph permissions degrade to an actionable `unknown` signal.
- Engine: best-status-per-capability → control state + score; manual overrides;
  time-boxed audited exceptions; evidence trail; **posture snapshots** (trend) and a
  **change ledger** (what drove each change).
- API + UI (posture ring, framework enable + scores, controls with evidence
  drill-down, overrides, exceptions, score trend + history). Nav + feature flag.
- **Per-framework dashboard** (`/compliance/:framework`): score + adherence-over-time
  trend, controls, drivers (provider health), open issues, and the specific
  troubling accounts/systems (from evidence `detail.entities`).
- Rules relabelled "data governance" (drives compliance; not the assessment).

## Delivered — scope-aware assurance (2026-09)
> Moved the engine from best-status-wins to **coverage-based, scope-aware** scoring
> and fed it real M365 evidence. Commits `b1ade1d`, `bd824df`, `672b1a7`, `9c00084`,
> `7b6aff6`, `78c1898`, `edfdd0a`.
- **Evidence contract**: `ComplianceSignal` gained scoped columns (expected/covered/
  failed populations, `evidence_level`, `evidence_ref`, `policy_version`, `expires_at`,
  `entities`, `remediation`); `signals.record`/`is_expired`/`effective_status`/
  `latest_at(per scope)`.
- **Scope-aware engine** (`SCORING_VERSION="2.0-scope-aware"`): control state/score
  from coverage (covered/expected populations; synthetic pop=1 for binary; `none`=
  excluded, `unknown`/`expired`=penalized 0, population=coverage). Coverage rollup on
  snapshots. `unknown`/`expired` never counts as met.
- **Federation**: nodes push `compliance_signals` UP; posture runs on the owning box;
  the CP no longer clobbers node-collected signals.
- **New capabilities (30 total)**: `coverage_completeness`, `backup_freshness`,
  `phishing_resistant_mfa`, `privileged_mfa`, `privileged_access_review`,
  `guest_access`, `integrity_verified`, `restore_test` — mapped into each framework's
  backup/recovery/access/sharing controls.
- **M365 driver**: local coverage/freshness reconciliation (expected in-scope sources
  vs active + recoverable; 48h freshness that expires); split MFA into registration /
  phishing-resistant / admin coverage; guest governance; scope-aware CA + residency —
  all with permission-named `unknown` fallbacks (no new Graph permission required).
- **Restore/integrity from receipts**: `integrity_verified` (hybrid-signed manifests)
  + `restore_test` (index replicas fetched, decrypted and row-count-verified).
- **UI**: framework page shows per-control coverage num/den + bar, evidence-level
  badges, and stale/freshness — alongside the troubling accounts/systems table.
- **Docs**: canonical `docs/integrations/microsoft365-permissions.md` + Help Center.

## Next (roadmap)
| Item | Notes |
|---|---|
| More frameworks | PCI DSS, CMMC — registry entries + control→capability maps. |
| Scheduled auto-evaluation | A worker that re-evaluates + snapshots on a cadence (currently on-demand + on enable), so the trend fills without manual re-assess. |
| Deeper M365 evidence | Retention labels + litigation/legal hold are **not reachable via app-only Graph v1.0** (like Purview DLP) — left to Arkive-core evidence, not faked. Mailbox audit + DLP hits pending a stable Graph surface. |
| More integration drivers | Ubiquiti (network segmentation/logging), desktop agent (endpoint encryption/backup), appliance (offline/immutable copy) as compliance drivers. |
| Evidence export | Downloadable auditor report (PDF/CSV) per framework: controls, states, evidence, exceptions, history. |
| Control → source scoping | Per-control applicability + scoping (e.g. HIPAA only over vaults tagged ePHI). |
| Benchmarking | Compare a tenant's score to an anonymized peer baseline (opt-in). |
| Remediation guidance | Actionable "how to move this control to met" links (enable a workload, add a rule, enrol passkeys). |
| Custom frameworks | Admin-defined control sets mapping to existing capabilities. |
| Notifications | Alert on score regression / control moving to failed / exception expiry. |
| Integration-embedded view | A compact "Compliance assurance" card inside the M365 integration detail (posture surfaces on the platform Compliance page today). |

## Deviations / notes
- The earlier **M365-scoped compliance packs** (`integrations/microsoft365/compliance.py`
  + `/microsoft365/compliance/*`) are **superseded** by the platform engine; the M365
  workspace "Governance" tab now defers posture to the org Compliance page. The old
  endpoints/tables remain (harmless) — remove in a later cleanup.
- Scope is Arkive's **data-protection coverage** of each framework, not the whole
  framework — controls only assert what Arkive can genuinely evidence.
