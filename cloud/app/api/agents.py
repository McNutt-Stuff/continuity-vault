"""
Desktop agent endpoints.

Tenant-facing: create linking codes, list agents, push commands, edit config.
Agent-facing (outbound-only, bearer agent token): activate, heartbeat (with
telemetry + config + pending command), and ingest (push locally-collected,
normalized objects into the protection pipeline).
"""

from __future__ import annotations

import base64
import io
import hashlib
import logging
import secrets
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
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
logger = logging.getLogger("cv.agents")

# In-memory fast path (agent_token -> agent_id); the durable source of truth is
# the sha256 hash persisted on the DesktopAgent row, so tokens survive restarts.
_agent_tokens: dict[str, str] = {}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now():
    # Naive UTC to match SQLAlchemy DateTime columns (TIMESTAMP WITHOUT TIME ZONE),
    # which Postgres returns tz-naive — avoids naive/aware comparison TypeErrors.
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Tenant-facing management
# --------------------------------------------------------------------------

fleet_router = APIRouter(prefix="/agents", tags=["agents"])

DEFAULT_AGENT_CONFIG = {
    "collectors": ["onepassword"],
    "destinations": ["cv-cloud"],
    "schedule_minutes": 360,
    "appliance_endpoint": None,  # set to push directly to an appliance ingest gateway
    "verbose_logging": False,  # advanced: DEBUG-level agent logging
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

    audit.record(db, actor=principal.user_id, action="agent.installer_downloaded",
                 tenant_id=tenant.id, detail={"code": code})
    # Tiny, self-contained .command: no git/Xcode — curls the cloud bootstrap.
    api = settings.api_base_url.rstrip("/")
    script = (
        "#!/bin/bash\n"
        "# Arkive Desktop Agent — one-click installer (downloads from the cloud).\n"
        f'export ARKIVE_CLOUD_URL="{api}"\n'
        f'export ARKIVE_LINKING_CODE="{code}"\n'
        'curl -fsSL "$ARKIVE_CLOUD_URL/agent/bootstrap" -o /tmp/arkive-bootstrap.sh '
        '&& bash /tmp/arkive-bootstrap.sh\n'
    )
    return {"code": code, "filename": "arkive-agent-installer.command", "script": script}


@fleet_router.get("")
def list_agents(tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    rows = db.query(DesktopAgent).filter(
        DesktopAgent.tenant_id == tenant.id,
        DesktopAgent.state != "retired").all()
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
    a.enqueue_command({"type": body.type, "params": body.params})
    db.commit()
    audit.record(db, actor=principal.user_id, action="agent.command",
                 tenant_id=tenant.id, resource=a.id, detail={"type": body.type})
    return {"ok": True, "queued": body.type}


class AgentConfigUpdate(BaseModel):
    destinations: list[str] | None = None
    schedule_minutes: int | None = None
    collectors: list[str] | None = None
    appliance_endpoint: str | None = None
    verbose_logging: bool | None = None


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


class FsScanRequest(BaseModel):
    path: str = ""  # "" = list roots/drives
    rebuild: bool = False  # force a fresh index rebuild (vs. serve the cache)


@fleet_router.post("/{agent_id}/fs-scan")
def request_fs_scan(agent_id: str, body: FsScanRequest,
                    principal: security.Principal = Depends(security.get_principal),
                    tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    """Ask the agent for its cached folder tree (served instantly from its index)
    or, with ``rebuild``, to rebuild the index. The agent answers on its next
    heartbeat; its background indexer also pushes a fresh tree periodically."""
    a = db.get(DesktopAgent, agent_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "agent not found")
    request_id = secrets.token_hex(8)
    a.enqueue_command({"type": "scan_fs",
                       "params": {"path": body.path, "request_id": request_id,
                                  "rebuild": body.rebuild}})
    db.commit()
    return {"request_id": request_id, "queued": True,
            "message": "The agent serves its cached folder index on its next heartbeat."}


@fleet_router.get("/{agent_id}/fs-scan")
def get_fs_scan(agent_id: str, tenant: Tenant = Depends(security.get_tenant),
                db: Session = Depends(get_db)):
    a = db.get(DesktopAgent, agent_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "agent not found")
    return {"scan": a.last_scan}


class FsExpandRequest(BaseModel):
    path: str


@fleet_router.post("/{agent_id}/fs-expand")
def request_fs_expand(agent_id: str, body: FsExpandRequest,
                      principal: security.Principal = Depends(security.get_principal),
                      tenant: Tenant = Depends(security.get_tenant),
                      db: Session = Depends(get_db)):
    """Ask the agent to scan one folder's immediate children on demand, so the
    portal can expand the tree lazily (no giant up-front index)."""
    a = db.get(DesktopAgent, agent_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "agent not found")
    if not (body.path or "").strip():
        raise HTTPException(400, "path required")
    request_id = secrets.token_hex(8)
    a.enqueue_command({"type": "scan_fs",
                       "params": {"path": body.path, "request_id": request_id}})
    db.commit()
    return {"request_id": request_id, "queued": True}


@fleet_router.get("/{agent_id}/fs-expand")
def get_fs_expand(agent_id: str, path: str = "",
                  tenant: Tenant = Depends(security.get_tenant),
                  db: Session = Depends(get_db)):
    a = db.get(DesktopAgent, agent_id)
    if not a or a.tenant_id != tenant.id:
        raise HTTPException(404, "agent not found")
    return {"expansion": (a.fs_expansions or {}).get(path)}


def _agent_view(a: DesktopAgent) -> dict:
    return {
        "id": a.id, "name": a.name, "platform": a.platform, "hostname": a.hostname,
        "version": a.version, "state": a.state, "collectors": a.collectors,
        "config": a.config, "telemetry": a.telemetry,
        "last_heartbeat_at": a.last_heartbeat_at.isoformat() if a.last_heartbeat_at else None,
        "last_collection_at": a.last_collection_at.isoformat() if a.last_collection_at else None,
    }


def _record_discovered_collector(db: Session, agent: DesktopAgent, source_type: str) -> None:
    """Remember a collector the agent can provide so the operator can add it as a
    source in the portal (discovery-then-approve, no silent auto-create)."""
    cols = list(agent.collectors or [])
    if source_type and source_type not in cols:
        cols.append(source_type)
        agent.collectors = cols
        db.commit()


# --------------------------------------------------------------------------
# Agent-facing (outbound-only)
# --------------------------------------------------------------------------

agent_router = APIRouter(prefix="/agent", tags=["agent"])


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@agent_router.get("/bootstrap")
def bootstrap():
    """Serve the macOS bootstrap installer (no auth — it's the open agent code)."""
    path = _repo_root() / "installers" / "agent-bootstrap-macos.sh"
    try:
        return PlainTextResponse(path.read_text())
    except Exception:
        raise HTTPException(404, "bootstrap unavailable")


_EXCLUDE = (".venv", "__pycache__", "node_modules", ".git", ".pyc")

_bundle_version_cache: str | None = None


def _agent_bundle_version() -> str:
    """Stable content hash of the agent bundle; changes only when code changes.

    Cached for the process lifetime (a deploy restarts the process), so heartbeats
    don't re-hash every call. The agent compares this to its installed VERSION and
    self-updates when they differ."""
    global _bundle_version_cache
    if _bundle_version_cache:
        return _bundle_version_cache
    root = _repo_root()
    h = hashlib.sha256()
    for d in ("desktop-agent", "shared"):
        base = root / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and not any(x in str(f) for x in _EXCLUDE) \
                    and f.name != "VERSION":
                h.update(str(f.relative_to(root)).encode())
                try:
                    h.update(f.read_bytes())
                except Exception:
                    pass
    _bundle_version_cache = h.hexdigest()[:12]
    return _bundle_version_cache


@agent_router.get("/bundle")
def bundle():
    """Serve the agent code (desktop-agent + shared) as a tar.gz."""
    root = _repo_root()
    buf = io.BytesIO()

    def _filter(ti: tarfile.TarInfo):
        return None if any(x in ti.name for x in _EXCLUDE) else ti

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for d in ("desktop-agent", "shared"):
            p = root / d
            if p.exists():
                tar.add(str(p), arcname=d, filter=_filter)
        # Stamp the stable bundle version so the agent can detect when to update.
        version = _agent_bundle_version().encode()
        vi = tarfile.TarInfo("desktop-agent/VERSION")
        vi.size = len(version)
        tar.addfile(vi, io.BytesIO(version))
    return Response(content=buf.getvalue(), media_type="application/gzip",
                    headers={"Content-Disposition": "attachment; filename=arkive-agent.tar.gz"})


def _auth_agent(authorization: str = Header(default=""),
                db: Session = Depends(get_db)) -> DesktopAgent:
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing agent token")
    token = authorization.split(" ", 1)[1]
    a = None
    agent_id = _agent_tokens.get(token)
    if agent_id:
        a = db.get(DesktopAgent, agent_id)
    if a is None:
        # Fall back to the durable hash so tokens survive cloud restarts.
        a = db.query(DesktopAgent).filter(
            DesktopAgent.agent_token_hash == _hash_token(token)).first()
        if a:
            _agent_tokens[token] = a.id  # repopulate fast path
    if not a:
        raise HTTPException(401, "invalid agent token")
    if a.state == "retired":
        raise HTTPException(401, "agent retired")
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

    # Retire any prior agents for this same host so stale installs disappear from
    # the fleet and can no longer act (their tokens are invalidated).
    prior = db.query(DesktopAgent).filter(
        DesktopAgent.tenant_id == lc.tenant_id,
        DesktopAgent.hostname == body.hostname,
        DesktopAgent.state != "retired",
    ).all()
    for p in prior:
        p.state = "retired"
        p.agent_token_hash = None
        p.clear_commands()

    # Provision escrow material BEFORE consuming the code so a transient failure
    # never burns a single-use linking code.
    vault = db.query(Vault).filter(Vault.tenant_id == lc.tenant_id).first()
    recovery = keybroker.provision_recovery_keypair(vault.id) if vault else {}
    cloud_bundle = fleet.cloud_public_bundle()

    db.add(agent)
    lc.consumed = True
    db.commit()
    db.refresh(agent)

    token = secrets.token_urlsafe(32)
    agent.agent_token_hash = _hash_token(token)
    db.commit()
    _agent_tokens[token] = agent.id
    audit.record(db, actor=f"agent:{body.hostname}", action="agent.activated",
                 tenant_id=lc.tenant_id, resource=agent.id)

    return {
        "agent_id": agent.id, "agent_token": token, "tenant_id": lc.tenant_id,
        "config": agent.config, "cloud_public_bundle": cloud_bundle,
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


def _agent_mappings(db: Session, agent: DesktopAgent) -> list[dict]:
    """The agent-collected sources this agent owns, with the cadence + selection
    the operator set in the Data Map. Agent sources are PUSH: the agent runs these
    on its own timer (only when it's online and can reach the data) instead of the
    cloud queuing collects blindly on a schedule."""
    default_min = max(1, get_settings().sync_interval_minutes)
    out: list[dict] = []
    for c in (db.query(Collection)
              .filter(Collection.tenant_id == agent.tenant_id).all()):
        conn = get_connector(c.source_type)
        if not (conn and conn.capabilities().requires_agent):
            continue  # cloud-pull sources are scheduled server-side
        if c.agent_id and c.agent_id != agent.id:
            continue  # bound to a different agent
        if not c.agent_id and c.source_type not in (agent.collectors or []):
            continue  # unbound and this agent can't collect it
        interval = (default_min if c.backup_interval_minutes is None
                    else c.backup_interval_minutes)
        out.append({
            "collection_id": c.id,
            "source_type": c.source_type,
            "interval_minutes": interval,  # 0 = manual only (no auto cadence)
            "file_config": c.config or {},
            "destinations": c.destinations or ["cv-cloud"],
        })
    return out


@agent_router.post("/heartbeat")
def heartbeat(body: AgentHeartbeat, request: Request,
              agent: DesktopAgent = Depends(_auth_agent),
              db: Session = Depends(get_db)):
    fwd = request.headers.get("x-forwarded-for", "")
    public_ip = (fwd.split(",")[0].strip() if fwd
                 else (request.client.host if request.client else None))
    tel = dict(body.telemetry or {})
    if public_ip:
        tel["public_ip"] = public_ip
    agent.state = body.state
    agent.version = body.version
    agent.telemetry = tel
    # Let the agent advertise which collectors it supports so new capabilities
    # (e.g. endpoint files) appear for already-linked agents after they update.
    advertised = tel.get("collectors")
    if isinstance(advertised, list) and advertised:
        merged = list(dict.fromkeys([*(agent.collectors or []), *advertised]))
        if merged != (agent.collectors or []):
            agent.collectors = merged
    agent.last_heartbeat_at = _now()
    # Drain several commands per heartbeat so interactive work (folder expansion)
    # isn't throttled to one per cycle. `command` (singular) stays for older agents.
    commands = []
    for _ in range(25):
        c = agent.dequeue_command()
        if not c:
            break
        commands.append(c)
    db.commit()
    return {"config": agent.config,
            "command": commands[0] if commands else None,
            "commands": commands,
            "pending_more": agent.has_pending_command,
            "mappings": _agent_mappings(db, agent),
            "latest_version": _agent_bundle_version(),
            "next_heartbeat_seconds": settings.heartbeat_interval_seconds}


@agent_router.post("/deregister")
def deregister(agent: DesktopAgent = Depends(_auth_agent),
               db: Session = Depends(get_db)):
    """Retire this agent (called by the installer before a clean reinstall)."""
    agent.state = "retired"
    agent.agent_token_hash = None
    agent.clear_commands()
    db.commit()
    audit.record(db, actor=f"agent:{agent.hostname}", action="agent.deregistered",
                 tenant_id=agent.tenant_id, resource=agent.id)
    return {"ok": True, "state": "retired"}


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
    content_hash: str | None = None  # sha256 of plaintext (for versioning/dedup)


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

    # Sources are created by the operator in the cloud and bound to this agent.
    # The agent NEVER auto-creates a source — that caused duplicate entries. If no
    # source is configured yet, the discovered collector is recorded on the agent
    # and the push is skipped until the operator adds it in the portal.
    matches = (db.query(Collection)
               .filter(Collection.tenant_id == agent.tenant_id,
                       Collection.agent_id == agent.id,
                       Collection.source_type == body.source_type)
               .order_by(Collection.created_at.desc()).all())
    if len(matches) > 1:
        # Duplicate mappings for the same source route non-deterministically —
        # some pushes could land at a different (e.g. cloud) destination. Use the
        # most recently created one (the mapping the operator configured last) and
        # flag it so the operator can remove the stale duplicates.
        logger.warning("agent %s has %d mappings for source %s (%s) — using %s; "
                       "remove duplicates in the Data Map to avoid split routing",
                       agent.id, len(matches), body.source_type,
                       [f"{c.id}:{c.destinations}" for c in matches], matches[0].id)
    collection = matches[0] if matches else None
    if not collection:
        # Fall back to a legacy agent-collected source (bound before agent_id
        # existed) matched by source type + no connector account, then adopt it.
        legacy = (db.query(Collection)
                  .filter(Collection.tenant_id == agent.tenant_id,
                          Collection.source_type == body.source_type,
                          Collection.agent_id.is_(None),
                          Collection.connector_account_id.is_(None)).first())
        if legacy:
            legacy.agent_id = agent.id
            db.commit()
            collection = legacy

    if not collection:
        _record_discovered_collector(db, agent, body.source_type)
        return {"status": "unconfigured", "object_count": 0,
                "message": f"'{body.source_type}' is not configured for this agent yet. "
                           "Add it from the portal (Data Map → add source)."}

    source_objects = [
        SourceObject(object_id=o.object_id, doc_type=o.kind, title=o.title,
                     content=base64.b64decode(o.content_b64), preview=o.preview,
                     meta={**o.meta, "client_encrypted": o.client_encrypted},
                     labels=o.labels, size_bytes=o.size_bytes,
                     content_hash=o.content_hash)
        for o in body.objects
    ]
    # The operator-created source→vault mapping drives routing; changing it in the
    # portal reroutes subsequent syncs without touching the Mac.
    dests = collection.destinations or ["cv-cloud"]
    searchable = None
    facets = None
    conn = get_connector(body.source_type)
    if conn:
        caps = conn.capabilities()
        searchable = caps.searchable_fields
        facets = caps.facet_fields

    receipt = ingest_objects(db, collection, source_objects, dests,
                             searchable_fields=searchable, facet_fields=facets,
                             actor=f"agent:{agent.hostname}")
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


class FsScanResultBody(BaseModel):
    request_id: str = ""
    ok: bool = True
    error: str | None = None
    result: dict = {}


@agent_router.post("/fs-scan-result")
def fs_scan_result(body: FsScanResultBody, agent: DesktopAgent = Depends(_auth_agent),
                   db: Session = Depends(get_db)):
    """Store the folder tree the agent reported for a scan request so the portal
    can render it in the Data Map file picker."""
    scan = dict(body.result or {})
    scan["request_id"] = body.request_id
    scan["ok"] = body.ok
    scan["at"] = _now().isoformat()
    if body.error:
        scan["error"] = body.error
    agent.last_scan = scan
    db.commit()
    return {"ok": True}


class FsExpandResultBody(BaseModel):
    request_id: str = ""
    path: str = ""
    ok: bool = True
    error: str | None = None
    result: dict = {}


@agent_router.post("/fs-expand-result")
def fs_expand_result(body: FsExpandResultBody, agent: DesktopAgent = Depends(_auth_agent),
                     db: Session = Depends(get_db)):
    """Store one folder's on-demand children so the portal can expand it lazily."""
    exps = dict(agent.fs_expansions or {})
    exps[body.path] = {"request_id": body.request_id, "ok": body.ok,
                       "error": body.error, "result": body.result,
                       "at": _now().isoformat()}
    # Cap growth: keep the most recent expansions only.
    if len(exps) > 400:
        for k in sorted(exps, key=lambda k: exps[k].get("at", ""))[:len(exps) - 400]:
            exps.pop(k, None)
    agent.fs_expansions = exps
    db.commit()
    return {"ok": True}
