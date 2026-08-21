"""Customer-facing Organization Admin (owner/admin only).

Everything an org owner or admin needs to run a multi-user organization:
manage members and their roles, assign appliances to members, and oversee each
member's encryption keys — including authorized key recovery for the lost-key /
end-of-life use case. Members never reach these endpoints (require_org_admin);
and even here admins see aggregate statistics and key *fingerprints*, never
another member's actual content.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import audit, authcodes, emailer, keybroker, security
from ..config import get_settings
from ..db import get_db
from ..models import (
    Appliance,
    ApplianceAssignment,
    ApplianceStorage,
    Collection,
    SearchDocument,
    Tenant,
    User,
    Vault,
)

router = APIRouter(prefix="/org", tags=["organization"])

ASSIGNABLE_ROLES = ("member", "admin", "owner")


# --- helpers ---------------------------------------------------------------


def _object_counts_by_vault(db: Session, tenant_id: str) -> dict[str, int]:
    rows = (db.query(SearchDocument.vault_id, func.count(SearchDocument.id))
            .filter(SearchDocument.tenant_id == tenant_id)
            .group_by(SearchDocument.vault_id).all())
    return {vid: int(n) for vid, n in rows if vid}


def _bytes_by_vault(db: Session, tenant_id: str) -> dict[str, int]:
    rows = (db.query(SearchDocument.vault_id,
                     func.coalesce(func.sum(SearchDocument.size_bytes), 0))
            .filter(SearchDocument.tenant_id == tenant_id)
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
                           text=body)
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


# --- members ---------------------------------------------------------------


class CreateUserRequest(BaseModel):
    email: str
    display_name: str
    role: str = "member"


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    role: str | None = None
    status: str | None = None


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
    if body.display_name is not None:
        u.display_name = body.display_name.strip() or u.display_name
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
    db.commit()
    audit.record(db, actor=principal.user_id, action="org.user_updated",
                 tenant_id=tenant.id, resource=u.id, category="admin", severity="notice",
                 detail={"email": u.email, "role": u.role, "status": u.status})
    return {"ok": True, "id": u.id, "role": u.role, "status": u.status}


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
