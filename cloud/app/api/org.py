"""Customer-facing Organization Admin (owner/admin only).

Everything an org owner or admin needs to run a multi-user organization:
manage members and their roles, assign appliances to members, and oversee each
member's encryption keys — including authorized key recovery for the lost-key /
end-of-life use case. Members never reach these endpoints (require_org_admin);
and even here admins see aggregate statistics and key *fingerprints*, never
another member's actual content.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import audit, authcodes, emailer, keybroker, security
from ..config import get_settings
from ..db import get_db
from ..models import (
    Appliance,
    ApplianceAssignment,
    ApplianceStorage,
    AuditEvent,
    Collection,
    Passkey,
    SearchDocument,
    Tenant,
    User,
    Vault,
)

router = APIRouter(prefix="/org", tags=["organization"])

ASSIGNABLE_ROLES = ("member", "admin", "owner")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --- helpers ---------------------------------------------------------------


def _object_counts_by_vault(db: Session, tenant_id: str) -> dict[str, int]:
    rows = (db.query(SearchDocument.vault_id, func.count(SearchDocument.id))
            .filter(SearchDocument.tenant_id == tenant_id,
                    SearchDocument.is_current.is_(True))
            .group_by(SearchDocument.vault_id).all())
    return {vid: int(n) for vid, n in rows if vid}


def _bytes_by_vault(db: Session, tenant_id: str) -> dict[str, int]:
    rows = (db.query(SearchDocument.vault_id,
                     func.coalesce(func.sum(SearchDocument.size_bytes), 0))
            .filter(SearchDocument.tenant_id == tenant_id,
                    SearchDocument.is_current.is_(True))
            .group_by(SearchDocument.vault_id).all())
    return {vid: int(n) for vid, n in rows if vid}


def _user_view(db: Session, u: User, vaults: list[Vault],
               obj_by_vault: dict[str, int], bytes_by_vault: dict[str, int]) -> dict:
    my_vaults = [v for v in vaults if v.owner_user_id == u.id]
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "status": u.status,
        "email_verified": bool(u.email_verified),
        "has_passkey": len(u.passkeys) > 0,
        "allow_impersonation": bool(getattr(u, "allow_impersonation", False)),
        "vault_count": len(my_vaults),
        "object_count": sum(obj_by_vault.get(v.id, 0) for v in my_vaults),
        "protected_bytes": sum(bytes_by_vault.get(v.id, 0) for v in my_vaults),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def _send_invite(db: Session, u: User, org_name: str) -> dict:
    try:
        code = authcodes.issue_code(u.email, "login")
    except Exception:
        return {"sent": False}
    settings = get_settings()
    subject = f"You've been added to {org_name} on Arkive"
    body = (f"You've been added to {org_name} on Arkive as {u.role}.\n\n"
            f"Your sign-in code: {code}\n\nOpen {settings.rp_origin} to sign in "
            f"and set up your device passkey.")
    channel = emailer.send(u.email, subject,
                           html=emailer.render(subject, emailer.text_to_html(body),
                                               cta={"label": "Sign in", "url": settings.rp_origin}),
                           text=body, category="access")
    out = {"sent": channel in ("ses", "smtp", "log"), "channel": channel}
    if settings.environment == "development":
        out["dev_code"] = code
    return out


def _send_verification(u: User) -> dict:
    """Email a confirmation code to a member's address (after an admin sets/changes
    it) and flag the address unverified until they confirm."""
    try:
        code = authcodes.issue_code(u.email, "verify")
    except Exception:  # noqa: BLE001
        return {"sent": False}
    settings = get_settings()
    subject = "Confirm your Arkive email address"
    body = (f"Please confirm this email address for your Arkive account.\n\n"
            f"Your confirmation code: {code}\n\nOpen {settings.rp_origin}, sign in, "
            f"and enter this code when prompted. The code expires shortly.")
    channel = emailer.send(u.email, subject,
                           html=emailer.render(subject, emailer.text_to_html(body),
                                               cta={"label": "Confirm email", "url": settings.rp_origin}),
                           text=body, category="access")
    out = {"sent": channel in ("ses", "smtp", "log"), "channel": channel}
    if settings.environment == "development":
        out["dev_code"] = code
    return out



# --- organization summary --------------------------------------------------


@router.get("")
def org_summary(principal: security.Principal = Depends(security.require_org_admin),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    users = db.query(User).filter(User.tenant_id == tenant.id).all()
    vaults = db.query(Vault).filter(Vault.tenant_id == tenant.id).all()
    appliances = db.query(Appliance).filter(Appliance.tenant_id == tenant.id).all()
    return {
        "id": tenant.id,
        "name": tenant.name,
        "plan": tenant.plan,
        "key_ownership_model": tenant.key_ownership_model,
        "counts": {
            "users": len(users),
            "admins": sum(1 for u in users if security.is_org_admin(u.role)),
            "vaults": len(vaults),
            "appliances": len(appliances),
        },
    }


class OrgUpdateRequest(BaseModel):
    name: str


@router.put("")
def rename_org(body: OrgUpdateRequest,
               principal: security.Principal = Depends(security.require_org_admin),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    """Rename the organization (owner/admin)."""
    name = (body.name or "").strip()[:120]
    if not name:
        raise HTTPException(400, "enter an organization name")
    old = tenant.name
    tenant.name = name
    db.commit()
    audit.record(db, actor=principal.user_id, action="org.renamed",
                 tenant_id=tenant.id, category="admin", severity="notice",
                 detail={"from": old, "to": name})
    return {"ok": True, "name": name}


# --- members ---------------------------------------------------------------


class CreateUserRequest(BaseModel):
    email: str
    display_name: str
    role: str = "member"


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    role: str | None = None
    status: str | None = None
    allow_impersonation: bool | None = None


@router.get("/users")
def list_users(principal: security.Principal = Depends(security.require_org_admin),
               tenant: Tenant = Depends(security.get_tenant),
               db: Session = Depends(get_db)):
    users = db.query(User).filter(User.tenant_id == tenant.id).all()
    vaults = db.query(Vault).filter(Vault.tenant_id == tenant.id).all()
    obj_by_vault = _object_counts_by_vault(db, tenant.id)
    bytes_by_vault = _bytes_by_vault(db, tenant.id)
    return [_user_view(db, u, vaults, obj_by_vault, bytes_by_vault)
            for u in sorted(users, key=lambda u: (u.role != "owner", u.display_name))]


@router.post("/users")
def create_user(body: CreateUserRequest,
                principal: security.Principal = Depends(security.require_org_admin),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    role = body.role if body.role in ASSIGNABLE_ROLES else "member"
    if role == "owner" and not security.is_owner(principal.role):
        raise HTTPException(403, "only an owner can add another owner")
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(400, "a valid email is required")
    # One account per email address, platform-wide (case-insensitive).
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "a user with this email already exists")
    # Seat/licence limit (no-op unless entitlement enforcement is enabled for the tenant).
    from .. import entitlements
    entitlements.require_seat(db, tenant, db.get(User, principal.user_id))
    user = User(tenant_id=tenant.id, email=email,
                display_name=body.display_name.strip() or email.split("@")[0],
                role=role, status="active")
    db.add(user)
    db.flush()
    # Every member owns their own vault (their data demarcation) with keys.
    vault = Vault(tenant_id=tenant.id, owner_user_id=user.id,
                  name=f"{user.display_name.split()[0]}'s Vault",
                  key_ownership_model=tenant.key_ownership_model or "customer-managed",
                  crypto_profile_id="cvp-hybrid-2026a")
    db.add(vault)
    db.flush()
    result = keybroker.provision_vault_root_key(vault.id, vault.key_ownership_model)
    vault.wrapped_keys = [{"recipient": "primary", "hash": result["record"]["rootKeyHash"]}]
    keybroker.provision_recovery_keypair(vault.id)
    db.commit()
    invite = _send_invite(db, user, tenant.name)
    audit.record(db, actor=principal.user_id, action="org.user_added",
                 tenant_id=tenant.id, resource=user.id, category="admin",
                 severity="notice", detail={"email": email, "role": role})
    return {"id": user.id, "email": user.email, "role": user.role, "invite": invite}


@router.put("/users/{uid}")
def update_user(uid: str, body: UpdateUserRequest,
                principal: security.Principal = Depends(security.require_org_admin),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u or u.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    changed_email = None
    if body.first_name is not None:
        u.first_name = body.first_name.strip()[:80]
    if body.last_name is not None:
        u.last_name = body.last_name.strip()[:80]
    if body.display_name is not None:
        u.display_name = body.display_name.strip() or u.display_name
    elif body.first_name is not None or body.last_name is not None:
        full = f"{u.first_name} {u.last_name}".strip()
        if full:
            u.display_name = full
    if body.email is not None:
        email = body.email.strip().lower()
        if not _EMAIL_RE.match(email):
            raise HTTPException(400, "enter a valid email address")
        if email != (u.email or "").lower():
            if db.query(User).filter(func.lower(User.email) == email,
                                     User.id != u.id).first():
                raise HTTPException(409, "a user with this email already exists")
            u.email = email
            u.email_verified = False   # must re-confirm the new address
            changed_email = email
    if body.role is not None and body.role in ASSIGNABLE_ROLES and body.role != u.role:
        # Only an owner may grant or revoke the owner role, and the last active
        # owner can't be demoted.
        if (body.role == "owner" or u.role == "owner") and not security.is_owner(principal.role):
            raise HTTPException(403, "only an owner can change owner assignments")
        if u.role == "owner" and body.role != "owner":
            others = (db.query(func.count(User.id))
                      .filter(User.tenant_id == tenant.id, User.role == "owner",
                              User.id != u.id, User.status == "active").scalar())
            if not others:
                raise HTTPException(409, "the organization must keep at least one owner")
        u.role = body.role
    if body.status is not None and body.status in ("active", "suspended"):
        u.status = body.status
    if body.allow_impersonation is not None:
        u.allow_impersonation = bool(body.allow_impersonation)
    db.commit()
    verify = _send_verification(u) if changed_email else None
    audit.record(db, actor=principal.user_id, action="org.user_updated",
                 tenant_id=tenant.id, resource=u.id, category="admin", severity="notice",
                 detail={"email": u.email, "role": u.role, "status": u.status,
                         "email_changed": bool(changed_email)})
    return {"ok": True, "id": u.id, "role": u.role, "status": u.status,
            "email": u.email, "email_verified": bool(u.email_verified),
            "verification": verify}


@router.delete("/users/{uid}")
def remove_user(uid: str,
                principal: security.Principal = Depends(security.require_org_admin),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u or u.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    if u.id == principal.user_id:
        raise HTTPException(409, "you can't remove yourself")
    if u.role == "owner":
        others = (db.query(func.count(User.id))
                  .filter(User.tenant_id == tenant.id, User.role == "owner",
                          User.id != u.id, User.status == "active").scalar())
        if not others:
            raise HTTPException(409, "the organization must keep at least one owner")
        if not security.is_owner(principal.role):
            raise HTTPException(403, "only an owner can remove an owner")
    from ..models import Passkey
    db.query(Passkey).filter(Passkey.user_id == uid).delete()
    email = u.email
    db.delete(u)
    db.commit()
    audit.record(db, actor=principal.user_id, action="org.user_removed",
                 tenant_id=tenant.id, category="admin", severity="warning",
                 detail={"email": email})
    return {"ok": True}


# --- member detail / account management ------------------------------------
def _passkey_view(p: Passkey) -> dict:
    return {"id": p.id, "label": p.label or "Passkey", "transport": p.transport or "internal",
            "created_at": p.created_at.isoformat() if p.created_at else None}


@router.get("/users/{uid}")
def user_detail(uid: str,
                principal: security.Principal = Depends(security.require_org_admin),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    """Full account details for one member: profile, permissions, passkeys (2FA),
    usage, and recent account activity."""
    u = db.get(User, uid)
    if not u or u.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    vaults = db.query(Vault).filter(Vault.tenant_id == tenant.id,
                                    Vault.owner_user_id == u.id).all()
    obj_by_vault = _object_counts_by_vault(db, tenant.id)
    bytes_by_vault = _bytes_by_vault(db, tenant.id)
    passkeys = db.query(Passkey).filter(Passkey.user_id == u.id).all()
    return {
        "id": u.id, "email": u.email, "display_name": u.display_name,
        "first_name": u.first_name or "", "last_name": u.last_name or "",
        "phone": u.phone or "", "role": u.role, "status": u.status,
        "email_verified": bool(u.email_verified),
        "is_you": u.id == principal.user_id,
        "is_platform_admin": bool(u.is_platform_admin),
        "allow_impersonation": bool(getattr(u, "allow_impersonation", False)),
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "permissions": {
            "is_admin": security.is_org_admin(u.role),
            "is_owner": security.is_owner(u.role),
            "can_manage_org": security.is_org_admin(u.role),
        },
        "passkeys": [_passkey_view(p) for p in
                     sorted(passkeys, key=lambda p: p.created_at or _dt_min())],
        "usage": {
            "vault_count": len(vaults),
            "object_count": sum(obj_by_vault.get(v.id, 0) for v in vaults),
            "protected_bytes": sum(bytes_by_vault.get(v.id, 0) for v in vaults),
            "vaults": [{"id": v.id, "name": v.name,
                        "object_count": obj_by_vault.get(v.id, 0),
                        "protected_bytes": bytes_by_vault.get(v.id, 0)} for v in vaults],
        },
    }


def _dt_min():
    from datetime import datetime
    return datetime.min


@router.get("/users/{uid}/activity")
def user_activity(uid: str, limit: int = 50,
                  principal: security.Principal = Depends(security.require_org_admin),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    """Recent audit events by or about this member (sign-ins, admin actions, …)."""
    u = db.get(User, uid)
    if not u or u.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    rows = (db.query(AuditEvent)
            .filter(AuditEvent.tenant_id == tenant.id,
                    or_(AuditEvent.actor == u.id, AuditEvent.actor == u.email,
                        AuditEvent.resource == u.id))
            .order_by(AuditEvent.created_at.desc())
            .limit(min(200, max(1, limit))).all())
    return [{"action": r.action, "resource": r.resource, "category": r.category,
             "severity": r.severity, "detail": r.detail or {},
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rows]


@router.post("/users/{uid}/resend-verification")
def resend_verification(uid: str,
                        principal: security.Principal = Depends(security.require_org_admin),
                        tenant: Tenant = Depends(security.get_tenant),
                        db: Session = Depends(get_db)):
    """Re-send the email-confirmation code to a member's address."""
    u = db.get(User, uid)
    if not u or u.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    if u.email_verified:
        return {"ok": True, "already_verified": True}
    verify = _send_verification(u)
    audit.record(db, actor=principal.user_id, action="org.user_verification_sent",
                 tenant_id=tenant.id, resource=u.id, category="admin", severity="info",
                 detail={"email": u.email})
    db.commit()
    return {"ok": True, "verification": verify}


@router.post("/users/{uid}/reset-passkeys")
def reset_passkeys(uid: str,
                   principal: security.Principal = Depends(security.require_org_admin),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    """Reset a member's sign-in: remove all their passkeys (2FA) and email them a
    fresh sign-in code so they re-enroll a device. The passkey-only equivalent of a
    password reset. Owner passkeys can only be reset by an owner."""
    u = db.get(User, uid)
    if not u or u.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    if u.role == "owner" and not security.is_owner(principal.role):
        raise HTTPException(403, "only an owner can reset an owner's sign-in")
    n = db.query(Passkey).filter(Passkey.user_id == u.id).delete()
    db.commit()
    invite = _send_invite(db, u, tenant.name)
    audit.record(db, actor=principal.user_id, action="org.user_passkeys_reset",
                 tenant_id=tenant.id, resource=u.id, category="security",
                 severity="warning", detail={"email": u.email, "removed": int(n or 0)})
    db.commit()
    return {"ok": True, "removed": int(n or 0), "invite": invite}


@router.delete("/users/{uid}/passkeys/{pid}")
def remove_passkey(uid: str, pid: str,
                   principal: security.Principal = Depends(security.require_org_admin),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    """Remove a single passkey from a member (e.g. a lost device)."""
    u = db.get(User, uid)
    if not u or u.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    if u.role == "owner" and not security.is_owner(principal.role):
        raise HTTPException(403, "only an owner can manage an owner's passkeys")
    p = db.get(Passkey, pid)
    if not p or p.user_id != u.id:
        raise HTTPException(404, "passkey not found")
    db.delete(p)
    db.commit()
    audit.record(db, actor=principal.user_id, action="org.user_passkey_removed",
                 tenant_id=tenant.id, resource=u.id, category="security", severity="warning",
                 detail={"email": u.email, "passkey": p.label})
    return {"ok": True}


# --- impersonation ---------------------------------------------------------


@router.post("/users/{uid}/impersonate")
def impersonate_user(uid: str,
                     principal: security.Principal = Depends(security.require_org_admin),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    """Start an OWNER-only impersonation session: mint a session token that assumes
    the member's identity (so the owner sees exactly their experience). The member
    must have opted in (``allow_impersonation``, admin-enabled). The token records
    the real actor so every action stays attributable, and is NOT passkey-verified
    (so the member's own passkey is still required for recovery/destructive ops)."""
    if not security.is_owner(principal.role):
        raise HTTPException(403, "only an owner can impersonate a member")
    if principal.impersonator_id:
        raise HTTPException(409, "already impersonating — exit the current session first")
    u = db.get(User, uid)
    if not u or u.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    if u.id == principal.user_id:
        raise HTTPException(400, "you can't impersonate yourself")
    if u.is_platform_admin or u.role == "owner":
        raise HTTPException(403, "owners and platform administrators can't be impersonated")
    if u.status != "active":
        raise HTTPException(409, "this member's account isn't active")
    if not getattr(u, "allow_impersonation", False):
        raise HTTPException(403, "this member hasn't allowed impersonation — enable it "
                                 "on their account first")
    token = security.create_session_token(u, passkey_verified=False,
                                           impersonator_id=principal.user_id)
    audit.record(db, actor=principal.user_id, action="org.impersonation_started",
                 tenant_id=tenant.id, resource=u.id, category="admin", severity="warning",
                 detail={"target_email": u.email, "target": u.display_name})
    return {"token": token, "user_id": u.id, "display_name": u.display_name,
            "tenant_id": u.tenant_id, "role": u.role, "is_platform_admin": False,
            "passkey_verified": False}


# --- appliances (assignment) ----------------------------------------------


class AssignRequest(BaseModel):
    user_id: str
    can_manage: bool = False


@router.get("/appliances")
def list_appliances(principal: security.Principal = Depends(security.require_org_admin),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    appliances = db.query(Appliance).filter(Appliance.tenant_id == tenant.id).all()
    users = {u.id: u for u in db.query(User).filter(User.tenant_id == tenant.id).all()}
    assigns = (db.query(ApplianceAssignment)
               .filter(ApplianceAssignment.tenant_id == tenant.id).all())
    by_appliance: dict[str, list] = {}
    for a in assigns:
        by_appliance.setdefault(a.appliance_id, []).append(a)
    stores = db.query(ApplianceStorage).filter(ApplianceStorage.tenant_id == tenant.id).all()
    stores_by_appliance: dict[str, list] = {}
    for s in stores:
        stores_by_appliance.setdefault(s.appliance_id, []).append(s)
    out = []
    for a in appliances:
        members = []
        for asn in by_appliance.get(a.id, []):
            u = users.get(asn.user_id)
            if not u:
                continue
            members.append({"user_id": u.id, "display_name": u.display_name,
                            "email": u.email, "role": u.role,
                            "can_manage": bool(asn.can_manage)})
        st = stores_by_appliance.get(a.id, [])
        out.append({
            "id": a.id, "name": a.name, "model": a.model, "serial": a.serial,
            "state": a.state, "online": bool(a.last_heartbeat_at),
            "location_label": a.location_label,
            "capacity_bytes": sum(int(s.capacity_bytes or 0) for s in st),
            "used_bytes": sum(int(s.used_bytes or 0) for s in st),
            "assignments": sorted(members, key=lambda m: m["display_name"]),
        })
    return out


@router.post("/appliances/{aid}/assignments")
def assign_appliance(aid: str, body: AssignRequest,
                     principal: security.Principal = Depends(security.require_org_admin),
                     tenant: Tenant = Depends(security.get_tenant),
                     db: Session = Depends(get_db)):
    appliance = db.get(Appliance, aid)
    if not appliance or appliance.tenant_id != tenant.id:
        raise HTTPException(404, "appliance not found")
    user = db.get(User, body.user_id)
    if not user or user.tenant_id != tenant.id:
        raise HTTPException(404, "member not found")
    existing = (db.query(ApplianceAssignment)
                .filter(ApplianceAssignment.appliance_id == aid,
                        ApplianceAssignment.user_id == user.id).first())
    if existing:
        existing.can_manage = body.can_manage
    else:
        db.add(ApplianceAssignment(tenant_id=tenant.id, appliance_id=aid,
                                   user_id=user.id, can_manage=body.can_manage))
    db.commit()
    audit.record(db, actor=principal.user_id, action="org.appliance_assigned",
                 tenant_id=tenant.id, resource=aid, category="admin", severity="notice",
                 detail={"user": user.email, "can_manage": body.can_manage})
    return {"ok": True}


@router.delete("/appliances/{aid}/assignments/{uid}")
def unassign_appliance(aid: str, uid: str,
                       principal: security.Principal = Depends(security.require_org_admin),
                       tenant: Tenant = Depends(security.get_tenant),
                       db: Session = Depends(get_db)):
    n = (db.query(ApplianceAssignment)
         .filter(ApplianceAssignment.appliance_id == aid,
                 ApplianceAssignment.user_id == uid,
                 ApplianceAssignment.tenant_id == tenant.id).delete())
    db.commit()
    audit.record(db, actor=principal.user_id, action="org.appliance_unassigned",
                 tenant_id=tenant.id, resource=aid, category="admin", severity="notice",
                 detail={"user_id": uid})
    return {"ok": bool(n)}


# --- keys (per-member overview + recovery) ---------------------------------


@router.get("/keys")
def list_keys(principal: security.Principal = Depends(security.require_org_admin),
              tenant: Tenant = Depends(security.get_tenant),
              db: Session = Depends(get_db)):
    users = {u.id: u for u in db.query(User).filter(User.tenant_id == tenant.id).all()}
    vaults = db.query(Vault).filter(Vault.tenant_id == tenant.id).all()
    out = []
    for v in vaults:
        owner = users.get(v.owner_user_id)
        meta = keybroker.key_metadata(v.id)
        out.append({
            "vault_id": v.id,
            "vault_name": v.name,
            "owner_user_id": v.owner_user_id,
            "owner_name": owner.display_name if owner else "Unassigned",
            "owner_email": owner.email if owner else None,
            **meta,
        })
    return sorted(out, key=lambda r: r["owner_name"])


@router.post("/keys/{vault_id}/recover")
def recover_key(vault_id: str,
                principal: security.Principal = Depends(security.require_org_admin),
                tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    """Authorized recovery of a member's vault key (lost-key / end-of-life).

    Gated behind an org admin *and* a verified passkey step-up, and written to
    the tamper-evident audit ledger."""
    if not principal.passkey_verified:
        raise HTTPException(403, "unlock with your passkey to recover a key")
    vault = db.get(Vault, vault_id)
    if not vault or vault.tenant_id != tenant.id:
        raise HTTPException(404, "vault not found")
    owner = db.get(User, vault.owner_user_id) if vault.owner_user_id else None
    try:
        result = keybroker.recover_vault_key(vault_id)
    except FileNotFoundError:
        raise HTTPException(404, "no key material is provisioned for this vault")
    audit.record(db, actor=principal.user_id, action="org.key_recovered",
                 tenant_id=tenant.id, resource=vault_id, category="security",
                 severity="critical",
                 detail={"vault": vault.name,
                         "owner": owner.email if owner else None,
                         "root_key_hash": result.get("root_key_hash")})
    return result
