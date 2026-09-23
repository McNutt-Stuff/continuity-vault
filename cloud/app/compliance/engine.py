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

# Scoring algorithm version — bumped when the calculation changes so the trend
# shows a labeled boundary instead of a silent rewrite (spec §3.3).
SCORING_VERSION = "2.0-scope-aware"


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


def _derive(caps: list[str], by_cap: dict) -> tuple[str, int, list, dict]:
    """Scope-aware aggregation (spec §3.3): evaluate the applicable POPULATION for
    each capability, not best-status-wins. A capability's status comes from its
    coverage (covered / expected across every scope), so a healthy core signal can't
    conceal a failed M365 scope. Unknown/expired evidence never counts as met.

    Distinguishes three cases so scores aren't unfairly tanked:
      * capability with NO provider evidence at all  -> not assessable, excluded;
      * capability CONNECTED but unverifiable (unknown/permission) -> counts as 0;
      * capability with population evidence -> scored by coverage.

    Returns (state, score, kept_evidence, coverage_info)."""
    cap_scores: list[int] = []
    cap_statuses: list[str] = []   # "met"|"partial"|"unmet"|"unknown"|"none"
    kept: list = []
    tot_exp = tot_cov = tot_fail = 0.0
    tot_unknown = 0
    entities: list = []
    levels: set = set()

    for cap in caps:
        rows = by_cap.get(cap, [])
        if not rows:
            cap_statuses.append("none")   # no provider covers this — not assessable
            continue
        kept.extend(rows)
        exp = cov = fail = 0.0
        unk = 0
        for r in rows:
            if r.status == "not_applicable":
                continue
            if getattr(r, "entities", None):
                entities.extend(r.entities)
            lvl = getattr(r, "evidence_level", "") or ""
            if lvl:
                levels.add(lvl)
            if int(getattr(r, "expected", 0) or 0) > 0:
                exp += int(r.expected)
                cov += int(getattr(r, "covered", 0) or 0)
                fail += int(getattr(r, "failed", 0) or 0)
            else:
                # A binary (non-population) row is a synthetic population of one.
                s = r.status
                if s == "met":
                    exp += 1; cov += 1
                elif s == "partial":
                    exp += 1; cov += 0.5
                elif s == "unmet":
                    exp += 1; fail += 1
                elif s == "unknown":
                    unk += 1
        if exp <= 0:
            # Rows exist but none were assessable (all unknown / not_applicable).
            if unk > 0:
                # Connected but unverifiable — penalize (never inflate to met).
                cap_statuses.append("unknown")
                cap_scores.append(0)
                tot_unknown += unk
            else:
                cap_statuses.append("none")
            continue
        coverage = max(0.0, min(1.0, cov / exp))
        tot_exp += exp; tot_cov += cov; tot_fail += fail
        if coverage >= 0.999 and fail == 0 and unk == 0:
            st = "met"
        elif coverage <= 0.0 and fail > 0:
            st = "unmet"
        else:
            st = "partial"
        cap_statuses.append(st)
        cap_scores.append(round(coverage * 100))

    coverage_info = {
        "expected": round(tot_exp), "covered": round(tot_cov),
        "failed": round(tot_fail), "unknown": tot_unknown,
        "evidence_levels": sorted(levels), "entities": entities[:50],
    }
    meaningful = [s for s in cap_statuses if s != "none"]
    if not meaningful:
        return "not_assessed", 0, kept, coverage_info
    score = round(sum(cap_scores) / len(cap_scores)) if cap_scores else 0
    if all(s == "met" for s in meaningful):
        state = "operating"
    elif all(s in ("unmet", "unknown") for s in meaningful):
        state = "planned"
    elif score >= 100:
        state = "implemented"
    else:
        state = "partially_implemented"
    return state, score, kept, coverage_info


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
        fw_cov_exp = fw_cov_cov = fw_cov_fail = 0
        for c in controls:
            state, score, kept, coverage = _derive(c.capabilities or [], by_cap)
            # Refresh the evidence trail for this control.
            db.query(m.ComplianceEvidence).filter(
                m.ComplianceEvidence.control_id == c.id).delete(synchronize_session=False)
            for r in kept:
                detail = dict(r.detail or {})
                lvl = getattr(r, "evidence_level", "") or ""
                if lvl and "evidence_level" not in detail:
                    detail["evidence_level"] = lvl
                db.add(m.ComplianceEvidence(
                    tenant_id=tenant.id, control_id=c.id, capability=r.capability,
                    provider=r.provider, status=r.status, summary=r.summary,
                    detail=detail))
            fw_cov_exp += int(coverage.get("expected", 0) or 0)
            fw_cov_cov += int(coverage.get("covered", 0) or 0)
            fw_cov_fail += int(coverage.get("failed", 0) or 0)
            prior = c.state
            # Keep the coverage rollup on the control so the UI can show num/den,
            # failed entities, evidence levels and staleness without re-deriving.
            c.meta = {**(c.meta or {}), "coverage": coverage}
            if c.id in exceptions:
                c.state = "exception"
            elif not c.auto:
                pass  # manual override preserved
            else:
                if state != c.state:
                    _event(db, tenant, kind="control_state", framework=pack.framework,
                           control_id=c.control_id, actor="engine",
                           summary=f"{c.control_id}: {prior} → {state}",
                           detail={"from": prior, "to": state, "score": score,
                                   "coverage": coverage})
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
                                    exceptions=sum(1 for c in controls if c.state == "exception"),
                                    scoring_version=SCORING_VERSION,
                                    coverage_expected=fw_cov_exp, coverage_covered=fw_cov_cov,
                                    coverage_failed=fw_cov_fail))
        results.append({"framework": pack.framework, "score": fw_score,
                        "met": met, "total": len(controls),
                        "coverage": {"expected": fw_cov_exp, "covered": fw_cov_cov,
                                     "failed": fw_cov_fail}})
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
        cov = (c.meta or {}).get("coverage") or {}
        out.append({
            "id": c.id, "control_id": c.control_id, "title": c.title,
            "family": c.family, "state": c.state, "score": c.score,
            "owner": c.owner, "auto": bool(c.auto),
            "guidance": spec.get("guidance", ""),
            "capabilities": c.capabilities or [],
            "last_evaluated_at": c.last_evaluated_at.isoformat() if c.last_evaluated_at else None,
            # Scoped coverage (num/den, failed entities, evidence levels) so the UI
            # can show technical state honestly — spec §3.3/§6.
            "coverage": {"expected": cov.get("expected", 0), "covered": cov.get("covered", 0),
                         "failed": cov.get("failed", 0), "unknown": cov.get("unknown", 0),
                         "evidence_levels": cov.get("evidence_levels", [])},
            "evidence": [{"capability": x.capability, "provider": x.provider,
                          "status": x.status, "summary": x.summary,
                          "evidence_level": (x.detail or {}).get("evidence_level", ""),
                          "stale": bool((x.detail or {}).get("stale"))}
                         for x in ev_by_ctrl.get(c.id, [])],
            "exception": ({"reason": e.reason, "approved_by": e.approved_by,
                           "expires_at": e.expires_at.isoformat() if e.expires_at else None,
                           "expired": bool(e.expires_at and e.expires_at < _now())}
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
            "scoring_version": (cur.scoring_version or "") if cur else "",
            "coverage": ({"expected": cur.coverage_expected or 0,
                          "covered": cur.coverage_covered or 0,
                          "failed": cur.coverage_failed or 0} if cur else None),
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
              "scoring_version": s.scoring_version or "", "at": s.taken_at.isoformat()}
             for s in snaps]
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
        "scoring_version": (cur.scoring_version or "") if cur else "",
        "coverage": ({"expected": cur.coverage_expected or 0,
                      "covered": cur.coverage_covered or 0,
                      "failed": cur.coverage_failed or 0} if cur else None),
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
