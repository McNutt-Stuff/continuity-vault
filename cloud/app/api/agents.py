"""
Desktop agent endpoints.

Tenant-facing: create linking codes, list agents, push commands, edit config.
Agent-facing (outbound-only, bearer agent token): activate, heartbeat (with
telemetry + config + pending command), and ingest (push locally-collected,
normalized objects into the protection pipeline).
"""

from __future__ import annotations

import base64
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, fleet, keybroker, security
from ..config import get_settings
from ..connectors import get_connector
from ..connectors.base import SourceObject
from ..db import get_db
from ..models import Collection, DesktopAgent, LinkingCode, Tenant, Vault
from ..workers.sync_worker import ingest_objects

settings = get_settings()

# Prototype agent bearer tokens (agent_token -> agent_id).
_agent_tokens: dict[str, str] = {}


def _now():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Tenant-facing management
# --------------------------------------------------------------------------

fleet_router = APIRouter(prefix="/agents", tags=["agents"])

DEFAULT_AGENT_CONFIG = {
    "collectors": ["onepassword"],
    "destinations": ["cv-cloud"],
    "schedule_minutes": 360,
    "appliance_endpoint": None,  # set to push directly to an appliance ingest gateway
}


class CreateAgentCode(BaseModel):
    name: str = "Mac Agent"
    collectors: list[str] = ["onepassword"]
    destinations: list[str] = ["cv-cloud"]
    schedule_minutes: int = 360


@fleet_router.post("/linking-code")
def create_agent_code(body: CreateAgentCode,
                      principal: security.Principal = Depends(security.require_security_admin),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    code = f"AG-{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}"
    lc = LinkingCode(
        tenant_id=tenant.id, code=code, kind="agent", name=body.name,
        expires_at=_now() + timedelta(seconds=settings.linking_code_ttl_seconds),
    )
    db.add(lc)
    db.commit()
    audit.record(db, actor=principal.user_id, action="agent.linking_code_created",
                 tenant_id=tenant.id, detail={"name": body.name})
    return {"code": code, "expires_at": lc.expires_at.isoformat(),
            "config": {"collectors": body.collectors, "destinations": body.destinations,
                       "schedule_minutes": body.schedule_minutes}}


@fleet_router.post("/installer")
def download_installer(body: CreateAgentCode,
                       principal: security.Principal = Depends(security.require_passkey),
                       tenant: Tenant = Depends(security.get_tenant),
                       db: Session = Depends(get_db)):
    """Create a linking code and return a self-contained macOS installer with the
    code + cloud URL baked in (the UI saves it as a .command file)."""
    code = f"AG-{secrets.token_hex(3).upper()}-{secrets.token_hex(3).upper()}"
    lc = LinkingCode(
        tenant_id=tenant.id, code=code, kind="agent", name=body.name,
        expires_at=_now() + timedelta(seconds=settings.linking_code_ttl_seconds),
    )
    db.add(lc)
    db.commit()

    template_path = Path(__file__).resolve().parents[3] / "installers" / "desktop-agent-install-macos.sh"
    try:
        template = template_path.read_text()
    except Exception:
        template = "#!/usr/bin/env bash\necho 'installer template unavailable'\nexit 1\n"
    body_script = template.split("\n", 1)[1] if template.startswith("#!") else template
    header = (
        "#!/usr/bin/env bash\n"
        "# Arkive Desktop Agent — one-click installer (linking code baked in).\n"
        f'export ARKIVE_CLOUD_URL="{settings.api_base_url}"\n'
        f'export ARKIVE_LINKING_CODE="{code}"\n'
        'export ARKIVE_REPO_URL="https://github.com/mcnutter1/continuity-vault.git"\n\n'
    )
    audit.record(db, actor=principal.user_id, action="agent.installer_downloaded",
                 tenant_id=tenant.id, detail={"code": code})
    return {"code": code, "filename": "arkive-agent-installer.command",
            "script": header + body_script}


@fleet_router.get("")
def list_agents(tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    rows = db.query(DesktopAgent).filter(DesktopAgent.tenant_id == tenant.id).all()
    return [_agent_view(a) for a in rows]


@fleet_router.get("/{agent_id}")
def get_agent(agent_id: str, tenant: Tenant = Depends(security.get_tenant),
              db: Session = Depends(get_db)):
    a = db.get(DesktopAgent, agent_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "agent not found")
    return _agent_view(a)


class AgentCommand(BaseModel):
    type: str  # collect | update | reconfigure | quarantine
    params: dict = {}


@fleet_router.post("/{agent_id}/command")
def command(agent_id: str, body: AgentCommand,
            principal: security.Principal = Depends(security.require_security_admin),
            tenant: Tenant = Depends(security.get_tenant),
            db: Session = Depends(get_db)):
    a = db.get(DesktopAgent, agent_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "agent not found")
    a.pending_command = {"type": body.type, "params": body.params}
    db.commit()
    audit.record(db, actor=principal.user_id, action="agent.command",
                 tenant_id=tenant.id, resource=a.id, detail={"type": body.type})
    return {"ok": True, "queued": body.type}


class AgentConfigUpdate(BaseModel):
    destinations: list[str] | None = None
    schedule_minutes: int | None = None
    collectors: list[str] | None = None
    appliance_endpoint: str | None = None


@fleet_router.put("/{agent_id}/config")
def update_config(agent_id: str, body: AgentConfigUpdate,
                  principal: security.Principal = Depends(security.require_security_admin),
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    a = db.get(DesktopAgent, agent_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "agent not found")
    cfg = dict(a.config or {})
    for k, v in body.dict(exclude_none=True).items():
        cfg[k] = v
    a.config = cfg
    db.commit()
    return {"ok": True, "config": cfg}


def _agent_view(a: DesktopAgent) -> dict:
    return {
        "id": a.id, "name": a.name, "platform": a.platform, "hostname": a.hostname,
        "version": a.version, "state": a.state, "collectors": a.collectors,
        "config": a.config, "telemetry": a.telemetry,
        "last_heartbeat_at": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None,
        "last_collection_at": a.last_collection_at.isoformat() if a.last_collection_at else None,
    }


# --------------------------------------------------------------------------
# Agent-facing (outbound-only)
# --------------------------------------------------------------------------

agent_router = APIRouter(prefix="/agent", tags=["agent"])


def _auth_agent(db: Session, authorization: str = Header(default="")) -> DesktopAgent:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing agent token")
    agent_id = _agent_tokens.get(authorization.split(" ", 1)[1])
    if not agent_id:
        raise HTTPException(401, "invalid agent token")
    a = db.get(DesktopAgent, agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    return a


class AgentActivate(BaseModel):
    linking_code: str
    hostname: str
    platform: str = "macos"
    version: str = "1.0.0"
    collectors: list[str] = ["onepassword"]
    identity_bundle: dict | None = None


@agent_router.post("/activate")
def activate(body: AgentActivate, db: Session = Depends(get_db)):
    lc = db.query(LinkingCode).filter(LinkingCode.code == body.linking_code,
                                      LinkingCode.kind == "agent").first()
    if not lc or lc.consumed or lc.expires_at < _now():
        raise HTTPException(400, "invalid or expired linking code")

    agent = DesktopAgent(
        tenant_id=lc.tenant_id, name=lc.name or "Desktop Agent",
        platform=body.platform, hostname=body.hostname, version=body.version,
        collectors=body.collectors, config=dict(DEFAULT_AGENT_CONFIG),
        identity_bundle=body.identity_bundle, state="active",
        last_heartbeat_at=_now(),
    )
    agent.config["collectors"] = body.collectors
    db.add(agent)
    lc.consumed = True
    db.commit()
    db.refresh(agent)

    token = secrets.token_urlsafe(32)
    _agent_tokens[token] = agent.id
    audit.record(db, actor=f"agent:{body.hostname}", action="agent.activated",
                 tenant_id=lc.tenant_id, resource=agent.id)

    # Recovery public key for endpoint (client-side) encryption escrow.
    vault = db.query(Vault).filter(Vault.tenant_id == lc.tenant_id).first()
    recovery = keybroker.provision_recovery_keypair(vault.id) if vault else {}

    return {
        "agent_id": agent.id, "agent_token": token, "tenant_id": lc.tenant_id,
        "config": agent.config, "cloud_public_bundle": fleet.cloud_public_bundle(),
        "recovery_public_key": recovery.get("public_key"),
        "recovery_kem_alg": recovery.get("kem_alg"),
        "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
    }


class RegisterKey(BaseModel):
    wrapped_key: dict


@agent_router.post("/register-key")
def register_key(body: RegisterKey, agent: DesktopAgent = Depends(_auth_agent),
                 db: Session = Depends(get_db)):
    """Escrow the agent's client-side data key (wrapped to the recovery key)."""
    cfg = dict(agent.config or {})
    cfg["escrow_wrapped_key"] = body.wrapped_key
    agent.config = cfg
    db.commit()
    audit.record(db, actor=f"agent:{agent.hostname}", action="agent.key_escrowed",
                 tenant_id=agent.tenant_id, resource=agent.id)
    return {"ok": True}


class AgentHeartbeat(BaseModel):
    state: str = "active"
    version: str = "1.0.0"
    telemetry: dict = {}


@agent_router.post("/heartbeat")
def heartbeat(body: AgentHeartbeat, agent: DesktopAgent = Depends(_auth_agent),
              db: Session = Depends(get_db)):
    agent.state = body.state
    agent.version = body.version
    agent.telemetry = body.telemetry
    agent.last_heartbeat_at = _now()
    command = agent.pending_command
    agent.pending_command = None  # consume
    db.commit()
    return {"config": agent.config, "command": command,
            "next_heartbeat_seconds": settings.heartbeat_interval_seconds}


class AgentObject(BaseModel):
    object_id: str
    kind: str
    title: str
    content_b64: str
    preview: str = ""
    meta: dict = {}
    labels: list[str] = []
    size_bytes: int = 0
    client_encrypted: bool = False


class AgentIngest(BaseModel):
    source_type: str = "onepassword"
    collection_name: str | None = None
    destinations: list[str] | None = None
    objects: list[AgentObject]


@agent_router.post("/ingest")
def ingest(body: AgentIngest, agent: DesktopAgent = Depends(_auth_agent),
           db: Session = Depends(get_db)):
    vault = db.query(Vault).filter(Vault.tenant_id == agent.tenant_id).first()
    if not vault:
        raise HTTPException(400, "no vault provisioned for tenant")

    name = body.collection_name or f"{body.source_type} ({agent.hostname})"
    collection = (db.query(Collection)
                  .filter(Collection.tenant_id == agent.tenant_id,
                          Collection.vault_id == vault.id,
                          Collection.source_type == body.source_type,
                          Collection.name == name).first())
    if not collection:
        collection = Collection(tenant_id=agent.tenant_id, vault_id=vault.id,
                                name=name, source_type=body.source_type,
                                sensitivity="restricted")
        db.add(collection)
        db.commit()
        db.refresh(collection)

    source_objects = [
        SourceObject(object_id=o.object_id, doc_type=o.kind, title=o.title,
                     content=base64.b64decode(o.content_b64), preview=o.preview,
                     meta={**o.meta, "client_encrypted": o.client_encrypted},
                     labels=o.labels, size_bytes=o.size_bytes)
        for o in body.objects
    ]
    dests = body.destinations or agent.config.get("destinations") or ["cv-cloud"]
    searchable = None
    conn = get_connector(body.source_type)
    if conn:
        searchable = conn.capabilities().searchable_fields

    receipt = ingest_objects(db, collection, source_objects, dests,
                             searchable_fields=searchable, actor=f"agent:{agent.hostname}")
    agent.last_collection_at = _now()
    db.commit()
    audit.record(db, actor=f"agent:{agent.hostname}", action="agent.ingest",
                 tenant_id=agent.tenant_id, resource=collection.id,
                 detail={"objects": len(source_objects), "destinations": dests})
    return {"snapshot_id": receipt.snapshot_id, "object_count": receipt.object_count,
            "recoverable": receipt.recoverable, "destinations": dests}


class AgentCommandResult(BaseModel):
    type: str
    ok: bool
    detail: dict = {}


@agent_router.post("/command-result")
def command_result(body: AgentCommandResult, agent: DesktopAgent = Depends(_auth_agent),
                   db: Session = Depends(get_db)):
    audit.record(db, actor=f"agent:{agent.hostname}", action="agent.command_result",
                 tenant_id=agent.tenant_id, resource=agent.id,
                 detail={"type": body.type, "ok": body.ok})
    return {"ok": True}
