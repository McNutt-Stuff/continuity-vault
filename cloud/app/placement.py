"""Active/passive tenant placement — HA standby assignment, switchover & failover.

A tenant's ``node_id`` is its ACTIVE node (runs its workers, serves its file ops);
``standby_node_id`` is a warm PASSIVE replica kept in sync by ``node_sync`` /
``node_replication`` (config + keys + receipts + search index). Because object
bytes live in shared cloud storage — and appliances re-target their node on the
next heartbeat — promoting the standby is a metadata flip:

    switchover(A→B):  node_id = B (was standby),  standby_node_id = A (old active)

The old active becomes the new warm standby, so a tenant can ping-pong between two
nodes with no data movement. ``placement_state="switching"`` makes proxied file
ops return a brief maintenance response until devices + the portal retarget; a
scheduler pass clears it. Everything here is a no-op for tenants with no standby.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import Node, Tenant

logger = logging.getLogger("cv.placement")

# How long file ops report "maintenance" after a switchover before the state is
# cleared (devices + the portal retarget to the new active within a heartbeat).
SWITCH_GRACE_SECONDS = 45
# A tenant can't switch active nodes more than once inside this window (anti-flap).
SWITCH_COOLDOWN_SECONDS = 300


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_switching(tenant: Tenant) -> bool:
    """True while a tenant is mid-switchover (file ops should show maintenance)."""
    if (tenant.placement_state or "") != "switching":
        return False
    at = tenant.switchover_at
    if at and (_now() - at).total_seconds() > SWITCH_GRACE_SECONDS:
        return False  # grace elapsed — treat as settled even if not yet cleared
    return True


def can_switch(tenant: Tenant) -> tuple[bool, str]:
    """Whether the tenant may switch active nodes right now (has a standby + not in
    the anti-flap cooldown). Returns (ok, reason-if-not)."""
    if not tenant.standby_node_id:
        return False, "no standby node assigned"
    if tenant.standby_node_id == tenant.node_id:
        return False, "standby is the same as the active node"
    if not is_standby_ready(tenant):
        if tenant.standby_synced_at is None:
            return False, "the standby replica hasn't finished its first sync yet"
        return False, (f"the standby replica is still syncing "
                       f"({tenant.standby_pending or 0} item(s) pending)")
    at = tenant.switchover_at
    if at and (_now() - at).total_seconds() < SWITCH_COOLDOWN_SECONDS:
        return False, "a switchover happened recently — try again shortly"
    return True, ""


def is_standby_ready(tenant: Tenant) -> bool:
    """True when the standby has a CONFIRMED, complete replica (the node reported a
    clean, caught-up apply and nothing is pending). This reflects ACTUAL apply
    success, so a node that pulled but skipped the tenant's rows is NOT 'ready'."""
    return bool(tenant.standby_node_id and tenant.standby_synced_at is not None
                and not (tenant.standby_pending or 0))


def set_standby(db: Session, tenant: Tenant, standby_node_id: str | None, *,
                actor: str = "system") -> dict:
    """Assign / change / clear a tenant's warm standby node. Replication starts
    warming the new standby automatically (config + keys + receipts + index)."""
    if standby_node_id:
        node = db.get(Node, standby_node_id)
        if node is None or node.role != "customer-tenant":
            raise ValueError("standby must be an existing customer-tenant node")
        if node.id == tenant.node_id:
            raise ValueError("standby must be a different node than the active one")
        if not node.endpoint:
            raise ValueError("the standby node has no endpoint yet")
    prev = tenant.standby_node_id
    tenant.standby_node_id = standby_node_id or None
    # A freshly (re)assigned standby is NOT synced until the node confirms a clean
    # apply — clear readiness so the UI shows "syncing" and switchover refuses it.
    tenant.standby_synced_at = None
    tenant.standby_pending = 0 if not standby_node_id else 1
    db.commit()
    to_name = _node_name(db, standby_node_id) if standby_node_id else "none"
    msg = (f"HA standby set to {to_name}" if standby_node_id
           else "HA standby cleared")
    _audit(db, actor, "tenant.standby_set", tenant,
           {"from": prev, "to": standby_node_id, "to_name": to_name, "message": msg})
    logger.warning("placement: tenant %s (%s) standby %s -> %s (by %s)",
                   tenant.id, tenant.name, prev, standby_node_id, actor)
    return {"tenant_id": tenant.id, "standby_node_id": tenant.standby_node_id}


def switchover(db: Session, tenant: Tenant, *, actor: str = "system",
               reason: str = "manual", force: bool = False) -> dict:
    """Promote the tenant's standby to active (and demote the old active to standby).

    The standby is a warm replica, so this is an immediate metadata flip — no data
    movement. Devices retarget to the new active via their next heartbeat (the CP
    returns the new node_url); proxied portal file ops show a brief maintenance
    window while ``placement_state`` is "switching". ``force`` skips the anti-flap
    cooldown (used by automatic failover when the active node is confirmed down)."""
    if not force:
        ok, why = can_switch(tenant)
        if not ok:
            raise ValueError(why)
    elif not tenant.standby_node_id or tenant.standby_node_id == tenant.node_id:
        raise ValueError("no standby node assigned")
    old_active = tenant.node_id
    new_active = tenant.standby_node_id
    tenant.node_id = new_active
    tenant.standby_node_id = old_active  # old active becomes the warm standby
    tenant.placement_state = "switching"
    tenant.switchover_at = _now()
    db.commit()
    an = _node_name(db, new_active)
    on = _node_name(db, old_active)
    devices = _device_counts(db, tenant.id)
    _audit(db, actor, "tenant.switchover", tenant, {
        "reason": reason, "from_node": old_active, "to_node": new_active,
        "appliances": devices["appliances"], "agents": devices["agents"],
        "message": f"switched over {on} -> {an} ({reason}); "
                   f"{devices['appliances']} appliance(s) + {devices['agents']} "
                   f"agent(s) retarget on next heartbeat"})
    logger.warning("placement: SWITCHOVER tenant %s (%s) %s -> %s (%s, by %s); "
                   "%d appliance(s) + %d agent(s) will retarget",
                   tenant.id, tenant.name, on, an, reason, actor,
                   devices["appliances"], devices["agents"])
    return {"tenant_id": tenant.id, "active_node_id": new_active,
            "active_node_name": an, "standby_node_id": old_active, "reason": reason}


def clear_stale_switching(db: Session) -> int:
    """Clear ``placement_state="switching"`` once the grace window has elapsed (the
    new active node + devices have retargeted). Called from the scheduler."""
    cutoff = _now()
    n = 0
    for t in (db.query(Tenant)
              .filter(Tenant.placement_state == "switching").all()):
        if not t.switchover_at or (cutoff - t.switchover_at).total_seconds() >= SWITCH_GRACE_SECONDS:
            t.placement_state = ""
            n += 1
    if n:
        db.commit()
    return n


def _node_name(db: Session, node_id: str | None) -> str:
    if not node_id:
        return "control plane"
    n = db.get(Node, node_id)
    return (n.name if n else node_id) or node_id


def _device_counts(db: Session, tenant_id: str) -> dict:
    """How many appliances + desktop agents will retarget to the new active node
    (they discover it via their next heartbeat's node_url)."""
    out = {"appliances": 0, "agents": 0}
    try:
        from .models import Appliance, DesktopAgent
        from sqlalchemy import func
        out["appliances"] = int(db.query(func.count(Appliance.id))
                                .filter(Appliance.tenant_id == tenant_id).scalar() or 0)
        out["agents"] = int(db.query(func.count(DesktopAgent.id))
                            .filter(DesktopAgent.tenant_id == tenant_id).scalar() or 0)
    except Exception:  # noqa: BLE001
        logger.debug("device count failed for tenant %s", tenant_id, exc_info=True)
    return out


def _audit(db: Session, actor: str, action: str, tenant: Tenant, detail: dict) -> None:
    try:
        from . import audit
        audit.record(db, actor=actor, action=action, tenant_id=tenant.id,
                     resource=tenant.id, category="admin", severity="warning",
                     detail={"tenant": tenant.name, **detail})
    except Exception:  # noqa: BLE001 — auditing must never block a placement change
        logger.exception("placement audit failed for %s", action)
