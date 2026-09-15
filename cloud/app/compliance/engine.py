"""Compliance engine — evaluate controls from evidence, score frameworks, and
record posture history + a change ledger.

Flow: ``evaluate`` collects capability evidence from every provider, derives each
enabled control's state + score (best status per capability), preserves manual
overrides and exceptions, writes an evidence trail, emits change events, and takes
a per-framework posture snapshot for the trend view.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import registry
from . import models as m
from .providers import collect_evidence, refresh_all


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


_STATUS_SCORE = {"met": 100, "partial": 50, "unmet": 0, "unknown": 0}
_STATUS_RANK = {"met": 3, "partial": 2, "unmet": 1, "unknown": 0, "not_applicable": -1}
# Final control state -> contribution to the framework score (exception = accepted risk).
_STATE_SCORE = {"operating": 100, "implemented": 100, "exception": 100,
                "partially_implemented": 50, "planned": 0, "not_assessed": 0,
                "failed": 0, "not_applicable": None}


def available(db: Session, tenant) -> list[dict]:
    """Every framework in the registry with enabled state + latest score."""
    packs = {p.framework: p for p in db.query(m.CompliancePack)
             .filter(m.CompliancePack.tenant_id == tenant.id).all()}
    out = []
    for fw, spec in registry.FRAMEWORKS.items():
        p = packs.get(fw)
        snap = None
        if p and p.enabled:
            snap = (db.query(m.ComplianceSnapshot)
                    .filter(m.ComplianceSnapshot.tenant_id == tenant.id,
                            m.ComplianceSnapshot.framework == fw)
                    .order_by(m.ComplianceSnapshot.taken_at.desc()).first())
        out.append({
            "framework": fw, "label": spec["label"], "version": spec["version"],
            "authority": spec.get("authority"), "url": spec.get("url"),
            "description": spec.get("description"),
            "controls": len(spec["controls"]),
            "enabled": bool(p and p.enabled),
            "score": snap.score if snap else None,
            "met": snap.controls_met if snap else None,
            "total": snap.controls_total if snap else None,
        })
    out.sort(key=lambda x: (not x["enabled"], x["label"]))
    return out


def _event(db, tenant, *, kind, framework="", control_id="", actor="", summary="", detail=None):
    db.add(m.ComplianceEvent(tenant_id=tenant.id, framework=framework, control_id=control_id,
                             kind=kind, actor=actor or "engine", summary=summary,
                             detail=detail or {}))


def enable_pack(db: Session, tenant, framework: str, enabled: bool, actor: str = "") -> m.CompliancePack:
    spec = registry.framework(framework)
    if spec is None:
        raise ValueError(f"unknown framework {framework}")
    pack = (db.query(m.CompliancePack)
            .filter(m.CompliancePack.tenant_id == tenant.id,
                    m.CompliancePack.framework == framework).first())
    if pack is None:
        pack = m.CompliancePack(tenant_id=tenant.id, framework=framework,
                                version=spec["version"], enabled=enabled)
        db.add(pack)
        db.flush()
    else:
        pack.enabled = enabled
    if enabled:
        have = {c.control_id for c in db.query(m.ComplianceControl)
                .filter(m.ComplianceControl.pack_id == pack.id).all()}
        for ctrl in spec["controls"]:
            if ctrl["id"] in have:
                continue
            db.add(m.ComplianceControl(
                tenant_id=tenant.id, pack_id=pack.id, framework=framework,
                control_id=ctrl["id"], title=ctrl["title"], family=ctrl.get("family", ""),
                capabilities=ctrl.get("capabilities", []), state="not_assessed"))
    _event(db, tenant, kind="pack_enabled" if enabled else "pack_disabled",
           framework=framework, actor=actor,
           summary=f"{spec['label']} {'enabled' if enabled else 'disabled'}")
    db.commit()
    if enabled:
        evaluate(db, tenant, actor=actor, only_framework=framework)
    return pack


def _derive(caps: list[str], by_cap: dict) -> tuple[str, int, list]:
    """Best-status-per-capability → control state + score + evidence rows kept."""
    scores: list[int] = []
    kept = []
    all_status: list[str] = []
    for cap in caps:
        rows = by_cap.get(cap, [])
        kept.extend(rows)
        applicable = [r for r in rows if r.status != "not_applicable"]
        if not applicable:
            best = "unknown"
        else:
            best = max((r.status for r in applicable), key=lambda s: _STATUS_RANK.get(s, 0))
        all_status.append(best)
        if best != "unknown":
            scores.append(_STATUS_SCORE.get(best, 0))
    if not scores:
        return "not_assessed", 0, kept
    score = round(sum(scores) / len(scores))
    if all(s == "met" for s in all_status):
        state = "operating"
    elif all(s in ("unmet", "unknown") for s in all_status):
        state = "planned"
    elif score >= 100:
        state = "implemented"
    else:
        state = "partially_implemented"
    return state, score, kept


def evaluate(db: Session, tenant, actor: str = "", only_framework: str = "") -> dict:
    """Re-assess every enabled pack from live evidence; write evidence, events + a
    posture snapshot per framework. Returns a short report."""
    refresh_all(db, tenant)  # let integrations collect fresh posture signals first
    by_cap = collect_evidence(db, tenant)
    packs = (db.query(m.CompliancePack)
             .filter(m.CompliancePack.tenant_id == tenant.id,
                     m.CompliancePack.enabled.is_(True)).all())
    exceptions = {e.control_id: e for e in db.query(m.ComplianceException)
                  .filter(m.ComplianceException.tenant_id == tenant.id).all()}
    results = []
    for pack in packs:
        if only_framework and pack.framework != only_framework:
            continue
        controls = (db.query(m.ComplianceControl)
                    .filter(m.ComplianceControl.pack_id == pack.id).all())
        met = 0
        for c in controls:
            state, score, kept = _derive(c.capabilities or [], by_cap)
            # Refresh the evidence trail for this control.
            db.query(m.ComplianceEvidence).filter(
                m.ComplianceEvidence.control_id == c.id).delete(synchronize_session=False)
            for r in kept:
                db.add(m.ComplianceEvidence(
                    tenant_id=tenant.id, control_id=c.id, capability=r.capability,
                    provider=r.provider, status=r.status, summary=r.summary,
                    detail=r.detail or {}))
            prior = c.state
            if c.id in exceptions:
                c.state = "exception"
            elif not c.auto:
                pass  # manual override preserved
            else:
                if state != c.state:
                    _event(db, tenant, kind="control_state", framework=pack.framework,
                           control_id=c.control_id, actor="engine",
                           summary=f"{c.control_id}: {prior} → {state}",
                           detail={"from": prior, "to": state, "score": score})
                c.state = state
                c.score = score
            c.last_evaluated_at = _now()
            if c.state in ("operating", "implemented"):
                met += 1
        applicable = [c for c in controls if _STATE_SCORE.get(c.state) is not None]
        fw_score = round(sum(_STATE_SCORE[c.state] for c in applicable) / len(applicable)) if applicable else 0
        db.add(m.ComplianceSnapshot(tenant_id=tenant.id, framework=pack.framework,
                                    score=fw_score, controls_total=len(controls),
                                    controls_met=met,
                                    exceptions=sum(1 for c in controls if c.state == "exception")))
        results.append({"framework": pack.framework, "score": fw_score,
                        "met": met, "total": len(controls)})
    db.commit()
    return {"frameworks": results, "evaluated_at": _now().isoformat()}


def controls_view(db: Session, tenant, framework: str) -> list[dict]:
    pack = (db.query(m.CompliancePack)
            .filter(m.CompliancePack.tenant_id == tenant.id,
                    m.CompliancePack.framework == framework).first())
    if pack is None:
        return []
    spec_by_id = {c["id"]: c for c in registry.controls_for(framework)}
    exc = {e.control_id: e for e in db.query(m.ComplianceException)
           .filter(m.ComplianceException.tenant_id == tenant.id).all()}
    rows = (db.query(m.ComplianceControl)
            .filter(m.ComplianceControl.pack_id == pack.id)
            .order_by(m.ComplianceControl.control_id.asc()).all())
    ev_by_ctrl: dict[str, list] = {}
    for e in (db.query(m.ComplianceEvidence)
              .filter(m.ComplianceEvidence.tenant_id == tenant.id).all()):
        ev_by_ctrl.setdefault(e.control_id, []).append(e)
    out = []
    for c in rows:
        spec = spec_by_id.get(c.control_id, {})
        e = exc.get(c.id)
        out.append({
            "id": c.id, "control_id": c.control_id, "title": c.title,
            "family": c.family, "state": c.state, "score": c.score,
            "owner": c.owner, "auto": bool(c.auto),
            "guidance": spec.get("guidance", ""),
            "capabilities": c.capabilities or [],
            "last_evaluated_at": c.last_evaluated_at.isoformat() if c.last_evaluated_at else None,
            "evidence": [{"capability": x.capability, "provider": x.provider,
                          "status": x.status, "summary": x.summary}
                         for x in ev_by_ctrl.get(c.id, [])],
            "exception": ({"reason": e.reason, "approved_by": e.approved_by,
                           "expires_at": e.expires_at.isoformat() if e.expires_at else None}
                          if e else None),
        })
    return out


def report(db: Session, tenant) -> dict:
    """Overall posture + per-framework score with a trend delta vs. the previous snapshot."""
    packs = (db.query(m.CompliancePack)
             .filter(m.CompliancePack.tenant_id == tenant.id,
                     m.CompliancePack.enabled.is_(True)).all())
    frameworks = []
    tot_met = tot = 0
    for p in packs:
        snaps = (db.query(m.ComplianceSnapshot)
                 .filter(m.ComplianceSnapshot.tenant_id == tenant.id,
                         m.ComplianceSnapshot.framework == p.framework)
                 .order_by(m.ComplianceSnapshot.taken_at.desc()).limit(2).all())
        cur = snaps[0] if snaps else None
        prev = snaps[1] if len(snaps) > 1 else None
        spec = registry.framework(p.framework) or {}
        frameworks.append({
            "framework": p.framework, "label": spec.get("label", p.framework),
            "score": cur.score if cur else 0,
            "met": cur.controls_met if cur else 0,
            "total": cur.controls_total if cur else 0,
            "delta": (cur.score - prev.score) if (cur and prev) else 0,
        })
        if cur:
            tot_met += cur.controls_met
            tot += cur.controls_total
    exceptions = (db.query(m.ComplianceException)
                  .filter(m.ComplianceException.tenant_id == tenant.id).count())
    return {
        "frameworks": frameworks,
        "score": round(sum(f["score"] for f in frameworks) / len(frameworks)) if frameworks else None,
        "controls_total": tot, "controls_met": tot_met, "exceptions": exceptions,
    }


_OPEN_STATES = {"planned", "partially_implemented", "failed", "not_assessed"}


def framework_detail(db: Session, tenant, framework: str) -> dict:
    """Everything the dedicated framework dashboard needs: header + score, the
    adherence trend, controls, the drivers (providers/capabilities behind the
    score), open issues, and the specific troubling accounts/systems flagged by
    evidence. One call powers the whole drill-in page."""
    spec = registry.framework(framework)
    if spec is None:
        return {}
    pack = (db.query(m.CompliancePack)
            .filter(m.CompliancePack.tenant_id == tenant.id,
                    m.CompliancePack.framework == framework).first())
    enabled = bool(pack and pack.enabled)
    controls = controls_view(db, tenant, framework) if enabled else []

    # Latest snapshot + full trend for the adherence-over-time chart.
    snaps = (db.query(m.ComplianceSnapshot)
             .filter(m.ComplianceSnapshot.tenant_id == tenant.id,
                     m.ComplianceSnapshot.framework == framework)
             .order_by(m.ComplianceSnapshot.taken_at.asc()).all())
    trend = [{"score": s.score, "met": s.controls_met, "total": s.controls_total,
              "at": s.taken_at.isoformat()} for s in snaps]
    cur = snaps[-1] if snaps else None
    prev = snaps[-2] if len(snaps) > 1 else None

    # Evidence rows (with detail) for this framework's controls → drivers + entities.
    ctrl_by_id = {c["id"]: c for c in controls}
    ev_rows = []
    if ctrl_by_id:
        ev_rows = (db.query(m.ComplianceEvidence)
                   .filter(m.ComplianceEvidence.tenant_id == tenant.id,
                           m.ComplianceEvidence.control_id.in_(list(ctrl_by_id.keys()))).all())
    # Drivers = each provider's contribution (how many signals, and their health).
    drivers: dict[str, dict] = {}
    for e in ev_rows:
        d = drivers.setdefault(e.provider or "arkive",
                               {"provider": e.provider or "arkive", "met": 0, "partial": 0,
                                "unmet": 0, "unknown": 0, "signals": 0, "capabilities": set()})
        d["signals"] += 1
        d["capabilities"].add(e.capability)
        if e.status in d:
            d[e.status] += 1
    drivers_out = [{**{k: v for k, v in d.items() if k != "capabilities"},
                    "capabilities": sorted(d["capabilities"])}
                   for d in drivers.values()]
    drivers_out.sort(key=lambda x: (-x["signals"], x["provider"]))

    # Open issues = controls not fully met, with the failing evidence reasons.
    open_issues = []
    for c in controls:
        if c["state"] in _OPEN_STATES:
            reasons = [f"{ev['capability']}: {ev['summary']}" for ev in c["evidence"]
                       if ev["status"] in ("unmet", "partial", "unknown")]
            open_issues.append({"control_id": c["control_id"], "title": c["title"],
                                "family": c["family"], "state": c["state"],
                                "capabilities": c["capabilities"], "reasons": reasons})

    # Troubling accounts/systems — entities providers flagged in evidence detail.
    entities = []
    seen = set()
    for e in ev_rows:
        for ent in ((e.detail or {}).get("entities") or []):
            label = ent.get("label") or ""
            key = (ent.get("kind", ""), label, e.capability)
            if not label or key in seen:
                continue
            seen.add(key)
            ctrl = ctrl_by_id.get(e.control_id, {})
            entities.append({"kind": ent.get("kind", "item"), "label": label,
                             "status": ent.get("status", "unmet"), "note": ent.get("note", ""),
                             "capability": e.capability, "provider": e.provider or "arkive",
                             "control_id": ctrl.get("control_id", "")})
    entities.sort(key=lambda x: (x["kind"], x["label"]))

    events = history(db, tenant, framework, limit=60)["events"]
    return {
        "framework": framework, "label": spec["label"], "version": spec["version"],
        "authority": spec.get("authority"), "url": spec.get("url"),
        "description": spec.get("description"), "enabled": enabled,
        "score": cur.score if cur else None,
        "met": cur.controls_met if cur else None,
        "total": cur.controls_total if cur else (len(spec["controls"]) if not enabled else 0),
        "delta": (cur.score - prev.score) if (cur and prev) else 0,
        "last_assessed_at": cur.taken_at.isoformat() if cur else None,
        "trend": trend, "controls": controls, "drivers": drivers_out,
        "open_issues": open_issues, "entities": entities, "events": events,
    }


def history(db: Session, tenant, framework: str = "", limit: int = 60) -> dict:
    sq = db.query(m.ComplianceSnapshot).filter(m.ComplianceSnapshot.tenant_id == tenant.id)
    if framework:
        sq = sq.filter(m.ComplianceSnapshot.framework == framework)
    snaps = sq.order_by(m.ComplianceSnapshot.taken_at.desc()).limit(limit).all()
    eq = db.query(m.ComplianceEvent).filter(m.ComplianceEvent.tenant_id == tenant.id)
    if framework:
        eq = eq.filter(m.ComplianceEvent.framework == framework)
    events = eq.order_by(m.ComplianceEvent.created_at.desc()).limit(50).all()
    return {
        "snapshots": [{"framework": s.framework, "score": s.score,
                       "met": s.controls_met, "total": s.controls_total,
                       "at": s.taken_at.isoformat()} for s in reversed(snaps)],
        "events": [{"kind": e.kind, "framework": e.framework, "control_id": e.control_id,
                    "actor": e.actor, "summary": e.summary, "detail": e.detail,
                    "at": e.created_at.isoformat()} for e in events],
    }
