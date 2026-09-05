"""Integrations API — user portal, appliance data plane, and admin/analytics.

Integrations gather auxiliary intelligence (network/app telemetry) rather than
vaulting data. Appliance-run integrations (e.g. UniFi) collect on the customer's
appliance and ship reports here; the portal drives setup, drill-downs and the
"clients/apps of interest" curation; admins toggle integrations platform-wide and
mine cross-customer analytics to decide which new sources to build.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import credstore, security, audit
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
    NetworkSample,
    SearchDocument,
    Tenant,
    User,
)
from .appliances import _agent_appliance

logger = logging.getLogger("cv.integrations")

router = APIRouter(prefix="/integrations", tags=["integrations"],
                   dependencies=[Depends(security.require_feature("integrations_enabled"))])
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
        "health": _instance_health(inst),
        "poll_interval_minutes": inst.poll_interval_minutes,
        "host": (inst.config or {}).get("host", ""),
        "provision_state": inst.provision_state or "idle",
        "provision_message": inst.provision_message,
        "last_run_at": inst.last_run_at.isoformat() if inst.last_run_at else None,
        "last_success_at": inst.last_success_at.isoformat() if inst.last_success_at else None,
        "last_error": inst.last_error,
        "last_stats": inst.last_stats or {},
    }


def _instance_health(inst: IntegrationInstance) -> str:
    """Coarse health for the card/detail badge: paused | setup | error | stale |
    pending | ok. Lets the user see at a glance if collection is failing."""
    if not inst.enabled:
        return "paused"
    prov = inst.provision_state or "idle"
    if prov not in ("idle", "done"):
        return "error" if prov == "error" else "setup"
    if inst.status == "error" or inst.last_error:
        return "error"
    if inst.last_run_at is None:
        return "pending"
    # No successful run in a while (3× the poll interval, min 30m) → stale.
    ref = inst.last_success_at or inst.last_run_at
    stale_after = max(30, int(inst.poll_interval_minutes or 30) * 3)
    if (_now() - ref).total_seconds() > stale_after * 60:
        return "stale"
    return "ok"



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
    tenant = db.get(Tenant, principal.tenant_id)
    return {
        "available": available,
        "instances": [_instance_view(i) for i in instances],
        "appliances": [{"id": a.id, "name": a.name, "state": a.state,
                        "online": a.state not in ("retired", "offline")} for a in appliances],
        "plan": (tenant.plan if tenant else "") or "",
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
    # Integrations that mint their own key start in an interactive provisioning
    # handshake (which also covers MFA/OTP); others are ready to collect.
    prov = "starting" if spec.auto_provision_key else "idle"
    inst = IntegrationInstance(
        tenant_id=principal.tenant_id, owner_user_id=principal.user_id,
        integration_type=body.integration_type, label=body.label or spec.display_name,
        runs_on=spec.runs_on, appliance_id=appliance_id, enabled=True,
        credentials=credstore.encrypt(principal.tenant_id, creds) if creds else None,
        config=config, poll_interval_minutes=interval, status="pending",
        provision_state=prov, provision_message=("Contacting your controller…"
                                                 if prov == "starting" else None))
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
        # Only the fields the user actually filled in are treated as changes.
        creds = {k: v for k, v in creds.items() if v not in (None, "")}
        if creds:
            # A fresh login was entered — replace the stored credential and drop
            # any minted API key so the appliance re-provisions against it.
            inst.credentials = credstore.encrypt(principal.tenant_id, creds)
            inst.status = "pending"
            integ = get_integration(inst.integration_type)
            if integ and integ.spec().auto_provision_key:
                inst.provision_state = "starting"
                inst.provision_message = "Contacting your controller…"
                inst.provision_otp = None
        elif host is not None:
            # Host changed but the same key/login stays — re-validate on next poll.
            inst.status = "pending"
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


# ---- Interactive provisioning (OTP handshake with the appliance) ------------
_PROVISION_STEPS = ["Connecting to your controller", "Verify your identity", "Securing an API key"]
_STATE_STEP = {"starting": 0, "authenticating": 0, "awaiting_otp": 1,
               "verifying": 1, "done": 2, "error": 0}


def _provision_view(inst: IntegrationInstance) -> dict:
    state = inst.provision_state or "idle"
    return {
        "provision_state": state,
        "message": inst.provision_message,
        "needs_otp": state == "awaiting_otp",
        "done": state == "done",
        "error": state == "error",
        "step": _STATE_STEP.get(state, 0),
        "steps": _PROVISION_STEPS,
    }


@router.post("/{iid}/provision")
def start_provision(iid: str,
                    principal: security.Principal = Depends(security.get_principal),
                    db: Session = Depends(get_db)):
    """(Re)start the interactive setup handshake — the appliance attempts the
    controller login and, if MFA is required, asks for a verification code."""
    inst = _owned_instance(db, principal, iid)
    inst.provision_state = "starting"
    inst.provision_message = "Contacting your controller…"
    inst.provision_otp = None
    inst.status = "pending"
    inst.updated_at = _now()
    db.commit()
    return _provision_view(inst)


@router.get("/{iid}/provision")
def get_provision(iid: str,
                  principal: security.Principal = Depends(security.get_principal),
                  db: Session = Depends(get_db)):
    return _provision_view(_owned_instance(db, principal, iid))


class OtpBody(BaseModel):
    otp: str


@router.post("/{iid}/provision/otp")
def submit_provision_otp(iid: str, body: OtpBody,
                         principal: security.Principal = Depends(security.get_principal),
                         db: Session = Depends(get_db)):
    inst = _owned_instance(db, principal, iid)
    if inst.provision_state != "awaiting_otp":
        raise HTTPException(409, "the integration is not waiting for a code")
    inst.provision_otp = (body.otp or "").strip()
    inst.provision_state = "verifying"
    inst.provision_message = "Verifying your code…"
    inst.updated_at = _now()
    db.commit()
    return _provision_view(inst)


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
    """Aggregated drill-down across all of the user's integrations."""
    return _drilldown(db, principal, _user_instance_ids(db, principal))


@router.get("/{iid}/data")
def integration_instance_data(iid: str,
                              principal: security.Principal = Depends(security.get_principal),
                              db: Session = Depends(get_db)):
    """Drill-down scoped to a single integration instance."""
    inst = _owned_instance(db, principal, iid)
    return _drilldown(db, principal, [inst.id])


@router.get("/{iid}/usage")
def integration_usage(iid: str, app_key: str | None = None,
                      client_key: str | None = None, source_type: str | None = None,
                      principal: security.Principal = Depends(security.get_principal),
                      db: Session = Depends(get_db)):
    """Relationship drill-down for one instance: which clients use an app
    (``app_key`` or ``source_type``), or which apps a client uses (``client_key``).
    Powers the expandable rows on the Apps/Clients tabs and the shadow-app popup."""
    inst = _owned_instance(db, principal, iid)
    tid = principal.tenant_id
    clients = {c.client_key: c for c in db.query(NetworkClient).filter(
        NetworkClient.tenant_id == tid, NetworkClient.integration_id == inst.id).all()}
    apps = {a.app_key: a for a in db.query(NetworkApp).filter(
        NetworkApp.tenant_id == tid, NetworkApp.integration_id == inst.id).all()}
    usage = db.query(NetworkUsage).filter(
        NetworkUsage.tenant_id == tid, NetworkUsage.integration_id == inst.id).all()

    if client_key:  # → the apps this device uses
        rows: dict[str, dict] = {}
        for u in usage:
            if u.client_key != client_key:
                continue
            meta = apps.get(u.app_key)
            r = rows.setdefault(u.app_key, {
                "app_key": u.app_key, "name": meta.name if meta else u.app_key,
                "category": meta.category if meta else "",
                "source_type": meta.source_type if meta else "", "total_bytes": 0})
            r["total_bytes"] += int(u.total_bytes or 0)
        return {"mode": "apps",
                "apps": sorted(rows.values(), key=lambda x: -x["total_bytes"])}

    # → the clients using this app (or any app mapped to this source_type)
    if source_type:
        want = {k for k, a in apps.items() if a.source_type == source_type}
    elif app_key:
        want = {app_key}
    else:
        raise HTTPException(400, "specify app_key, client_key or source_type")
    rows2: dict[str, dict] = {}
    for u in usage:
        if u.app_key not in want or not u.client_key:
            continue
        c = clients.get(u.client_key)
        r = rows2.setdefault(u.client_key, {
            "client_key": u.client_key,
            "id": c.id if c else None,
            "name": (c.nickname or c.name or c.hostname or c.mac) if c else u.client_key,
            "device_type": c.device_type if c else "",
            "ip": c.ip if c else "", "mac": c.mac if c else u.client_key,
            "monitor_state": c.monitor_state if c else "normal",
            "total_bytes": 0})
        r["total_bytes"] += int(u.total_bytes or 0)
    return {"mode": "clients",
            "clients": sorted(rows2.values(), key=lambda x: -x["total_bytes"])}


# --------------------------------------------------------------------------- #
# Time-series analytics + trends (from the 90-day NetworkSample rollups)        #
# --------------------------------------------------------------------------- #
_TREND_WINDOWS = {"7d": 7, "14d": 14, "30d": 30, "90d": 90}


def _window_days(window: str | None) -> int:
    return _TREND_WINDOWS.get((window or "30d"), 30)


def _fill_series(by_day: dict, end_day: datetime, days: int) -> list[dict]:
    """Contiguous oldest→newest daily list, zero-filled for missing days."""
    out = []
    for i in range(days - 1, -1, -1):
        d = end_day - timedelta(days=i)
        out.append({"day": d.date().isoformat(), "bytes": int(by_day.get(d, 0))})
    return out


def _change_pct(curr: int, prev: int) -> float | None:
    if not prev:
        return None if not curr else 100.0
    return round((curr - prev) * 100.0 / prev, 1)


def _network_analytics(db: Session, tenant_id: str, iids: list[str],
                       window: str = "30d", top: int = 8) -> dict:
    """Trends + top-mover analytics over the 90-day NetworkSample rollups.
    Ranks apps/clients by traffic in the window with per-entity daily series and
    a change vs the immediately-preceding window of equal length."""
    days = _window_days(window)
    end_day = _day_bucket(_now())
    cur_since = end_day - timedelta(days=days - 1)
    prev_since = cur_since - timedelta(days=days)
    if not iids:
        return {"window": window, "days": days, "series": _fill_series({}, end_day, days),
                "top_apps": [], "top_clients": [], "summary": {}}
    rows = (db.query(NetworkSample)
            .filter(NetworkSample.tenant_id == tenant_id,
                    NetworkSample.integration_id.in_(iids),
                    NetworkSample.day >= prev_since)
            .all())

    total_by_day: dict = {}
    # dim -> key -> {"name","category","source_type","device_type","cur","prev","by_day"}
    apps: dict = {}
    clients: dict = {}
    cur_apps: set = set()
    cur_clients: set = set()
    for s in rows:
        in_cur = s.day >= cur_since
        b = int(s.total_bytes or 0)
        if s.dim == "total":
            if in_cur:
                total_by_day[s.day] = total_by_day.get(s.day, 0) + b
            continue
        bucket = apps if s.dim == "app" else clients if s.dim == "client" else None
        if bucket is None:
            continue
        e = bucket.setdefault(s.key, {"key": s.key, "name": s.name or s.key,
                                      "category": s.category or "", "source_type": s.source_type or "",
                                      "device_type": s.device_type or "", "cur": 0, "prev": 0, "by_day": {}})
        if s.name:
            e["name"] = s.name
        if in_cur:
            e["cur"] += b
            e["by_day"][s.day] = e["by_day"].get(s.day, 0) + b
            if b:
                (cur_apps if s.dim == "app" else cur_clients).add(s.key)
        else:
            e["prev"] += b

    def _rank(bucket: dict, extra: tuple) -> list[dict]:
        ranked = sorted(bucket.values(), key=lambda x: -x["cur"])[:top]
        out = []
        for e in ranked:
            row = {"key": e["key"], "name": e["name"], "total_bytes": e["cur"],
                   "prev_bytes": e["prev"], "change_pct": _change_pct(e["cur"], e["prev"]),
                   "series": _fill_series(e["by_day"], end_day, days)}
            for f in extra:
                row[f] = e.get(f, "")
            out.append(row)
        return out

    cur_total = sum(total_by_day.values()) or sum(a["cur"] for a in apps.values())
    prev_total = sum(a["prev"] for a in apps.values())
    return {
        "window": window, "days": days,
        "series": _fill_series(total_by_day or {d: sum(a["by_day"].get(d, 0) for a in apps.values())
                                                for d in {s.day for s in rows if s.day >= cur_since}},
                               end_day, days),
        "top_apps": _rank(apps, ("category", "source_type")),
        "top_clients": _rank(clients, ("device_type",)),
        "summary": {
            "total_bytes": int(cur_total), "prev_total_bytes": int(prev_total),
            "change_pct": _change_pct(int(cur_total), int(prev_total)),
            "active_apps": len(cur_apps), "active_clients": len(cur_clients),
            "avg_daily_bytes": int(cur_total / days) if days else 0,
        },
    }


def _entity_trend(db: Session, tenant_id: str, iids: list[str], dim: str,
                  key: str, window: str) -> dict:
    """Daily series for ONE app/client/total across the window (drilldown over time)."""
    days = _window_days(window)
    end_day = _day_bucket(_now())
    cur_since = end_day - timedelta(days=days - 1)
    prev_since = cur_since - timedelta(days=days)
    rows = (db.query(NetworkSample)
            .filter(NetworkSample.tenant_id == tenant_id,
                    NetworkSample.integration_id.in_(iids),
                    NetworkSample.dim == dim, NetworkSample.key == key,
                    NetworkSample.day >= prev_since).all()) if iids else []
    by_day: dict = {}
    cur = prev = 0
    name = ""
    for s in rows:
        b = int(s.total_bytes or 0)
        if s.name:
            name = s.name
        if s.day >= cur_since:
            by_day[s.day] = by_day.get(s.day, 0) + b
            cur += b
        else:
            prev += b
    return {"dim": dim, "key": key, "name": name, "window": window, "days": days,
            "total_bytes": int(cur), "prev_bytes": int(prev),
            "change_pct": _change_pct(int(cur), int(prev)),
            "series": _fill_series(by_day, end_day, days)}


@router.get("/{iid}/analytics")
def integration_analytics(iid: str, window: str = "30d",
                          principal: security.Principal = Depends(security.get_principal),
                          db: Session = Depends(get_db)):
    """Trends + top movers for ONE integration over the chosen window."""
    inst = _owned_instance(db, principal, iid)
    return _network_analytics(db, principal.tenant_id, [inst.id], window)


@router.get("/analytics")
def all_integration_analytics(window: str = "30d",
                              principal: security.Principal = Depends(security.get_principal),
                              db: Session = Depends(get_db)):
    """Trends across all of the caller's integrations."""
    return _network_analytics(db, principal.tenant_id, _user_instance_ids(db, principal), window)


@router.get("/{iid}/trend")
def integration_entity_trend(iid: str, dim: str, key: str = "", window: str = "30d",
                             principal: security.Principal = Depends(security.get_principal),
                             db: Session = Depends(get_db)):
    """Daily series for a single app/client/total in one integration (drilldown)."""
    inst = _owned_instance(db, principal, iid)
    if dim not in ("total", "app", "client"):
        raise HTTPException(400, "dim must be total, app or client")
    return _entity_trend(db, principal.tenant_id, [inst.id], dim, key, window)


def _drilldown(db: Session, principal, iids: list[str]) -> dict:
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
            "mine": sum(1 for c in clients if c.ownership == "personal"),
            "family": sum(1 for c in clients if c.ownership == "family"),
            "organization": sum(1 for c in clients if c.ownership == "organization"),
            "apps": len(apps),
            "bytes": sum(a["total_bytes"] for a in apps),
        },
    }


def _client_view(c: NetworkClient) -> dict:
    return {
        "id": c.id, "name": c.nickname or c.name or c.hostname or c.mac,
        "device_name": c.name or c.hostname or c.mac, "nickname": c.nickname or "",
        "hostname": c.hostname,
        "ip": c.ip, "mac": c.mac, "device_type": c.device_type,
        "is_wired": c.is_wired, "is_guest": c.is_guest,
        "monitor_state": c.monitor_state, "of_interest": c.of_interest,
        "ownership": c.ownership or "", "owner_user_id": c.owner_user_id,
        "total_bytes": int(c.total_bytes or 0),
        "last_seen": c.last_seen.isoformat() if c.last_seen else None,
    }


class ClientState(BaseModel):
    monitor_state: str | None = None  # normal | ignored | monitored
    of_interest: bool | None = None
    nickname: str | None = None
    ownership: str | None = None  # "" | personal | family | organization


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
    if body.nickname is not None:
        c.nickname = body.nickname.strip()[:120]
    if body.ownership is not None:
        owner = body.ownership.strip().lower()
        if owner not in ("", "personal", "family", "organization"):
            raise HTTPException(400, "invalid ownership")
        c.ownership = owner
        # "personal" (mine) binds the device to the assigning user; the broader
        # scopes (family/organization) aren't tied to one person.
        c.owner_user_id = principal.user_id if owner == "personal" else None
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


@router.post("/{iid}/repoll")
def repoll_instance(iid: str,
                    principal: security.Principal = Depends(security.get_principal),
                    db: Session = Depends(get_db)):
    """Request an immediate collection run (the appliance runs it on its next
    poll, bypassing the interval)."""
    inst = _owned_instance(db, principal, iid)
    if not inst.enabled:
        raise HTTPException(409, "the integration is paused")
    inst.repoll_requested = True
    inst.updated_at = _now()
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Appliance data plane (fleet/appliance-token authed)                          #
# --------------------------------------------------------------------------- #
@agent_router.get("/pull")
def appliance_pull(appliance: Appliance = Depends(_agent_appliance),
                   db: Session = Depends(get_db)):
    """The integrations this appliance should run, with decrypted credentials
    (delivered over the appliance's authenticated TLS channel). Only fully
    provisioned integrations collect data — ones mid-setup use the provision flow."""
    insts = (db.query(IntegrationInstance)
             .filter(IntegrationInstance.appliance_id == appliance.id,
                     IntegrationInstance.enabled == True,  # noqa: E712
                     IntegrationInstance.provision_state.in_(("idle", "done"))).all())
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
            "repoll": bool(i.repoll_requested),
        })
    return {"integrations": out}


def _merge_credentials_update(tid: str, inst: IntegrationInstance, update: dict) -> None:
    try:
        cur = credstore.decrypt(tid, inst.credentials) if inst.credentials else {}
    except Exception:
        cur = {}
    cur.update(update)
    if cur.get("api_key"):  # a reusable key exists — the raw login isn't needed
        cur.pop("password", None)
    inst.credentials = credstore.encrypt(tid, cur)


# ---- Interactive provisioning (appliance side) ------------------------------
@agent_router.get("/provision/pending")
def provision_pending(appliance: Appliance = Depends(_agent_appliance),
                      db: Session = Depends(get_db)):
    """Integrations on this appliance that need an auth action: begin the login
    (``start``) or submit the user's verification code (``otp``)."""
    insts = (db.query(IntegrationInstance)
             .filter(IntegrationInstance.appliance_id == appliance.id,
                     IntegrationInstance.provision_state.in_(("starting", "verifying"))).all())
    out = []
    for i in insts:
        try:
            creds = credstore.decrypt(i.tenant_id, i.credentials) if i.credentials else {}
        except Exception:
            creds = {}
        out.append({
            "id": i.id, "integration_type": i.integration_type,
            "config": i.config or {}, "credentials": creds,
            "action": "otp" if i.provision_state == "verifying" else "start",
            "otp": i.provision_otp or "",
        })
    return {"pending": out}


class ProvisionReport(BaseModel):
    integration_id: str
    provision_state: str  # authenticating | awaiting_otp | done | error
    message: str | None = None
    credentials_update: dict | None = None


@agent_router.post("/provision/report")
def provision_report(body: ProvisionReport,
                     appliance: Appliance = Depends(_agent_appliance),
                     db: Session = Depends(get_db)):
    """The appliance reports progress of the interactive handshake."""
    inst = db.get(IntegrationInstance, body.integration_id)
    if not inst or inst.appliance_id != appliance.id:
        raise HTTPException(404, "integration not found for this appliance")
    inst.provision_state = body.provision_state
    inst.provision_message = body.message
    if body.credentials_update:
        _merge_credentials_update(inst.tenant_id, inst, body.credentials_update)
    if body.provision_state in ("awaiting_otp", "done", "error"):
        inst.provision_otp = None  # consumed / no longer valid
    if body.provision_state == "done":
        inst.status = "pending"  # ready to collect on the next data poll
        inst.last_error = None
    inst.updated_at = _now()
    db.commit()
    return {"ok": True}


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
    try:
        _ingest_report(db, tid, inst, body, appliance_id=appliance.id)
        db.commit()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        now = _now()
        msg = _normalize_integration_error("error", str(exc)) or str(exc)
        inst.last_run_at = now
        inst.status = "error"
        inst.last_error = msg[:500]
        inst.repoll_requested = False
        db.add(IntegrationRun(
            tenant_id=tid,
            integration_id=inst.id,
            integration_type=inst.integration_type,
            appliance_id=appliance.id,
            status="error",
            started_at=now,
            finished_at=now,
            clients=0,
            apps=0,
            bytes_seen=0,
            error=inst.last_error,
        ))
        db.commit()
        logger.exception("integration report ingest failed: integration=%s appliance=%s",
                         inst.id, appliance.id)
        # Tenant-attributed audit → Platform Logs shows WHICH customer/integration
        # failed and WHY (feeds the source-problem notification pipeline too).
        try:
            audit.record(db, actor=f"appliance:{appliance.serial}",
                         action="integration.run_failed", tenant_id=tid, resource=inst.id,
                         category="connector", severity="warning",
                         detail={"type": inst.integration_type,
                                 "account": inst.name or inst.integration_type,
                                 "error": inst.last_error, "appliance": appliance.id})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": inst.last_error}


def _parse_dt(v) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v)
        return d.replace(tzinfo=None) if d.tzinfo else d
    except ValueError:
        return None


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default


def _is_auth_error_text(text: str) -> bool:
    s = (text or "").lower()
    return any(t in s for t in (
        "401", "403", "unauthorized", "forbidden", "invalid token",
        "invalid credentials", "access_denied", "invalid_grant", "reauth",
        "expired token", "token expired",
    ))


def _is_timeout_error_text(text: str) -> bool:
    s = (text or "").lower()
    return any(t in s for t in ("timeout", "timed out", "read timeout", "connect timeout"))


def _normalize_integration_error(status: str, error: str | None) -> str | None:
    if status == "ok":
        return None
    msg = (error or "").strip()
    if not msg:
        return "Integration run failed. Please retry or reconnect this integration."
    if _is_auth_error_text(msg):
        return f"Authentication failed: {msg}"
    if _is_timeout_error_text(msg):
        return f"Connection timeout: {msg}"
    return msg


def _ingest_report(db: Session, tid: str, inst: IntegrationInstance,
                   body: IntegrationReport, appliance_id: str | None) -> None:
    now = _now()
    status = (body.status or "").strip().lower()
    status = "ok" if status == "ok" else "error"
    err = _normalize_integration_error(status, body.error)
    inst.last_run_at = now
    inst.status = "active" if status == "ok" else "error"
    inst.last_error = err
    inst.last_stats = body.stats or {}
    inst.repoll_requested = False  # a run just completed; clear any pending re-poll
    if status == "ok":
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
        c.tx_bytes = _safe_int(cd.get("tx_bytes", 0))
        c.rx_bytes = _safe_int(cd.get("rx_bytes", 0))
        c.total_bytes = _safe_int(cd.get("total_bytes", 0)) or (c.tx_bytes + c.rx_bytes)
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
        a.tx_bytes = _safe_int(ad.get("tx_bytes", 0))
        a.rx_bytes = _safe_int(ad.get("rx_bytes", 0))
        a.total_bytes = _safe_int(ad.get("total_bytes", 0)) or (a.tx_bytes + a.rx_bytes)
        a.client_count = _safe_int(ad.get("client_count", 0))
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
        u.tx_bytes = _safe_int(ud.get("tx_bytes", 0))
        u.rx_bytes = _safe_int(ud.get("rx_bytes", 0))
        u.total_bytes = _safe_int(ud.get("total_bytes", 0)) or (u.tx_bytes + u.rx_bytes)
        u.last_seen = _parse_dt(ud.get("last_seen")) or now

    st = body.stats or {}
    db.add(IntegrationRun(
        tenant_id=tid, integration_id=inst.id, integration_type=inst.integration_type,
        appliance_id=appliance_id, status=status, started_at=now, finished_at=now,
        clients=_safe_int(st.get("clients", 0)), apps=_safe_int(st.get("apps", 0)),
        bytes_seen=_safe_int(st.get("bytes_seen", 0)), error=err))
    if status == "ok":
        _roll_daily_samples(db, tid, inst, body, now)


def _day_bucket(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _roll_daily_samples(db: Session, tid: str, inst: IntegrationInstance,
                        body: IntegrationReport, now: datetime) -> None:
    """Record the day's trailing-24h snapshot per app + per client + overall, so
    trends over 90 days can be derived. Latest report of a day wins (SET, not sum)
    — consistent with the collector's rolling-24h window. Denormalizes display
    fields so old rows still render. Bounded: (apps + clients + 1) rows per day."""
    day = _day_bucket(now)
    existing = {(s.dim, s.key): s for s in db.query(NetworkSample).filter(
        NetworkSample.tenant_id == tid, NetworkSample.integration_id == inst.id,
        NetworkSample.day == day).all()}

    def _upsert(dim: str, key: str, **fields) -> None:
        s = existing.get((dim, key))
        if s is None:
            s = NetworkSample(tenant_id=tid, integration_id=inst.id, day=day,
                              dim=dim, key=key)
            db.add(s)
            existing[(dim, key)] = s
        for k, v in fields.items():
            setattr(s, k, v)

    app_total = 0
    for ad in body.apps:
        key = ad.get("app_key")
        if not key:
            continue
        tot = _safe_int(ad.get("total_bytes", 0)) or (
            _safe_int(ad.get("tx_bytes", 0)) + _safe_int(ad.get("rx_bytes", 0)))
        app_total += tot
        _upsert("app", key, name=ad.get("name") or key,
                category=ad.get("category") or "",
                source_type=map_app_to_source(ad.get("name") or "", ad.get("category") or "") or "",
                total_bytes=tot, tx_bytes=_safe_int(ad.get("tx_bytes", 0)),
                rx_bytes=_safe_int(ad.get("rx_bytes", 0)),
                count=_safe_int(ad.get("client_count", 0)))

    client_total = 0
    for cd in body.clients:
        key = cd.get("client_key") or cd.get("mac")
        if not key:
            continue
        tot = _safe_int(cd.get("total_bytes", 0)) or (
            _safe_int(cd.get("tx_bytes", 0)) + _safe_int(cd.get("rx_bytes", 0)))
        client_total += tot
        _upsert("client", key, name=cd.get("name") or cd.get("hostname") or key,
                device_type=cd.get("device_type") or "",
                total_bytes=tot, tx_bytes=_safe_int(cd.get("tx_bytes", 0)),
                rx_bytes=_safe_int(cd.get("rx_bytes", 0)), count=0)

    # Overall: prefer the authoritative site app-total; else the client sum.
    _upsert("total", "", name="All traffic",
            total_bytes=app_total or client_total,
            count=len([c for c in body.clients if (c.get("client_key") or c.get("mac"))]))


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


def _source_display(source_type: str) -> str:
    try:
        from ..connectors import get_connector
        c = get_connector(source_type)
        if c and getattr(c, "display_name", None):
            return c.display_name
    except Exception:  # noqa: BLE001
        pass
    return (source_type or "Source").replace("_", " ").title()


def _source_analytics(db: Session, tenant_id: str | None) -> dict:
    """Data-source usage across customers (or one tenant): which connectors are
    adopted, by how many accounts/users/tenants, how much they protect, and their
    health. Combines ConnectorAccount adoption with protected bytes/objects from the
    current search index (is_current)."""
    aq = db.query(ConnectorAccount)
    if tenant_id:
        aq = aq.filter(ConnectorAccount.tenant_id == tenant_id)
    accounts = aq.all()
    by_type: dict[str, dict] = {}
    health = {"healthy": 0, "reauth": 0, "error": 0, "deactivated": 0}
    users_with_sources: set = set()

    def _entry(st: str) -> dict:
        return by_type.setdefault(st, {
            "source_type": st, "accounts": 0, "users": set(), "tenants": set(),
            "objects": 0, "protected_bytes": 0, "healthy": 0, "issues": 0, "active": 0})

    for a in accounts:
        e = _entry(a.connector_type or "unknown")
        e["accounts"] += 1
        if a.owner_user_id:
            e["users"].add(a.owner_user_id)
            users_with_sources.add(a.owner_user_id)
        e["tenants"].add(a.tenant_id)
        e["objects"] += int(a.last_object_count or 0)
        if not a.active:
            health["deactivated"] += 1
        elif a.auth_status == "needs-reauth":
            health["reauth"] += 1
            e["issues"] += 1
        elif a.last_error:
            health["error"] += 1
            e["issues"] += 1
        else:
            health["healthy"] += 1
            e["healthy"] += 1
        if a.active:
            e["active"] += 1

    # Protected data per source type from the CURRENT search index (deduped rows).
    try:
        sq = db.query(SearchDocument.source_type,
                      func.coalesce(func.sum(SearchDocument.size_bytes), 0),
                      func.count(SearchDocument.id)).filter(SearchDocument.is_current.is_(True))
        if tenant_id:
            sq = sq.filter(SearchDocument.tenant_id == tenant_id)
        for st, byts, cnt in sq.group_by(SearchDocument.source_type).all():
            e = _entry(st or "unknown")
            e["protected_bytes"] = int(byts or 0)
            e["objects"] = int(cnt or 0)  # is_current count = current object count
    except Exception:  # noqa: BLE001 — is_current may not be backfilled yet
        pass

    sources = sorted(
        ({"source_type": e["source_type"], "display_name": _source_display(e["source_type"]),
          "accounts": e["accounts"], "users": len(e["users"]), "tenants": len(e["tenants"]),
          "objects": e["objects"], "protected_bytes": e["protected_bytes"],
          "healthy": e["healthy"], "issues": e["issues"], "active": e["active"]}
         for e in by_type.values()),
        key=lambda x: (-x["protected_bytes"], -x["accounts"], -x["objects"]))
    return {
        "sources": sources,
        "health": health,
        "totals": {
            "connected": len(accounts),
            "source_types": len([s for s in sources if s["accounts"]]),
            "users_with_sources": len(users_with_sources),
            "protected_bytes": sum(s["protected_bytes"] for s in sources),
            "objects": sum(s["objects"] for s in sources),
        },
    }


@admin_router.get("/analytics")
def admin_analytics(scope: str = "platform", tenant_id: str | None = None,
                    window: str = "30d",
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
        # Adopted data sources (connected connectors) usage + health.
        "data_sources": _source_analytics(db, tenant_id if scope == "tenant" else None),
        # 90-day network usage trends (traffic over time + top movers).
        "network_trends": _admin_network_trends(
            db, tenant_id if scope == "tenant" else None, window),
    }


def _admin_network_trends(db: Session, tenant_id: str | None, window: str = "30d",
                          top: int = 10) -> dict:
    """Fleet-wide (or single-tenant) network trends from the 90-day NetworkSample
    rollups: total traffic over time, top apps + shadow (unprotected) services and
    device growth, each with a change vs the preceding equal-length window."""
    days = _window_days(window)
    end_day = _day_bucket(_now())
    cur_since = end_day - timedelta(days=days - 1)
    prev_since = cur_since - timedelta(days=days)
    q = db.query(NetworkSample).filter(NetworkSample.day >= prev_since)
    if tenant_id:
        q = q.filter(NetworkSample.tenant_id == tenant_id)
    rows = q.all()

    total_by_day: dict = {}
    apps: dict = {}
    client_days: dict = {}  # day -> set(client keys) for device-count growth
    for s in rows:
        in_cur = s.day >= cur_since
        b = int(s.total_bytes or 0)
        if s.dim == "total" and in_cur:
            total_by_day[s.day] = total_by_day.get(s.day, 0) + b
        elif s.dim == "app":
            e = apps.setdefault(s.name or s.key, {
                "name": s.name or s.key, "category": s.category or "",
                "source_type": s.source_type or "", "cur": 0, "prev": 0, "by_day": {},
                "tenants": set()})
            e["tenants"].add(s.tenant_id)
            if in_cur:
                e["cur"] += b
                e["by_day"][s.day] = e["by_day"].get(s.day, 0) + b
            else:
                e["prev"] += b
        elif s.dim == "client" and in_cur and b:
            client_days.setdefault(s.day, set()).add(f"{s.tenant_id}:{s.key}")

    ranked = sorted(apps.values(), key=lambda x: -x["cur"])[:top]
    top_apps = [{"name": e["name"], "category": e["category"], "source_type": e["source_type"],
                 "has_source": bool(e["source_type"]), "tenant_count": len(e["tenants"]),
                 "total_bytes": e["cur"], "change_pct": _change_pct(e["cur"], e["prev"]),
                 "series": _fill_series(e["by_day"], end_day, days)} for e in ranked]
    cur_total = sum(total_by_day.values()) or sum(e["cur"] for e in apps.values())
    prev_total = sum(e["prev"] for e in apps.values())
    device_series = [{"day": d.date().isoformat(), "count": len(client_days.get(d, set()))}
                     for d in (end_day - timedelta(days=i) for i in range(days - 1, -1, -1))]
    return {
        "window": window, "days": days,
        "series": _fill_series(total_by_day, end_day, days),
        "top_apps": top_apps,
        "device_series": device_series,
        "summary": {"total_bytes": int(cur_total), "change_pct": _change_pct(int(cur_total), int(prev_total)),
                    "active_devices": len(set().union(*client_days.values())) if client_days else 0},
    }
