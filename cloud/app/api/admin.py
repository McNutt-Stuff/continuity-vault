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

from .. import audit, authcodes, keybroker, security
from ..config import get_settings
from ..db import get_db
from ..models import (
    Appliance,
    AuditEvent,
    Collection,
    ConnectorAccount,
    DesktopAgent,
    Node,
    PricingConfig,
    SearchDocument,
    SnapshotReceipt,
    SoftwareRelease,
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
    out = []
    for t in db.query(Tenant).all():
        out.append({
            "id": t.id,
            "name": t.name,
            "plan": t.plan,
            "key_ownership_model": t.key_ownership_model,
            "status": t.status,
            "users": db.query(func.count(User.id)).filter(User.tenant_id == t.id).scalar(),
            "appliances": db.query(func.count(Appliance.id))
                .filter(Appliance.tenant_id == t.id).scalar(),
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


def _tenant_view(db: Session, t: Tenant, detail: bool = False) -> dict:
    v = {
        "id": t.id, "name": t.name, "plan": t.plan, "status": t.status,
        "key_ownership_model": t.key_ownership_model,
        "licensed_bytes": int(t.licensed_bytes or 0),
        "protection_options": t.protection_options or [],
        "created_at": t.created_at.isoformat() if t.created_at else None,
        **_tenant_counts(db, t.id),
    }
    if detail:
        v["members"] = [_user_view(u) for u in
                        db.query(User).filter(User.tenant_id == t.id).order_by(User.email.asc()).all()]
        v["vaults"] = [{"id": vv.id, "name": vv.name,
                        "key_ownership_model": vv.key_ownership_model}
                       for vv in db.query(Vault).filter(Vault.tenant_id == t.id).all()]
    return v


class TenantCreate(BaseModel):
    name: str
    plan: str = "business"
    key_ownership_model: str = "customer-managed"
    licensed_tb: float = 0
    owner_email: str | None = None
    owner_name: str | None = None


@router.post("/tenants")
def create_tenant(body: TenantCreate,
                  principal: security.Principal = Depends(security.require_platform_admin),
                  db: Session = Depends(get_db)):
    tenant = Tenant(
        name=body.name.strip() or "New Organization", plan=body.plan,
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
    if body.status is not None:
        t.status = body.status
    if body.key_ownership_model is not None:
        t.key_ownership_model = body.key_ownership_model
    if body.licensed_tb is not None:
        t.licensed_bytes = int(max(0.0, body.licensed_tb) * _TB)
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.tenant_updated",
                 tenant_id=t.id, category="admin",
                 detail={"status": t.status, "plan": t.plan})
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
    rows = []
    totals = {"tenants": 0, "users": 0, "objects": 0, "bytes": 0,
              "recovery_points": 0, "monthly_revenue": 0.0}
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
        rows.append({
            "id": t.id, "name": t.name, "plan": t.plan, "status": t.status,
            "users": counts["users"], "appliances": counts["appliances"],
            "agents": counts["agents"], "sources": counts["sources"],
            "objects": objects, "used_bytes": used_bytes,
            "licensed_bytes": int(t.licensed_bytes or 0),
            "recovery_points": counts["recovery_points"],
            "monthly_cost": monthly, "options": options,
        })
        totals["tenants"] += 1
        totals["users"] += counts["users"]
        totals["objects"] += objects
        totals["bytes"] += used_bytes
        totals["recovery_points"] += counts["recovery_points"]
        totals["monthly_revenue"] += monthly
    totals["monthly_revenue"] = round(totals["monthly_revenue"], 2)
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
    n = db.query(Node).filter(Node.is_self.is_(True)).first()
    if n is None:
        s = get_settings()
        n = Node(name=s.domain, role="control-plane", endpoint=s.api_base_url,
                 is_self=True, status="active")
        db.add(n)
        db.commit()
        db.refresh(n)
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
    return {
        "id": n.id, "name": n.name, "region": n.region, "role": n.role,
        "endpoint": n.endpoint, "status": n.status, "is_self": bool(n.is_self),
        "version": n.version, "online": online, "telemetry": tel,
        "last_heartbeat_at": n.last_heartbeat_at.isoformat() if n.last_heartbeat_at else None,
    }


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


@router.put("/nodes/{nid}")
def update_node(nid: str, body: NodeUpdate,
                principal: security.Principal = Depends(security.require_platform_admin),
                db: Session = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(404, "node not found")
    for k, val in body.dict(exclude_none=True).items():
        setattr(n, k, val.strip() if isinstance(val, str) else val)
    db.commit()
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
