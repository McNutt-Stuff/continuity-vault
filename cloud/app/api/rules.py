"""Rules engine API — declarative ingestion rules (compliance).

CRUD for rules plus a builder-options endpoint and a preview evaluator. Gated by
the ``rules_enabled`` feature flag; every route is tenant-scoped. Rules are
control-plane-owned and federate to customer-tenant nodes, which evaluate them
during their own ingestion.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, features, rules_engine, security
from ..db import get_db
from ..models import Collection, Rule, Tenant, User

router = APIRouter(prefix="/rules", tags=["rules"])
logger = logging.getLogger("cv.rules")

_VALID_ACTIONS = {a["id"] for a in rules_engine.ACTION_TYPES}
_VALID_OPS = {o["id"] for o in rules_engine.OPERATORS}
_VALID_PLANS = set(rules_engine.PLAN_RANK)


def _require_enabled(db: Session, principal, tenant: Tenant) -> None:
    """404 unless rules are enabled for this account. Honours the USER-level flag
    too — personal/shared accounts can only be enabled per user (tenant flags
    aren't exposed for shared tenants)."""
    user = db.get(User, principal.user_id) if principal else None
    if not features.resolve(user, tenant, "rules_enabled"):
        raise HTTPException(404, "not found")  # hide the feature entirely when off


class Condition(BaseModel):
    field: str
    op: str = "equals"
    value: str | None = None


class Action(BaseModel):
    type: str
    value: str | None = None
    min_plan: str | None = None


class RuleBody(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    priority: int = 100
    match: str = "all"                 # all | any
    conditions: list[Condition] = []
    actions: list[Action] = []
    collection_ids: list[str] = []     # empty = all collections
    source_types: list[str] = []       # empty = all sources
    min_plan: str = "personal"


def _validate(body: RuleBody) -> None:
    if not body.name.strip():
        raise HTTPException(400, "a rule name is required")
    if body.match not in ("all", "any"):
        raise HTTPException(400, "match must be 'all' or 'any'")
    if body.min_plan not in _VALID_PLANS:
        raise HTTPException(400, f"min_plan must be one of {sorted(_VALID_PLANS)}")
    if not body.conditions:
        raise HTTPException(400, "add at least one condition")
    for c in body.conditions:
        if not c.field.strip():
            raise HTTPException(400, "every condition needs a field")
        if c.op not in _VALID_OPS:
            raise HTTPException(400, f"unknown operator '{c.op}'")
    if not body.actions:
        raise HTTPException(400, "add at least one action")
    for a in body.actions:
        if a.type not in _VALID_ACTIONS:
            raise HTTPException(400, f"unknown action '{a.type}'")


def _view(r: Rule) -> dict:
    return {
        "id": r.id, "name": r.name, "description": r.description or "",
        "enabled": bool(r.enabled), "priority": r.priority,
        "match": r.match or "all", "conditions": r.conditions or [],
        "actions": r.actions or [], "collection_ids": r.collection_ids or [],
        "source_types": r.source_types or [], "min_plan": r.min_plan or "personal",
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@router.get("/options")
def options(principal: security.Principal = Depends(security.get_principal),
            tenant: Tenant = Depends(security.get_tenant),
            db: Session = Depends(get_db)):
    """Builder metadata: operators, action types (+ the plan each needs), field
    suggestions, the tenant's plan, and its collections to scope rules to."""
    _require_enabled(db, principal, tenant)
    colls = (db.query(Collection)
             .filter(Collection.tenant_id == tenant.id)
             .order_by(Collection.name.asc()).all())
    return {
        "operators": rules_engine.OPERATORS,
        "action_types": rules_engine.ACTION_TYPES,
        "field_suggestions": rules_engine.FIELD_SUGGESTIONS,
        "plans": list(rules_engine.PLAN_RANK),
        "plan": (tenant.plan or "personal"),
        "collections": [{"id": c.id, "name": c.name, "source_type": c.source_type}
                        for c in colls],
    }


@router.get("")
def list_rules(collection: str | None = Query(default=None),
               principal: security.Principal = Depends(security.get_principal),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    _require_enabled(db, principal, tenant)
    rows = (db.query(Rule)
            .filter(Rule.tenant_id == tenant.id)
            .order_by(Rule.priority.asc(), Rule.created_at.asc()).all())
    if collection:
        rows = [r for r in rows if not (r.collection_ids or []) or collection in (r.collection_ids or [])]
    return {"rules": [_view(r) for r in rows]}


@router.post("")
def create_rule(body: RuleBody,
                principal: security.Principal = Depends(security.get_principal),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    _require_enabled(db, principal, tenant)
    _validate(body)
    r = Rule(
        tenant_id=tenant.id, name=body.name.strip(), description=body.description or "",
        enabled=body.enabled, priority=body.priority, match=body.match,
        conditions=[c.model_dump() for c in body.conditions],
        actions=[a.model_dump(exclude_none=True) for a in body.actions],
        collection_ids=body.collection_ids, source_types=body.source_types,
        min_plan=body.min_plan)
    db.add(r)
    db.commit()
    db.refresh(r)
    audit.record(db, actor=principal.user_id, action="rule.created",
                 tenant_id=tenant.id, resource=r.id, detail={"name": r.name})
    return _view(r)


@router.put("/{rule_id}")
def update_rule(rule_id: str, body: RuleBody,
                principal: security.Principal = Depends(security.get_principal),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    _require_enabled(db, principal, tenant)
    _validate(body)
    r = db.get(Rule, rule_id)
    if not r or r.tenant_id != tenant.id:
        raise HTTPException(404, "rule not found")
    r.name = body.name.strip()
    r.description = body.description or ""
    r.enabled = body.enabled
    r.priority = body.priority
    r.match = body.match
    r.conditions = [c.model_dump() for c in body.conditions]
    r.actions = [a.model_dump(exclude_none=True) for a in body.actions]
    r.collection_ids = body.collection_ids
    r.source_types = body.source_types
    r.min_plan = body.min_plan
    db.commit()
    db.refresh(r)
    audit.record(db, actor=principal.user_id, action="rule.updated",
                 tenant_id=tenant.id, resource=r.id, detail={"name": r.name})
    return _view(r)


@router.post("/{rule_id}/toggle")
def toggle_rule(rule_id: str,
                principal: security.Principal = Depends(security.get_principal),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    _require_enabled(db, principal, tenant)
    r = db.get(Rule, rule_id)
    if not r or r.tenant_id != tenant.id:
        raise HTTPException(404, "rule not found")
    r.enabled = not bool(r.enabled)
    db.commit()
    audit.record(db, actor=principal.user_id,
                 action="rule.enabled" if r.enabled else "rule.disabled",
                 tenant_id=tenant.id, resource=r.id, detail={"name": r.name})
    return {"id": r.id, "enabled": r.enabled}


@router.delete("/{rule_id}")
def delete_rule(rule_id: str,
                principal: security.Principal = Depends(security.get_principal),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    _require_enabled(db, principal, tenant)
    r = db.get(Rule, rule_id)
    if not r or r.tenant_id != tenant.id:
        raise HTTPException(404, "rule not found")
    name = r.name
    db.delete(r)
    db.commit()
    audit.record(db, actor=principal.user_id, action="rule.deleted",
                 tenant_id=tenant.id, resource=rule_id, detail={"name": name})
    return {"ok": True}


class PreviewBody(BaseModel):
    # A sample object's fields to test against the tenant's rules.
    doc_type: str = ""
    category: str = ""
    title: str = ""
    source_type: str = ""
    labels: list[str] = []
    meta: dict = {}
    collection_id: str | None = None


@router.post("/preview")
def preview(body: PreviewBody,
            principal: security.Principal = Depends(security.get_principal),
            tenant: Tenant = Depends(security.get_tenant),
            db: Session = Depends(get_db)):
    """Evaluate a sample object against the tenant's rules and report exactly which
    rules match and what they'd do — the 'check rule evaluation results' surface."""
    _require_enabled(db, principal, tenant)
    rows = (db.query(Rule)
            .filter(Rule.tenant_id == tenant.id, Rule.enabled.is_(True))
            .order_by(Rule.priority.asc(), Rule.created_at.asc()).all())
    if body.collection_id:
        rows = [r for r in rows if not (r.collection_ids or [])
                or body.collection_id in (r.collection_ids or [])]
    fields = rules_engine.object_fields(
        doc_type=body.doc_type, category=body.category, title=body.title,
        source_type=body.source_type, labels=body.labels, meta=body.meta)
    outcome = rules_engine.evaluate(rows, fields, plan=(tenant.plan or "personal"))
    return {
        "matched": outcome.matched,
        "add_labels": outcome.add_labels,
        "restricted": outcome.restricted,
        "obfuscate": outcome.obfuscate,
        "no_index": outcome.no_index,
        "discard": outcome.discard,
    }
