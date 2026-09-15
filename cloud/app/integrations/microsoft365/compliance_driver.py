"""Microsoft 365 compliance driver — the first integration compliance provider.

Registers a platform compliance provider that reports capability evidence derived
from the tenant's Microsoft 365 managed protection: which workloads are actively
protected, how much is captured, and reauth/permission health. The platform engine
folds this into every enabled framework alongside Arkive-core evidence.

This is the pattern every future integration follows (see
``.github/instructions/compliance.instructions.md``): a driver that maps the
integration's live detections/state to compliance capabilities — never secrets.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ...compliance.providers import CapabilityEvidence, register_provider
from . import models as m


def _m365_evidence(db: Session, tenant, scope: dict) -> list[CapabilityEvidence]:
    ev: list[CapabilityEvidence] = []
    from ...models import IntegrationInstance, SearchDocument, Collection
    from sqlalchemy import func

    instances = (db.query(IntegrationInstance)
                 .filter(IntegrationInstance.tenant_id == tenant.id,
                         IntegrationInstance.integration_type == "microsoft365").all())
    if not instances:
        return ev  # M365 not connected — contribute nothing (Arkive core still applies)

    def add(cap, status, summary, **detail):
        ev.append(CapabilityEvidence(capability=cap, status=status, summary=summary,
                                     provider="microsoft365", detail=detail))

    inst_ids = {i.id for i in instances}
    active = 0
    needs_attention = 0
    for inst in instances:
        active += db.query(m.ManagedSource).filter(
            m.ManagedSource.integration_instance_id == inst.id,
            m.ManagedSource.state.notin_(("paused_by_admin", "decommissioned", "planned"))).count()
        needs_attention += db.query(m.ManagedSource).filter(
            m.ManagedSource.integration_instance_id == inst.id,
            m.ManagedSource.state.in_(("permission_required", "credential_error"))).count()

    # Managed-collection object footprint (the durable protected count).
    mc = [c for c in db.query(Collection).filter(Collection.tenant_id == tenant.id).all()
          if (c.config or {}).get("m365_instance_id") in inst_ids and (c.config or {}).get("managed")]
    objects = 0
    if mc:
        objects = int(db.query(func.count(SearchDocument.id))
                      .filter(SearchDocument.collection_id.in_([c.id for c in mc])).scalar() or 0)

    add("backup_coverage",
        "met" if (active and needs_attention == 0) else ("partial" if active else "unmet"),
        f"Microsoft 365: {active} managed source(s) protected, {objects:,} object(s)"
        + (f"; {needs_attention} need attention" if needs_attention else ""),
        active=active, objects=objects, needs_attention=needs_attention)
    add("inventory", "met" if active else "unmet",
        f"Microsoft 365 discovered + protecting {active} source(s)", active=active)
    if needs_attention:
        add("access_control", "partial",
            f"{needs_attention} Microsoft 365 source(s) need re-consent/permissions",
            needs_attention=needs_attention)
    add("audit_logging", "met",
        "Microsoft 365 collection + admin actions are recorded in the audit ledger.")
    return ev


register_provider("microsoft365", _m365_evidence)
