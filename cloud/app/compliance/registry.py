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
    "phishing_resistant_mfa": {
        "title": "Phishing-resistant MFA",
        "description": "Users are registered with a phishing-resistant authentication method "
                       "(FIDO2/passkey, Windows Hello, or certificate).",
        "domain": "protect"},
    "privileged_mfa": {
        "title": "Privileged-account MFA",
        "description": "Administrative/privileged accounts are protected by strong MFA.",
        "domain": "protect"},
    "privileged_access_review": {
        "title": "Privileged access review",
        "description": "Privileged/administrative accounts are limited and their access is reviewed.",
        "domain": "govern"},
    "guest_access": {
        "title": "Guest / external account governance",
        "description": "External/guest accounts are limited, known and governed.",
        "domain": "protect"},
    "integrity_verified": {
        "title": "Recovery-point integrity verification",
        "description": "Recovery points are cryptographically sealed (hybrid-signed manifests) and their "
                       "stored copies are integrity-verified.",
        "domain": "resilience"},
    "restore_test": {
        "title": "Restore verification",
        "description": "Protected data is periodically read back, decrypted and verified intact "
                       "(restore/recoverability testing).",
        "domain": "resilience"},
    # --- Automated posture from Microsoft 365 (Secure Score, Entra, Intune). ---
    "security_posture": {
        "title": "Security posture score",
        "description": "Overall security configuration posture (e.g. Microsoft Secure Score).",
        "domain": "detect"},
    "device_compliance": {
        "title": "Endpoint device compliance",
        "description": "Managed endpoints meet device-compliance policy (enrolled, healthy, compliant).",
        "domain": "protect"},
    "device_encryption": {
        "title": "Endpoint disk encryption",
        "description": "Managed endpoints have disk encryption enabled (BitLocker/FileVault).",
        "domain": "protect"},
    "password_policy": {
        "title": "Authentication method policy",
        "description": "A strong authentication-method policy is enforced (weak methods disabled).",
        "domain": "protect"},
    # --- Attestable capabilities (procedural / policy — not automatically evidenced;
    # answered via the compliance questionnaire and turned into manual evidence). ---
    "security_policy": {
        "title": "Information security policy",
        "description": "A documented, approved and communicated information security policy is maintained.",
        "domain": "govern", "attestable": True},
    "risk_assessment": {
        "title": "Risk assessment",
        "description": "Security risks are assessed periodically and treated.",
        "domain": "govern", "attestable": True},
    "security_training": {
        "title": "Security awareness training",
        "description": "Staff receive security awareness training on a defined cadence.",
        "domain": "protect", "attestable": True},
    "incident_response_plan": {
        "title": "Incident response plan",
        "description": "A documented incident-response plan exists and is exercised.",
        "domain": "respond", "attestable": True},
    "continuity_plan": {
        "title": "Business continuity / DR plan",
        "description": "A business-continuity / disaster-recovery plan is documented and tested.",
        "domain": "resilience", "attestable": True},
    "vendor_risk_management": {
        "title": "Third-party / vendor risk management",
        "description": "Third parties and suppliers are risk-assessed and governed by agreements.",
        "domain": "govern", "attestable": True},
    "access_review": {
        "title": "Periodic access reviews",
        "description": "User access rights are reviewed and recertified periodically.",
        "domain": "protect", "attestable": True},
    "change_management": {
        "title": "Change management",
        "description": "Changes to systems follow a documented, approved change-management process.",
        "domain": "protect", "attestable": True},
    "vulnerability_management": {
        "title": "Vulnerability & patch management",
        "description": "Vulnerabilities are scanned for and remediated; systems are patched.",
        "domain": "detect", "attestable": True},
    "penetration_testing": {
        "title": "Penetration testing",
        "description": "Independent penetration testing is performed periodically.",
        "domain": "detect", "attestable": True},
    "physical_security": {
        "title": "Physical & environmental security",
        "description": "Facilities and equipment holding data are physically protected.",
        "domain": "protect", "attestable": True},
    "personnel_security": {
        "title": "Personnel security",
        "description": "Screening, onboarding and offboarding controls govern personnel access.",
        "domain": "govern", "attestable": True},
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
             "capabilities": ["encryption_at_rest", "immutability", "device_encryption", "device_compliance"],
             "guidance": "Encrypt data at rest and make it tamper-evident."},
            {"id": "PR.DS-02", "title": "Data-in-transit protection", "family": "Protect",
             "capabilities": ["encryption_in_transit"],
             "guidance": "Encrypt data in transit."},
            {"id": "PR.DS-11", "title": "Backups are created & protected", "family": "Protect",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "encryption_at_rest", "offsite_copy", "versioning"],
             "guidance": "Create and protect backups of critical data, kept offsite with version history."},
            {"id": "PR.AA-05", "title": "Least-privilege access", "family": "Protect",
             "capabilities": ["access_control", "mfa", "conditional_access", "phishing_resistant_mfa", "privileged_mfa", "privileged_access_review", "password_policy"],
             "guidance": "Enforce least privilege, strong authentication and conditional access."},
            {"id": "PR.DS-05", "title": "Protections against data leaks", "family": "Protect",
             "capabilities": ["dlp", "external_sharing_control", "guest_access"],
             "guidance": "Deploy DLP and control external sharing to prevent data leaks."},
            {"id": "DE.AE-03", "title": "Event data collection", "family": "Detect",
             "capabilities": ["audit_logging", "monitoring", "security_posture"],
             "guidance": "Collect and correlate audit/event data."},
            {"id": "RS.MA-01", "title": "Incident response", "family": "Respond",
             "capabilities": ["incident_response"],
             "guidance": "Surface failures and security events for response."},
            {"id": "RC.RP-01", "title": "Recovery execution", "family": "Recover",
             "capabilities": ["recovery", "backup_coverage", "versioning", "restore_test", "integrity_verified"],
             "guidance": "Execute point-in-time recovery of data from backups."},
            {"id": "RC.RP-04", "title": "Backup redundancy & integrity", "family": "Recover",
             "capabilities": ["offsite_copy", "air_gapped_copy", "immutability"],
             "guidance": "Keep redundant, offsite and (where possible) air-gapped immutable copies."},
            {"id": "GV.PO-01", "title": "Retention policy", "family": "Govern",
             "capabilities": ["retention", "legal_hold", "data_residency"],
             "guidance": "Establish and enforce data retention, legal hold and residency."},
            {"id": "GV.OC-01", "title": "Security policy & risk governance", "family": "Govern",
             "capabilities": ["security_policy", "risk_assessment"],
             "guidance": "Maintain an approved information security policy; assess and treat risk periodically."},
            {"id": "GV.SC-01", "title": "Supply chain / third-party risk", "family": "Govern",
             "capabilities": ["vendor_risk_management"],
             "guidance": "Risk-assess and govern suppliers and third parties."},
            {"id": "PR.AT-01", "title": "Security awareness & training", "family": "Protect",
             "capabilities": ["security_training"],
             "guidance": "Train personnel on security awareness on a defined cadence."},
            {"id": "PR.AA-06", "title": "Access reviews & change management", "family": "Protect",
             "capabilities": ["access_review", "change_management"],
             "guidance": "Recertify access rights periodically; control changes through a documented process."},
            {"id": "ID.RA-08", "title": "Vulnerability & penetration testing", "family": "Identify",
             "capabilities": ["vulnerability_management", "penetration_testing"],
             "guidance": "Scan for and remediate vulnerabilities; test independently."},
            {"id": "RS.MA-02", "title": "Incident-response & continuity plans", "family": "Respond",
             "capabilities": ["incident_response_plan", "continuity_plan"],
             "guidance": "Maintain and exercise incident-response and business-continuity plans."},
            {"id": "GV.RR-04", "title": "Physical & personnel security", "family": "Govern",
             "capabilities": ["physical_security", "personnel_security"],
             "guidance": "Protect facilities; screen and manage personnel access."},
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
             "capabilities": ["encryption_at_rest", "immutability", "device_encryption", "device_compliance"], "guidance": "Encrypt at rest."},
            {"id": "3.10", "title": "Encrypt sensitive data in transit", "family": "Data protection",
             "capabilities": ["encryption_in_transit"], "guidance": "Encrypt in transit."},
            {"id": "3.3", "title": "Configure data access control lists", "family": "Data protection",
             "capabilities": ["access_control", "external_sharing_control", "guest_access"], "guidance": "Least-privilege data access; control external sharing."},
            {"id": "3.13", "title": "Deploy a data loss prevention solution", "family": "Data protection",
             "capabilities": ["dlp"], "guidance": "Deploy DLP for sensitive data."},
            {"id": "6.3", "title": "Require MFA", "family": "Access control",
             "capabilities": ["mfa", "conditional_access", "phishing_resistant_mfa", "privileged_mfa", "privileged_access_review", "password_policy"], "guidance": "Require MFA + conditional access for privileged access."},
            {"id": "8.2", "title": "Collect audit logs", "family": "Audit log management",
             "capabilities": ["audit_logging"], "guidance": "Collect audit logs."},
            {"id": "8.11", "title": "Review audit logs", "family": "Audit log management",
             "capabilities": ["monitoring", "security_posture"], "guidance": "Monitor/review audit logs."},
            {"id": "11.2", "title": "Perform automated backups", "family": "Data recovery",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "versioning"], "guidance": "Automated backups with version history."},
            {"id": "11.1", "title": "Data recovery process", "family": "Data recovery",
             "capabilities": ["recovery", "restore_test", "integrity_verified"], "guidance": "Maintain a recovery process."},
            {"id": "11.3", "title": "Protect recovery data", "family": "Data recovery",
             "capabilities": ["encryption_at_rest", "immutability"], "guidance": "Protect backups."},
            {"id": "11.4", "title": "Isolated recovery copy", "family": "Data recovery",
             "capabilities": ["offsite_copy", "air_gapped_copy"], "guidance": "Keep an offsite/air-gapped recovery copy."},
            {"id": "14.1", "title": "Security awareness program", "family": "Security awareness",
             "capabilities": ["security_training"], "guidance": "Run a security awareness training program."},
            {"id": "15.1", "title": "Service provider management", "family": "Service provider management",
             "capabilities": ["vendor_risk_management"], "guidance": "Inventory and risk-manage service providers."},
            {"id": "17.1", "title": "Incident response & continuity", "family": "Incident response",
             "capabilities": ["incident_response_plan", "continuity_plan"], "guidance": "Maintain incident-response and continuity plans."},
            {"id": "7.1", "title": "Vulnerability management", "family": "Vulnerability management",
             "capabilities": ["vulnerability_management"], "guidance": "Run a continuous vulnerability-management process."},
            {"id": "18.1", "title": "Penetration testing", "family": "Penetration testing",
             "capabilities": ["penetration_testing"], "guidance": "Perform periodic penetration testing."},
            {"id": "4.1", "title": "Secure configuration & change", "family": "Secure configuration",
             "capabilities": ["change_management", "access_review"], "guidance": "Manage changes and review access."},
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
             "capabilities": ["recovery", "backup_coverage", "restore_test", "integrity_verified"], "guidance": "Restore ePHI."},
            {"id": "164.312(a)(2)(iv)", "title": "Encryption & decryption", "family": "Technical",
             "capabilities": ["encryption_at_rest", "device_encryption", "device_compliance"], "guidance": "Encrypt ePHI at rest."},
            {"id": "164.312(e)(2)(ii)", "title": "Transmission encryption", "family": "Technical",
             "capabilities": ["encryption_in_transit"], "guidance": "Encrypt ePHI in transit."},
            {"id": "164.312(a)(1)", "title": "Access control", "family": "Technical",
             "capabilities": ["access_control", "mfa", "conditional_access", "external_sharing_control", "phishing_resistant_mfa", "privileged_mfa", "privileged_access_review", "guest_access", "password_policy"], "guidance": "Restrict ePHI access."},
            {"id": "164.312(b)", "title": "Audit controls", "family": "Technical",
             "capabilities": ["audit_logging"], "guidance": "Audit ePHI access."},
            {"id": "164.312(c)(1)", "title": "Integrity", "family": "Technical",
             "capabilities": ["immutability"], "guidance": "Protect ePHI from improper alteration."},
            {"id": "164.316(b)(2)(i)", "title": "Retention (6 years)", "family": "Administrative",
             "capabilities": ["retention", "legal_hold", "data_residency"], "guidance": "Retain records; know residency."},
            {"id": "164.308(a)(1)(ii)(D)", "title": "Information system activity review",
             "family": "Administrative", "capabilities": ["monitoring", "security_posture"],
             "guidance": "Review activity logs."},
            {"id": "164.308(a)(1)(i)", "title": "Security management process", "family": "Administrative",
             "capabilities": ["security_policy", "risk_assessment"],
             "guidance": "Maintain security policies; conduct risk analysis and management."},
            {"id": "164.308(a)(5)", "title": "Security awareness & training", "family": "Administrative",
             "capabilities": ["security_training"], "guidance": "Implement a security awareness/training program."},
            {"id": "164.308(a)(6)", "title": "Security incident procedures", "family": "Administrative",
             "capabilities": ["incident_response_plan"], "guidance": "Document and follow incident procedures."},
            {"id": "164.308(a)(7)(i)", "title": "Contingency plan", "family": "Administrative",
             "capabilities": ["continuity_plan"], "guidance": "Maintain and test a contingency/continuity plan."},
            {"id": "164.308(b)(1)", "title": "Business associate contracts", "family": "Administrative",
             "capabilities": ["vendor_risk_management"], "guidance": "Govern business associates by agreement."},
            {"id": "164.308(a)(3)", "title": "Workforce security & access review", "family": "Administrative",
             "capabilities": ["personnel_security", "access_review"], "guidance": "Authorize/supervise workforce; review access."},
            {"id": "164.308(a)(8)", "title": "Evaluation & testing", "family": "Administrative",
             "capabilities": ["vulnerability_management", "penetration_testing", "change_management"],
             "guidance": "Periodically evaluate safeguards; test and manage change."},
            {"id": "164.310(a)(1)", "title": "Facility access controls", "family": "Physical",
             "capabilities": ["physical_security"], "guidance": "Limit physical access to systems and facilities."},
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
             "capabilities": ["access_control", "external_sharing_control", "guest_access"],
             "guidance": "Least-privilege access; govern external sharing."},
            {"id": "A.5.17", "title": "Authentication information", "family": "Organizational",
             "capabilities": ["mfa", "conditional_access", "phishing_resistant_mfa", "privileged_mfa", "privileged_access_review", "password_policy"],
             "guidance": "Strong authentication and conditional access for privileged use."},
            {"id": "A.8.13", "title": "Information backup", "family": "Technological",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "recovery", "versioning", "offsite_copy", "restore_test", "integrity_verified"],
             "guidance": "Back up information; keep offsite copies with version history and test recovery."},
            {"id": "A.8.14", "title": "Redundancy of information processing", "family": "Technological",
             "capabilities": ["offsite_copy", "air_gapped_copy"],
             "guidance": "Maintain redundant / independent copies of information."},
            {"id": "A.8.24", "title": "Use of cryptography", "family": "Technological",
             "capabilities": ["encryption_at_rest", "encryption_in_transit", "device_encryption", "device_compliance"],
             "guidance": "Encrypt information at rest and in transit."},
            {"id": "A.8.12", "title": "Data leakage prevention", "family": "Technological",
             "capabilities": ["dlp", "external_sharing_control"],
             "guidance": "Prevent data leakage via DLP and sharing controls."},
            {"id": "A.8.15", "title": "Logging", "family": "Technological",
             "capabilities": ["audit_logging", "monitoring", "security_posture"],
             "guidance": "Produce, protect and review event logs."},
            {"id": "A.8.10", "title": "Information deletion & retention", "family": "Technological",
             "capabilities": ["retention", "legal_hold", "data_residency"],
             "guidance": "Retain and delete information per policy; know residency."},
            {"id": "A.5.1", "title": "Policies for information security", "family": "Organizational",
             "capabilities": ["security_policy", "risk_assessment"],
             "guidance": "Define, approve and review information security policies; assess risk."},
            {"id": "A.5.19", "title": "Supplier relationships", "family": "Organizational",
             "capabilities": ["vendor_risk_management"], "guidance": "Manage information security in supplier relationships."},
            {"id": "A.6.3", "title": "Awareness, education & training", "family": "People",
             "capabilities": ["security_training"], "guidance": "Provide security awareness education and training."},
            {"id": "A.6.1", "title": "Screening & personnel security", "family": "People",
             "capabilities": ["personnel_security"], "guidance": "Screen candidates and govern personnel access."},
            {"id": "A.5.24", "title": "Incident management & continuity", "family": "Organizational",
             "capabilities": ["incident_response_plan", "continuity_plan"],
             "guidance": "Plan and prepare incident management and continuity."},
            {"id": "A.8.32", "title": "Change, vulnerability & access management", "family": "Technological",
             "capabilities": ["change_management", "vulnerability_management", "penetration_testing", "access_review"],
             "guidance": "Control changes; manage vulnerabilities/testing; review access."},
            {"id": "A.7.1", "title": "Physical security perimeters", "family": "Physical",
             "capabilities": ["physical_security"], "guidance": "Protect facilities with physical security controls."},
        ],
    },
    "soc2": {
        "label": "SOC 2 (Trust Services)", "version": "2017 TSC", "authority": "AICPA",
        "url": "https://www.aicpa-cima.com/topic/audit-assurance/audit-and-assurance-greater-than-soc-2",
        "description": "SOC 2 Trust Services Criteria Arkive supports — the Security, Availability "
                       "and Confidentiality criteria for protected data.",
        "controls": [
            {"id": "CC6.1", "title": "Logical access security", "family": "Security (CC6)",
             "capabilities": ["access_control", "encryption_at_rest", "external_sharing_control", "guest_access"],
             "guidance": "Restrict logical access; encrypt protected data."},
            {"id": "CC6.6", "title": "Authentication of users", "family": "Security (CC6)",
             "capabilities": ["mfa", "conditional_access", "phishing_resistant_mfa", "privileged_mfa", "privileged_access_review", "password_policy"],
             "guidance": "Strong authentication + conditional access for access to systems."},
            {"id": "CC6.7", "title": "Transmission & movement of data", "family": "Security (CC6)",
             "capabilities": ["encryption_in_transit", "dlp"],
             "guidance": "Protect data in transit; restrict exfiltration."},
            {"id": "CC7.2", "title": "Monitoring for anomalies", "family": "Operations (CC7)",
             "capabilities": ["monitoring", "audit_logging", "security_posture"],
             "guidance": "Monitor systems and log/detect anomalies."},
            {"id": "CC7.3", "title": "Incident response", "family": "Operations (CC7)",
             "capabilities": ["incident_response"],
             "guidance": "Evaluate and respond to security events."},
            {"id": "A1.2", "title": "Backup & recovery for availability", "family": "Availability (A1)",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "recovery", "offsite_copy", "versioning", "restore_test", "integrity_verified"],
             "guidance": "Back up data and maintain recoverability for availability commitments."},
            {"id": "A1.3", "title": "Recovery testing / redundancy", "family": "Availability (A1)",
             "capabilities": ["offsite_copy", "air_gapped_copy", "immutability"],
             "guidance": "Keep redundant, tamper-evident recovery copies."},
            {"id": "C1.1", "title": "Confidential data protection", "family": "Confidentiality (C1)",
             "capabilities": ["data_classification", "encryption_at_rest", "device_encryption", "device_compliance"],
             "guidance": "Identify and protect confidential information."},
            {"id": "C1.2", "title": "Confidential data disposal", "family": "Confidentiality (C1)",
             "capabilities": ["retention", "data_minimization", "legal_hold"],
             "guidance": "Retain and dispose of confidential data per policy."},
            {"id": "CC1.1", "title": "Control environment & policy", "family": "Control environment (CC1)",
             "capabilities": ["security_policy", "personnel_security"],
             "guidance": "Establish a security policy and integrity/ethics; screen personnel."},
            {"id": "CC3.1", "title": "Risk assessment", "family": "Risk assessment (CC3)",
             "capabilities": ["risk_assessment"], "guidance": "Identify and assess risks to objectives."},
            {"id": "CC2.2", "title": "Security awareness & training", "family": "Communication (CC2)",
             "capabilities": ["security_training"], "guidance": "Communicate and train on security responsibilities."},
            {"id": "CC9.2", "title": "Vendor & third-party risk", "family": "Risk mitigation (CC9)",
             "capabilities": ["vendor_risk_management"], "guidance": "Assess and manage vendors and business partners."},
            {"id": "CC7.5", "title": "Incident response & continuity", "family": "Operations (CC7)",
             "capabilities": ["incident_response_plan", "continuity_plan"],
             "guidance": "Maintain incident-response and recovery/continuity plans."},
            {"id": "CC8.1", "title": "Change management", "family": "Change management (CC8)",
             "capabilities": ["change_management", "vulnerability_management", "penetration_testing"],
             "guidance": "Authorize and test changes; manage vulnerabilities."},
            {"id": "CC6.4", "title": "Physical access & access review", "family": "Security (CC6)",
             "capabilities": ["physical_security", "access_review"],
             "guidance": "Restrict physical access; review logical access rights."},
        ],
    },
    "gdpr": {
        "label": "GDPR", "version": "2016/679", "authority": "EU",
        "url": "https://gdpr-info.eu/",
        "description": "EU GDPR obligations Arkive helps evidence — security of processing (Art. 32), "
                       "records & inventory (Art. 30), data minimization (Art. 5), residency and erasure.",
        "controls": [
            {"id": "Art.32(1)(a)", "title": "Encryption of personal data", "family": "Security of processing",
             "capabilities": ["encryption_at_rest", "encryption_in_transit", "device_encryption", "device_compliance"],
             "guidance": "Encrypt personal data at rest and in transit."},
            {"id": "Art.32(1)(b)", "title": "Confidentiality & access control", "family": "Security of processing",
             "capabilities": ["access_control", "mfa", "external_sharing_control", "phishing_resistant_mfa", "privileged_mfa", "privileged_access_review", "guest_access", "password_policy"],
             "guidance": "Ensure ongoing confidentiality via access control and strong auth."},
            {"id": "Art.32(1)(c)", "title": "Restore availability after an incident", "family": "Security of processing",
             "capabilities": ["backup_coverage", "coverage_completeness", "backup_freshness", "recovery", "offsite_copy", "versioning", "restore_test", "integrity_verified"],
             "guidance": "Restore availability and access to personal data in a timely manner."},
            {"id": "Art.32(1)(d)", "title": "Monitoring & audit of processing", "family": "Security of processing",
             "capabilities": ["audit_logging", "monitoring", "security_posture"],
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
            {"id": "Art.24", "title": "Governance, policy & risk", "family": "Accountability",
             "capabilities": ["security_policy", "risk_assessment"],
             "guidance": "Implement policies and demonstrate compliance; assess risk (incl. DPIA)."},
            {"id": "Art.28", "title": "Processors & third parties", "family": "Accountability",
             "capabilities": ["vendor_risk_management"], "guidance": "Govern processors by contract and due diligence."},
            {"id": "Art.39", "title": "Awareness & personnel", "family": "Accountability",
             "capabilities": ["security_training", "personnel_security"],
             "guidance": "Train staff and govern personnel handling personal data."},
            {"id": "Art.33", "title": "Breach response & continuity", "family": "Security of processing",
             "capabilities": ["incident_response_plan", "continuity_plan"],
             "guidance": "Maintain breach-response and continuity plans (72-hour notification)."},
            {"id": "Art.32(2)", "title": "Security testing & operations", "family": "Security of processing",
             "capabilities": ["vulnerability_management", "penetration_testing", "change_management", "access_review", "physical_security"],
             "guidance": "Regularly test and evaluate the security of processing."},
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
