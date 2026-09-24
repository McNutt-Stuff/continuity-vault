# Microsoft 365 — Microsoft Graph permissions (canonical)

Arkive's Microsoft 365 managed integration is **read-only** against Microsoft Graph
and app-only (client-credentials). It uses two tiers of **Application** permissions
(all admin-consented; `.default` grants exactly what's configured on the app
registration):

1. **Backup permissions** — read the content Arkive protects.
2. **Posture permissions (optional)** — read tenant security posture so the
   compliance engine can *assess* identity, access-policy, sharing and residency
   controls. Every posture permission is optional: if it isn't granted, the related
   compliance capability is recorded as **`unknown`** naming the exact permission to
   grant (it is *tracked*, never a silent pass and never a crash).

> Arkive never writes to Microsoft 365, never stores tokens/claims/PII in compliance
> evidence, and only records derived, non-secret counts + affected-entity labels.

## 1. Backup permissions (content)

| Permission | Workload it protects |
|---|---|
| `User.Read.All` | Discover the organization's Entra users (also used for guest/member governance). |
| `Mail.Read` | Exchange Online mailboxes. |
| `Files.Read.All` | OneDrive. |
| `Sites.Read.All` | SharePoint site libraries. |
| `Group.Read.All` | List Teams (to discover channels). |
| `ChannelMessage.Read.All` | Teams channel conversations. |
| `Chat.Read.All` | Teams chats. |
| `AiEnterpriseInteraction.Read.All` | Microsoft 365 Copilot interaction history *(optional workload)*. |
| `Calendars.Read` | Calendars *(optional workload)*. |
| `Contacts.Read` | Contacts *(optional workload)*. |
| `Notes.Read.All` | OneNote *(optional workload; app-only is Microsoft-limited)*. |

**Teams is a Microsoft "protected API."** App-only reading of Teams channel/chat
messages additionally requires completing Microsoft's *Request access to protected
APIs* process, or Teams returns `403 Forbidden — Missing role permissions` even after
consent. SharePoint and Exchange have no such requirement.

## 2. Posture permissions (compliance assessment — optional)

These let the compliance engine assess controls it otherwise records as `unknown`.
Each maps to specific capabilities in the framework packs:

| Permission | Capabilities it evidences |
|---|---|
| `Reports.Read.All` (or `AuditLog.Read.All`) | `mfa` (registration), `phishing_resistant_mfa`, `privileged_mfa`, `privileged_access_review` (baseline) — all from the `userRegistrationDetails` report. |
| `Policy.Read.All` | `conditional_access` (enabled conditional-access policies) **and** `password_policy` (authentication-method policy — strong methods on, weak SMS/voice off). |
| `SharePointTenantSettings.Read.All` | `external_sharing_control` (tenant sharing capability). |
| `User.Read.All` *(already a backup permission)* | `guest_access` (guest vs member accounts). |
| `Organization.Read.All` | `data_residency` (tenant country / preferred data location). |
| `SecurityEvents.Read.All` | `security_posture` (Microsoft Secure Score current/max ratio). |
| `RoleManagement.Read.Directory` | `privileged_access_review` (authoritative — real Entra directory-role holders; refines the Reports baseline). |
| `DeviceManagementManagedDevices.Read.All` | `device_compliance` (Intune compliant devices) **and** `device_encryption` (Intune disk-encrypted devices). |

> **Not feasible app-only (Graph v1.0):** Purview **retention labels / policies** and
> **legal hold** have no app-only read surface today, so Arkive does **not** fake a
> `retention`/`legal_hold` reading from M365 — it relies on Arkive's own immutable,
> receipt-verified retention instead. This is a deliberate limitation, not a gap in
> scoring.

No posture permission is required for the **coverage** capabilities
(`coverage_completeness`, `backup_freshness`, `integrity_verified`, `restore_test`):
those are reconciled entirely from Arkive's own protection state (managed sources,
recovery-point receipts, index-replica integrity checks) with no Graph call.

## What each capability means

- **coverage_completeness** — every in-scope managed source is active *and* has a
  recoverable recovery point (expected vs covered, with the failed sources listed).
- **backup_freshness** — active sources collected within the freshness window (48h);
  the signal itself expires, so a stalled node can't leave a stale "fresh" reading.
- **phishing_resistant_mfa / privileged_mfa** — users with a FIDO2/passkey/Windows
  Hello/certificate method; admin accounts covered by MFA.
- **privileged_access_review** — a small, well-scoped privileged footprint. Uses real
  Entra directory-role holders when `RoleManagement.Read.Directory` is granted,
  otherwise a baseline estimate from the registration report.
- **security_posture** — Microsoft Secure Score (current ÷ max); ≥70% met, ≥40% partial.
- **password_policy** — the tenant authentication-method policy enforces strong methods
  (FIDO2 / Authenticator / Windows Hello / certificate) and disables weak ones (SMS/voice).
- **device_compliance / device_encryption** — Intune managed devices reporting
  compliant / disk-encrypted (covered vs total).
- **guest_access** — external/guest account sprawl vs the directory.
- **conditional_access / external_sharing_control / data_residency** — tenant policy
  posture, scope-tagged to the M365 instance.
- **integrity_verified / restore_test** — recovery points are hybrid-signed (ML-DSA +
  Ed25519) and stored index copies are periodically read back, decrypted and
  row-count-verified.

## Setup summary

1. Register the Arkive Microsoft 365 (Entra) application once.
2. Add the **backup** Application permissions (and any optional workloads you protect).
3. Optionally add the **posture** Application permissions to unlock compliance
   assessment of identity/policy/sharing/residency controls.
4. **Grant admin consent** for every permission.
5. Add the Web redirect URI
   (`https://<your-control-plane>/api/integrations/microsoft365/oauth/redirect`) and a
   client secret.

Consent uses `.default`, so it grants exactly the Application permissions configured
on the app registration. A missing posture permission degrades gracefully to an
actionable `unknown` on the specific control — nothing else is affected.

## App registration / Auth grant changes (Phase 2 posture)

Phase 2 deepens the M365 posture assessment (Secure Score, Entra privileged roles,
Intune device compliance/encryption, authentication-method policy). To light these up,
a Global/Application administrator adds **three new Application permissions** to the
existing Arkive Entra app registration and re-consents — no new app, secret or redirect
URI is required.

**Add these Microsoft Graph → Application permissions:**

| Permission | Unlocks |
|---|---|
| `SecurityEvents.Read.All` | `security_posture` (Microsoft Secure Score). |
| `RoleManagement.Read.Directory` | authoritative `privileged_access_review` (real directory-role holders). |
| `DeviceManagementManagedDevices.Read.All` | `device_compliance` + `device_encryption` (Intune). |

`password_policy` needs **no** new grant — it reuses the existing `Policy.Read.All`
posture permission (also used by `conditional_access`).

**Steps (Entra admin center → App registrations → Arkive app → API permissions):**

1. **Add a permission → Microsoft Graph → Application permissions**, tick the three
   permissions above, **Add permissions**.
2. **Grant admin consent for `<tenant>`** (the `.default` scope then picks them up).
3. In the Arkive portal, open the Microsoft 365 integration and use **Re-authorize
   Microsoft 365** so the next posture refresh sees the new grants.

Each new permission is **optional and read-only**: if it isn't granted, its capability
is recorded as `unknown` naming the exact permission to add — never a silent pass, never
a crash. No client secret, redirect URI or app-manifest change is needed for Phase 2.

