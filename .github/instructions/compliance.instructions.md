---
applyTo: "cloud/app/compliance/**,cloud/app/integrations/**/compliance_driver.py,cloud/app/api/compliance.py"
description: "Compliance engine — frameworks, controls, capabilities, evidence providers/drivers, posture history."
---

# Compliance engine — how it works and how to extend it

Arkive's compliance feature is a **first-class, organization-level, capability-driven**
posture engine that Arkive core + its integrations **drive**. It is feature-flagged
(`compliance_enabled`, OFF by default), **Business/Enterprise only**, and org-admin gated.
It is NOT the rules engine (rules HELP DRIVE compliance; they are not the assessment).

## The model (registry → providers → engine → history)
- **Capabilities** (`registry.CAPABILITIES`) — the atomic, *evidenceable* facts (backup
  coverage, encryption at rest/in transit, immutability, recovery, retention, access control,
  MFA, audit logging, monitoring, legal hold, data classification/minimization, inventory,
  incident response). A control is built from capabilities, never from raw queries.
- **Frameworks** (`registry.FRAMEWORKS`) — each control maps to one or more capabilities.
  Scope each framework to **Arkive's coverage** (data protection / backup / recovery /
  access governance / audit) — do NOT claim controls Arkive can't evidence.
- **Providers** (`providers.py`) — a provider inspects live state and returns
  `CapabilityEvidence(capability, status, summary, provider, detail)` with status
  `met|partial|unmet|not_applicable|unknown`. `arkive` (core) is always registered.
- **Engine** (`engine.py`) — for each enabled framework, derives each control's state+score
  from the **best status per capability**, preserves manual overrides + exceptions, writes an
  **evidence trail** (`compliance_evidence`), emits a **change ledger** (`compliance_events`)
  on every state change, and takes a per-framework **posture snapshot** (`compliance_snapshots`)
  so the trend/improvement over time is visible.

## Adding a framework
Add an entry to `registry.FRAMEWORKS`: `{label, version, authority, url, description,
controls:[{id, title, family, capabilities:[...], guidance}]}`. Nothing else — the engine,
API, scoring, history and UI pick it up automatically. Keep controls to what Arkive +
integrations can genuinely evidence.

## Adding a capability
Add to `registry.CAPABILITIES`, then make at least one provider report it (core in
`providers._arkive_core`, or an integration driver). Map existing/new controls to it.

## Integrations are compliance DRIVERS
An integration contributes evidence by registering a provider in its own
`integrations/<pkg>/compliance_driver.py` and importing it from the package `__init__`:
```python
from ...compliance.providers import CapabilityEvidence, register_provider
def _driver(db, tenant, scope): return [CapabilityEvidence("backup_coverage", "met", "…", "<pkg>")]
register_provider("<pkg>", _driver)
```
Rules: derive evidence from the tenant's OWN live state (managed sources, detections,
config); NEVER return secrets or payloads in `detail`; one bad provider must not break the
engine (the engine isolates each). Microsoft 365 is the reference driver.

## STANDARD — capture compliance on EVERY enhancement
When you add or change a capability, integration, or detection, ask: **does this produce a
signal that could evidence a compliance capability?** If yes:
1. If it's a NEW kind of signal, add/extend a `CAPABILITY` and map it into the relevant
   framework controls.
2. Emit it from the right provider (core for platform-wide; the integration's driver for
   integration-specific detections — e.g. MFA posture, DLP hits, sharing/exposure, external
   access, malware/threat detections, geo/residency).
3. Prefer richer statuses (`partial`) over binary so the score reflects reality.
This keeps Arkive's compliance value growing with the product instead of drifting.

## History & audit (never lose the "why")
- Every state change and admin action writes a `compliance_events` row (kind + summary +
  detail) AND is `audit.record`-ed. Exceptions are time-boxed and audited (`severity=warning`).
- `compliance_snapshots` is the time series — one row per framework per evaluation. The report
  shows the delta vs. the previous snapshot (improving / regressing).
- Manual overrides set `control.auto = False` so `engine.reassess` won't clobber them.

## Gotchas
- New tables only (`compliance_*`) — no `db.py` ALTER needed; `db.init_db` imports the package.
- Scoring: control state → contribution (operating/implemented/exception = 100, partial = 50,
  planned/failed/not_assessed = 0, not_applicable excluded). Framework score = mean over
  applicable controls.
- Federation: evidence is computed on the CP from replicated data (managed sources,
  SearchDocuments, receipts, rules). If a provider needs node-only runtime, replicate it up first.
- Don't route ingest enforcement here — that stays in `rules_engine`.
