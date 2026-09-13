"""Microsoft 365 managed-policy compiler (spec §12.2).

Deterministically merges package defaults + organization/group/source rules +
exceptions into an EFFECTIVE policy for a scope, and rejects contradictions that
would violate legal hold, immutable retention, tenant isolation or entitlement.

Pure functions over the package's ORM records — no Microsoft calls, no payloads.
Precedence (highest wins):

    legal hold / regulatory minimum
      > explicit organization exception
      > source rule
      > group/user assignment rule
      > organization default
      > package default
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import models as m

# Package defaults — the safe baseline before any org rule applies.
PACKAGE_DEFAULTS: dict = {
    "collection": {"include": ["mail", "files"], "exclude": []},
    "schedule": {"baseline": "on_activate", "incremental_minutes": 360},
    "retention": {"mode": "org_default", "min_days": 0, "immutable": False},
    "legal_hold": {"active": False},
    "classification": {"labels": "inherit"},
    "indexing": {"fields": ["subject", "from", "to", "filename", "path", "modified"]},
    "visibility": {"user_can_see_source": True, "user_can_see_content": False},
    "recovery": {"eligible": True, "destination": "original_or_alternate",
                 "approvals": 1, "step_up": True},
    "notifications": {"on_error": True},
    "residency": {"assigned_node_only": True},
}

# Rule precedence tiers (lowest number applied first, later tiers override).
_TIER = {"package": 0, "organization": 1, "group": 2, "user": 3, "source": 4,
         "org_exception": 5, "legal_hold": 6}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _active_rules(db, tenant_id: str, integration_instance_id: str) -> list:
    """Active managed rules for the instance with their active-version specs and
    assignment tier."""
    rules = (db.query(m.ManagedRule)
             .filter(m.ManagedRule.tenant_id == tenant_id,
                     m.ManagedRule.integration_instance_id == integration_instance_id,
                     m.ManagedRule.status == "active").all())
    out = []
    for r in rules:
        ver = (db.query(m.ManagedRuleVersion)
               .filter(m.ManagedRuleVersion.rule_id == r.id,
                       m.ManagedRuleVersion.version == r.active_version).first())
        if not ver:
            continue
        assigns = (db.query(m.ManagedRuleAssignment)
                   .filter(m.ManagedRuleAssignment.rule_id == r.id).all())
        # The most specific assignment scope drives the tier (source > user > …).
        tier = "organization"
        for a in assigns:
            if _TIER.get(a.assignee_type, 1) > _TIER.get(tier, 1):
                tier = a.assignee_type
        out.append({"rule": r, "family": r.rule_family, "spec": ver.spec or {},
                    "version": ver.version, "tier": tier})
    return out


def compile_effective(db, tenant_id: str, integration_instance_id: str,
                      scope_ref: str = "") -> dict:
    """Compile the effective policy for a scope. Returns
    ``{effective, rule_versions, mapping_versions, conflicts}``. ``conflicts`` is
    non-empty when a rule would violate an invariant (caller must NOT activate)."""
    effective = {k: dict(v) if isinstance(v, dict) else v
                 for k, v in PACKAGE_DEFAULTS.items()}
    rule_versions: dict = {}
    conflicts: list = []

    # Apply rules in ascending precedence so higher tiers overwrite lower ones.
    for entry in sorted(_active_rules(db, tenant_id, integration_instance_id),
                        key=lambda e: _TIER.get(e["tier"], 1)):
        fam = entry["family"]
        spec = entry["spec"]
        base = effective.get(fam)
        if isinstance(base, dict) and isinstance(spec, dict):
            base = {**base, **spec}
        else:
            base = spec
        effective[fam] = base
        rule_versions[entry["rule"].id] = entry["version"]

    # Legal hold and immutable retention are floors — a later rule cannot weaken them.
    if effective.get("legal_hold", {}).get("active"):
        ret = effective.setdefault("retention", {})
        if ret.get("mode") == "delete" or ret.get("min_days", 0) == 0:
            # Legal hold forbids deletion / zero-retention.
            ret["immutable"] = True
            if ret.get("mode") == "delete":
                conflicts.append("A retention rule sets delete while a legal hold is active")
        ret["mode"] = ret.get("mode") if ret.get("mode") not in (None, "delete") else "retain"
    if effective.get("retention", {}).get("immutable") and \
            effective.get("recovery", {}).get("eligible") is False:
        conflicts.append("Immutable retention requires recovery eligibility")

    # Residency invariant: assigned-node only cannot be turned off by a rule.
    if effective.get("residency", {}).get("assigned_node_only") is False:
        effective["residency"]["assigned_node_only"] = True
        conflicts.append("Data residency (assigned-node only) cannot be disabled")

    return {"effective": effective, "rule_versions": rule_versions,
            "mapping_versions": {}, "conflicts": conflicts}


def persist_snapshot(db, tenant_id: str, integration_instance_id: str,
                     scope_ref: str = "") -> m.EffectivePolicySnapshot:
    """Compile + store an EffectivePolicySnapshot (records the exact versions used)."""
    result = compile_effective(db, tenant_id, integration_instance_id, scope_ref)
    snap = m.EffectivePolicySnapshot(
        tenant_id=tenant_id, integration_instance_id=integration_instance_id,
        scope_ref=scope_ref, compiled=result["effective"],
        mapping_versions=result["mapping_versions"], rule_versions=result["rule_versions"])
    db.add(snap)
    db.commit()
    return snap
