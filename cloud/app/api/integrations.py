"""Integrations API — user portal, appliance data plane, and admin/analytics.

Integrations gather auxiliary intelligence (network/app telemetry) rather than
vaulting data. Appliance-run integrations (e.g. UniFi) collect on the customer's
appliance and ship reports here; the portal drives setup, drill-downs and the
"clients/apps of interest" curation; admins toggle integrations platform-wide and
mine cross-customer analytics to decide which new sources to build.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import credstore, security
from ..db import get_db
from ..integrations import all_integrations, get_integration
from ..integrations.source_map import candidate_service, map_app_to_source
from ..models import (
    Appliance,
    ConnectorAccount,
    IntegrationConfig,
    IntegrationInstance,
    IntegrationRun,
    NetworkApp,
    NetworkClient,
    NetworkUsage,
    Tenant,
    User,
)
from .appliances import _agent_appliance

router = APIRouter(prefix="/integrations", tags=["integrations"])
agent_router = APIRouter(prefix="/appliance/integrations", tags=["appliance-integrations"])
admin_router = APIRouter(prefix="/admin", tags=["admin-integrations"])


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Views                                                                        #
# --------------------------------------------------------------------------- #
def _spec_view(spec) -> dict:
    return {
        "integration_type": spec.integration_type,
        "display_name": spec.display_name,
        "description": spec.description,
        "icon": spec.icon,
        "color": spec.color,
        "category": spec.category,
        "runs_on": spec.runs_on,
        "needs_appliance": spec.needs_appliance,
        "default_interval_minutes": spec.default_interval_minutes,
        "auto_provision_key": spec.auto_provision_key,
        "provides": spec.provides,
        "credential_fields": [
            {"name": f.name, "label": f.label, "type": f.type,
             "placeholder": f.placeholder, "required": f.required, "help": f.help}
            for f in spec.credential_fields
        ],
    }


def _instance_view(inst: IntegrationInstance) -> dict:
    spec = get_integration(inst.integration_type)
    disp = spec.spec().display_name if spec else inst.integration_type
    return {
        "id": inst.id,
        "integration_type": inst.integration_type,
        "display_name": disp,
        "label": inst.label or disp,
        "enabled": inst.enabled,
        "runs_on": inst.runs_on,
        "appliance_id": inst.appliance_id,
        "status": inst.status,
        "poll_interval_minutes": inst.poll_interval_minutes,
        "host": (inst.config or {}).get("host", ""),
        "last_run_at": inst.last_run_at.isoformat() if inst.last_run_at else None,
        "last_success_at": inst.last_success_at.isoformat() if inst.last_success_at else None,
        "last_error": inst.last_error,
        "last_stats": inst.last_stats or {},
    }


def _admin_enabled(db: Session, integration_type: str) -> bool:
    cfg = db.get(IntegrationConfig, integration_type)
    return True if cfg is None else bool(cfg.enabled)


# --------------------------------------------------------------------------- #
# User portal                                                                  #
# --------------------------------------------------------------------------- #
@router.get("")
def list_integrations(principal: security.Principal = Depends(security.get_principal),
                      db: Session = Depends(get_db)):
    """Available integration types (admin-enabled) + this user's instances."""
    available = [_spec_view(i.spec()) for i in all_integrations()
                 if _admin_enabled(db, i.integration_type)]
    instances = (db.query(IntegrationInstance)
                 .filter(IntegrationInstance.tenant_id == principal.tenant_id,
                         IntegrationInstance.owner_user_id == principal.user_id)
                 .order_by(IntegrationInstance.created_at.desc()).all())
    # Appliances the user can run LAN integrations on.
    appliances = (db.query(Appliance)
                  .filter(Appliance.tenant_id == principal.tenant_id,
                          Appliance.state != "retired").all())
    return {
        "available": available,
        "instances": [_instance_view(i) for i in instances],
        "appliances": [{"id": a.id, "name": a.name, "state": a.state,
                        "online": a.state not in ("retired", "offline")} for a in appliances],
    }


class CreateInstance(BaseModel):
    integration_type: str
    appliance_id: str | None = None
    label: str = ""
    credentials: dict = {}
    poll_interval_minutes: int | None = None


@router.post("")
def create_instance(body: CreateInstance,
                    principal: security.Principal = Depends(security.get_principal),
                    db: Session = Depends(get_db)):
    integ = get_integration(body.integration_type)
    if integ is None or not _admin_enabled(db, body.integration_type):
        raise HTTPException(404, "integration not available")
    spec = integ.spec()
    appliance_id = body.appliance_id
    if spec.needs_appliance:
        if not appliance_id:
            raise HTTPException(400, "an appliance is required for this integration")
        a = db.get(Appliance, appliance_id)
        if not a or a.tenant_id != principal.tenant_id:
            raise HTTPException(404, "appliance not found")
    # Split the submitted fields: host (non-secret) → config; the rest → creds.
    creds = dict(body.credentials or {})
    host = creds.pop("host", "")
    config = {"host": host, "site": creds.pop("site", "default")}
    interval = int(body.poll_interval_minutes or spec.default_interval_minutes)
    inst = IntegrationInstance(
        tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
        integration_type=body.integration_type, label=body.label or spec.display_name,
        runs_on=spec.runs_on, appliance_id=appliance_id, enabled=True,
        credentials=credstore.encrypt(principal.tenant_id, creds) if creds else None,
        config=config, poll_interval_minutes=interval, status="pending")
    db.add(inst)
    db.commit()
    return _instance_view(inst)


class UpdateInstance(BaseModel):
    label: str | None = None
    enabled: bool | None = None
    poll_interval_minutes: int | None = None
    credentials: dict | None = None  # re-submit to update login / host


@router.put("/{iid}")
def update_instance(iid: str, body: UpdateInstance,
                    principal: security.Principal = Depends(security.get_principal),
                    db: Session = Depends(get_db)):
    inst = _owned_instance(db, principal, iid)
    if body.label is not None:
        inst.label = body.label
    if body.enabled is not None:
        inst.enabled = body.enabled
        inst.status = "pending" if body.enabled else "disabled"
    if body.poll_interval_minutes is not None:
        inst.poll_interval_minutes = max(5, int(body.poll_interval_minutes))
    if body.credentials is not None:
        creds = dict(body.credentials)
        host = creds.pop("host", None)
        if host is not None:
            inst.config = {**(inst.config or {}), "host": host}
        if creds:
            inst.credentials = credstore.encrypt(principal.tenant_id, creds)
            inst.status = "pending"  # re-provision with the new login
    inst.updated_at = _now()
    db.commit()
    return _instance_view(inst)


@router.delete("/{iid}")
def delete_instance(iid: str,
                    principal: security.Principal = Depends(security.get_principal),
                    db: Session = Depends(get_db)):
    inst = _owned_instance(db, principal, iid)
    # Drop the telemetry it produced too.
    for model in (NetworkUsage, NetworkApp, NetworkClient, IntegrationRun):
        db.query(model).filter(model.integration_id == iid).delete()
    db.delete(inst)
    db.commit()
    return {"ok": True}


def _owned_instance(db: Session, principal, iid: str) -> IntegrationInstance:
    inst = db.get(IntegrationInstance, iid)
    if not inst or inst.tenant_id != principal.tenant_id \
            or (inst.owner_user_id and inst.owner_user_id != principal.user_id):
        raise HTTPException(404, "integration not found")
    return inst


# ---- Drill-down data (clients / apps / shadow sources) ----------------------
def _user_instance_ids(db: Session, principal) -> list[str]:
    return [i.id for i in db.query(IntegrationInstance.id).filter(
        IntegrationInstance.tenant_id == principal.tenant_id,
        IntegrationInstance.owner_user_id == principal.user_id).all()]


def _enabled_source_types(db: Session, principal) -> set[str]:
    rows = (db.query(ConnectorAccount.connector_type)
            .filter(ConnectorAccount.tenant_id == principal.tenant_id,
                    ConnectorAccount.owner_user_id == principal.user_id).all())
    return {r[0] for r in rows}


def _app_aggregates(db: Session, tenant_id: str, iids: list[str],
                    ignored: set[str]) -> list[dict]:
    """Aggregate app usage across instances, excluding ignored clients' traffic
    (falls back to the stored per-app aggregate when there's no usage detail)."""
    if not iids:
        return []
    apps = {a.app_key: a for a in db.query(NetworkApp).filter(
        NetworkApp.tenant_id == tenant_id, NetworkApp.integration_id.in_(iids)).all()}
    agg: dict[str, dict] = {}
    usage = db.query(NetworkUsage).filter(
        NetworkUsage.tenant_id == tenant_id, NetworkUsage.integration_id.in_(iids)).all()
    for u in usage:
        if u.client_key in ignored:
            continue
        meta = apps.get(u.app_key)
        a = agg.setdefault(u.app_key, {
            "app_key": u.app_key,
            "name": meta.name if meta else u.app_key,
            "category": meta.category if meta else "",
            "source_type": meta.source_type if meta else "",
            "of_interest": bool(meta.of_interest) if meta else False,
            "total_bytes": 0, "clients": set(), "last_seen": None,
        })
        a["total_bytes"] += int(u.total_bytes or 0)
        if u.client_key:
            a["clients"].add(u.client_key)
        if u.last_seen and (a["last_seen"] is None or u.last_seen > a["last_seen"]):
            a["last_seen"] = u.last_seen
    if not agg:  # no per-client detail — use the stored aggregate
        for a in apps.values():
            agg[a.app_key] = {
                "app_key": a.app_key, "name": a.name, "category": a.category,
                "source_type": a.source_type, "of_interest": bool(a.of_interest),
                "total_bytes": int(a.total_bytes or 0),
                "clients": set(range(a.client_count or 0)), "last_seen": a.last_seen,
            }
    out = []
    for a in agg.values():
        out.append({**a, "client_count": len(a["clients"]),
                    "clients": None,
                    "last_seen": a["last_seen"].isoformat() if a["last_seen"] else None})
    out.sort(key=lambda x: -x["total_bytes"])
    return out


@router.get("/data")
def integration_data(principal: security.Principal = Depends(security.get_principal),
                     db: Session = Depends(get_db)):
    """Everything the user drill-down needs: clients, apps, shadow sources, stats."""
    iids = _user_instance_ids(db, principal)
    if not iids:
        return {"clients": [], "apps": [], "shadow": [], "stats": {}}
    clients = (db.query(NetworkClient)
               .filter(NetworkClient.tenant_id == principal.tenant_id,
                       NetworkClient.integration_id.in_(iids))
               .order_by(NetworkClient.total_bytes.desc()).all())
    ignored = {c.client_key for c in clients if c.monitor_state == "ignored"}
    apps = _app_aggregates(db, principal.tenant_id, iids, ignored)
    enabled = _enabled_source_types(db, principal)
    # Shadow sources: apps that map to a connector the user hasn't enabled.
    shadow: dict[str, dict] = {}
    for a in apps:
        st = a.get("source_type")
        if st and st not in enabled:
            s = shadow.setdefault(st, {"source_type": st, "name": a["name"],
                                       "total_bytes": 0, "apps": 0})
            s["total_bytes"] += a["total_bytes"]
            s["apps"] += 1
    return {
        "clients": [_client_view(c) for c in clients],
        "apps": apps,
        "shadow": sorted(shadow.values(), key=lambda s: -s["total_bytes"]),
        "stats": {
            "clients": len(clients),
            "monitored": sum(1 for c in clients if c.monitor_state == "monitored"),
            "ignored": len(ignored),
            "apps": len(apps),
            "bytes": sum(a["total_bytes"] for a in apps),
        },
    }


def _client_view(c: NetworkClient) -> dict:
    return {
        "id": c.id, "name": c.name or c.hostname or c.mac, "hostname": c.hostname,
        "ip": c.ip, "mac": c.mac, "device_type": c.device_type,
        "is_wired": c.is_wired, "is_guest": c.is_guest,
        "monitor_state": c.monitor_state, "of_interest": c.of_interest,
        "total_bytes": int(c.total_bytes or 0),
        "last_seen": c.last_seen.isoformat() if c.last_seen else None,
    }


class ClientState(BaseModel):
    monitor_state: str | None = None  # normal | ignored | monitored
    of_interest: bool | None = None


@router.post("/clients/{cid}")
def set_client_state(cid: str, body: ClientState,
                     principal: security.Principal = Depends(security.get_principal),
                     db: Session = Depends(get_db)):
    c = db.get(NetworkClient, cid)
    if not c or c.tenant_id != principal.tenant_id:
        raise HTTPException(404, "client not found")
    if body.monitor_state in ("normal", "ignored", "monitored"):
        c.monitor_state = body.monitor_state
    if body.of_interest is not None:
        c.of_interest = body.of_interest
    c.updated_at = _now()
    db.commit()
    return _client_view(c)


class AppInterest(BaseModel):
    app_key: str
    of_interest: bool


@router.post("/apps/interest")
def set_app_interest(body: AppInterest,
                     principal: security.Principal = Depends(security.get_principal),
                     db: Session = Depends(get_db)):
    iids = _user_instance_ids(db, principal)
    if not iids:
        raise HTTPException(404, "no integrations")
    rows = (db.query(NetworkApp)
            .filter(NetworkApp.tenant_id == principal.tenant_id,
                    NetworkApp.integration_id.in_(iids),
                    NetworkApp.app_key == body.app_key).all())
    if not rows:
        raise HTTPException(404, "app not found")
    for a in rows:
        a.of_interest = body.of_interest
        a.updated_at = _now()
    db.commit()
    return {"ok": True, "of_interest": body.of_interest}


# --------------------------------------------------------------------------- #
# Appliance data plane (fleet/appliance-token authed)                          #
# --------------------------------------------------------------------------- #
@agent_router.get("/pull")
def appliance_pull(appliance: Appliance = Depends(_agent_appliance),
                   db: Session = Depends(get_db)):
    """The integrations this appliance should run, with decrypted credentials
    (delivered over the appliance's authenticated TLS channel)."""
    insts = (db.query(IntegrationInstance)
             .filter(IntegrationInstance.appliance_id == appliance.id,
                     IntegrationInstance.enabled == True).all())  # noqa: E712
    out = []
    for i in insts:
        try:
            creds = credstore.decrypt(i.tenant_id, i.credentials) if i.credentials else {}
        except Exception:
            creds = {}
        out.append({
            "id": i.id, "integration_type": i.integration_type,
            "enabled": i.enabled, "config": i.config or {},
            "credentials": creds, "poll_interval_minutes": i.poll_interval_minutes,
        })
    return {"integrations": out}


class IntegrationReport(BaseModel):
    integration_id: str
    integration_type: str
    status: str = "ok"
    error: str | None = None
    clients: list[dict] = []
    apps: list[dict] = []
    usage: list[dict] = []
    stats: dict = {}
    credentials_update: dict | None = None


@agent_router.post("/report")
def appliance_report(body: IntegrationReport,
                     appliance: Appliance = Depends(_agent_appliance),
                     db: Session = Depends(get_db)):
    """Ingest an integration run: persist network telemetry (preserving the
    user's monitor/interest curation) and update the instance's status."""
    inst = db.get(IntegrationInstance, body.integration_id)
    if not inst or inst.appliance_id != appliance.id:
        raise HTTPException(404, "integration not found for this appliance")
    tid = inst.tenant_id
    _ingest_report(db, tid, inst, body, appliance_id=appliance.id)
    db.commit()
    return {"ok": True}


def _parse_dt(v) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v)
        return d.replace(tzinfo=None) if d.tzinfo else d
    except ValueError:
        return None


def _ingest_report(db: Session, tid: str, inst: IntegrationInstance,
                   body: IntegrationReport, appliance_id: str | None) -> None:
    now = _now()
    inst.last_run_at = now
    inst.status = "active" if body.status == "ok" else "error"
    inst.last_error = body.error
    inst.last_stats = body.stats or {}
    if body.status == "ok":
        inst.last_success_at = now
    if body.credentials_update:
        try:
            cur = credstore.decrypt(tid, inst.credentials) if inst.credentials else {}
        except Exception:
            cur = {}
        cur.update(body.credentials_update)
        if cur.get("api_key"):  # a reusable key was minted — the raw login isn't needed
            cur.pop("password", None)
        inst.credentials = credstore.encrypt(tid, cur)

    # Clients — upsert by (integration, client_key); preserve user curation.
    existing_c = {c.client_key: c for c in db.query(NetworkClient).filter(
        NetworkClient.tenant_id == tid, NetworkClient.integration_id == inst.id).all()}
    for cd in body.clients:
        key = cd.get("client_key") or cd.get("mac")
        if not key:
            continue
        c = existing_c.get(key)
        if c is None:
            c = NetworkClient(tenant_id=tid, integration_id=inst.id, client_key=key,
                              first_seen=now)
            db.add(c)
        c.name = cd.get("name") or c.name or key
        c.hostname = cd.get("hostname") or c.hostname
        c.ip = cd.get("ip") or c.ip
        c.mac = cd.get("mac") or c.mac or key
        c.is_wired = bool(cd.get("is_wired"))
        c.is_guest = bool(cd.get("is_guest"))
        c.device_type = cd.get("device_type") or c.device_type
        c.tx_bytes = int(cd.get("tx_bytes", 0) or 0)
        c.rx_bytes = int(cd.get("rx_bytes", 0) or 0)
        c.total_bytes = int(cd.get("total_bytes", 0) or 0) or (c.tx_bytes + c.rx_bytes)
        c.last_seen = _parse_dt(cd.get("last_seen")) or now

    # Apps — upsert by app_key; map to a connector source; preserve of_interest.
    existing_a = {a.app_key: a for a in db.query(NetworkApp).filter(
        NetworkApp.tenant_id == tid, NetworkApp.integration_id == inst.id).all()}
    for ad in body.apps:
        key = ad.get("app_key")
        if not key:
            continue
        a = existing_a.get(key)
        if a is None:
            a = NetworkApp(tenant_id=tid, integration_id=inst.id, app_key=key,
                           first_seen=now)
            db.add(a)
        a.name = ad.get("name") or a.name or key
        a.category = ad.get("category") or a.category
        a.source_type = map_app_to_source(a.name, a.category)
        a.tx_bytes = int(ad.get("tx_bytes", 0) or 0)
        a.rx_bytes = int(ad.get("rx_bytes", 0) or 0)
        a.total_bytes = int(ad.get("total_bytes", 0) or 0) or (a.tx_bytes + a.rx_bytes)
        a.client_count = int(ad.get("client_count", 0) or 0)
        a.last_seen = _parse_dt(ad.get("last_seen")) or now

    # Usage edges — upsert by (client_key, app_key).
    existing_u = {(u.client_key, u.app_key): u for u in db.query(NetworkUsage).filter(
        NetworkUsage.tenant_id == tid, NetworkUsage.integration_id == inst.id).all()}
    for ud in body.usage:
        k = (ud.get("client_key"), ud.get("app_key"))
        if not k[0] or not k[1]:
            continue
        u = existing_u.get(k)
        if u is None:
            u = NetworkUsage(tenant_id=tid, integration_id=inst.id,
                             client_key=k[0], app_key=k[1])
            db.add(u)
        u.tx_bytes = int(ud.get("tx_bytes", 0) or 0)
        u.rx_bytes = int(ud.get("rx_bytes", 0) or 0)
        u.total_bytes = int(ud.get("total_bytes", 0) or 0) or (u.tx_bytes + u.rx_bytes)
        u.last_seen = _parse_dt(ud.get("last_seen")) or now

    st = body.stats or {}
    db.add(IntegrationRun(
        tenant_id=tid, integration_id=inst.id, integration_type=inst.integration_type,
        appliance_id=appliance_id, status=inst.status, started_at=now, finished_at=now,
        clients=int(st.get("clients", 0) or 0), apps=int(st.get("apps", 0) or 0),
        bytes_seen=int(st.get("bytes_seen", 0) or 0), error=body.error))


# --------------------------------------------------------------------------- #
# Admin — platform enable/disable + customer analytics                         #
# --------------------------------------------------------------------------- #
@admin_router.get("/integrations")
def admin_list(principal: security.Principal = Depends(security.require_platform_admin),
               db: Session = Depends(get_db)):
    rows = []
    for i in all_integrations():
        cfg = db.get(IntegrationConfig, i.integration_type)
        used = (db.query(func.count(IntegrationInstance.id))
                .filter(IntegrationInstance.integration_type == i.integration_type).scalar())
        rows.append({**_spec_view(i.spec()),
                     "enabled": True if cfg is None else bool(cfg.enabled),
                     "instances": int(used or 0)})
    return rows


class AdminIntegrationUpdate(BaseModel):
    enabled: bool | None = None
    config: dict | None = None


@admin_router.put("/integrations/{itype}")
def admin_update(itype: str, body: AdminIntegrationUpdate,
                 principal: security.Principal = Depends(security.require_platform_admin),
                 db: Session = Depends(get_db)):
    if get_integration(itype) is None:
        raise HTTPException(404, "unknown integration")
    cfg = db.get(IntegrationConfig, itype)
    if cfg is None:
        cfg = IntegrationConfig(integration_type=itype)
        db.add(cfg)
    if body.enabled is not None:
        cfg.enabled = body.enabled
    if body.config is not None:
        cfg.config = body.config
    cfg.updated_at = _now()
    db.commit()
    return {"ok": True, "integration_type": itype, "enabled": cfg.enabled}


@admin_router.get("/analytics")
def admin_analytics(scope: str = "platform", tenant_id: str | None = None,
                    principal: security.Principal = Depends(security.require_platform_admin),
                    db: Session = Depends(get_db)):
    """Cross-customer (or single-tenant) view of the apps, services, clients and
    devices observed — and which new sources the platform should build."""
    aq = db.query(NetworkApp)
    cq = db.query(NetworkClient)
    if scope == "tenant" and tenant_id:
        aq = aq.filter(NetworkApp.tenant_id == tenant_id)
        cq = cq.filter(NetworkClient.tenant_id == tenant_id)
    apps = aq.all()
    clients = cq.all()

    # Aggregate by app name across tenants.
    by_app: dict[str, dict] = {}
    for a in apps:
        e = by_app.setdefault(a.name or a.app_key, {
            "name": a.name or a.app_key, "category": a.category,
            "source_type": a.source_type, "total_bytes": 0, "tenants": set()})
        e["total_bytes"] += int(a.total_bytes or 0)
        e["tenants"].add(a.tenant_id)
    top_apps = sorted(
        ({"name": e["name"], "category": e["category"], "source_type": e["source_type"],
          "total_bytes": e["total_bytes"], "tenant_count": len(e["tenants"]),
          "has_source": bool(e["source_type"])} for e in by_app.values()),
        key=lambda x: -x["total_bytes"])[:50]

    # Recommended new sources: popular services seen in traffic with no connector.
    rec: dict[str, dict] = {}
    for a in apps:
        cand = candidate_service(a.name or "", a.category or "")
        if not cand:
            continue
        r = rec.setdefault(cand["name"], {"name": cand["name"], "kind": cand["kind"],
                                          "total_bytes": 0, "tenants": set()})
        r["total_bytes"] += int(a.total_bytes or 0)
        r["tenants"].add(a.tenant_id)
    recommended = sorted(
        ({"name": r["name"], "kind": r["kind"], "total_bytes": r["total_bytes"],
          "tenant_count": len(r["tenants"])} for r in rec.values()),
        key=lambda x: (-x["tenant_count"], -x["total_bytes"]))

    # Adoption of apps we DO have a source for (shadow opportunity sizing).
    device_types: dict[str, int] = {}
    for c in clients:
        device_types[c.device_type or "device"] = device_types.get(c.device_type or "device", 0) + 1

    return {
        "scope": scope,
        "totals": {
            "apps": len(by_app),
            "clients": len(clients),
            "tenants": len({a.tenant_id for a in apps}),
            "bytes": sum(int(a.total_bytes or 0) for a in apps),
        },
        "top_apps": top_apps,
        "recommended_sources": recommended,
        "device_types": [{"type": k, "count": v}
                         for k, v in sorted(device_types.items(), key=lambda kv: -kv[1])],
    }
