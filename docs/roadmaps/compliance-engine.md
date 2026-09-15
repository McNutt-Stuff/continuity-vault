# Compliance engine — roadmap

> Platform-level, capability-driven compliance posture that Arkive core + its
> integrations drive. Feature-flagged (`compliance_enabled`), Business/Enterprise,
> org-admin. Code: `cloud/app/compliance/`, `cloud/app/api/compliance.py`,
> `web/src/pages/Compliance.tsx`. Extension rules: `.github/instructions/compliance.instructions.md`.

## Delivered (preview)
- Capability registry (15 capabilities) + frameworks **NIST CSF 2.0, CIS v8, HIPAA**.
- Evidence providers: **Arkive core** (encryption, immutability, backup coverage,
  recovery, access control, MFA/passkeys, audit logging, monitoring, retention,
  legal hold, classification/minimization from rules, inventory, incident response)
  and the **Microsoft 365 driver** (managed-source coverage/health).
- Engine: best-status-per-capability → control state + score; manual overrides;
  time-boxed audited exceptions; evidence trail; **posture snapshots** (trend) and a
  **change ledger** (what drove each change).
- API + UI (posture ring, framework enable + scores, controls with evidence
  drill-down, overrides, exceptions, score trend + history). Nav + feature flag.
- Rules relabelled "data governance" (drives compliance; not the assessment).

## Next (roadmap)
| Item | Notes |
|---|---|
| More frameworks | ISO 27001, SOC 2, GDPR, PCI DSS, CMMC — registry entries + control→capability maps. |
| Scheduled auto-evaluation | A worker that re-evaluates + snapshots on a cadence (currently on-demand + on enable), so the trend fills without manual re-assess. |
| Richer M365 evidence | Map real M365 detections to capabilities: MFA/conditional-access posture, DLP hits, external sharing/exposure, guest access, mailbox audit, retention/hold labels, residency. |
| More integration drivers | Ubiquiti (network segmentation/logging), desktop agent (endpoint encryption/backup), appliance (offline/immutable copy) as compliance drivers. |
| Evidence export | Downloadable auditor report (PDF/CSV) per framework: controls, states, evidence, exceptions, history. |
| Control → source scoping | Per-control applicability + scoping (e.g. HIPAA only over vaults tagged ePHI). |
| Benchmarking | Compare a tenant's score to an anonymized peer baseline (opt-in). |
| Remediation guidance | Actionable "how to move this control to met" links (enable a workload, add a rule, enrol passkeys). |
| Custom frameworks | Admin-defined control sets mapping to existing capabilities. |
| Notifications | Alert on score regression / control moving to failed / exception expiry. |
| Federation | If a provider needs node-only runtime signals, replicate them to the CP first (evidence is computed on the CP today). |

## Deviations / notes
- The earlier **M365-scoped compliance packs** (`integrations/microsoft365/compliance.py`
  + `/microsoft365/compliance/*`) are **superseded** by the platform engine; the M365
  workspace "Governance" tab now defers posture to the org Compliance page. The old
  endpoints/tables remain (harmless) — remove in a later cleanup.
- Scope is Arkive's **data-protection coverage** of each framework, not the whole
  framework — controls only assert what Arkive can genuinely evidence.
