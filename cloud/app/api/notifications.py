"""Customer-facing notification preference API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import notifications, security
from ..db import get_db
from ..models import Tenant, User

router = APIRouter(prefix="/me", tags=["notifications"])


def _applicable_types(db: Session, user: User) -> list[dict]:
    """The notification types shown to this user — org summaries only for org admins."""
    tenant = db.get(Tenant, user.tenant_id)
    is_org = bool(tenant and (tenant.tenant_type or "dedicated") != "shared")
    can_org = user.role in ("owner", "security-admin") or user.is_platform_admin
    out = []
    for t in notifications.NOTIFICATION_TYPES:
        if t["scope"] == "org" and not (is_org and can_org):
            continue
        out.append(t)
    return out


@router.get("/notifications")
def get_notifications(principal: security.Principal = Depends(security.get_principal),
                      db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    types = _applicable_types(db, user)
    prefs = notifications.normalized_prefs(user)
    return {"types": types, "prefs": {t["key"]: prefs[t["key"]] for t in types},
            "emails": notifications.normalized_emails(user),
            "max_emails": notifications.MAX_NOTIFICATION_EMAILS}


class PrefsIn(BaseModel):
    prefs: dict


@router.put("/notifications")
def set_notifications(body: PrefsIn,
                      principal: security.Principal = Depends(security.get_principal),
                      db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    prefs = dict(user.notification_prefs or {})
    valid = {t["key"] for t in notifications.NOTIFICATION_TYPES}
    for k, v in (body.prefs or {}).items():
        if k in valid:
            prefs[k] = bool(v)
    user.notification_prefs = prefs
    db.commit()
    return {"ok": True, "prefs": notifications.normalized_prefs(user)}


class EmailsIn(BaseModel):
    emails: list[str]


@router.put("/notification-emails")
def set_notification_emails(body: EmailsIn,
                            principal: security.Principal = Depends(security.get_principal),
                            db: Session = Depends(get_db)):
    """Manage the additional addresses that also receive this account's email
    notifications. These are never used for login."""
    user = db.get(User, principal.user_id)
    user.notification_emails = notifications.sanitize_emails(body.emails)
    db.commit()
    return {"ok": True, "emails": notifications.normalized_emails(user)}


class ContactLinkingIn(BaseModel):
    enabled: bool


@router.get("/contact-linking")
def get_contact_linking(principal: security.Principal = Depends(security.get_principal),
                        db: Session = Depends(get_db)):
    user = db.get(User, principal.user_id)
    return {"enabled": bool(user.contact_linking_enabled)}


@router.put("/contact-linking")
def set_contact_linking(body: ContactLinkingIn,
                        principal: security.Principal = Depends(security.get_principal),
                        db: Session = Depends(get_db)):
    """Opt in/out of contact linking — resolving phone numbers / emails in
    messages to a saved contact's name (built by a background node job)."""
    user = db.get(User, principal.user_id)
    user.contact_linking_enabled = bool(body.enabled)
    db.commit()
    return {"ok": True, "enabled": bool(user.contact_linking_enabled)}
