"""Vault Recovery Key management: create / rotate / delete / status.

The plaintext recovery code is returned ONCE at creation (and rotation) and is
never stored. All mutating operations require a passkey step-up. See
``cloud/app/recovery_key.py`` for the crypto and eligibility rules.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import audit, recovery_key, security
from ..db import get_db
from ..models import Tenant, User

router = APIRouter(prefix="/recovery-key", tags=["recovery-key"])


@router.get("/status")
def get_status(principal: security.Principal = Depends(security.get_principal),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    return recovery_key.status(db, user, tenant)


@router.post("")
def create_key(principal: security.Principal = Depends(security.require_passkey),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    if recovery_key.get_record(db, user.id):
        raise HTTPException(409, "a recovery key already exists — rotate it instead")
    result = recovery_key.create(db, user, tenant)
    if result is None:
        raise HTTPException(400, "no eligible vaults for a recovery key")
    audit.record(db, actor=user.email, action="recovery_key.created",
                 tenant_id=tenant.id, severity="notice")
    return result


@router.post("/rotate")
def rotate_key(principal: security.Principal = Depends(security.require_passkey),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    result = recovery_key.create(db, user, tenant)  # create replaces any existing
    if result is None:
        raise HTTPException(400, "no eligible vaults for a recovery key")
    audit.record(db, actor=user.email, action="recovery_key.rotated",
                 tenant_id=tenant.id, severity="notice")
    return result


@router.delete("")
def delete_key(principal: security.Principal = Depends(security.require_passkey),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    rec = recovery_key.get_record(db, user.id)
    if rec is not None:
        db.delete(rec)
        db.commit()
        audit.record(db, actor=user.email, action="recovery_key.deleted",
                     tenant_id=tenant.id, severity="warning")
    return {"deleted": True}
