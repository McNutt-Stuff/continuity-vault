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

from .. import audit, authcodes, credstore, platform_config, security, services
from ..config import get_settings
from ..db import get_db
from ..models import (
    Appliance,
    AuditEvent,
    BackupRun,
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
def list_all_users(q: str = "", tenant_id: str = "", plan: str = "",
                   status: str = "", tenant_type: str = "",
                   db: Session = Depends(get_db)):
    """Global directory of every account across all tenants — filterable and
    searchable, with the plan, tenant, last-login, usage and billing each
    account rolls up to. Also backs the broadcast-recipient chooser."""
    from .billing import get_pricing, effective_plan
    pricing = get_pricing(db)
    tenants = {t.id: t for t in db.query(Tenant).all()}
    query = db.query(User)
    if tenant_id:
        query = query.filter(User.tenant_id == tenant_id)
    if status:
        query = query.filter(User.status == status)
    needle = (q or "").strip().lower()
    out = []
    for u in query.order_by(User.email.asc()).all():
        t = tenants.get(u.tenant_id)
        ttype = (t.tenant_type if t else "dedicated") or "dedicated"
        if tenant_type and ttype != tenant_type:
            continue
        plan_id = "personal" if ttype == "shared" else ((t.plan if t else None) or None)
        pl = effective_plan(pricing, plan_id)
        if plan and (pl.get("id") or "") != plan:
            continue
        if needle:
            hay = " ".join([u.email or "", u.full_name or "", u.display_name or "",
                            u.first_name or "", u.last_name or "", u.phone or "",
                            (t.name if t else "")]).lower()
            if needle not in hay:
                continue
        billing = _user_billing(db, u, t, pricing) if t else None
        out.append({
            "id": u.id, "email": u.email, "display_name": u.display_name,
            "full_name": u.full_name, "first_name": u.first_name or "",
            "last_name": u.last_name or "", "phone": u.phone or "",
            "role": u.role, "status": u.status,
            "is_platform_admin": bool(u.is_platform_admin),
            "email_verified": bool(u.email_verified),
            "feature_flags": u.feature_flags or {},
            "tenant_id": u.tenant_id, "tenant_name": (t.name if t else ""),
            "tenant_type": ttype,
            "plan": {"id": pl.get("id"), "name": pl.get("name")},
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "usage_bytes": (billing["used_bytes"] if billing else 0),
            "billing_monthly": (billing["total_monthly"] if billing else 0),
            "currency": pricing.currency,
        })
    return out


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


def _user_billing(db: Session, u: User, tenant: Tenant, pricing=None) -> dict:
    """Per-account cost via the ONE canonical pricing calculation (billing.user_plan)
    so admin numbers match the customer's Protection Setup exactly. Flattens the
    canonical breakdown into the compact shape the user tables consume."""
    from .billing import user_plan
    up = user_plan(db, u, tenant)
    c = up["costs"]
    return {
        "plan": up["license_plan"],
        "used_bytes": up["used_bytes"], "used_tb": up["used_tb"],
        "objects": up["objects_total"],
        "billable_tb": up["billable_tb"],
        "protection_monthly": c["protection_monthly"],
        "cloud_storage_monthly": c["cloud_storage_monthly"],
        "customer_storage_monthly": c["third_party_estimate_monthly"],
        "appliance_monthly": c["appliance_monthly"],
        "total_monthly": c["total_monthly"],
        "currency": up["currency"],
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
        "feature_flags": t.feature_flags or {},
        "created_at": t.created_at.isoformat() if t.created_at else None,
        **_tenant_counts(db, t.id),
    }
    n = db.get(Node, t.node_id) if t.node_id else None
    v["node"] = ({"id": n.id, "name": n.name, "role": n.role,
                  "endpoint": n.endpoint, "status": n.status} if n else None)
    if detail:
        from .billing import _compute_plan, get_pricing
        pricing = get_pricing(db)
        shared = (t.tenant_type or "dedicated") == "shared"
        members = []
        for u in db.query(User).filter(User.tenant_id == t.id).order_by(User.email.asc()).all():
            mv = _user_view(u)
            if shared:
                mv["billing"] = _user_billing(db, u, t, pricing)
            members.append(mv)
        v["members"] = members
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
    # Create the owner first so the primary vault can belong to them.
    owner = None
    if body.owner_email:
        email = body.owner_email.strip().lower()
        if not db.query(User).filter(func.lower(User.email) == email).first():
            owner = User(tenant_id=tenant.id, email=email,
                         display_name=(body.owner_name or email).strip(),
                         role="owner", status="active")
            db.add(owner)
            db.flush()
    from .tenant import provision_vault
    provision_vault(db, tenant=tenant, owner_user_id=(owner.id if owner else None),
                    name="Primary Vault", key_ownership_model=body.key_ownership_model)
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


@router.get("/feature-flags")
def feature_flag_catalog():
    from .. import features
    return [{"name": k, "label": features.LABELS.get(k, k), "default": v}
            for k, v in features.FLAGS.items()]


class FlagsUpdate(BaseModel):
    feature_flags: dict  # {flag: true|false|null(=unset/inherit)}


def _merge_flags(current: dict | None, incoming: dict) -> dict:
    from .. import features
    ff = dict(current or {})
    for k, v in (incoming or {}).items():
        if k not in features.FLAGS:
            continue
        if v is None:
            ff.pop(k, None)  # revert to tenant/default
        else:
            ff[k] = bool(v)
    return ff


@router.put("/tenants/{tid}/flags")
def set_tenant_flags(tid: str, body: FlagsUpdate,
                     principal: security.Principal = Depends(security.require_platform_admin),
                     db: Session = Depends(get_db)):
    t = db.get(Tenant, tid)
    if not t:
        raise HTTPException(404, "tenant not found")
    t.feature_flags = _merge_flags(t.feature_flags, body.feature_flags)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.tenant_flags_updated",
                 tenant_id=t.id, category="security", severity="notice",
                 detail={"flags": t.feature_flags})
    return {"feature_flags": t.feature_flags}


@router.put("/users/{uid}/flags")
def set_user_flags(uid: str, body: FlagsUpdate,
                   principal: security.Principal = Depends(security.require_platform_admin),
                   db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "user not found")
    u.feature_flags = _merge_flags(u.feature_flags, body.feature_flags)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.user_flags_updated",
                 tenant_id=u.tenant_id, category="security", severity="notice",
                 detail={"flags": u.feature_flags, "email": u.email})
    return {"feature_flags": u.feature_flags}


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
            "full_name": u.full_name,
            "first_name": u.first_name or "", "last_name": u.last_name or "",
            "phone": u.phone or "",
            "role": u.role, "status": u.status,
            "is_platform_admin": bool(u.is_platform_admin),
            "email_verified": bool(u.email_verified),
            "tenant_id": u.tenant_id,
            "feature_flags": u.feature_flags or {},
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat() if u.created_at else None}


class UserCreate(BaseModel):
    email: str
    display_name: str = ""
    first_name: str = ""
    last_name: str = ""
    phone: str = ""
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
    if not email:
        raise HTTPException(400, "email is required")
    # One account per email address, platform-wide (case-insensitive).
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "a user with this email already exists")
    first = (body.first_name or "").strip()
    last = (body.last_name or "").strip()
    display = ((body.display_name or "").strip()
               or " ".join(p for p in [first, last] if p).strip()
               or email)
    shared = (t.tenant_type or "dedicated") == "shared"
    # Shared tenants hold isolated 1:1 personal accounts — no roles.
    role = "member" if shared else (body.role or "member")
    u = User(tenant_id=tid, email=email, display_name=display,
             first_name=first, last_name=last, phone=(body.phone or "").strip(),
             role=role, status="active")
    db.add(u)
    db.flush()
    # Every account gets its own encrypted vault so it can store data immediately.
    from .tenant import provision_vault
    vname = f"{(first or display).split()[0]}'s Vault" if (first or display) else "My Vault"
    provision_vault(db, tenant=t, owner_user_id=u.id, name=vname)
    db.commit()
    db.refresh(u)
    invited = None
    if body.send_invite:
        invited = _send_welcome_email(db, u, t)
    audit.record(db, actor=principal.user_id, action="admin.user_created",
                 tenant_id=tid, category="admin", detail={"email": email, "role": role})
    return {**_user_view(u), "invite": invited}


class UserUpdate(BaseModel):
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
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
    if body.first_name is not None:
        u.first_name = body.first_name.strip()
    if body.last_name is not None:
        u.last_name = body.last_name.strip()
    if body.phone is not None:
        u.phone = body.phone.strip()
    if body.display_name is not None:
        u.display_name = body.display_name.strip()
    elif body.first_name is not None or body.last_name is not None:
        derived = " ".join(p for p in [u.first_name, u.last_name] if p).strip()
        if derived:
            u.display_name = derived
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


def _send_welcome_email(db: Session, u: User, t: Tenant) -> dict:
    """Welcome a brand-new account: greet them, explain Arkive, and hand them a
    one-time code with clear sign-in instructions. Uses the branded template."""
    from .. import emailer
    try:
        code = authcodes.issue_code(u.email, "login")
    except Exception:
        code = None
    origin = get_settings().rp_origin
    name = (u.first_name or u.display_name or "there").strip()
    lines = [
        f"Hi {name},",
        "",
        f"Welcome to Arkive — your account on {t.name} is ready to go.",
        "Arkive keeps a secure, searchable, quantum-safe copy of the data that "
        "matters most to you, so it's always recoverable.",
        "",
        "Here's how to sign in for the first time:",
        f"  1. Open {origin}",
        f"  2. Enter your email address: {u.email}",
    ]
    if code:
        lines.append(f"  3. Enter this one-time sign-in code: {code}")
    else:
        lines.append("  3. Request a sign-in code and follow the emailed instructions.")
    lines += [
        "",
        "On first sign-in you'll register this device with a passkey, so every "
        "future login is a single secure tap — no passwords to remember.",
        "",
        "Welcome aboard,",
        "The Arkive team",
    ]
    body = "\n".join(lines)
    subject = "Welcome to Arkive"
    channel = emailer.send(
        u.email, subject,
        html=emailer.render(subject, emailer.text_to_html(body),
                            preheader="Your Arkive account is ready — here's how to sign in.",
                            cta={"label": "Sign in to Arkive", "url": origin}),
        text=body)
    out = {"sent": channel in ("ses", "smtp", "log"), "channel": channel}
    if code and get_settings().environment == "development":
        out["dev_code"] = code
    return out


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
    from .billing import _compute_plan, get_pricing
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
        counts = _tenant_counts(db, t.id)
        # SINGLE SOURCE OF TRUTH: same calculation as the customer's Protection
        # Setup and the tenant detail view (base plan + selections + usage).
        bp = _compute_plan(db, t)
        objects = bp["objects_total"]
        used_bytes = bp["used_bytes"]
        monthly = bp["costs"]["total_monthly"]
        cloud_bytes = int(cloud_by_tenant.get(t.id, 0) or 0)
        rows.append({
            "id": t.id, "name": t.name, "plan": t.plan, "status": t.status,
            "users": counts["users"], "appliances": counts["appliances"],
            "agents": counts["agents"], "sources": counts["sources"],
            "objects": objects, "used_bytes": used_bytes,
            "cloud_bytes": cloud_bytes,
            "licensed_bytes": int(t.licensed_bytes or 0),
            "recovery_points": counts["recovery_points"],
            "monthly_cost": monthly, "options": bp["options"],
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
    try:
        from .. import sysinfo
        health.update(sysinfo.snapshot())
    except Exception:
        path = os.environ.get("CV_OBJECT_STORE") or "/var/lib/continuity-vault"
        try:
            du = shutil.disk_usage(path if os.path.exists(path) else "/")
            health["storage"] = {"total": du.total, "used": du.used, "free": du.free,
                                 "pct": round(du.used / du.total * 100, 1) if du.total else 0}
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


_NODE_CATEGORY = {"control-plane": "Control Plane", "customer-tenant": "Customer Nodes",
                  "public-web": "Public Web"}


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

    mem = tel.get("memory") or {}
    stg = tel.get("storage") or {}
    tenant_count = db.query(func.count(Tenant.id)).filter(Tenant.node_id == n.id).scalar()
    backup_ids = list(n.backup_service_ids or [])
    return {
        "id": n.id, "name": n.name, "region": n.region, "role": n.role,
        "category": _NODE_CATEGORY.get(n.role, "Other"),
        "endpoint": n.endpoint, "status": n.status, "is_self": bool(n.is_self),
        "version": n.version, "online": online, "telemetry": tel,
        "cloud": n.cloud or {},
        "health": {
            "cpu_pct": tel.get("cpu_pct"),
            "mem_pct": mem.get("pct"),
            "disk_pct": stg.get("pct"),
        },
        "cpus": tel.get("cpus"),
        "uptime_seconds": tel.get("uptime_seconds"),
        "tenants": int(tenant_count or 0),
        "storage_service_id": n.storage_service_id,
        "email_service_id": n.email_service_id,
        "storage_service": _svc_name(n.storage_service_id),
        "email_service": _svc_name(n.email_service_id),
        "backup_service_ids": backup_ids,
        "backup_services": [nm for nm in (_svc_name(s) for s in backup_ids) if nm],
        "last_heartbeat_at": n.last_heartbeat_at.isoformat() if n.last_heartbeat_at else None,
    }


# --------------------------------------------------------------------------- #
# Worker processes — background backup/sync jobs (view + kill)                 #
# --------------------------------------------------------------------------- #

def _job_view(j, tenants, colls, accounts, nodes, users=None) -> dict:
    c = colls.get(j.collection_id)
    label = "—"
    username = None
    owner = None
    if c is not None:
        acc = accounts.get(c.connector_account_id) if c.connector_account_id else None
        label = acc.account_label if acc else c.name
        username = acc.account_username if acc else None
        if acc and acc.owner_user_id and users:
            u = users.get(acc.owner_user_id)
            owner = (u.email or u.full_name) if u else None
    return {
        "id": j.id, "tenant_id": j.tenant_id, "tenant": tenants.get(j.tenant_id) or "—",
        "owner": owner,
        "collection_id": j.collection_id, "source": label, "source_username": username,
        "source_type": c.source_type if c else None,
        "kind": j.kind, "status": j.status, "trigger": j.trigger or "manual",
        "node_id": j.node_id,
        "node": nodes.get(j.node_id) if j.node_id else "Control plane",
        "processed": j.processed or 0, "total": j.total or 0,
        "message": j.message or "", "error": j.error or "",
        "has_log": bool(j.log),
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
    uids = {a.owner_user_id for a in accounts.values() if a.owner_user_id}
    users = ({u.id: u for u in db.query(User).filter(User.id.in_(uids)).all()} if uids else {})
    nodes = {n.id: n.name for n in db.query(Node).all()}
    active_n = db.query(func.count(SyncJob.id)).filter(SyncJob.status.in_(_ACTIVE)).scalar()
    return {"active": int(active_n or 0),
            "jobs": [_job_view(j, tenants, colls, accounts, nodes, users) for j in jobs]}


@router.get("/jobs/{job_id}/log")
def job_log(job_id: str, db: Session = Depends(get_db)):
    """Full verbose process log for one job (success or failure), including any
    log shipped up from a customer node."""
    j = db.get(SyncJob, job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return {"id": j.id, "status": j.status, "error": j.error or "",
            "message": j.message or "", "trigger": j.trigger or "manual",
            "log": j.log or []}


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
    backup_service_ids: list[str] | None = None


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
    # De-dupe backup destinations (resiliency needs DIFFERENT services).
    if body.backup_service_ids is not None:
        seen: list[str] = []
        for sid in body.backup_service_ids:
            if sid and sid not in seen:
                seen.append(sid)
        n.backup_service_ids = seen
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
# Node telemetry drill-down: live metrics, history, logs, keys, controls,     #
# per-tenant usage. Self node runs locally; remote nodes are fleet-proxied.   #
# --------------------------------------------------------------------------- #

def _node_call(node: Node, path: str, method: str = "GET",
               params: dict | None = None, json: dict | None = None, timeout: float = 12):
    import httpx
    from .site import _fleet_secret
    url = (node.endpoint or "").rstrip("/") + path
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, url, params=params, json=json,
                      headers={"Authorization": f"Bearer {_fleet_secret()}"})
        r.raise_for_status()
        return r.json()


def _remote_capable(n: Node) -> bool:
    return n.role == "customer-tenant" and bool(n.endpoint)


def _node_live(db: Session, n: Node) -> dict:
    if n.is_self:
        from .. import sysinfo
        from .node_sync import _db_stats
        out = sysinfo.live(cert_host=get_settings().domain)
        out["db"] = _db_stats(db)
        return out
    if _remote_capable(n):
        try:
            return _node_call(n, "/nodes/sync/live")
        except Exception:  # noqa: BLE001
            pass
    # public-web / unreachable → last heartbeat snapshot only.
    tel = dict(n.telemetry or {})
    tel["processes"] = tel.get("processes", [])
    tel["services"] = tel.get("services", [])
    tel["source"] = "heartbeat"
    return tel


@router.get("/nodes/{nid}")
def node_detail(nid: str, db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    if n.is_self:
        _ensure_self_node(db)
    return _node_view(db, n)


@router.get("/nodes/{nid}/live")
def node_live_metrics(nid: str, db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    return _node_live(db, n)


@router.get("/nodes/{nid}/history")
def node_history(nid: str, window: str = "24h", db: Session = Depends(get_db)):
    from datetime import timedelta
    from ..models import NodeMetric
    hours = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720, "90d": 2160}.get(window, 24)
    since = _now() - timedelta(hours=hours)
    rows = (db.query(NodeMetric)
            .filter(NodeMetric.node_id == nid, NodeMetric.ts >= since)
            .order_by(NodeMetric.ts.asc()).all())
    step = max(1, len(rows) // 240)  # cap the payload at ~240 points
    series = []
    for i in range(0, len(rows), step):
        r = rows[i]
        series.append({"ts": r.ts.isoformat(), "cpu": r.cpu_pct, "mem": r.mem_pct,
                       "disk": r.disk_pct, "net_sent": r.net_sent_rate,
                       "net_recv": r.net_recv_rate, "load": r.load1})
    return {"window": window, "points": len(series), "series": series}


@router.get("/nodes/{nid}/logs")
def node_logs(nid: str, source: str = "app", lines: int = 200, db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    if n.is_self:
        from .. import sysinfo
        return {"source": source, "lines": sysinfo.logs(source, lines)}
    if _remote_capable(n):
        try:
            return _node_call(n, "/nodes/sync/logs", params={"source": source, "lines": lines})
        except Exception:  # noqa: BLE001
            raise HTTPException(502, "node unreachable")
    raise HTTPException(400, "logs are not available for this node type")


@router.get("/nodes/{nid}/keys")
def node_keys(nid: str, db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    if n.is_self:
        from .. import sysinfo
        from .node_sync import keys_report
        out = keys_report(db)
        out["certificate"] = sysinfo.cert_info(get_settings().domain)
        return out
    if _remote_capable(n):
        try:
            return _node_call(n, "/nodes/sync/keys")
        except Exception:  # noqa: BLE001
            pass
    return {"vault_keys": {"total": 0, "provisioned": 0}, "certificate": {"reachable": False}}


class NodeControl(BaseModel):
    action: str
    unit: str = ""


@router.post("/nodes/{nid}/control")
def node_control(nid: str, body: NodeControl,
                 principal: security.Principal = Depends(security.require_platform_admin),
                 db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    audit.record(db, actor=principal.user_id, action="admin.node_control",
                 category="admin", severity="warning",
                 detail={"node": n.name, "action": body.action, "unit": body.unit})
    if n.is_self:
        from .. import sysinfo
        return sysinfo.control(body.action, body.unit,
                               get_settings().node_role or "control-plane")
    if _remote_capable(n):
        try:
            return _node_call(n, "/nodes/sync/control", method="POST",
                              json={"action": body.action, "unit": body.unit})
        except Exception:  # noqa: BLE001
            raise HTTPException(502, "node unreachable")
    raise HTTPException(400, "controls are not available for this node type")


@router.post("/nodes/{nid}/backup")
def node_backup_now(nid: str,
                    principal: security.Principal = Depends(security.require_platform_admin),
                    db: Session = Depends(get_db)):
    """Trigger an infrastructure backup of a specific node now. The control-plane
    node runs it in-process; a remote node starts its own ``cv-backup.service``."""
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    audit.record(db, actor=principal.user_id, action="admin.backup_triggered",
                 category="admin", detail={"node": n.name})
    if n.is_self:
        import threading
        from ..backup_service import run_backup_once

        def _go():
            from ..db import WorkerSessionLocal
            try:
                with WorkerSessionLocal() as wdb:
                    run_backup_once(wdb)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("cv.admin").exception("manual node backup failed")

        threading.Thread(target=_go, name="cv-backup-manual", daemon=True).start()
        return {"ok": True, "message": f"Backup started on {n.name}"}
    if _remote_capable(n):
        try:
            res = _node_call(n, "/nodes/sync/control", method="POST",
                             json={"action": "start", "unit": "cv-backup.service"})
        except Exception:  # noqa: BLE001
            raise HTTPException(502, "node unreachable")
        ok = bool(res.get("ok"))
        return {"ok": ok, "message": (f"Backup started on {n.name}" if ok
                                      else res.get("error") or "could not start backup")}
    raise HTTPException(400, "backups can't be triggered remotely for this node type")


@router.get("/nodes/{nid}/tenants")
def node_tenants(nid: str, db: Session = Depends(get_db)):
    """Per-tenant usage on a node — to spot the heaviest tenants (rebalancing)."""
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    tenants = db.query(Tenant).filter(Tenant.node_id == nid).all()
    # The control-plane node also serves every tenant not pinned to a node.
    if n.is_self and n.role == "control-plane":
        tenants = tenants + db.query(Tenant).filter(Tenant.node_id.is_(None)).all()
    rows = []
    for t in tenants:
        objects = db.query(func.count(distinct(SearchDocument.object_id))).filter(
            SearchDocument.tenant_id == t.id).scalar() or 0
        used = db.query(func.coalesce(func.sum(SnapshotReceipt.total_bytes), 0)).filter(
            SnapshotReceipt.tenant_id == t.id).scalar() or 0
        users = db.query(func.count(User.id)).filter(User.tenant_id == t.id).scalar() or 0
        rps = db.query(func.count(SnapshotReceipt.id)).filter(
            SnapshotReceipt.tenant_id == t.id).scalar() or 0
        last = db.query(func.max(SnapshotReceipt.created_at)).filter(
            SnapshotReceipt.tenant_id == t.id).scalar()
        rows.append({"id": t.id, "name": t.name, "tenant_type": t.tenant_type or "dedicated",
                     "objects": int(objects), "bytes": int(used), "users": int(users),
                     "recovery_points": int(rps),
                     "last_activity": last.isoformat() if last else None})
    total = sum(r["bytes"] for r in rows)
    for r in rows:
        r["share"] = round(r["bytes"] / total * 100, 1) if total else 0.0
        # Heavy = a disproportionate share of the node's footprint.
        r["heavy"] = bool(total and r["bytes"] and
                          r["share"] >= max(30.0, (100.0 / max(1, len(rows))) * 2.5))
    rows.sort(key=lambda r: -r["bytes"])
    return {"node": n.name, "total_bytes": total, "tenants": rows}


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
    """Every OAuth platform integration that can link to a Config Object, enriched
    with its brand icon/colour + default family/category for the admin Sources page.
    (Amazon SES is a Service Object, not a source, so it is not listed here.)"""
    from ..connectors import get_connector, oauth
    from .connectors import _SOURCE_FAMILY, _SOURCE_TYPE
    slots: list[dict] = []
    for ct in sorted(oauth.OAUTH_TYPES):
        conn = get_connector(ct)
        spec = conn.oauth_spec() if conn else None
        slots.append({
            "type": ct,
            "label": spec.display_name if spec else ct,
            "kind": "oauth",
            "icon": (spec.icon if spec else "link") or "link",
            "color": (spec.color if spec else "#6b7280") or "#6b7280",
            "family": _SOURCE_FAMILY.get(ct, "Other"),
            "category": _SOURCE_TYPE.get(ct, "Other"),
            "keys": ["client_id", "client_secret"],
            "required": ["client_id", "client_secret"],
        })
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
            "icon": slot["icon"], "color": slot["color"], "category": slot["category"],
            # Admin family override wins over the built-in default grouping.
            "family": (sc.family if sc and sc.family else slot["family"]),
            "keys": slot["keys"],
            "enabled": True if sc is None else bool(sc.enabled),
            "config_object_id": sc.config_object_id if sc else None,
            "configured": all(vals.get(k) for k in slot["required"]),
        })
    return out


class SourceUpdate(BaseModel):
    enabled: bool | None = None
    config_object_id: str | None = None
    family: str | None = None


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
    if body.family is not None:
        sc.family = body.family.strip() or None
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
        "capabilities": ["cloud", "backup"],
    },
    "storage-azure": {
        "label": "Azure Blob storage",
        "category": "storage",
        "credential_keys": ["connection_string", "account_name", "account_key"],
        "settings": ["container", "access_tier", "account_url"],
        "setting_defaults": {"access_tier": "Cool"},
        "required": ["container"],
        "capabilities": ["cloud", "backup"],
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
        "capabilities": svc.storage_capabilities(),
        "capability_options": spec.get("capabilities", []),
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
    capabilities: list[str] | None = None


class ServiceTest(BaseModel):
    to: str | None = None  # recipient for an email-service test send


def _clean_capabilities(kind: str, caps: list[str] | None) -> list[str]:
    """Restrict a storage service's 'used for' list to the kind's allowed options;
    non-storage kinds carry none. Empty means both (handled at read time)."""
    allowed = _SERVICE_KINDS.get(kind, {}).get("capabilities", [])
    if not allowed or caps is None:
        return [] if caps is None else [c for c in caps if c in allowed]
    return [c for c in allowed if c in caps]


@router.post("/service-objects")
def create_service_object(body: ServiceObjectBody,
                          principal: security.Principal = Depends(security.require_platform_admin),
                          db: Session = Depends(get_db)):
    if body.kind not in _SERVICE_KINDS:
        raise HTTPException(400, "unknown service kind")
    svc = ServiceObject(name=body.name.strip() or "Service", kind=body.kind,
                        enabled=body.enabled, config_object_id=body.config_object_id or None,
                        settings=body.settings or {},
                        capabilities=_clean_capabilities(body.kind, body.capabilities))
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
    capabilities: list[str] | None = None


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
    if body.capabilities is not None:
        svc.capabilities = _clean_capabilities(svc.kind, body.capabilities)
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


def _test_storage_service(db: Session, principal, svc: ServiceObject, cfg: dict) -> dict:
    """Diagnose a storage service object across three stages — configuration,
    reachability and writeability — returning a per-check breakdown so the admin
    sees exactly where an S3 / Azure backend fails."""
    from ..storage import destination_from_service

    spec = _SERVICE_KINDS.get(svc.kind, {})
    checks: list[dict] = []

    def _finish() -> dict:
        ok = all(c["ok"] for c in checks)
        error = None if ok else next((c["detail"] for c in checks if not c["ok"]), "test failed")
        audit.record(db, actor=principal.user_id, action="admin.service_object_test",
                     category="admin", detail={"name": svc.name, "kind": svc.kind,
                                               "ok": ok, "checks": checks})
        return {"ok": ok, "error": error, "checks": checks}

    # 1. Configuration — required routing settings and at least one credential present.
    missing = [k for k in spec.get("required", []) if not str(cfg.get(k) or "").strip()]
    cred_keys = spec.get("credential_keys", [])
    has_creds = any(str(cfg.get(k) or "").strip() for k in cred_keys)
    if missing:
        checks.append({"name": "configuration", "ok": False,
                       "detail": f"missing required setting(s): {', '.join(missing)}"})
        return _finish()
    if cred_keys and not has_creds:
        checks.append({"name": "configuration", "ok": False,
                       "detail": "no credentials set — link a configuration object providing "
                                 + " or ".join(cred_keys)})
        return _finish()
    checks.append({"name": "configuration", "ok": True,
                   "detail": "required settings and credentials are present"})

    # 2. Reachability — build the destination, which connects, authenticates and
    #    ensures the bucket/container exists (mirrors the first real write).
    try:
        dest = destination_from_service(svc.kind, cfg)
        if dest is None:
            checks.append({"name": "reachability", "ok": False,
                           "detail": "could not build a storage destination from this config"})
            return _finish()
    except Exception as exc:  # bad creds, wrong region, unreachable endpoint, …
        checks.append({"name": "reachability", "ok": False, "detail": str(exc)})
        return _finish()
    target = getattr(dest, "bucket", None) or getattr(dest, "container", None) or ""
    checks.append({"name": "reachability", "ok": True,
                   "detail": f"connected and reached {target}".strip()})

    # 3. Writeability — write, read back and remove a probe object.
    try:
        detail = dest.probe()
        checks.append({"name": "writeability", "ok": True, "detail": detail})
    except Exception as exc:  # surface the backend error (permissions, object-lock, …)
        checks.append({"name": "writeability", "ok": False, "detail": str(exc)})
    return _finish()


@router.post("/service-objects/{sid}/test")
def test_service_object(sid: str, body: "ServiceTest | None" = None,
                        principal: security.Principal = Depends(security.require_platform_admin),
                        db: Session = Depends(get_db)):
    svc = db.get(ServiceObject, sid)
    if not svc:
        raise HTTPException(404, "service object not found")
    cfg = _service_merged(db, svc)
    if svc.kind.startswith("storage-"):
        return _test_storage_service(db, principal, svc, cfg)
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
        if "cloud" not in s.storage_capabilities():
            continue
        services.append({
            "id": s.id, "name": s.name, "kind": s.kind,
            "kind_label": _SERVICE_KINDS.get(s.kind, {}).get("label", s.kind),
            "enabled": bool(s.enabled),
            "capabilities": s.storage_capabilities(),
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


# =========================================================================== #
# Infrastructure backups (node/CP core-state backups to storage services)     #
# =========================================================================== #

@router.get("/backups")
def backups_overview(db: Session = Depends(get_db)):
    """Fleet-wide infrastructure backup status: coverage, per-node last run,
    storage totals per backup service, schedules and recent runs."""
    _ensure_self_node(db)
    nodes = db.query(Node).all()
    svc_by_id = {s.id: s for s in db.query(ServiceObject).all()}

    # Latest run per node (rows arrive newest-first).
    runs = (db.query(BackupRun).order_by(BackupRun.created_at.desc()).limit(500).all())
    latest: dict = {}
    for r in runs:
        key = r.node_id or r.node_name
        if key not in latest:
            latest[key] = r

    def _run_row(r) -> dict:
        return {
            "id": r.id, "status": r.status,
            "total_bytes": r.total_bytes or 0,
            "components": r.components or [],
            "destinations": r.destinations or [],
            "message": r.message or "", "error": r.error or "",
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }

    node_rows = []
    protected = 0
    for n in nodes:
        r = latest.get(n.id)
        ids = list(n.backup_service_ids or [])
        if ids:
            protected += 1
        node_rows.append({
            "id": n.id, "name": n.name, "role": n.role,
            "category": _NODE_CATEGORY.get(n.role, "Other"),
            "is_self": bool(n.is_self),
            "backup_service_ids": ids,
            "backup_services": [svc_by_id[s].name for s in ids if s in svc_by_id],
            "last_backup": _run_row(r) if r else None,
        })
    node_rows.sort(key=lambda x: (not x["is_self"], x["name"] or ""))

    # Storage totals per backup service (latest successful run per node counts once).
    svc_totals: dict = {}
    for key, r in latest.items():
        if r.status not in ("success", "partial"):
            continue
        for d in (r.destinations or []):
            if d.get("status") != "ok":
                continue
            sid = d.get("service_id")
            agg = svc_totals.setdefault(sid, {"bytes": 0, "nodes": set()})
            agg["bytes"] += int(d.get("bytes") or 0)
            agg["nodes"].add(r.node_name)

    svc_used: dict = {}  # which nodes ASSIGN each service for backup
    for n in nodes:
        for sid in (n.backup_service_ids or []):
            svc_used.setdefault(sid, []).append(n.name)
    services = []
    for s in db.query(ServiceObject).filter(ServiceObject.kind.like("storage-%")).all():
        if "backup" not in s.storage_capabilities():
            continue
        tot = svc_totals.get(s.id, {"bytes": 0, "nodes": set()})
        services.append({
            "id": s.id, "name": s.name, "kind": s.kind,
            "kind_label": _SERVICE_KINDS.get(s.kind, {}).get("label", s.kind),
            "enabled": bool(s.enabled),
            "capabilities": s.storage_capabilities(),
            "settings": s.settings or {},
            "nodes": svc_used.get(s.id, []),
            "bytes": tot["bytes"], "backed_up_nodes": len(tot["nodes"]),
        })

    now = _now()
    ok24 = fail24 = 0
    for r in runs:
        if r.created_at and (now - r.created_at).total_seconds() < 86400:
            if r.status == "success":
                ok24 += 1
            elif r.status in ("failed", "partial"):
                fail24 += 1
    total_stored = sum(v["bytes"] for v in svc_totals.values())
    last_run = runs[0] if runs else None

    return {
        "summary": {
            "nodes_total": len(nodes),
            "nodes_protected": protected,
            "total_stored_bytes": total_stored,
            "success_24h": ok24, "failed_24h": fail24,
            "interval_minutes": get_settings().backup_interval_minutes,
            "last_run_at": last_run.created_at.isoformat() if last_run and last_run.created_at else None,
        },
        "nodes": node_rows,
        "services": services,
        "recent": [{
            "id": r.id, "node_name": r.node_name, "role": r.role, "status": r.status,
            "total_bytes": r.total_bytes or 0, "message": r.message or "", "error": r.error or "",
            "destinations": r.destinations or [], "components": r.components or [],
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        } for r in runs[:30]],
    }


@router.post("/backups/run")
def run_backup_now(principal: security.Principal = Depends(security.require_platform_admin),
                   db: Session = Depends(get_db)):
    """Trigger an infrastructure backup of the control plane now (runs in a
    background thread). Remote nodes back up on their own cv-backup timer."""
    import threading
    from ..backup_service import run_backup_once

    def _go():
        from ..db import WorkerSessionLocal
        try:
            with WorkerSessionLocal() as wdb:
                run_backup_once(wdb)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger("cv.admin").exception("manual infrastructure backup failed")

    threading.Thread(target=_go, name="cv-backup-manual", daemon=True).start()
    audit.record(db, actor=principal.user_id, action="admin.backup_triggered",
                 category="admin", detail={})
    return {"ok": True, "message": "Control-plane backup started"}
