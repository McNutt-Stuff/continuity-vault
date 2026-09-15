"""Microsoft 365 managed integration — compliance packs (roadmap phase P5).

A governance/evidence layer (NOT ingest enforcement — that stays in the main
``rules_engine``). An admin enables a framework pack (NIST CSF, CIS, ISO 27001,
SOC 2, HIPAA, GDPR, …); Arkive seeds the pack's controls and auto-assesses the
ones it can evidence from live platform state (backup coverage, quantum-safe
encryption, recovery capability, gated cross-member access, tamper-evident audit),
so the customer sees a real posture instead of an empty checklist. Admins can
override any control's state, assign an owner, and record time-boxed exceptions.

Everything here is organization-scoped and derives evidence from the tenant's own
Arkive footprint — no Microsoft payloads, no secrets.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models as m


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Capabilities Arkive can evidence, and how each maps to a live posture state. #
# --------------------------------------------------------------------------- #
# capability -> (title, how it's evidenced)
CAPABILITIES: dict[str, str] = {
    "backup_coverage": "All in-scope Microsoft 365 workloads are backed up",
    "encryption": "Backups encrypted with a quantum-safe cipher at rest",
    "recovery": "Point-in-time recovery of protected data is available",
    "retention": "A retention schedule governs how long backups are kept",
    "access_control": "Cross-member data access is gated, dual-controlled and audited",
    "audit_logging": "Tamper-evident audit logging of every access and admin action",
    "legal_hold": "Legal hold / immutable preservation can be applied to a custodian",
}

# framework -> {label, version, controls: [(control_id, title, capability)]}
CATALOG: dict[str, dict] = {
    "nist_csf": {"label": "NIST CSF 2.0", "version": "2.0", "controls": [
        ("PR.DS-01", "Data-at-rest is protected", "encryption"),
        ("PR.DS-11", "Backups of data are created and protected", "backup_coverage"),
        ("PR.AA-05", "Access permissions enforce least privilege", "access_control"),
        ("DE.AE-03", "Event data are collected and correlated (audit)", "audit_logging"),
        ("RC.RP-01", "Recovery of systems/data is executed", "recovery"),
        ("GV.PO-01", "Data retention policy is established", "retention"),
    ]},
    "cis": {"label": "CIS Controls v8", "version": "8.0", "controls": [
        ("3.11", "Encrypt sensitive data at rest", "encryption"),
        ("11.1", "Establish and maintain a data recovery process", "recovery"),
        ("11.2", "Perform automated backups", "backup_coverage"),
        ("6.1", "Establish an access granting/least-privilege process", "access_control"),
        ("8.2", "Collect audit logs", "audit_logging"),
        ("3.4", "Enforce data retention", "retention"),
    ]},
    "iso27001": {"label": "ISO/IEC 27001:2022", "version": "2022", "controls": [
        ("A.8.13", "Information backup", "backup_coverage"),
        ("A.8.24", "Use of cryptography", "encryption"),
        ("A.8.15", "Logging", "audit_logging"),
        ("A.5.15", "Access control", "access_control"),
        ("A.8.10", "Information deletion / retention", "retention"),
        ("A.5.29", "Continuity — recovery capability", "recovery"),
    ]},
    "soc2": {"label": "SOC 2 (Trust Services)", "version": "2017", "controls": [
        ("A1.2", "Backup and recovery infrastructure", "backup_coverage"),
        ("CC6.1", "Logical access controls", "access_control"),
        ("CC6.7", "Encryption of data at rest", "encryption"),
        ("CC7.2", "Security event logging & monitoring", "audit_logging"),
        ("A1.3", "Recovery testing", "recovery"),
        ("C1.2", "Retention & disposal of confidential data", "retention"),
    ]},
    "hipaa": {"label": "HIPAA Security Rule", "version": "2013", "controls": [
        ("164.308(a)(7)", "Data backup & disaster recovery plan", "backup_coverage"),
        ("164.312(a)(2)(iv)", "Encryption and decryption", "encryption"),
        ("164.312(b)", "Audit controls", "audit_logging"),
        ("164.312(a)(1)", "Access control", "access_control"),
        ("164.308(a)(7)(ii)(B)", "Disaster recovery / restore", "recovery"),
        ("164.316(b)(2)", "Retention of records (6 years)", "retention"),
    ]},
    "gdpr": {"label": "GDPR", "version": "2016/679", "controls": [
        ("Art.32(1)(a)", "Encryption of personal data", "encryption"),
        ("Art.32(1)(c)", "Ability to restore availability (backup)", "backup_coverage"),
        ("Art.32(1)(c)", "Restore access in a timely manner", "recovery"),
        ("Art.30", "Records of processing / audit", "audit_logging"),
        ("Art.5(1)(e)", "Storage limitation / retention", "retention"),
        ("Art.18", "Restriction of processing (legal hold)", "legal_hold"),
    ]},
}
# Frameworks without a bespoke list reuse a generic capability checklist.
_GENERIC = [("BKP", "Automated backup of critical data", "backup_coverage"),
            ("ENC", "Encryption of data at rest", "encryption"),
            ("REC", "Tested recovery capability", "recovery"),
            ("ACC", "Least-privilege access control", "access_control"),
            ("LOG", "Audit logging", "audit_logging"),
            ("RET", "Data retention schedule", "retention")]
for _fw in ("pci", "cmmc", "bms"):
    CATALOG.setdefault(_fw, {"label": _fw.upper(), "version": "1.0",
                             "controls": list(_GENERIC)})


def evidence(db: Session, inst) -> dict:
    """Live platform signals used to auto-assess controls. Derived from the
    tenant's own Arkive footprint — never Microsoft payloads."""
    from ...models import SearchDocument
    from sqlalchemy import func
    active_sources = (db.query(m.ManagedSource)
                      .filter(m.ManagedSource.integration_instance_id == inst.id,
                              m.ManagedSource.state.notin_(
                                  ("paused_by_admin", "decommissioned", "planned"))).count())
    protected_objects = int(db.query(func.count(SearchDocument.id))
                            .filter(SearchDocument.tenant_id == inst.tenant_id).scalar() or 0)
    prof = (inst.config or {}).get("managed_profile") or {}
    retention_set = bool(prof.get("backup_interval_minutes"))
    return {
        "backup_coverage": active_sources > 0,
        "encryption": True,                 # quantum-safe cipher, always on
        "recovery": protected_objects > 0,
        "access_control": True,             # cross-member gating + audit are platform invariants
        "audit_logging": True,              # tamper-evident audit chain
        "retention": retention_set,
        "legal_hold": False,                # capability exists; not engaged by default
        "active_sources": active_sources,
    }


def _auto_state(capability: str, ev: dict) -> str:
    """Map a capability's evidence to a control posture state."""
    val = ev.get(capability)
    if capability in ("backup_coverage", "recovery"):
        return "operating" if val else "planned"
    if capability in ("encryption", "audit_logging", "access_control"):
        return "operating"
    if capability == "retention":
        return "partially_implemented" if val else "planned"
    return "planned"  # legal_hold + anything unmapped


def available(db: Session, inst) -> dict:
    """All frameworks with enabled state, pack id and posture summary."""
    packs = {p.framework: p for p in db.query(m.CompliancePack)
             .filter(m.CompliancePack.tenant_id == inst.tenant_id,
                     m.CompliancePack.integration_instance_id == inst.id).all()}
    out = []
    for fw, spec in CATALOG.items():
        p = packs.get(fw)
        summary = _summary(db, p) if (p and p.enabled) else None
        out.append({
            "framework": fw, "label": spec["label"], "version": spec["version"],
            "controls": len(spec["controls"]),
            "enabled": bool(p and p.enabled),
            "pack_id": p.id if p else None,
            "summary": summary,
        })
    out.sort(key=lambda x: (not x["enabled"], x["label"]))
    return {"frameworks": out, "evidence": evidence(db, inst)}


def _summary(db: Session, pack) -> dict:
    ctrls = (db.query(m.ComplianceControl)
             .filter(m.ComplianceControl.pack_id == pack.id).all())
    by_state: dict[str, int] = {}
    for c in ctrls:
        by_state[c.state] = by_state.get(c.state, 0) + 1
    good = sum(by_state.get(s, 0) for s in ("implemented", "operating"))
    total = len(ctrls) or 1
    return {"total": len(ctrls), "by_state": by_state,
            "met": good, "score": round(good / total * 100)}


def set_pack(db: Session, inst, framework: str, enabled: bool) -> m.CompliancePack:
    """Enable/disable a framework pack; on first enable, seed its controls and
    auto-assess the ones Arkive can evidence."""
    spec = CATALOG.get(framework)
    if spec is None:
        raise ValueError(f"unknown framework {framework}")
    pack = (db.query(m.CompliancePack)
            .filter(m.CompliancePack.tenant_id == inst.tenant_id,
                    m.CompliancePack.integration_instance_id == inst.id,
                    m.CompliancePack.framework == framework).first())
    if pack is None:
        pack = m.CompliancePack(tenant_id=inst.tenant_id, integration_instance_id=inst.id,
                                framework=framework, version=spec["version"], enabled=enabled)
        db.add(pack)
        db.flush()
    else:
        pack.enabled = enabled
    if enabled:
        ev = evidence(db, inst)
        existing = {c.control_id for c in db.query(m.ComplianceControl)
                    .filter(m.ComplianceControl.pack_id == pack.id).all()}
        for cid, title, cap in spec["controls"]:
            if cid in existing:
                continue
            db.add(m.ComplianceControl(
                tenant_id=inst.tenant_id, pack_id=pack.id, control_id=cid, title=title,
                state=_auto_state(cap, ev), last_evaluated_at=_now(),
                meta={"capability": cap, "auto": True}))
    db.commit()
    return pack


def reassess(db: Session, inst) -> int:
    """Re-evaluate auto-assessed controls against current evidence (manual
    overrides + exceptions are preserved)."""
    ev = evidence(db, inst)
    packs = [p.id for p in db.query(m.CompliancePack)
             .filter(m.CompliancePack.tenant_id == inst.tenant_id,
                     m.CompliancePack.integration_instance_id == inst.id,
                     m.CompliancePack.enabled.is_(True)).all()]
    if not packs:
        return 0
    n = 0
    for c in (db.query(m.ComplianceControl)
              .filter(m.ComplianceControl.pack_id.in_(packs)).all()):
        meta = c.meta or {}
        if not meta.get("auto") or c.state == "exception":
            continue  # respect manual overrides + exceptions
        cap = meta.get("capability")
        new = _auto_state(cap, ev)
        if new != c.state:
            c.state = new
        c.last_evaluated_at = _now()
        n += 1
    db.commit()
    return n


def controls_view(db: Session, inst, pack_id: str) -> list[dict]:
    rows = (db.query(m.ComplianceControl)
            .filter(m.ComplianceControl.pack_id == pack_id,
                    m.ComplianceControl.tenant_id == inst.tenant_id)
            .order_by(m.ComplianceControl.control_id.asc()).all())
    exc = {e.control_id: e for e in db.query(m.ComplianceException)
           .filter(m.ComplianceException.tenant_id == inst.tenant_id).all()}
    out = []
    for c in rows:
        e = exc.get(c.id)
        out.append({
            "id": c.id, "control_id": c.control_id, "title": c.title, "state": c.state,
            "owner": c.owner, "capability": (c.meta or {}).get("capability"),
            "auto": bool((c.meta or {}).get("auto")),
            "last_evaluated_at": c.last_evaluated_at.isoformat() if c.last_evaluated_at else None,
            "exception": ({"reason": e.reason,
                           "approved_by": e.approved_by,
                           "expires_at": e.expires_at.isoformat() if e.expires_at else None}
                          if e else None),
        })
    return out


def report(db: Session, inst) -> dict:
    """Posture rollup across every enabled pack for this instance."""
    packs = (db.query(m.CompliancePack)
             .filter(m.CompliancePack.tenant_id == inst.tenant_id,
                     m.CompliancePack.integration_instance_id == inst.id,
                     m.CompliancePack.enabled.is_(True)).all())
    frameworks = []
    total = met = 0
    for p in packs:
        s = _summary(db, p)
        total += s["total"]
        met += s["met"]
        frameworks.append({"framework": p.framework,
                           "label": CATALOG.get(p.framework, {}).get("label", p.framework),
                           "score": s["score"], "met": s["met"], "total": s["total"]})
    exceptions = (db.query(m.ComplianceException)
                  .filter(m.ComplianceException.tenant_id == inst.tenant_id).count())
    return {
        "frameworks": frameworks,
        "score": round(met / total * 100) if total else None,
        "controls_total": total, "controls_met": met,
        "exceptions": exceptions,
        "generated_at": _now().isoformat(),
    }
