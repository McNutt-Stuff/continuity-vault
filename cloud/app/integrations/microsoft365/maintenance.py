"""Microsoft 365 managed-source maintenance — dedupe natural-key duplicates.

A managed source can end up duplicated on a box when it was provisioned under a
divergent primary id (a warm-standby echo, or a re-parented instance). node_sync
now merges pushed rows on the natural key so new duplicates can't form; this
reconciles any that already exist. Idempotent — safe to run repeatedly, and it is
called from the daily prune job as well as the manage.py CLI.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy.orm import Session

from . import models as m

logger = logging.getLogger("cv.integrations.m365.maintenance")

# Managed-source state ranking: higher = more progressed. A collected row must win
# over a 'planned' echo. Mirrors node_sync._MS_STATE_RANK.
_STATE_RANK = {
    "decommissioned": 0, "planned": 1, "provisioning": 2, "paused_by_admin": 3,
    "disconnected": 3, "baseline_pending": 4, "permission_required": 4,
    "credential_error": 4, "source_unavailable": 4, "retention_hold": 5,
    "empty": 6, "delayed": 6, "partial": 7, "active": 8,
}


def _score(s) -> tuple:
    return (1 if s.last_collected_at else 0,
            s.last_collected_at or datetime.min,
            _STATE_RANK.get(s.state, 0),
            s.created_at or datetime.min)


def dedupe_managed_sources(db: Session, *, dry_run: bool = False,
                           on_drop: Optional[Callable] = None) -> int:
    """Delete duplicate m365_managed_sources sharing a natural key
    (tenant, integration_instance, workload, source_key), keeping the most-
    progressed row (collected first, then state rank, then newest). Returns the
    number removed (or that would be removed for ``dry_run``). Does NOT commit — the
    caller owns the transaction. ``on_drop(loser, keeper)`` is called per removal."""
    groups: dict = {}
    for s in db.query(m.ManagedSource).all():
        groups.setdefault(
            (s.tenant_id, s.integration_instance_id, s.workload, s.source_key), []).append(s)
    removed = 0
    for grp in groups.values():
        if len(grp) < 2:
            continue
        grp.sort(key=_score, reverse=True)
        keeper = grp[0]
        for loser in grp[1:]:
            if on_drop is not None:
                on_drop(loser, keeper)
            if not dry_run:
                db.query(m.SourceAssignment).filter(
                    m.SourceAssignment.managed_source_id == loser.id).delete(
                        synchronize_session=False)
                db.delete(loser)
            removed += 1
    if removed and not dry_run:
        logger.info("m365 dedupe: removed %d duplicate managed source(s)", removed)
    return removed
