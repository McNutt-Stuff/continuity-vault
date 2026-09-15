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
             "capabilities": ["backup_coverage", "encryption_at_rest"],
             "guidance": "Create and protect backups of critical data."},
            {"id": "PR.AA-05", "title": "Least-privilege access", "family": "Protect",
             "capabilities": ["access_control", "mfa"],
             "guidance": "Enforce least privilege and strong authentication."},
            {"id": "DE.AE-03", "title": "Event data collection", "family": "Detect",
             "capabilities": ["audit_logging", "monitoring"],
             "guidance": "Collect and correlate audit/event data."},
            {"id": "RS.MA-01", "title": "Incident response", "family": "Respond",
             "capabilities": ["incident_response"],
             "guidance": "Surface failures and security events for response."},
            {"id": "RC.RP-01", "title": "Recovery execution", "family": "Recover",
             "capabilities": ["recovery", "backup_coverage"],
             "guidance": "Execute recovery of data from backups."},
            {"id": "GV.PO-01", "title": "Retention policy", "family": "Govern",
             "capabilities": ["retention", "legal_hold"],
             "guidance": "Establish and enforce data retention + legal hold."},
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
             "capabilities": ["retention", "legal_hold"], "guidance": "Enforce retention."},
            {"id": "3.11", "title": "Encrypt sensitive data at rest", "family": "Data protection",
             "capabilities": ["encryption_at_rest", "immutability"], "guidance": "Encrypt at rest."},
            {"id": "3.10", "title": "Encrypt sensitive data in transit", "family": "Data protection",
             "capabilities": ["encryption_in_transit"], "guidance": "Encrypt in transit."},
            {"id": "3.3", "title": "Configure data access control lists", "family": "Data protection",
             "capabilities": ["access_control"], "guidance": "Least-privilege data access."},
            {"id": "6.3", "title": "Require MFA", "family": "Access control",
             "capabilities": ["mfa"], "guidance": "Require MFA for privileged access."},
            {"id": "8.2", "title": "Collect audit logs", "family": "Audit log management",
             "capabilities": ["audit_logging"], "guidance": "Collect audit logs."},
            {"id": "8.11", "title": "Review audit logs", "family": "Audit log management",
             "capabilities": ["monitoring"], "guidance": "Monitor/review audit logs."},
            {"id": "11.2", "title": "Perform automated backups", "family": "Data recovery",
             "capabilities": ["backup_coverage"], "guidance": "Automated backups."},
            {"id": "11.1", "title": "Data recovery process", "family": "Data recovery",
             "capabilities": ["recovery"], "guidance": "Maintain a recovery process."},
            {"id": "11.3", "title": "Protect recovery data", "family": "Data recovery",
             "capabilities": ["encryption_at_rest", "immutability"], "guidance": "Protect backups."},
        ],
    },
    "hipaa": {
        "label": "HIPAA Security Rule", "version": "45 CFR 164", "authority": "HHS",
        "url": "https://www.hhs.gov/hipaa/for-professionals/security",
        "description": "HIPAA Security Rule safeguards Arkive supports — backup, recovery, "
                       "encryption, access control, audit and retention of ePHI.",
        "controls": [
            {"id": "164.308(a)(7)(ii)(A)", "title": "Data backup plan", "family": "Administrative",
             "capabilities": ["backup_coverage"], "guidance": "Back up ePHI."},
            {"id": "164.308(a)(7)(ii)(B)", "title": "Disaster recovery plan", "family": "Administrative",
             "capabilities": ["recovery", "backup_coverage"], "guidance": "Restore ePHI."},
            {"id": "164.312(a)(2)(iv)", "title": "Encryption & decryption", "family": "Technical",
             "capabilities": ["encryption_at_rest"], "guidance": "Encrypt ePHI at rest."},
            {"id": "164.312(e)(2)(ii)", "title": "Transmission encryption", "family": "Technical",
             "capabilities": ["encryption_in_transit"], "guidance": "Encrypt ePHI in transit."},
            {"id": "164.312(a)(1)", "title": "Access control", "family": "Technical",
             "capabilities": ["access_control", "mfa"], "guidance": "Restrict ePHI access."},
            {"id": "164.312(b)", "title": "Audit controls", "family": "Technical",
             "capabilities": ["audit_logging"], "guidance": "Audit ePHI access."},
            {"id": "164.312(c)(1)", "title": "Integrity", "family": "Technical",
             "capabilities": ["immutability"], "guidance": "Protect ePHI from improper alteration."},
            {"id": "164.316(b)(2)(i)", "title": "Retention (6 years)", "family": "Administrative",
             "capabilities": ["retention", "legal_hold"], "guidance": "Retain records."},
            {"id": "164.308(a)(1)(ii)(D)", "title": "Information system activity review",
             "family": "Administrative", "capabilities": ["monitoring"],
             "guidance": "Review activity logs."},
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
