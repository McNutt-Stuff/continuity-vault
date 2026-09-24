"""Compliance engine API — framework posture for Business/Enterprise organizations.

Feature-flagged (``compliance_enabled``), Business/Enterprise only, org-admin
gated. Reads/writes flow through ``compliance.engine``; every mutation is audited
and recorded in the compliance change ledger (posture history).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, features, security
from ..compliance import engine, models as m, registry
from ..db import get_db
from ..models import Tenant, User

router = APIRouter(prefix="/compliance", tags=["compliance"])

_PLANS = {"business", "enterprise"}


def require_compliance(principal: security.Principal = Depends(security.require_org_admin),
                       db: Session = Depends(get_db)) -> security.Principal:
    tenant = db.get(Tenant, principal.tenant_id)
    user = db.get(User, principal.user_id)
    plan = (tenant.plan if tenant else "") or ""
    if plan.lower() not in _PLANS:
        raise HTTPException(403, "Compliance requires the Business or Enterprise plan")
    if not features.resolve(user, tenant, "compliance_enabled", db):
        raise HTTPException(403, "The Compliance engine isn't enabled for your organization")
    return principal


def _tenant(db: Session, principal: security.Principal) -> Tenant:
    t = db.get(Tenant, principal.tenant_id)
    if t is None:
        raise HTTPException(404, "tenant not found")
    return t


@router.get("/catalog")
def catalog(principal: security.Principal = Depends(require_compliance)):
    """The framework + control + capability registry (static reference)."""
    return {
        "capabilities": registry.CAPABILITIES,
        "frameworks": [{"framework": fw, "label": s["label"], "version": s["version"],
                        "authority": s.get("authority"), "url": s.get("url"),
                        "description": s.get("description"),
                        "controls": s["controls"]}
                       for fw, s in registry.FRAMEWORKS.items()],
    }


@router.get("/frameworks")
def frameworks(principal: security.Principal = Depends(require_compliance),
               db: Session = Depends(get_db)):
    """Every framework with enabled state + latest score."""
    return {"frameworks": engine.available(db, _tenant(db, principal))}


class FrameworkToggle(BaseModel):
    enabled: bool


@router.post("/frameworks/{framework}")
def set_framework(framework: str, body: FrameworkToggle,
                  principal: security.Principal = Depends(require_compliance),
                  db: Session = Depends(get_db)):
    """Enable/disable a framework (seeds controls + assesses on enable)."""
    tenant = _tenant(db, principal)
    try:
        engine.enable_pack(db, tenant, framework, body.enabled, actor=principal.user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(db, actor=principal.user_id, action="compliance.framework",
                 category="admin", resource=framework,
                 detail={"framework": framework, "enabled": body.enabled})
    return {"frameworks": engine.available(db, tenant), "report": engine.report(db, tenant)}


@router.post("/evaluate")
def evaluate(principal: security.Principal = Depends(require_compliance),
             db: Session = Depends(get_db)):
    """Re-assess every enabled framework against current live evidence."""
    tenant = _tenant(db, principal)
    res = engine.evaluate(db, tenant, actor=principal.user_id)
    return {**res, "report": engine.report(db, tenant)}


@router.get("/report")
def report(principal: security.Principal = Depends(require_compliance),
           db: Session = Depends(get_db)):
    return engine.report(db, _tenant(db, principal))


@router.get("/controls")
def controls(framework: str,
             principal: security.Principal = Depends(require_compliance),
             db: Session = Depends(get_db)):
    return {"controls": engine.controls_view(db, _tenant(db, principal), framework)}


@router.get("/framework/{framework}")
def framework_detail(framework: str,
                     principal: security.Principal = Depends(require_compliance),
                     db: Session = Depends(get_db)):
    """The dedicated framework dashboard bundle: score + trend, controls, drivers,
    open issues and the specific troubling accounts/systems."""
    if registry.framework(framework) is None:
        raise HTTPException(404, "unknown framework")
    return engine.framework_detail(db, _tenant(db, principal), framework)


@router.get("/history")
def history(framework: str = "",
            principal: security.Principal = Depends(require_compliance),
            db: Session = Depends(get_db)):
    return engine.history(db, _tenant(db, principal), framework)


class ControlUpdate(BaseModel):
    state: str | None = None
    owner: str | None = None


@router.patch("/controls/{control_id}")
def update_control(control_id: str, body: ControlUpdate,
                   principal: security.Principal = Depends(require_compliance),
                   db: Session = Depends(get_db)):
    """Override a control's state / owner (a manual assessment the engine won't clobber)."""
    tenant = _tenant(db, principal)
    c = db.get(m.ComplianceControl, control_id)
    if c is None or c.tenant_id != tenant.id:
        raise HTTPException(404, "control not found")
    prior = c.state
    if body.state:
        c.state = body.state
        c.auto = False
    if body.owner is not None:
        c.owner = body.owner
    c.last_evaluated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(m.ComplianceEvent(tenant_id=tenant.id, framework=c.framework, control_id=c.control_id,
                             kind="control_state", actor=principal.user_id,
                             summary=f"{c.control_id}: {prior} → {c.state} (manual)",
                             detail={"from": prior, "to": c.state, "manual": True}))
    db.commit()
    audit.record(db, actor=principal.user_id, action="compliance.control",
                 category="admin", resource=control_id,
                 detail={"control": c.control_id, "state": c.state})
    return {"ok": True, "state": c.state, "owner": c.owner}


class ExceptionBody(BaseModel):
    reason: str
    expires_at: str | None = None


@router.post("/controls/{control_id}/exception")
def add_exception(control_id: str, body: ExceptionBody,
                  principal: security.Principal = Depends(require_compliance),
                  db: Session = Depends(get_db)):
    """Record a time-boxed, audited exception for a control (state → exception)."""
    tenant = _tenant(db, principal)
    c = db.get(m.ComplianceControl, control_id)
    if c is None or c.tenant_id != tenant.id:
        raise HTTPException(404, "control not found")
    if not (body.reason or "").strip():
        raise HTTPException(400, "a reason is required")
    exp = None
    if body.expires_at:
        try:
            exp = datetime.fromisoformat(body.expires_at.replace("Z", "+00:00"))
            exp = exp.replace(tzinfo=None) if exp.tzinfo else exp
        except ValueError:
            raise HTTPException(400, "invalid expires_at")
    for e in db.query(m.ComplianceException).filter(
            m.ComplianceException.control_id == control_id).all():
        db.delete(e)
    db.add(m.ComplianceException(tenant_id=tenant.id, control_id=control_id,
                                 reason=body.reason.strip(),
                                 approved_by=principal.user_id, expires_at=exp))
    c.state = "exception"
    c.auto = False
    db.add(m.ComplianceEvent(tenant_id=tenant.id, framework=c.framework, control_id=c.control_id,
                             kind="exception", actor=principal.user_id,
                             summary=f"Exception recorded for {c.control_id}",
                             detail={"reason": body.reason.strip()[:200]}))
    db.commit()
    audit.record(db, actor=principal.user_id, action="compliance.exception",
                 category="admin", severity="warning", resource=control_id,
                 detail={"control": c.control_id, "reason": body.reason.strip()[:200]})
    return {"ok": True}


def _now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/attestations")
def list_attestations(principal: security.Principal = Depends(require_compliance),
                      db: Session = Depends(get_db)):
    """The self-attestation questionnaire: every attestable capability (procedural /
    policy control the engine can't measure) + the tenant's current answer."""
    tenant = _tenant(db, principal)
    from ..compliance.models import ComplianceAttestation
    rows = {a.capability: a for a in db.query(ComplianceAttestation)
            .filter(ComplianceAttestation.tenant_id == tenant.id).all()}
    docs_by_cap: dict[str, list] = {}
    from ..compliance.models import ComplianceEvidenceDoc
    for d in (db.query(ComplianceEvidenceDoc)
              .filter(ComplianceEvidenceDoc.tenant_id == tenant.id)
              .order_by(ComplianceEvidenceDoc.uploaded_at.desc()).all()):
        docs_by_cap.setdefault(d.capability, []).append({
            "id": d.id, "filename": d.filename, "size_bytes": d.size_bytes,
            "content_type": d.content_type, "uploaded_by": d.uploaded_by,
            "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None})
    # Which enabled frameworks reference each attestable capability (for context).
    fw_by_cap: dict[str, list[str]] = {}
    enabled = {p.framework for p in db.query(m.CompliancePack)
               .filter(m.CompliancePack.tenant_id == tenant.id, m.CompliancePack.enabled.is_(True)).all()}
    for fw in enabled:
        spec = registry.framework(fw)
        if not spec:
            continue
        for ctrl in spec["controls"]:
            for cap in ctrl.get("capabilities", []):
                fw_by_cap.setdefault(cap, [])
                if spec["label"] not in fw_by_cap[cap]:
                    fw_by_cap[cap].append(spec["label"])
    now = _now_naive()
    items = []
    for cap, spec in registry.CAPABILITIES.items():
        if not spec.get("attestable"):
            continue
        a = rows.get(cap)
        items.append({
            "capability": cap, "title": spec["title"], "description": spec["description"],
            "domain": spec.get("domain", ""), "frameworks": fw_by_cap.get(cap, []),
            "status": (a.status if a else ""), "note": (a.note if a else ""),
            "evidence_url": (a.evidence_url if a else ""),
            "attested_by": (a.attested_by if a else ""),
            "attested_at": (a.attested_at.isoformat() if (a and a.attested_at) else None),
            "review_due_at": (a.review_due_at.isoformat() if (a and a.review_due_at) else None),
            "stale": bool(a and a.review_due_at and a.review_due_at < now),
            "documents": docs_by_cap.get(cap, []),
        })
    items.sort(key=lambda x: (x["domain"], x["title"]))
    return {"attestations": items}


class AttestationBody(BaseModel):
    capability: str
    status: str  # met | partial | unmet | not_applicable
    note: str = ""
    evidence_url: str = ""
    review_months: int = 12  # 0 = no review deadline


@router.post("/attestations")
def submit_attestation(body: AttestationBody,
                       principal: security.Principal = Depends(require_compliance),
                       db: Session = Depends(get_db)):
    """Record (upsert) a self-attestation for one attestable capability, audited, and
    re-score so the answer reflects immediately."""
    tenant = _tenant(db, principal)
    from ..compliance.models import ComplianceAttestation
    spec = registry.CAPABILITIES.get(body.capability)
    if not spec or not spec.get("attestable"):
        raise HTTPException(400, "not an attestable capability")
    if body.status not in ("met", "partial", "unmet", "not_applicable"):
        raise HTTPException(400, "invalid status")
    user = db.get(User, principal.user_id)
    now = _now_naive()
    row = (db.query(ComplianceAttestation)
           .filter(ComplianceAttestation.tenant_id == tenant.id,
                   ComplianceAttestation.capability == body.capability).first())
    if row is None:
        row = ComplianceAttestation(tenant_id=tenant.id, capability=body.capability)
        db.add(row)
    row.status = body.status
    row.note = (body.note or "")[:2000]
    row.evidence_url = (body.evidence_url or "")[:500]
    row.attested_by = ((user.email if user else None) or principal.user_id or "")
    row.attested_at = now
    months = max(0, min(60, int(body.review_months or 0)))
    row.review_due_at = (now + timedelta(days=30 * months)) if months else None
    db.commit()
    audit.record(db, actor=row.attested_by, action="compliance.attest",
                 tenant_id=tenant.id, resource=body.capability, category="admin",
                 severity="info",
                 detail={"capability": body.capability, "status": body.status})
    try:
        engine.evaluate(db, tenant, actor=row.attested_by)
    except Exception:  # noqa: BLE001 — never fail the attestation on a re-score hiccup
        pass
    return {"ok": True}


_MAX_DOC_BYTES = 25 * 1024 * 1024  # policy documents are small; cap at 25 MB


def _tenant_prefix(tenant: Tenant) -> str:
    return getattr(tenant, "storage_prefix", None) or f"t-{tenant.id[:8]}"


@router.post("/attestations/{capability}/document")
async def upload_evidence_doc(capability: str, file: UploadFile = File(...),
                              principal: security.Principal = Depends(require_compliance),
                              db: Session = Depends(get_db)):
    """Attach an encrypted proof document (policy/plan PDF) to an attestable
    capability. Stored ciphertext-only in Arkive Cloud under the tenant prefix."""
    import hashlib
    from ..compliance.models import ComplianceEvidenceDoc
    from .. import credstore
    from ..storage import build_destination
    tenant = _tenant(db, principal)
    spec = registry.CAPABILITIES.get(capability)
    if not spec or not spec.get("attestable"):
        raise HTTPException(400, "not an attestable capability")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    if len(raw) > _MAX_DOC_BYTES:
        raise HTTPException(413, "file too large (25 MB max)")
    user = db.get(User, principal.user_id)
    doc = ComplianceEvidenceDoc(
        tenant_id=tenant.id, capability=capability,
        filename=(file.filename or "document")[:200],
        content_type=(file.content_type or "application/octet-stream")[:100],
        size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
        uploaded_by=((user.email if user else None) or principal.user_id or ""),
        storage_dest="cv-cloud")
    doc.storage_key = f"compliance/{doc.id}"
    cipher = credstore.encrypt_bytes(f"compliance:{tenant.id}", raw)
    try:
        build_destination("cv-cloud").put_object(_tenant_prefix(tenant), doc.storage_key,
                                                 cipher, immutable=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"couldn't store the document: {exc}")
    db.add(doc)
    db.commit()
    audit.record(db, actor=doc.uploaded_by, action="compliance.evidence_upload",
                 tenant_id=tenant.id, resource=capability, category="admin", severity="info",
                 detail={"capability": capability, "filename": doc.filename, "bytes": doc.size_bytes})
    return {"id": doc.id, "filename": doc.filename, "size_bytes": doc.size_bytes,
            "content_type": doc.content_type, "uploaded_by": doc.uploaded_by,
            "uploaded_at": doc.uploaded_at.isoformat() if doc.uploaded_at else None}


@router.get("/documents/{doc_id}")
def download_evidence_doc(doc_id: str,
                          principal: security.Principal = Depends(require_compliance),
                          db: Session = Depends(get_db)):
    """Stream a decrypted evidence document (org-admin gated + audited)."""
    from ..compliance.models import ComplianceEvidenceDoc
    from .. import credstore
    from ..storage import build_destination
    tenant = _tenant(db, principal)
    doc = db.get(ComplianceEvidenceDoc, doc_id)
    if doc is None or doc.tenant_id != tenant.id:
        raise HTTPException(404, "document not found")
    try:
        cipher = build_destination(doc.storage_dest or "cv-cloud").get_object(
            _tenant_prefix(tenant), doc.storage_key)
        raw = credstore.decrypt_bytes(f"compliance:{tenant.id}", cipher)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"couldn't read the document: {exc}")
    audit.record(db, actor=principal.user_id, action="compliance.evidence_download",
                 tenant_id=tenant.id, resource=doc.capability, category="admin", severity="info",
                 detail={"document": doc.id, "filename": doc.filename})
    return Response(content=raw, media_type=doc.content_type or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{doc.filename}"'})


@router.delete("/documents/{doc_id}")
def delete_evidence_doc(doc_id: str,
                        principal: security.Principal = Depends(require_compliance),
                        db: Session = Depends(get_db)):
    from ..compliance.models import ComplianceEvidenceDoc
    tenant = _tenant(db, principal)
    doc = db.get(ComplianceEvidenceDoc, doc_id)
    if doc is None or doc.tenant_id != tenant.id:
        raise HTTPException(404, "document not found")
    cap = doc.capability
    fn = doc.filename
    db.delete(doc)  # the encrypted object is left orphaned (unreadable) — no WORM delete
    db.commit()
    audit.record(db, actor=principal.user_id, action="compliance.evidence_delete",
                 tenant_id=tenant.id, resource=cap, category="admin", severity="warning",
                 detail={"document": doc_id, "filename": fn})
    return {"ok": True}
