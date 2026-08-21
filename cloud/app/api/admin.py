"""
Backend admin console API (platform-admin only).

Provides cross-tenant fleet visibility, tenant administration, crypto-profile
and quantum-transition inventory, audit-ledger verification, and software-release
publishing / update dispatch. Deliberately excludes any operation that would let
an operator read customer plaintext (spec 3.1: no standing plaintext access).
"""

from __future__ import annotations

import os
import secrets
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import distinct, func
from sqlalchemy.orm import Session

from cv_crypto.profiles import PROFILE_REGISTRY
from cv_crypto.provider import get_provider

from .. import audit, authcodes, credstore, keybroker, platform_config, security, services
from ..config import get_settings
from ..db import get_db
from ..models import (
    Appliance,
    AuditEvent,
    Collection,
    ConfigObject,
    ConnectorAccount,
    DesktopAgent,
    Node,
    NodeBlueprint,
    PricingConfig,
    SearchDocument,
    ServiceObject,
    SnapshotReceipt,
    SoftwareRelease,
    SourceConfig,
    SyncJob,
    Tenant,
    UpdateJob,
    User,
    Vault,
)

router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(security.require_platform_admin)])

_TB = 1024 ** 4


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/pricing")
def get_pricing(db: Session = Depends(get_db)):
    from .billing import get_pricing as _get, pricing_public
    return pricing_public(_get(db))


class PricingUpdate(BaseModel):
    currency: str | None = None
    protection_price_per_tb_month: float | None = None
    cloud_price_per_tb_month: float | None = None
    s3_price_per_tb_month: float | None = None
    azure_price_per_tb_month: float | None = None
    license_plans: list[dict] | None = None
    appliance_tiers: list[dict] | None = None
    data_value_per_type: dict | None = None


@router.put("/pricing")
def update_pricing(body: PricingUpdate, db: Session = Depends(get_db)):
    from .billing import get_pricing as _get, pricing_public
    p = _get(db)
    for k, v in body.dict(exclude_none=True).items():
        setattr(p, k, v)
    db.commit()
    return pricing_public(p)


# --- Email: configuration, test, and broadcast ------------------------------

def _email_config(db: Session):
    from ..models import EmailConfig
    row = db.get(EmailConfig, "default")
    if row is None:
        row = EmailConfig(id="default")
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _email_config_view(row) -> dict:
    return {"provider": row.provider, "enabled": bool(row.enabled),
            "from_email": row.from_email, "from_name": row.from_name,
            "reply_to": row.reply_to, "region": row.region,
            "aws_access_key_id": row.aws_access_key_id or "",
            "has_aws_secret": bool(row.aws_secret_encrypted)}


@router.get("/email-config")
def get_email_config(db: Session = Depends(get_db)):
    return _email_config_view(_email_config(db))


class EmailConfigUpdate(BaseModel):
    provider: str | None = None
    enabled: bool | None = None
    from_email: str | None = None
    from_name: str | None = None
    reply_to: str | None = None
    region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None  # write-only; encrypted at rest


@router.put("/email-config")
def update_email_config(body: EmailConfigUpdate, db: Session = Depends(get_db)):
    from .. import emailer, credstore
    row = _email_config(db)
    data = body.dict(exclude_none=True)
    secret = data.pop("aws_secret_access_key", None)
    for k, v in data.items():
        setattr(row, k, v)
    if secret is not None:  # empty string clears it; a value (re)encrypts it
        row.aws_secret_encrypted = credstore.encrypt("platform", {"s": secret}) if secret.strip() else ""
    db.commit()
    emailer.invalidate_config_cache()
    return _email_config_view(row)


@router.get("/users")
def list_all_users(db: Session = Depends(get_db)):
    """Every user across tenants, for choosing broadcast recipients."""
    tenants = {t.id: t.name for t in db.query(Tenant).all()}
    return [{
        "id": u.id, "email": u.email, "display_name": u.display_name,
        "role": u.role, "status": u.status,
        "tenant_id": u.tenant_id, "tenant_name": tenants.get(u.tenant_id, ""),
    } for u in db.query(User).order_by(User.email.asc()).all()]


class EmailTest(BaseModel):
    to: str


@router.post("/email-test")
def send_email_test(body: EmailTest,
                    principal: security.Principal = Depends(security.require_platform_admin),
                    db: Session = Depends(get_db)):
    from .. import emailer
    html = emailer.render(
        "Test email from Arkive",
        emailer.text_to_html("This is a test message confirming your Arkive email "
                             "delivery is configured correctly.\n\nIf you received this, "
                             "SES is wired up and ready."),
        preheader="Arkive email delivery test")
    result = emailer.send_verbose(body.to.strip(), "Test email from Arkive", html=html,
                                  text="Arkive email delivery test — SES is configured correctly.")
    audit.record(db, actor=principal.user_id, action="admin.email_test",
                 detail={"to": body.to, "channel": result["channel"], "error": result["error"]})
    return {"channel": result["channel"], "provider": result["provider"],
            "error": result["error"],
            "delivered": result["channel"] in ("ses", "smtp")}


class EmailBroadcast(BaseModel):
    audience: str = "all"          # all | selected
    user_ids: list[str] = []       # when audience == selected
    tenant_id: str | None = None   # optional: limit "all" to one tenant
    subject: str
    message: str                   # plain text (escaped into the template)
    cta_label: str | None = None
    cta_url: str | None = None


@router.post("/email-broadcast")
def email_broadcast(body: EmailBroadcast,
                    principal: security.Principal = Depends(security.require_platform_admin),
                    db: Session = Depends(get_db)):
    from .. import emailer
    q = db.query(User).filter(User.status == "active")
    if body.audience == "selected":
        if not body.user_ids:
            return {"sent": 0, "failed": 0, "recipients": 0}
        q = q.filter(User.id.in_(body.user_ids))
    elif body.tenant_id:
        q = q.filter(User.tenant_id == body.tenant_id)
    recipients = [u.email for u in q.all() if u.email]

    cta = ({"label": body.cta_label, "url": body.cta_url}
           if body.cta_label and body.cta_url else None)
    html = emailer.render(body.subject, emailer.text_to_html(body.message),
                          preheader=body.subject, cta=cta)
    result = emailer.send_bulk(recipients, body.subject, html=html, text=body.message)
    audit.record(db, actor=principal.user_id, action="admin.email_broadcast",
                 category="admin", severity="notice",
                 detail={"audience": body.audience, "recipients": len(recipients),
                         "sent": result["sent"], "failed": result["failed"],
                         "subject": body.subject})
    return {**result, "recipients": len(recipients)}


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    provider = get_provider()
    return {
        "tenants": db.query(func.count(Tenant.id)).scalar(),
        "users": db.query(func.count(User.id)).scalar(),
        "appliances": db.query(func.count(Appliance.id)).scalar(),
        "connectors": db.query(func.count(ConnectorAccount.id)).scalar(),
        "nodes": db.query(func.count(Node.id)).scalar(),
        "snapshots": db.query(func.count(SnapshotReceipt.id)).scalar(),
        "recoverable_snapshots": db.query(func.count(SnapshotReceipt.id))
            .filter(SnapshotReceipt.recoverable.is_(True)).scalar(),
        "pq_available": provider.pq_available,
        "audit_chain_valid": audit.verify_chain(db),
    }


@router.get("/tenants")
def list_tenants(db: Session = Depends(get_db)):
    nodes = {n.id: n.name for n in db.query(Node).all()}
    out = []
    for t in db.query(Tenant).all():
        out.append({
            "id": t.id,
            "name": t.name,
            "plan": t.plan,
            "tenant_type": t.tenant_type or "dedicated",
            "key_ownership_model": t.key_ownership_model,
            "status": t.status,
            "node_id": t.node_id,
            "node": nodes.get(t.node_id) if t.node_id else None,
            "users": db.query(func.count(User.id)).filter(User.tenant_id == t.id).scalar(),
            "appliances": db.query(func.count(Appliance.id))
                .filter(Appliance.tenant_id == t.id).scalar(),
            "sources": db.query(func.count(ConnectorAccount.id))
                .filter(ConnectorAccount.tenant_id == t.id).scalar(),
        })
    return out


@router.get("/fleet")
def fleet_view(db: Session = Depends(get_db)):
    rows = db.query(Appliance).all()
    return [{
        "id": a.id, "serial": a.serial, "model": a.model, "name": a.name,
        "tenant_id": a.tenant_id, "state": a.state,
        "isolation_state": a.isolation_state, "attestation_ok": a.attestation_ok,
        "tamper_state": a.tamper_state, "software_version": a.software_version,
        "last_heartbeat_at": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None,
    } for a in rows]


@router.get("/crypto-profiles")
def crypto_profiles():
    """Cryptographic-profile registry + quantum-transition inventory (spec 9.7)."""
    return {"profiles": [p.as_dict() for p in PROFILE_REGISTRY.values()],
            "pq_available": get_provider().pq_available}


@router.get("/audit")
def audit_log(limit: int = 100, db: Session = Depends(get_db)):
    rows = (db.query(AuditEvent).order_by(AuditEvent.created_at.desc())
            .limit(limit).all())
    return {
        "chain_valid": audit.verify_chain(db),
        "events": [{
            "actor": e.actor, "action": e.action, "resource": e.resource,
            "tenant_id": e.tenant_id, "detail": e.detail,
            "entry_hash": e.entry_hash[:16],
            "created_at": e.created_at.isoformat(),
        } for e in rows],
    }


# =========================================================================== #
# Tenant administration                                                       #
# =========================================================================== #

def _tenant_counts(db: Session, tid: str) -> dict:
    return {
        "users": db.query(func.count(User.id)).filter(User.tenant_id == tid).scalar(),
        "appliances": db.query(func.count(Appliance.id)).filter(Appliance.tenant_id == tid).scalar(),
        "agents": db.query(func.count(DesktopAgent.id)).filter(DesktopAgent.tenant_id == tid).scalar(),
        "sources": db.query(func.count(ConnectorAccount.id)).filter(ConnectorAccount.tenant_id == tid).scalar(),
        "mappings": db.query(func.count(Collection.id)).filter(Collection.tenant_id == tid).scalar(),
        "recovery_points": db.query(func.count(SnapshotReceipt.id)).filter(SnapshotReceipt.tenant_id == tid).scalar(),
        "objects": db.query(func.count(distinct(SearchDocument.object_id))).filter(SearchDocument.tenant_id == tid).scalar(),
    }


def _storage_usage(db: Session, tid: str, pricing) -> dict:
    """Actual bytes physically stored for a tenant, split by channel (Arkive
    Cloud, appliance, customer bucket) + the recurring cost of each channel."""
    rows = (db.query(SnapshotReceipt.destination,
                     func.coalesce(func.sum(SnapshotReceipt.total_bytes), 0))
            .filter(SnapshotReceipt.tenant_id == tid)
            .group_by(SnapshotReceipt.destination).all())
    cloud = appliance = customer = 0
    for dest, b in rows:
        b = int(b or 0)
        d = dest or ""
        if d == "customer-s3":
            customer += b
        elif d == "cv-cloud":
            cloud += b
        else:  # store:<id> / appliance / appliance:<id>
            appliance += b
    return {
        "cloud_bytes": cloud, "appliance_bytes": appliance, "customer_bytes": customer,
        "cloud_monthly": round(cloud / _TB * pricing.cloud_price_per_tb_month, 2),
        "customer_monthly": round(customer / _TB * pricing.s3_price_per_tb_month, 2),
    }


def _tenant_view(db: Session, t: Tenant, detail: bool = False) -> dict:
    v = {
        "id": t.id, "name": t.name, "plan": t.plan, "status": t.status,
        "tenant_type": t.tenant_type or "dedicated",
        "node_id": t.node_id,
        "key_ownership_model": t.key_ownership_model,
        "licensed_bytes": int(t.licensed_bytes or 0),
        "protection_options": t.protection_options or [],
        "appliance_plan": t.appliance_plan or [],
        "created_at": t.created_at.isoformat() if t.created_at else None,
        **_tenant_counts(db, t.id),
    }
    n = db.get(Node, t.node_id) if t.node_id else None
    v["node"] = ({"id": n.id, "name": n.name, "role": n.role,
                  "endpoint": n.endpoint, "status": n.status} if n else None)
    if detail:
        from .billing import _compute_plan, get_pricing
        v["members"] = [_user_view(u) for u in
                        db.query(User).filter(User.tenant_id == t.id).order_by(User.email.asc()).all()]
        v["vaults"] = [{"id": vv.id, "name": vv.name,
                        "key_ownership_model": vv.key_ownership_model}
                       for vv in db.query(Vault).filter(Vault.tenant_id == t.id).all()]
        # Tight coupling to what the customer selected in Protection Setup + what
        # they pay Arkive, and the real footprint per storage channel.
        try:
            v["billing"] = _compute_plan(db, t)
        except Exception:  # noqa: BLE001 - never fail the tenant view on pricing
            v["billing"] = None
        v["storage_usage"] = _storage_usage(db, t.id, get_pricing(db))
    return v


class TenantCreate(BaseModel):
    name: str
    plan: str = "business"
    tenant_type: str = "dedicated"
    node_id: str | None = None
    key_ownership_model: str = "customer-managed"
    licensed_tb: float = 0
    owner_email: str | None = None
    owner_name: str | None = None


@router.post("/tenants")
def create_tenant(body: TenantCreate,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    ttype = body.tenant_type if body.tenant_type in security.TENANT_TYPES else "dedicated"
    tenant = Tenant(
        name=body.name.strip() or "New Organization", plan=body.plan,
        tenant_type=ttype,
        node_id=body.node_id or None,
        key_ownership_model=body.key_ownership_model,
        storage_prefix=f"t-{secrets.token_hex(4)}",
        licensed_bytes=int(max(0.0, body.licensed_tb) * _TB),
    )
    db.add(tenant)
    db.flush()
    vault = Vault(tenant_id=tenant.id, name="Primary Vault",
                  key_ownership_model=body.key_ownership_model)
    db.add(vault)
    db.flush()
    keybroker.provision_vault_root_key(vault.id, vault.key_ownership_model)
    if body.owner_email:
        email = body.owner_email.strip().lower()
        if not db.query(User).filter(User.email == email).first():
            db.add(User(tenant_id=tenant.id, email=email,
                        display_name=(body.owner_name or email).strip(),
                        role="owner", status="active"))
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.tenant_created",
                 tenant_id=tenant.id, category="admin", severity="notice",
                 detail={"name": tenant.name, "plan": tenant.plan})
    return _tenant_view(db, tenant, detail=True)


@router.get("/tenants/{tid}")
def tenant_detail(tid: str, db: Session = Depends(get_db)):
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "tenant not found")
    return _tenant_view(db, t, detail=True)


class TenantUpdate(BaseModel):
    name: str | None = None
    plan: str | None = None
    tenant_type: str | None = None
    node_id: str | None = None
    status: str | None = None
    key_ownership_model: str | None = None
    licensed_tb: float | None = None


@router.put("/tenants/{tid}")
def update_tenant(tid: str, body: TenantUpdate,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "tenant not found")
    if body.name is not None:
        t.name = body.name.strip()
    if body.plan is not None:
        t.plan = body.plan
    if body.tenant_type is not None and body.tenant_type in security.TENANT_TYPES:
        t.tenant_type = body.tenant_type
    if body.node_id is not None:
        t.node_id = body.node_id or None
    if body.status is not None:
        t.status = body.status
    if body.key_ownership_model is not None:
        t.key_ownership_model = body.key_ownership_model
    if body.licensed_tb is not None:
        t.licensed_bytes = int(max(0.0, body.licensed_tb) * _TB)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.tenant_updated",
                 tenant_id=t.id, category="admin",
                 detail={"status": t.status, "plan": t.plan, "tenant_type": t.tenant_type,
                         "node_id": t.node_id})
    return _tenant_view(db, t, detail=True)


@router.delete("/tenants/{tid}")
def delete_tenant(tid: str,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    """Suspend a tenant (reversible): freeze it and deactivate all its users +
    tokens. A safe stand-in for hard deletion, which would destroy recovery data."""
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "tenant not found")
    t.status = "suspended"
    for u in db.query(User).filter(User.tenant_id == tid).all():
        u.status = "suspended"
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.tenant_suspended",
                 tenant_id=t.id, category="admin", severity="warning")
    return {"ok": True, "status": t.status}


# =========================================================================== #
# User administration                                                         #
# =========================================================================== #

def _user_view(u: User) -> dict:
    return {"id": u.id, "email": u.email, "display_name": u.display_name,
            "role": u.role, "status": u.status,
            "is_platform_admin": bool(u.is_platform_admin),
            "email_verified": bool(u.email_verified),
            "tenant_id": u.tenant_id,
            "created_at": u.created_at.isoformat() if u.created_at else None}


class UserCreate(BaseModel):
    email: str
    display_name: str = ""
    role: str = "member"
    send_invite: bool = True


@router.post("/tenants/{tid}/users")
def create_user(tid: str, body: UserCreate,
                principal: security.Principal = Depends(security.require_platform_admin),
                db: Session = Depends(get_db)):
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "tenant not found")
    email = body.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "a user with this email already exists")
    u = User(tenant_id=tid, email=email, display_name=(body.display_name or email).strip(),
             role=body.role, status="active")
    db.add(u)
    db.commit()
    db.refresh(u)
    invited = None
    if body.send_invite:
        invited = _send_access_email(db, u, "You've been added to Arkive",
                                     f"An administrator added you to {t.name} on Arkive. "
                                     f"Use the code below to sign in and set up your account.")
    audit.record(db, actor=principal.user_id, action="admin.user_created",
                 tenant_id=tid, category="admin", detail={"email": email, "role": body.role})
    return {**_user_view(u), "invite": invited}


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    status: str | None = None
    is_platform_admin: bool | None = None


@router.put("/users/{uid}")
def update_user(uid: str, body: UserUpdate,
                principal: security.Principal = Depends(security.require_platform_admin),
                db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "user not found")
    if body.display_name is not None:
        u.display_name = body.display_name.strip()
    if body.role is not None:
        u.role = body.role
    if body.status is not None:
        u.status = body.status
    if body.is_platform_admin is not None:
        u.is_platform_admin = body.is_platform_admin
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.user_updated",
                 tenant_id=u.tenant_id, category="admin", detail={"email": u.email})
    return _user_view(u)


@router.delete("/users/{uid}")
def delete_user(uid: str,
                principal: security.Principal = Depends(security.require_platform_admin),
                db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "user not found")
    # Don't strand a tenant without an owner.
    if u.role == "owner":
        owners = (db.query(func.count(User.id))
                  .filter(User.tenant_id == u.tenant_id, User.role == "owner",
                          User.id != u.id, User.status == "active").scalar())
        if owners == 0:
            raise HTTPException(409, "cannot delete the tenant's only owner")
    from ..models import Passkey
    db.query(Passkey).filter(Passkey.user_id == uid).delete()
    tenant_id = u.tenant_id
    email = u.email
    db.delete(u)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.user_deleted",
                 tenant_id=tenant_id, category="admin", severity="warning",
                 detail={"email": email})
    return {"ok": True}


@router.post("/users/{uid}/reset")
def reset_user(uid: str,
               principal: security.Principal = Depends(security.require_platform_admin),
               db: Session = Depends(get_db)):
    """Reset a user's access: revoke all passkeys and email a fresh sign-in code
    so they can re-enroll a device."""
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "user not found")
    from ..models import Passkey
    revoked = db.query(Passkey).filter(Passkey.user_id == uid).delete()
    db.commit()
    invite = _send_access_email(db, u, "Your Arkive access was reset",
                                "An administrator reset your Arkive sign-in. Use the code "
                                "below to sign back in and re-register your device.")
    audit.record(db, actor=principal.user_id, action="admin.user_reset",
                 tenant_id=u.tenant_id, category="security", severity="notice",
                 detail={"email": u.email, "passkeys_revoked": revoked})
    return {"ok": True, "passkeys_revoked": revoked, "invite": invite}


def _send_access_email(db: Session, u: User, subject: str, intro: str) -> dict:
    """Issue a sign-in code and email it via the shared branded template."""
    from .. import emailer
    try:
        code = authcodes.issue_code(u.email, "login")
    except Exception:
        return {"sent": False}
    body = f"{intro}\n\nYour sign-in code: {code}\n\nOpen {get_settings().rp_origin} to sign in."
    channel = emailer.send(u.email, subject,
                           html=emailer.render(subject, emailer.text_to_html(body),
                                               cta={"label": "Sign in", "url": get_settings().rp_origin}),
                           text=body)
    out = {"sent": channel in ("ses", "smtp", "log"), "channel": channel}
    if get_settings().environment == "development":
        out["dev_code"] = code
    return out


# =========================================================================== #
# Cross-tenant reporting                                                       #
# =========================================================================== #

@router.get("/reports")
def reports(db: Session = Depends(get_db)):
    """Usage + billing rollup across every tenant."""
    from .billing import _usage, get_pricing
    pricing = get_pricing(db)
    # Actual bytes stored in Arkive Cloud (cv-cloud) per tenant — the footprint
    # we pay the storage provider for (distinct from logical protected bytes).
    cloud_by_tenant = dict(
        db.query(SnapshotReceipt.tenant_id,
                 func.coalesce(func.sum(SnapshotReceipt.total_bytes), 0))
        .filter(SnapshotReceipt.destination == "cv-cloud")
        .group_by(SnapshotReceipt.tenant_id).all()
    )
    rows = []
    totals = {"tenants": 0, "users": 0, "objects": 0, "bytes": 0,
              "recovery_points": 0, "monthly_revenue": 0.0, "cloud_bytes": 0}
    for t in db.query(Tenant).all():
        objects, used_bytes, _ = _usage(db, t.id)
        counts = _tenant_counts(db, t.id)
        options = t.protection_options or []
        licensed_tb = (t.licensed_bytes or 0) / _TB
        used_tb = used_bytes / _TB
        monthly = licensed_tb * pricing.protection_price_per_tb_month
        if "cv-cloud" in options:
            monthly += used_tb * pricing.cloud_price_per_tb_month
        monthly = round(monthly, 2)
        cloud_bytes = int(cloud_by_tenant.get(t.id, 0) or 0)
        rows.append({
            "id": t.id, "name": t.name, "plan": t.plan, "status": t.status,
            "users": counts["users"], "appliances": counts["appliances"],
            "agents": counts["agents"], "sources": counts["sources"],
            "objects": objects, "used_bytes": used_bytes,
            "cloud_bytes": cloud_bytes,
            "licensed_bytes": int(t.licensed_bytes or 0),
            "recovery_points": counts["recovery_points"],
            "monthly_cost": monthly, "options": options,
        })
        totals["tenants"] += 1
        totals["users"] += counts["users"]
        totals["objects"] += objects
        totals["bytes"] += used_bytes
        totals["cloud_bytes"] += cloud_bytes
        totals["recovery_points"] += counts["recovery_points"]
        totals["monthly_revenue"] += monthly
    totals["monthly_revenue"] = round(totals["monthly_revenue"], 2)
    # Estimated provider cost of the cloud footprint (AWS S3 Standard estimate).
    totals["cloud_cost_monthly"] = round(
        (totals["cloud_bytes"] / _TB) * pricing.s3_price_per_tb_month, 2)
    rows.sort(key=lambda r: -r["monthly_cost"])
    return {"currency": pricing.currency, "tenants": rows, "totals": totals}


# =========================================================================== #
# Node fleet (multi-node scale)                                               #
# =========================================================================== #

def _mem_info() -> dict | None:
    try:
        info: dict = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                parts = rest.strip().split()
                if parts:
                    info[k.strip()] = int(parts[0]) * 1024  # kB → bytes
        total, avail = info.get("MemTotal"), info.get("MemAvailable")
        if total and avail is not None:
            return {"total": total, "used": total - avail, "free": avail}
    except Exception:
        return None
    return None


def _self_health(db: Session) -> dict:
    health: dict = {"online": True}
    path = os.environ.get("CV_OBJECT_STORE") or "/var/lib/continuity-vault"
    try:
        du = shutil.disk_usage(path if os.path.exists(path) else "/")
        health["storage"] = {"total": du.total, "used": du.used, "free": du.free}
    except Exception:
        pass
    try:
        health["load"] = [round(x, 2) for x in os.getloadavg()]
        health["cpus"] = os.cpu_count()
    except Exception:
        pass
    mem = _mem_info()
    if mem:
        health["memory"] = mem
    health["recovery_points"] = db.query(func.count(SnapshotReceipt.id)).scalar()
    return health


def _ensure_self_node(db: Session) -> Node:
    s = get_settings()
    n = db.query(Node).filter(Node.is_self.is_(True)).first()
    if n is None:
        n = Node(name=s.node_name or s.domain, role=s.node_role or "control-plane",
                 endpoint=s.api_base_url, is_self=True, status="active")
        db.add(n)
        db.commit()
        db.refresh(n)
    # Detect (and cache) which cloud/region/instance this node runs on.
    if not n.cloud:
        try:
            from .. import cloud_detect
            n.cloud = cloud_detect.detect()
            if n.cloud.get("region") and not n.region:
                n.region = n.cloud["region"]
        except Exception:
            pass
    n.last_heartbeat_at = _now()
    db.commit()
    return n


def _node_view(db: Session, n: Node) -> dict:
    if n.is_self:
        tel, online = _self_health(db), True
    else:
        tel = n.telemetry or {}
        online = bool(n.last_heartbeat_at and
                      (_now() - n.last_heartbeat_at).total_seconds() < 90)

    def _svc_name(sid):
        if not sid:
            return None
        s = db.get(ServiceObject, sid)
        return s.name if s else None

    return {
        "id": n.id, "name": n.name, "region": n.region, "role": n.role,
        "endpoint": n.endpoint, "status": n.status, "is_self": bool(n.is_self),
        "version": n.version, "online": online, "telemetry": tel,
        "cloud": n.cloud or {},
        "storage_service_id": n.storage_service_id,
        "email_service_id": n.email_service_id,
        "storage_service": _svc_name(n.storage_service_id),
        "email_service": _svc_name(n.email_service_id),
        "last_heartbeat_at": n.last_heartbeat_at.isoformat() if n.last_heartbeat_at else None,
    }


# --------------------------------------------------------------------------- #
# Worker processes — background backup/sync jobs (view + kill)                 #
# --------------------------------------------------------------------------- #

def _job_view(j, tenants, colls, accounts) -> dict:
    c = colls.get(j.collection_id)
    label = "—"
    if c is not None:
        acc = accounts.get(c.connector_account_id) if c.connector_account_id else None
        label = acc.account_label if acc else c.name
    return {
        "id": j.id, "tenant_id": j.tenant_id, "tenant": tenants.get(j.tenant_id) or "—",
        "collection_id": j.collection_id, "source": label,
        "source_type": c.source_type if c else None,
        "kind": j.kind, "status": j.status,
        "processed": j.processed or 0, "total": j.total or 0,
        "message": j.message or "", "error": j.error or "",
        "started_at": j.started_at.isoformat() if j.started_at else None,
        "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        "created_at": j.created_at.isoformat() if j.created_at else None,
    }


@router.get("/jobs")
def list_jobs(active: bool = False, limit: int = 100, db: Session = Depends(get_db)):
    """Worker processes (background backup/sync jobs) across every tenant."""
    _ACTIVE = ["queued", "running", "cancelling"]
    q = db.query(SyncJob).order_by(SyncJob.created_at.desc())
    if active:
        q = q.filter(SyncJob.status.in_(_ACTIVE))
    jobs = q.limit(max(1, min(limit, 500))).all()
    tenants = {t.id: t.name for t in db.query(Tenant).all()}
    cids = {j.collection_id for j in jobs if j.collection_id}
    colls = ({c.id: c for c in db.query(Collection).filter(Collection.id.in_(cids)).all()}
             if cids else {})
    aids = {c.connector_account_id for c in colls.values() if c.connector_account_id}
    accounts = ({a.id: a for a in db.query(ConnectorAccount).filter(ConnectorAccount.id.in_(aids)).all()}
                if aids else {})
    active_n = db.query(func.count(SyncJob.id)).filter(SyncJob.status.in_(_ACTIVE)).scalar()
    return {"active": int(active_n or 0),
            "jobs": [_job_view(j, tenants, colls, accounts) for j in jobs]}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str,
               principal: security.Principal = Depends(security.require_platform_admin),
               db: Session = Depends(get_db)):
    """Ask a running worker to stop. It aborts at its next checkpoint (chunk
    boundary or progress tick) and is marked cancelled."""
    from ..workers import jobs as jobs_mod
    j = db.get(SyncJob, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    if j.status in ("done", "failed", "cancelled"):
        return {"ok": True, "status": j.status}
    j.status = "cancelling"
    j.message = "Cancelling…"
    db.commit()
    jobs_mod.request_cancel(job_id)
    audit.record(db, actor=principal.user_id, action="admin.job_cancelled",
                 tenant_id=j.tenant_id, category="admin", severity="notice",
                 detail={"job": job_id})
    return {"ok": True, "status": "cancelling"}


@router.get("/nodes")
def list_nodes(db: Session = Depends(get_db)):
    _ensure_self_node(db)
    nodes = db.query(Node).all()
    nodes.sort(key=lambda x: (not x.is_self, x.name or ""))
    return [_node_view(db, n) for n in nodes]


class NodeCreate(BaseModel):
    name: str
    region: str = ""
    role: str = "control-plane"
    endpoint: str = ""


@router.post("/nodes")
def create_node(body: NodeCreate,
                principal: security.Principal = Depends(security.require_platform_admin),
                db: Session = Depends(get_db)):
    n = Node(name=body.name.strip() or "node", region=body.region.strip(),
             role=body.role, endpoint=body.endpoint.strip(), status="active")
    db.add(n)
    db.commit()
    db.refresh(n)
    audit.record(db, actor=principal.user_id, action="admin.node_registered",
                 category="admin", detail={"name": n.name, "role": n.role})
    return _node_view(db, n)


class NodeUpdate(BaseModel):
    name: str | None = None
    region: str | None = None
    role: str | None = None
    endpoint: str | None = None
    status: str | None = None
    storage_service_id: str | None = None
    email_service_id: str | None = None


@router.put("/nodes/{nid}")
def update_node(nid: str, body: NodeUpdate,
                principal: security.Principal = Depends(security.require_platform_admin),
                db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    for k, val in body.dict(exclude_none=True).items():
        setattr(n, k, val.strip() if isinstance(val, str) else val)
    # Empty string on a service selector clears it (use env/global fallback).
    if not n.storage_service_id:
        n.storage_service_id = None
    if not n.email_service_id:
        n.email_service_id = None
    db.commit()
    services.invalidate()
    from .. import emailer
    emailer.invalidate_config_cache()
    audit.record(db, actor=principal.user_id, action="admin.node_updated",
                 category="admin", detail={"name": n.name})
    return _node_view(db, n)


@router.delete("/nodes/{nid}")
def delete_node(nid: str,
                principal: security.Principal = Depends(security.require_platform_admin),
                db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    if n.is_self:
        raise HTTPException(400, "cannot remove the current node")
    db.delete(n)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.node_removed",
                 category="admin", severity="warning", detail={"name": n.name})
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Node blueprints — the role-specific config/version pushed to fleet nodes.    #
# --------------------------------------------------------------------------- #

NODE_ROLES = ["control-plane", "customer-tenant", "public-web"]


def _blueprint_view(bp: NodeBlueprint) -> dict:
    return {"role": bp.role, "target_version": bp.target_version or "",
            "config": bp.config or {}, "settings": bp.settings or {},
            "updated_at": bp.updated_at.isoformat() if bp.updated_at else None}


@router.get("/node-blueprints")
def list_blueprints(db: Session = Depends(get_db)):
    existing = {b.role: b for b in db.query(NodeBlueprint).all()}
    out = []
    for role in NODE_ROLES:
        bp = existing.get(role)
        if bp is None:
            out.append({"role": role, "target_version": "", "config": {},
                        "settings": {}, "updated_at": None})
        else:
            out.append(_blueprint_view(bp))
    return out


class BlueprintUpdate(BaseModel):
    target_version: str | None = None
    config: dict | None = None
    settings: dict | None = None


@router.put("/node-blueprints/{role}")
def update_blueprint(role: str, body: BlueprintUpdate,
                     principal: security.Principal = Depends(security.require_platform_admin),
                     db: Session = Depends(get_db)):
    if role not in NODE_ROLES:
        raise HTTPException(400, "unknown role")
    bp = db.get(NodeBlueprint, role)
    if bp is None:
        bp = NodeBlueprint(role=role)
        db.add(bp)
    if body.target_version is not None:
        bp.target_version = body.target_version.strip()
    if body.config is not None:
        bp.config = body.config
    if body.settings is not None:
        bp.settings = body.settings
    db.commit()
    db.refresh(bp)
    audit.record(db, actor=principal.user_id, action="admin.blueprint_updated",
                 category="admin", detail={"role": role})
    return _blueprint_view(bp)


# =========================================================================== #
# Configuration objects + source integrations                                 #
# =========================================================================== #

_SECRET_HINTS = ("secret", "password", "token", "private")


def _is_secret(key: str) -> bool:
    k = key.lower()
    return any(h in k for h in _SECRET_HINTS)


def _decrypt_values(obj) -> dict:
    if not obj or not obj.encrypted_values:
        return {}
    try:
        return credstore.decrypt("platform", obj.encrypted_values)
    except Exception:
        return {}


def _config_object_view(obj) -> dict:
    """Values with secret-looking keys masked (never return the secret itself)."""
    keys = {}
    for k, v in _decrypt_values(obj).items():
        secret = _is_secret(k)
        keys[k] = {"secret": secret, "set": bool(v), "value": "" if secret else v}
    return {"id": obj.id, "name": obj.name, "kind": obj.kind, "keys": keys,
            "updated_at": obj.updated_at.isoformat() if obj.updated_at else None}


@router.get("/config-objects")
def list_config_objects(db: Session = Depends(get_db)):
    return [_config_object_view(o) for o in
            db.query(ConfigObject).order_by(ConfigObject.name.asc()).all()]


class ConfigObjectBody(BaseModel):
    name: str
    kind: str = "generic"
    values: dict = {}


@router.post("/config-objects")
def create_config_object(body: ConfigObjectBody,
                         principal: security.Principal = Depends(security.require_platform_admin),
                         db: Session = Depends(get_db)):
    obj = ConfigObject(name=body.name.strip() or "Config", kind=body.kind,
                       encrypted_values=credstore.encrypt("platform", body.values or {}))
    db.add(obj)
    db.commit()
    db.refresh(obj)
    platform_config.invalidate()
    audit.record(db, actor=principal.user_id, action="admin.config_object_created",
                 category="admin", detail={"name": obj.name, "kind": obj.kind})
    return _config_object_view(obj)


class ConfigObjectUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    values: dict | None = None  # merged; blank secret values preserve the existing


@router.put("/config-objects/{oid}")
def update_config_object(oid: str, body: ConfigObjectUpdate,
                         principal: security.Principal = Depends(security.require_platform_admin),
                         db: Session = Depends(get_db)):
    from .. import emailer
    obj = db.get(ConfigObject, oid)
    if not obj:
        raise HTTPException(404, "config object not found")
    if body.name is not None:
        obj.name = body.name.strip()
    if body.kind is not None:
        obj.kind = body.kind
    if body.values is not None:
        cur = _decrypt_values(obj)
        for k, v in body.values.items():
            if _is_secret(k) and (v is None or v == ""):
                continue  # keep existing secret when left blank
            cur[k] = v
        obj.encrypted_values = credstore.encrypt("platform", cur)
    db.commit()
    platform_config.invalidate()
    emailer.invalidate_config_cache()
    audit.record(db, actor=principal.user_id, action="admin.config_object_updated",
                 category="admin", detail={"name": obj.name})
    return _config_object_view(obj)


@router.delete("/config-objects/{oid}")
def delete_config_object(oid: str,
                         principal: security.Principal = Depends(security.require_platform_admin),
                         db: Session = Depends(get_db)):
    obj = db.get(ConfigObject, oid)
    if not obj:
        raise HTTPException(404, "config object not found")
    for sc in db.query(SourceConfig).filter(SourceConfig.config_object_id == oid).all():
        sc.config_object_id = None
    db.delete(obj)
    db.commit()
    platform_config.invalidate()
    audit.record(db, actor=principal.user_id, action="admin.config_object_deleted",
                 category="admin", severity="warning", detail={"name": obj.name})
    return {"ok": True}


def _source_slots() -> list[dict]:
    """Every platform integration that can link to a Config Object."""
    from ..connectors import get_connector, oauth
    slots: list[dict] = []
    for ct in sorted(oauth.OAUTH_TYPES):
        conn = get_connector(ct)
        label = conn.oauth_spec().display_name if conn else ct
        slots.append({"type": ct, "label": label, "kind": "oauth",
                      "keys": ["client_id", "client_secret"],
                      "required": ["client_id", "client_secret"]})
    slots.append({"type": "ses", "label": "Amazon SES (email delivery)", "kind": "ses",
                  "keys": ["aws_access_key_id", "aws_secret_access_key", "region", "from_email"],
                  "required": ["aws_access_key_id", "aws_secret_access_key"]})
    return slots


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    rows = {sc.connector_type: sc for sc in db.query(SourceConfig).all()}
    out = []
    for slot in _source_slots():
        sc = rows.get(slot["type"])
        vals = _decrypt_values(db.get(ConfigObject, sc.config_object_id)) if (sc and sc.config_object_id) else {}
        out.append({
            "type": slot["type"], "label": slot["label"], "kind": slot["kind"],
            "keys": slot["keys"],
            "enabled": True if sc is None else bool(sc.enabled),
            "config_object_id": sc.config_object_id if sc else None,
            "configured": all(vals.get(k) for k in slot["required"]),
        })
    return out


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    config_object_id: str | None = None


@router.put("/sources/{ctype}")
def update_source(ctype: str, body: SourceUpdate,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    from .. import emailer
    valid = {s["type"] for s in _source_slots()}
    if ctype not in valid:
        raise HTTPException(404, "unknown source")
    sc = db.get(SourceConfig, ctype)
    if sc is None:
        sc = SourceConfig(connector_type=ctype)
        db.add(sc)
    if body.enabled is not None:
        sc.enabled = body.enabled
    if body.config_object_id is not None:
        sc.config_object_id = body.config_object_id or None
    db.commit()
    platform_config.invalidate()
    emailer.invalidate_config_cache()
    audit.record(db, actor=principal.user_id, action="admin.source_updated",
                 category="admin", detail={"source": ctype, "enabled": sc.enabled})
    return {"ok": True, "enabled": sc.enabled, "config_object_id": sc.config_object_id}


# =========================================================================== #
# Service objects (storage + email backends, selectable per node)             #
# =========================================================================== #

# kind -> {label, category, credential_keys (live in the linked ConfigObject),
# settings (non-secret routing on the service object), required (for the status
# pill)}. Storage tiers default to low-cost online tiers so restore stays instant.
_SERVICE_KINDS: dict = {
    "storage-s3": {
        "label": "Amazon S3 storage",
        "category": "storage",
        "credential_keys": ["aws_access_key_id", "aws_secret_access_key"],
        "settings": ["bucket", "region", "storage_class", "endpoint_url"],
        "setting_defaults": {"region": "us-east-1", "storage_class": "INTELLIGENT_TIERING"},
        "required": ["bucket"],
    },
    "storage-azure": {
        "label": "Azure Blob storage",
        "category": "storage",
        "credential_keys": ["connection_string", "account_name", "account_key"],
        "settings": ["container", "access_tier", "account_url"],
        "setting_defaults": {"access_tier": "Cool"},
        "required": ["container"],
    },
    "email-ses": {
        "label": "Amazon SES email",
        "category": "email",
        "credential_keys": ["aws_access_key_id", "aws_secret_access_key"],
        "settings": ["from_email", "from_name", "reply_to", "region"],
        "setting_defaults": {"region": "us-east-1"},
        "required": ["from_email"],
    },
}


@router.get("/service-object-kinds")
def list_service_object_kinds():
    return [{"kind": k, **v} for k, v in _SERVICE_KINDS.items()]


def _service_merged(db: Session, svc: ServiceObject) -> dict:
    vals = _decrypt_values(db.get(ConfigObject, svc.config_object_id)) if svc.config_object_id else {}
    return {**vals, **(svc.settings or {})}


def _service_view(db: Session, svc: ServiceObject) -> dict:
    spec = _SERVICE_KINDS.get(svc.kind, {})
    merged = _service_merged(db, svc)
    required = spec.get("required", [])
    configured = all(merged.get(k) for k in required)
    return {
        "id": svc.id, "name": svc.name, "kind": svc.kind,
        "kind_label": spec.get("label", svc.kind),
        "category": spec.get("category", "storage" if svc.kind.startswith("storage-") else "email"),
        "enabled": bool(svc.enabled),
        "config_object_id": svc.config_object_id,
        "settings": svc.settings or {},
        "setting_keys": spec.get("settings", []),
        "credential_keys": spec.get("credential_keys", []),
        "configured": configured,
        "updated_at": svc.updated_at.isoformat() if svc.updated_at else None,
    }


@router.get("/service-objects")
def list_service_objects(db: Session = Depends(get_db)):
    return [_service_view(db, s) for s in
            db.query(ServiceObject).order_by(ServiceObject.name.asc()).all()]


class ServiceObjectBody(BaseModel):
    name: str
    kind: str
    enabled: bool = True
    config_object_id: str | None = None
    settings: dict = {}


class ServiceTest(BaseModel):
    to: str | None = None  # recipient for an email-service test send


@router.post("/service-objects")
def create_service_object(body: ServiceObjectBody,
                          principal: security.Principal = Depends(security.require_platform_admin),
                          db: Session = Depends(get_db)):
    if body.kind not in _SERVICE_KINDS:
        raise HTTPException(400, "unknown service kind")
    svc = ServiceObject(name=body.name.strip() or "Service", kind=body.kind,
                        enabled=body.enabled, config_object_id=body.config_object_id or None,
                        settings=body.settings or {})
    db.add(svc)
    db.commit()
    db.refresh(svc)
    services.invalidate()
    audit.record(db, actor=principal.user_id, action="admin.service_object_created",
                 category="admin", detail={"name": svc.name, "kind": svc.kind})
    return _service_view(db, svc)


class ServiceObjectUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    config_object_id: str | None = None
    settings: dict | None = None


@router.put("/service-objects/{sid}")
def update_service_object(sid: str, body: ServiceObjectUpdate,
                          principal: security.Principal = Depends(security.require_platform_admin),
                          db: Session = Depends(get_db)):
    from .. import emailer
    svc = db.get(ServiceObject, sid)
    if not svc:
        raise HTTPException(404, "service object not found")
    if body.name is not None:
        svc.name = body.name.strip()
    if body.enabled is not None:
        svc.enabled = body.enabled
    if body.config_object_id is not None:
        svc.config_object_id = body.config_object_id or None
    if body.settings is not None:
        svc.settings = body.settings
    db.commit()
    services.invalidate()
    emailer.invalidate_config_cache()
    audit.record(db, actor=principal.user_id, action="admin.service_object_updated",
                 category="admin", detail={"name": svc.name, "kind": svc.kind})
    return _service_view(db, svc)


@router.delete("/service-objects/{sid}")
def delete_service_object(sid: str,
                          principal: security.Principal = Depends(security.require_platform_admin),
                          db: Session = Depends(get_db)):
    svc = db.get(ServiceObject, sid)
    if not svc:
        raise HTTPException(404, "service object not found")
    # Unlink from any node that selected it, so the node falls back to defaults.
    for n in db.query(Node).all():
        if n.storage_service_id == sid:
            n.storage_service_id = None
        if n.email_service_id == sid:
            n.email_service_id = None
    db.delete(svc)
    db.commit()
    services.invalidate()
    audit.record(db, actor=principal.user_id, action="admin.service_object_deleted",
                 category="admin", severity="warning", detail={"name": svc.name})
    return {"ok": True}


@router.post("/service-objects/{sid}/test")
def test_service_object(sid: str, body: "ServiceTest | None" = None,
                        principal: security.Principal = Depends(security.require_platform_admin),
                        db: Session = Depends(get_db)):
    svc = db.get(ServiceObject, sid)
    if not svc:
        raise HTTPException(404, "service object not found")
    cfg = _service_merged(db, svc)
    if svc.kind.startswith("storage-"):
        from ..storage import destination_from_service
        try:
            dest = destination_from_service(svc.kind, cfg)
            if dest is None:
                return {"ok": False, "error": "missing required settings (bucket/container)"}
            probe = f"healthcheck/{secrets.token_hex(8)}"
            dest.put_object("_platform", probe, b"arkive-storage-check", immutable=False)
            data = dest.get_object("_platform", probe)
            ok = data == b"arkive-storage-check"
            return {"ok": ok, "error": None if ok else "readback mismatch"}
        except Exception as exc:  # surface the backend error to the admin
            return {"ok": False, "error": str(exc)}
    if svc.kind == "email-ses":
        from .. import emailer
        to = ((body.to if body else None) or "").strip()
        if not to:
            return {"ok": False, "error": "enter a recipient email address"}
        from_email = (cfg.get("from_email") or "").strip()
        if not from_email:
            return {"ok": False, "error": "set a From email on this service object"}
        # Test THIS service object's own config (not the node-resolved sender).
        send_cfg = {
            "region": cfg.get("region") or get_settings().s3_region,
            "from_email": from_email,
            "from_name": cfg.get("from_name") or "Arkive",
            "reply_to": cfg.get("reply_to") or "",
            "aws_access_key_id": (cfg.get("aws_access_key_id") or "").strip(),
            "aws_secret": (cfg.get("aws_secret_access_key") or "").strip(),
        }
        html = emailer.render(
            "Test email from Arkive",
            emailer.text_to_html(f"This confirms the '{svc.name}' email service object is "
                                 "configured correctly and can deliver mail."),
            preheader="Arkive email service test")
        try:
            emailer._send_ses(send_cfg, to, "Test email from Arkive", html,
                              "Arkive email service test — this service object is configured correctly.")
            result = {"ok": True, "error": None}
        except Exception as exc:  # surface the SES error to the admin
            result = {"ok": False, "error": str(exc)}
        audit.record(db, actor=principal.user_id, action="admin.service_object_test",
                     category="admin", detail={"name": svc.name, "to": to,
                                               "ok": result["ok"], "error": result["error"]})
        return result
    return {"ok": False, "error": "test not supported for this service kind"}


@router.get("/storage-usage")
def storage_usage(db: Session = Depends(get_db)):
    """How much data Arkive Cloud (cv-cloud) holds, broken down by tenant, plus
    the storage service objects and the nodes that write to them."""
    tenants = {t.id: t for t in db.query(Tenant).all()}
    by_tenant: dict = {}
    total_bytes = total_objects = total_points = 0
    for r in db.query(SnapshotReceipt).filter(SnapshotReceipt.destination == "cv-cloud").all():
        b = r.total_bytes or 0
        total_bytes += b
        total_objects += r.object_count or 0
        total_points += 1
        agg = by_tenant.setdefault(r.tenant_id, {"bytes": 0, "objects": 0, "points": 0})
        agg["bytes"] += b
        agg["objects"] += r.object_count or 0
        agg["points"] += 1

    tenant_rows = []
    for tid, agg in by_tenant.items():
        t = tenants.get(tid)
        tenant_rows.append({
            "tenant_id": tid,
            "tenant_name": t.name if t else "(unknown)",
            "plan": t.plan if t else "",
            "licensed_bytes": (t.licensed_bytes or 0) if t else 0,
            "bytes": agg["bytes"], "objects": agg["objects"],
            "recovery_points": agg["points"],
        })
    tenant_rows.sort(key=lambda x: x["bytes"], reverse=True)

    svc_nodes: dict = {}
    for n in db.query(Node).all():
        if n.storage_service_id:
            svc_nodes.setdefault(n.storage_service_id, []).append(n.name)
    services = []
    for s in db.query(ServiceObject).filter(ServiceObject.kind.like("storage-%")).all():
        services.append({
            "id": s.id, "name": s.name, "kind": s.kind,
            "kind_label": _SERVICE_KINDS.get(s.kind, {}).get("label", s.kind),
            "enabled": bool(s.enabled),
            "nodes": svc_nodes.get(s.id, []),
            "active": s.id in svc_nodes,
            "settings": s.settings or {},
        })

    return {
        "cloud_total": {"bytes": total_bytes, "objects": total_objects,
                        "recovery_points": total_points, "tenants": len(tenant_rows)},
        "by_tenant": tenant_rows,
        "services": services,
    }
