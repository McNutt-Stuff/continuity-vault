# Compliance coverage expansion — roadmap

> Where the compliance engine goes next: from **"validate Arkive against the controls
> it can evidence"** to **"assess the whole framework"** — automated where we can,
> attested/documented where we can't, and extended through new integrations.
> Companion to `compliance-engine.md` (the delivered scope-aware engine).

## Where we are today
The engine is **capability-driven** and **scope-aware**: framework controls map to
atomic capabilities; providers report coverage per capability; the engine rolls them
into control states + a per-framework score with history. Two evidence sources today:
- **Arkive core** — encryption, immutability, backup coverage/completeness/freshness,
  recovery, integrity-verified, restore-test, access control, MFA (passkeys), audit
  logging, monitoring, retention, legal hold, classification/minimization, inventory,
  incident response, versioning, offsite/air-gapped copy.
- **Microsoft 365 driver** — managed-source coverage/health + posture (MFA,
  phishing-resistant, privileged MFA, conditional access, external sharing, guest
  governance, residency).

**The gap:** every framework also has **organizational, policy and procedural
controls** — written policies, risk assessments, awareness training, incident-response
& business-continuity plans, vendor risk, access reviews, change management,
vulnerability management, penetration testing, physical/personnel security. Arkive
can't *automatically* prove these. Today they're simply absent from the packs, so a
framework score reflects only the technical slice.

## Strategy — three lanes
1. **Attest & document** (Phase 1) — a self-attestation questionnaire + policy-document
   evidence for the controls that are inherently procedural. This is the single biggest
   coverage unlock and needs no external integration.
2. **Automate more** (Phases 2–4) — new evidence drivers that pull real posture from
   the systems customers already run (more M365 services, other IdPs, MDM, cloud
   config, security tools).
3. **Govern the program** (Phase 5) — control applicability/scoping, evidence export
   (auditor pack), continuous re-assessment + alerting, and custom frameworks.

---

## Phase 1 — Self-attestation & policy evidence  *(this iteration)*
Turn the procedural controls into first-class, scored evidence.
- **Attestable capabilities** (registry flag `attestable: True`): `security_policy`,
  `risk_assessment`, `security_training`, `incident_response_plan`, `continuity_plan`,
  `vendor_risk_management`, `access_review`, `change_management`,
  `vulnerability_management`, `penetration_testing`, `physical_security`,
  `personnel_security`.
- New **organizational controls** added to each framework mapping to them (NIST GV/PR.AT/
  ID.RA, CIS 14/15/16/17/18, ISO A.5/A.6/A.8, SOC 2 CC1/CC3/CC9, HIPAA admin safeguards,
  GDPR Art.24/28/32/35).
- **`ComplianceAttestation`** model (per tenant × capability): status (met/partial/
  unmet/not_applicable), note, an evidence link, `attested_by`, and a **review cadence**
  (`review_due_at`) after which the attestation goes **stale → unknown** (must re-attest).
- **`attestation` provider** — turns each attestation into `evidence_level="manual"`
  evidence; an un-attested attestable capability reads **`unknown` ("needs attestation")**
  so it's tracked, never a silent pass.
- **API + UI** — a Compliance **Questionnaire** surface: answer each item, add a note,
  link proof, set the review date; audited + re-scored.
- **Docs** — Help Center questionnaire guidance.

**Phase 1b (delivered):** real **document upload** — attach the policy PDF as the
proof (encrypted in Arkive Cloud, ciphertext-only, org-admin gated + audited on
upload/download), shown against the attestation with a doc count on the control.

## Phase 2 — Deepen Microsoft 365 (highest ROI automation) — **DELIVERED**
- **Microsoft Secure Score** (`SecurityEvents.Read.All`) → `security_posture`
  (current/max ratio). ✅
- **Entra PIM / privileged roles** (`RoleManagement.Read.Directory`) → authoritative
  `privileged_access_review` (real standing directory-role holders; refines the
  registration-report baseline). ✅
- **Intune device compliance** (`DeviceManagementManagedDevices.Read.All`) → endpoint
  `device_compliance` + `device_encryption` (covered vs total). ✅
- **Entra password / auth-method policies** (`Policy.Read.All`, already granted) →
  `password_policy` (strong methods enforced, weak SMS/voice off). ✅
- **Purview** — retention labels + **legal/litigation hold**: **NOT feasible app-only
  on Graph v1.0** (no app-only read surface). Deliberately *not* faked — Arkive relies
  on its own immutable, receipt-verified retention instead. Deferred. ⛔
- **Unified audit log** enabled → strengthens `audit_logging`: deferred (best surfaced
  via Secure Score recommendations for now).

## Phase 3 — Beyond Microsoft (breadth) — **SHELLS DELIVERED**
Each vendor ships first as a **catalog + compliance shell**: a self-contained integration
package under `cloud/app/integrations/<type>/` (spec `status="coming_soon"`, real brand
icon, a `compliance_driver` that declares the capabilities it evidences + registers a
provider via `shell_compliance.register_shell_driver`, and its own Help Center page). No
posture is fabricated until a collector lands — an un-observed capability stays *not yet
demonstrated*. Turning a shell into a live driver = add a collector that records signals
(they flow through the standard `integration_signals` provider automatically).

- **Google Workspace** (`google_workspace`, identity) → `mfa`, `phishing_resistant_mfa`,
  `password_policy`, `conditional_access`, `external_sharing_control`, `guest_access`,
  `data_residency`, `audit_logging`. ✅ shell
- **Okta** (`okta`, identity) → `mfa`, `phishing_resistant_mfa`, `privileged_mfa`,
  `privileged_access_review`, `password_policy`, `conditional_access`, `access_control`. ✅ shell
- **MDM/endpoint** — **Jamf Pro** (`jamf`) + **Kandji** (`kandji`) → `device_compliance`,
  `device_encryption` (Intune already live via M365). ✅ shells
- **Cloud config** — **AWS** (`aws`), **Azure** (`azure`), **GCP** (`gcp`) →
  `encryption_at_rest`, `encryption_in_transit`, `audit_logging`, `monitoring`, `mfa`,
  `data_residency`. ✅ shells
- **Vulnerability mgmt** — **Qualys** (`qualys`) + **Tenable** (`tenable`) →
  `vulnerability_management` (Defender via M365). ✅ shells
- **Security awareness** — **KnowBe4** (`knowbe4`) + **Proofpoint** (`proofpoint`) →
  `security_training`. ✅ shells
- **Ubiquiti** (existing appliance integration) → network segmentation + logging. ⬜ next
- **JumpCloud** (identity, secondary IdP) → follow the Okta shell. ⬜ next

Icons: real Wikimedia Commons marks for google_workspace, okta, qualys, tenable, proofpoint;
aws/azure/gcp reuse the existing brand marks. Jamf/Kandji/KnowBe4 have no free Commons SVG,
so they use the graceful glyph fallback (never a hand-drawn logo).

## Phase 4 — Program governance & assurance
- **Control applicability/scoping** — mark controls N/A or scope them (e.g. HIPAA only
  over ePHI-tagged vaults); exclude cleanly from the denominator.
- **Evidence export** — a downloadable auditor pack per framework (controls, states,
  evidence, attestations + linked docs, exceptions, history) as PDF/CSV.
- **Continuous assessment** — a scheduled re-evaluate + snapshot worker; **alerts** on
  score regression, a control moving to failed, an attestation going stale, or an
  exception expiring.
- **Custom frameworks / crosswalks** — admin-defined control sets mapping to existing
  capabilities; reuse one attestation across frameworks (answer once, satisfy many).
- **More frameworks** — PCI DSS, CMMC, NIST 800-53/171, Essential Eight.

## Design rules (unchanged)
- `unknown`/`expired`/`stale` never counts as met. Config vs observed vs verified-test
  vs **attested (manual)** are distinct evidence levels.
- Attestations are audited and carry a review cadence — a signed statement, not a
  set-and-forget checkbox.
- New capability keys only when materially distinct; map into existing controls where
  they fit before adding new controls.
- Every new driver follows the provider/refresher pattern
  (`.github/instructions/compliance.instructions.md`) — never edits framework math.
