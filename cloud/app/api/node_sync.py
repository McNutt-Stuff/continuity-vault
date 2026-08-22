"""Federated node replication (data-plane isolation).

A customer-tenant node keeps its OWN local database + search index for the
tenants assigned to it, and cannot reach the control plane's database directly.
Instead it authenticates with the shared fleet secret and:

  * PULLS the config for its assigned tenants (tenants, users, vaults + wrapped
    keys, mappings, connector accounts + encrypted creds, storage/email service
    objects, pricing) into its local DB, then runs sync locally; and
  * PUSHES the results it produces (recovery points + search index + connector
    status) back so the control plane's platform DB stays authoritative for the
    portal (search, recovery, billing, activity).

Key material and connector credentials are wrapped with the fleet-wide
``CV_KEK_SECRET`` (see keybroker / credstore), so the node can use them directly
— the whole fleet MUST share that secret for federation to work.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import DateTime
from sqlalchemy.orm import Session

from .. import keybroker
from ..db import get_db
from ..models import (
    Collection,
    ConfigObject,
    ConnectorAccount,
    DesktopAgent,
    Node,
    PricingConfig,
    SearchDocument,
    ServiceObject,
    SnapshotReceipt,
    Tenant,
    User,
    Vault,
)
from .site import _fleet_secret

router = APIRouter(prefix="/nodes/sync", tags=["node-sync"])


def _require_fleet(authorization: str) -> None:
    token = (authorization or "").replace("Bearer ", "").strip()
    if not token or token != _fleet_secret():
        raise HTTPException(401, "invalid node credentials")


def _ser(obj) -> dict:
    """Serialize a SQLAlchemy row to a JSON-safe dict (datetimes → ISO)."""
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _deser(model, data: dict) -> dict:
    """Coerce an inbound dict back to column values (ISO strings → datetimes)."""
    cols = {c.name: c for c in model.__table__.columns}
    kw = {}
    for k, v in data.items():
        col = cols.get(k)
        if col is None:
            continue
        if isinstance(col.type, DateTime) and isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v)
                v = dt.replace(tzinfo=None) if dt.tzinfo else dt
            except ValueError:
                v = None
        kw[k] = v
    return kw


def _upsert(db: Session, model, data: dict):
    kw = _deser(model, data)
    pk = list(model.__table__.primary_key.columns)[0].name
    obj = db.get(model, kw.get(pk))
    if obj is not None:
        for k, v in kw.items():
            if k != pk:
                setattr(obj, k, v)
    else:
        obj = model(**kw)
        db.add(obj)
    return obj


class NodeIdent(BaseModel):
    name: str
    role: str = "customer-tenant"
    since: str | None = None  # ISO cursor for incremental pull (unused in v1)


@router.post("/pull")
def pull(body: NodeIdent, authorization: str = Header(default=""),
         db: Session = Depends(get_db)):
    """Return the full config bundle for the tenants assigned to this node."""
    _require_fleet(authorization)
    node = (db.query(Node)
            .filter(Node.name == body.name, Node.role == body.role).first())
    if node is None:
        # Not registered yet (heartbeat runs on its own cadence) — nothing to do.
        return {"node_id": None, "tenants": [], "assigned": 0}
    tenants = db.query(Tenant).filter(Tenant.node_id == node.id).all()
    tids = [t.id for t in tenants]
    if not tids:
        return {"node_id": node.id, "tenants": [], "assigned": 0}

    users = db.query(User).filter(User.tenant_id.in_(tids)).all()
    vaults = db.query(Vault).filter(Vault.tenant_id.in_(tids)).all()
    collections = db.query(Collection).filter(Collection.tenant_id.in_(tids)).all()
    accounts = db.query(ConnectorAccount).filter(ConnectorAccount.tenant_id.in_(tids)).all()
    agents = db.query(DesktopAgent).filter(DesktopAgent.tenant_id.in_(tids)).all()

    # Wrapped key material for each vault (fleet-shared KEK → usable on the node).
    key_records = {v.id: keybroker.export_key_records(v.id) for v in vaults}

    pricing = db.get(PricingConfig, "default")
    return {
        "node_id": node.id,
        "assigned": len(tids),
        "tenants": [_ser(t) for t in tenants],
        "users": [_ser(u) for u in users],
        "vaults": [_ser(v) for v in vaults],
        "desktop_agents": [_ser(a) for a in agents],
        "collections": [_ser(c) for c in collections],
        "connector_accounts": [_ser(a) for a in accounts],
        "service_objects": [_ser(s) for s in db.query(ServiceObject).all()],
        "config_objects": [_ser(c) for c in db.query(ConfigObject).all()],
        "nodes": [_ser(n) for n in db.query(Node).all()],
        "pricing": _ser(pricing) if pricing else None,
        "key_records": key_records,
    }


class PushPayload(BaseModel):
    node: str
    role: str = "customer-tenant"
    receipts: list[dict] = []
    documents: list[dict] = []
    connector_accounts: list[dict] = []


@router.post("/push")
def push(body: PushPayload, authorization: str = Header(default=""),
         db: Session = Depends(get_db)):
    """Ingest the results a node produced so the control-plane platform DB stays
    authoritative for the portal (search / recovery / billing / activity)."""
    _require_fleet(authorization)
    counts = {"receipts": 0, "documents": 0, "connector_accounts": 0}
    for r in body.receipts:
        _upsert(db, SnapshotReceipt, r)
        counts["receipts"] += 1
    for d in body.documents:
        _upsert(db, SearchDocument, d)
        counts["documents"] += 1
    # Only status/cursor fields for accounts — never overwrite the encrypted
    # credentials the control plane owns.
    for a in body.connector_accounts:
        acct = db.get(ConnectorAccount, a.get("id"))
        if not acct:
            continue
        for f in ("last_sync_at", "sync_cursor", "last_object_count",
                  "last_error", "last_error_at", "auth_status"):
            if f in a:
                val = a[f]
                if f.endswith("_at") and isinstance(val, str):
                    try:
                        dt = datetime.fromisoformat(val)
                        val = dt.replace(tzinfo=None) if dt.tzinfo else dt
                    except ValueError:
                        val = None
                setattr(acct, f, val)
        counts["connector_accounts"] += 1
    db.commit()
    return {"ok": True, **counts}
