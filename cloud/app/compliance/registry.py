"""Compliance framework + control registry (the single source of truth).

Arkive's compliance engine is **capability-driven**: every framework control maps
to one or more atomic *capabilities* that Arkive (or an integration) can EVIDENCE
from live platform state. Providers (``compliance/providers.py``) report a status
per capability; the engine (``compliance/engine.py``) rolls those up into each
control's state + a per-framework score, and records posture history over time.

Arkive does NOT claim to cover an entire framework — it covers the **data
protection, backup, recovery, retention, access-governance and audit** domains of
each one (its wheelhouse). Each control below is scoped to what Arkive + its
integrations can genuinely demonstrate.

EXTENSIBILITY (see .github/instructions/compliance.instructions.md):
- Add a capability to ``CAPABILITIES`` when the platform/an integration can newly
  evidence something.
- Add a framework by adding an entry to ``FRAMEWORKS`` (controls → capabilities).
- Integrations contribute evidence by registering a provider in
  ``providers.register_provider`` — they never edit this file's control math.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Capabilities — the atomic, evidenceable facts controls are built from.       #
# key -> {title, description, domain}                                          #
# --------------------------------------------------------------------------- #
CAPABILITIES: dict[str, dict] = {
    "backup_coverage": {
        "title": "Backup coverage",
        "description": "Critical systems and data are backed up automatically.",
        "domain": "resilience"},
    "encryption_at_rest": {
        "title": "Encryption at rest",
        "description": "Protected data is encrypted at rest with a strong (quantum-safe) cipher.",
        "domain": "protect"},
    "encryption_in_transit": {
        "title": "Encryption in transit",
        "description": "Data is encrypted in transit (TLS) end to end.",
        "domain": "protect"},
    "immutability": {
        "title": "Immutable / tamper-evident storage",
        "description": "Backups are tamper-evident and cannot be silently altered.",
        "domain": "protect"},
    "recovery": {
        "title": "Recoverability",
        "description": "Point-in-time recovery of protected data is available.",
        "domain": "resilience"},
    "retention": {
        "title": "Retention schedule",
        "description": "A retention schedule governs how long data is kept.",
        "domain": "govern"},
    "access_control": {
        "title": "Access control",
        "description": "Least-privilege access; cross-member access is gated, dual-controlled and audited.",
        "domain": "protect"},
    "mfa": {
        "title": "Strong authentication",
        "description": "Privileged/administrative access is protected by phishing-resistant MFA (passkeys).",
        "domain": "protect"},
    "conditional_access": {
        "title": "Conditional access",
        "description": "Conditional-access / sign-in policies restrict risky or non-compliant access.",
        "domain": "protect"},
    "dlp": {
        "title": "Data loss prevention",
        "description": "DLP policies detect and restrict exfiltration of sensitive data.",
        "domain": "protect"},
    "external_sharing_control": {
        "title": "External sharing control",
        "description": "External / guest sharing of organization data is restricted and governed.",
        "domain": "protect"},
    "data_residency": {
        "title": "Data residency",
        "description": "Data storage location / residency is known and controlled.",
        "domain": "govern"},
    "audit_logging": {
        "title": "Audit logging",
        "description": "Tamper-evident audit logging of access and administrative actions.",
        "domain": "detect"},
    "monitoring": {
        "title": "Monitoring & alerting",
        "description": "Activity is monitored and anomalies raise operator alerts.",
        "domain": "detect"},
    "legal_hold": {
        "title": "Legal hold / preservation",
        "description": "Data can be preserved under legal hold and exempt from deletion.",
        "domain": "govern"},
    "data_classification": {
        "title": "Data classification / labelling",
        "description": "Protected data is classified/labelled to drive handling.",
        "domain": "identify"},
    "data_minimization": {
        "title": "Data minimization",
        "description": "Governance rules restrict, obfuscate or discard sensitive data on ingest.",
        "domain": "protect"},
    "inventory": {
        "title": "Asset / data inventory",
        "description": "An inventory of protected data sources and accounts is maintained.",
        "domain": "identify"},
    "incident_response": {
        "title": "Incident response",
        "description": "Failures and security-relevant events are surfaced for response.",
        "domain": "respond"},
    "versioning": {
        "title": "Version history / point-in-time",
        "description": "Content-addressed version history lets any object be restored to a prior point in time.",
        "domain": "resilience"},
    "offsite_copy": {
        "title": "Offsite / redundant copy",
        "description": "Protected data is copied to a geographically separate or independent location (the 3-2-1 rule).",
        "domain": "resilience"},
    "air_gapped_copy": {
        "title": "Air-gapped / offline copy",
        "description": "An offline, immutable on-premises copy exists on an Arkive appliance.",
        "domain": "resilience"},
    "coverage_completeness": {
        "title": "Protection coverage completeness",
        "description": "Every in-scope source that should be protected is actually protected and "
                       "recoverable — no unprotected, failed or never-provisioned gaps.",
        "domain": "resilience"},
    "backup_freshness": {
        "title": "Backup freshness",
        "description": "Protected sources have a recent successful, recoverable backup (not stale).",
        "domain": "resilience"},
}

# --------------------------------------------------------------------------- #
# Frameworks — control → capability mapping. Scoped to Arkive's data-protection #
# coverage of each framework (NOT the whole framework).                        #
# framework -> {label, version, authority, url, description,                   #
#   controls: [{id, title, family, capabilities:[...], guidance}]}             #
# --------------------------------------------------------------------------- #
FRAMEWORKS: dict[str, dict] = {
    "nist_csf": {
        "label": "NIST CSF 2.0", "version": "2.0", "authority": "NIST",
        "url": "https://www.nist.gov/cyberframework",
        "description": "NIST Cybersecurity Framework 2.0 — data-protection, backup, recovery, "
                       "access-governance and audit functions.",
        "controls": [
            {"id": "ID.AM-01", "title": "Data & source inventory", "family": "Identify",
             "capabilities": ["inventory"],
             "guidance": "Maintain an inventory of protected data sources and accounts."},
            {"id": "ID.AM-05", "title": "Data classification", "family": "Identify",
             "capabilities": ["data_classification"],
             "guidance": "Classify/label protected data to drive handling."},
            {"id": "PR.DS-01", "title": "Data-at-rest protection", "family": "Protect",
             "capabilities": ["encryption_at_rest", "immutability"],
             "guidance": "Encrypt data at rest and make it tamper-evident."},
            {"id": "PR.DS-02", "title": "Data-in-transit protection", "family": "Protect",
             "capabilities": ["encryption_in_transit"],
             "guidance": "Encrypt data in transit."},
            {"id": "PR.DS-11", "title": "Backups are created & protected", "family": "Protect",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "encryption_at_rest", "offsite_copy", "versioning"],
             "guidance": "Create and protect backups of critical data, kept offsite with version history."},
            {"id": "PR.AA-05", "title": "Least-privilege access", "family": "Protect",
             "capabilities": ["access_control", "mfa", "conditional_access"],
             "guidance": "Enforce least privilege, strong authentication and conditional access."},
            {"id": "PR.DS-05", "title": "Protections against data leaks", "family": "Protect",
             "capabilities": ["dlp", "external_sharing_control"],
             "guidance": "Deploy DLP and control external sharing to prevent data leaks."},
            {"id": "DE.AE-03", "title": "Event data collection", "family": "Detect",
             "capabilities": ["audit_logging", "monitoring"],
             "guidance": "Collect and correlate audit/event data."},
            {"id": "RS.MA-01", "title": "Incident response", "family": "Respond",
             "capabilities": ["incident_response"],
             "guidance": "Surface failures and security events for response."},
            {"id": "RC.RP-01", "title": "Recovery execution", "family": "Recover",
             "capabilities": ["recovery", "backup_coverage", "versioning"],
             "guidance": "Execute point-in-time recovery of data from backups."},
            {"id": "RC.RP-04", "title": "Backup redundancy & integrity", "family": "Recover",
             "capabilities": ["offsite_copy", "air_gapped_copy", "immutability"],
             "guidance": "Keep redundant, offsite and (where possible) air-gapped immutable copies."},
            {"id": "GV.PO-01", "title": "Retention policy", "family": "Govern",
             "capabilities": ["retention", "legal_hold", "data_residency"],
             "guidance": "Establish and enforce data retention, legal hold and residency."},
        ],
    },
    "cis": {
        "label": "CIS Controls v8", "version": "8.0", "authority": "Center for Internet Security",
        "url": "https://www.cisecurity.org/controls",
        "description": "CIS Critical Security Controls v8 — the data-recovery, data-protection, "
                       "access-control and logging controls.",
        "controls": [
            {"id": "1.1", "title": "Inventory of enterprise assets/data", "family": "Inventory",
             "capabilities": ["inventory"], "guidance": "Maintain a data/source inventory."},
            {"id": "3.4", "title": "Enforce data retention", "family": "Data protection",
             "capabilities": ["retention", "legal_hold", "data_residency"], "guidance": "Enforce retention + know data residency."},
            {"id": "3.11", "title": "Encrypt sensitive data at rest", "family": "Data protection",
             "capabilities": ["encryption_at_rest", "immutability"], "guidance": "Encrypt at rest."},
            {"id": "3.10", "title": "Encrypt sensitive data in transit", "family": "Data protection",
             "capabilities": ["encryption_in_transit"], "guidance": "Encrypt in transit."},
            {"id": "3.3", "title": "Configure data access control lists", "family": "Data protection",
             "capabilities": ["access_control", "external_sharing_control"], "guidance": "Least-privilege data access; control external sharing."},
            {"id": "3.13", "title": "Deploy a data loss prevention solution", "family": "Data protection",
             "capabilities": ["dlp"], "guidance": "Deploy DLP for sensitive data."},
            {"id": "6.3", "title": "Require MFA", "family": "Access control",
             "capabilities": ["mfa", "conditional_access"], "guidance": "Require MFA + conditional access for privileged access."},
            {"id": "8.2", "title": "Collect audit logs", "family": "Audit log management",
             "capabilities": ["audit_logging"], "guidance": "Collect audit logs."},
            {"id": "8.11", "title": "Review audit logs", "family": "Audit log management",
             "capabilities": ["monitoring"], "guidance": "Monitor/review audit logs."},
            {"id": "11.2", "title": "Perform automated backups", "family": "Data recovery",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "versioning"], "guidance": "Automated backups with version history."},
            {"id": "11.1", "title": "Data recovery process", "family": "Data recovery",
             "capabilities": ["recovery"], "guidance": "Maintain a recovery process."},
            {"id": "11.3", "title": "Protect recovery data", "family": "Data recovery",
             "capabilities": ["encryption_at_rest", "immutability"], "guidance": "Protect backups."},
            {"id": "11.4", "title": "Isolated recovery copy", "family": "Data recovery",
             "capabilities": ["offsite_copy", "air_gapped_copy"], "guidance": "Keep an offsite/air-gapped recovery copy."},
        ],
    },
    "hipaa": {
        "label": "HIPAA Security Rule", "version": "45 CFR 164", "authority": "HHS",
        "url": "https://www.hhs.gov/hipaa/for-professionals/security",
        "description": "HIPAA Security Rule safeguards Arkive supports — backup, recovery, "
                       "encryption, access control, audit and retention of ePHI.",
        "controls": [
            {"id": "164.308(a)(7)(ii)(A)", "title": "Data backup plan", "family": "Administrative",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "offsite_copy"], "guidance": "Back up ePHI; keep an offsite copy."},
            {"id": "164.308(a)(7)(ii)(B)", "title": "Disaster recovery plan", "family": "Administrative",
             "capabilities": ["recovery", "backup_coverage"], "guidance": "Restore ePHI."},
            {"id": "164.312(a)(2)(iv)", "title": "Encryption & decryption", "family": "Technical",
             "capabilities": ["encryption_at_rest"], "guidance": "Encrypt ePHI at rest."},
            {"id": "164.312(e)(2)(ii)", "title": "Transmission encryption", "family": "Technical",
             "capabilities": ["encryption_in_transit"], "guidance": "Encrypt ePHI in transit."},
            {"id": "164.312(a)(1)", "title": "Access control", "family": "Technical",
             "capabilities": ["access_control", "mfa", "conditional_access", "external_sharing_control"], "guidance": "Restrict ePHI access."},
            {"id": "164.312(b)", "title": "Audit controls", "family": "Technical",
             "capabilities": ["audit_logging"], "guidance": "Audit ePHI access."},
            {"id": "164.312(c)(1)", "title": "Integrity", "family": "Technical",
             "capabilities": ["immutability"], "guidance": "Protect ePHI from improper alteration."},
            {"id": "164.316(b)(2)(i)", "title": "Retention (6 years)", "family": "Administrative",
             "capabilities": ["retention", "legal_hold", "data_residency"], "guidance": "Retain records; know residency."},
            {"id": "164.308(a)(1)(ii)(D)", "title": "Information system activity review",
             "family": "Administrative", "capabilities": ["monitoring"],
             "guidance": "Review activity logs."},
        ],
    },
    "iso_27001": {
        "label": "ISO/IEC 27001:2022", "version": "2022", "authority": "ISO/IEC",
        "url": "https://www.iso.org/standard/27001",
        "description": "ISO/IEC 27001:2022 Annex A controls Arkive supports — information backup, "
                       "redundancy, cryptography, access control, logging, retention and classification.",
        "controls": [
            {"id": "A.5.9", "title": "Inventory of information & assets", "family": "Organizational",
             "capabilities": ["inventory"], "guidance": "Maintain an inventory of protected data/assets."},
            {"id": "A.5.12", "title": "Classification of information", "family": "Organizational",
             "capabilities": ["data_classification"], "guidance": "Classify information to drive handling."},
            {"id": "A.5.15", "title": "Access control", "family": "Organizational",
             "capabilities": ["access_control", "external_sharing_control"],
             "guidance": "Least-privilege access; govern external sharing."},
            {"id": "A.5.17", "title": "Authentication information", "family": "Organizational",
             "capabilities": ["mfa", "conditional_access"],
             "guidance": "Strong authentication and conditional access for privileged use."},
            {"id": "A.8.13", "title": "Information backup", "family": "Technological",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "recovery", "versioning", "offsite_copy"],
             "guidance": "Back up information; keep offsite copies with version history and test recovery."},
            {"id": "A.8.14", "title": "Redundancy of information processing", "family": "Technological",
             "capabilities": ["offsite_copy", "air_gapped_copy"],
             "guidance": "Maintain redundant / independent copies of information."},
            {"id": "A.8.24", "title": "Use of cryptography", "family": "Technological",
             "capabilities": ["encryption_at_rest", "encryption_in_transit"],
             "guidance": "Encrypt information at rest and in transit."},
            {"id": "A.8.12", "title": "Data leakage prevention", "family": "Technological",
             "capabilities": ["dlp", "external_sharing_control"],
             "guidance": "Prevent data leakage via DLP and sharing controls."},
            {"id": "A.8.15", "title": "Logging", "family": "Technological",
             "capabilities": ["audit_logging", "monitoring"],
             "guidance": "Produce, protect and review event logs."},
            {"id": "A.8.10", "title": "Information deletion & retention", "family": "Technological",
             "capabilities": ["retention", "legal_hold", "data_residency"],
             "guidance": "Retain and delete information per policy; know residency."},
        ],
    },
    "soc2": {
        "label": "SOC 2 (Trust Services)", "version": "2017 TSC", "authority": "AICPA",
        "url": "https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-greater-than-soc-2",
        "description": "SOC 2 Trust Services Criteria Arkive supports — the Security, Availability "
                       "and Confidentiality criteria for protected data.",
        "controls": [
            {"id": "CC6.1", "title": "Logical access security", "family": "Security (CC6)",
             "capabilities": ["access_control", "encryption_at_rest", "external_sharing_control"],
             "guidance": "Restrict logical access; encrypt protected data."},
            {"id": "CC6.6", "title": "Authentication of users", "family": "Security (CC6)",
             "capabilities": ["mfa", "conditional_access"],
             "guidance": "Strong authentication + conditional access for access to systems."},
            {"id": "CC6.7", "title": "Transmission & movement of data", "family": "Security (CC6)",
             "capabilities": ["encryption_in_transit", "dlp"],
             "guidance": "Protect data in transit; restrict exfiltration."},
            {"id": "CC7.2", "title": "Monitoring for anomalies", "family": "Operations (CC7)",
             "capabilities": ["monitoring", "audit_logging"],
             "guidance": "Monitor systems and log/detect anomalies."},
            {"id": "CC7.3", "title": "Incident response", "family": "Operations (CC7)",
             "capabilities": ["incident_response"],
             "guidance": "Evaluate and respond to security events."},
            {"id": "A1.2", "title": "Backup & recovery for availability", "family": "Availability (A1)",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "recovery", "offsite_copy", "versioning"],
             "guidance": "Back up data and maintain recoverability for availability commitments."},
            {"id": "A1.3", "title": "Recovery testing / redundancy", "family": "Availability (A1)",
             "capabilities": ["offsite_copy", "air_gapped_copy", "immutability"],
             "guidance": "Keep redundant, tamper-evident recovery copies."},
            {"id": "C1.1", "title": "Confidential data protection", "family": "Confidentiality (C1)",
             "capabilities": ["data_classification", "encryption_at_rest"],
             "guidance": "Identify and protect confidential information."},
            {"id": "C1.2", "title": "Confidential data disposal", "family": "Confidentiality (C1)",
             "capabilities": ["retention", "data_minimization", "legal_hold"],
             "guidance": "Retain and dispose of confidential data per policy."},
        ],
    },
    "gdpr": {
        "label": "GDPR", "version": "2016/679", "authority": "EU",
        "url": "https://gdpr-info.eu/",
        "description": "EU GDPR obligations Arkive helps evidence — security of processing (Art. 32), "
                       "records & inventory (Art. 30), data minimization (Art. 5), residency and erasure.",
        "controls": [
            {"id": "Art.32(1)(a)", "title": "Encryption of personal data", "family": "Security of processing",
             "capabilities": ["encryption_at_rest", "encryption_in_transit"],
             "guidance": "Encrypt personal data at rest and in transit."},
            {"id": "Art.32(1)(b)", "title": "Confidentiality & access control", "family": "Security of processing",
             "capabilities": ["access_control", "mfa", "external_sharing_control"],
             "guidance": "Ensure ongoing confidentiality via access control and strong auth."},
            {"id": "Art.32(1)(c)", "title": "Restore availability after an incident", "family": "Security of processing",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "recovery", "offsite_copy", "versioning"],
             "guidance": "Restore availability and access to personal data in a timely manner."},
            {"id": "Art.32(1)(d)", "title": "Monitoring & audit of processing", "family": "Security of processing",
             "capabilities": ["audit_logging", "monitoring"],
             "guidance": "Regularly test, assess and log the security of processing."},
            {"id": "Art.30", "title": "Records of processing / inventory", "family": "Accountability",
             "capabilities": ["inventory", "data_classification"],
             "guidance": "Maintain records/inventory of data processed."},
            {"id": "Art.5(1)(c)", "title": "Data minimization", "family": "Principles",
             "capabilities": ["data_minimization"],
             "guidance": "Limit personal data to what is necessary."},
            {"id": "Art.5(1)(e)", "title": "Storage limitation & retention", "family": "Principles",
             "capabilities": ["retention", "legal_hold"],
             "guidance": "Keep personal data no longer than necessary; support holds."},
            {"id": "Art.44", "title": "Data residency / transfers", "family": "Transfers",
             "capabilities": ["data_residency"],
             "guidance": "Know and control where personal data is stored/transferred."},
        ],
    },
}


def framework(fw: str) -> dict | None:
    return FRAMEWORKS.get(fw)


def controls_for(fw: str) -> list[dict]:
    return (FRAMEWORKS.get(fw) or {}).get("controls", [])


def all_capabilities_for(fw: str) -> set[str]:
    caps: set[str] = set()
    for c in controls_for(fw):
        caps.update(c.get("capabilities", []))
    return caps
