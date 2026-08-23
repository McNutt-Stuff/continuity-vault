"""
Public marketing-site content API + admin CMS + fleet-node communication.

- ``GET  /site``                public: the editable marketing content (no auth)
- ``GET  /admin/site``         admin: current content for the editor
- ``PUT  /admin/site``         admin: save content
- ``POST /nodes/heartbeat``    node: a customer-tenant / public-web node reports
                               health + cloud info and receives its role blueprint
                               (config, settings, target version). Shared-secret auth.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, security
from ..config import get_settings
from ..db import get_db
from ..models import Node, NodeBlueprint, SiteContent
from ..site_defaults import DEFAULT_SITE

public_router = APIRouter(tags=["site"])          # unauthenticated (site + node heartbeat)
admin_router = APIRouter(prefix="/admin", tags=["site-admin"],
                         dependencies=[Depends(security.require_platform_admin)])


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _content_row(db: Session) -> SiteContent:
    row = db.get(SiteContent, "default")
    if row is None:
        row = SiteContent(id="default", content=DEFAULT_SITE)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@public_router.get("/site")
def get_site(db: Session = Depends(get_db)):
    """Public marketing content + public-safe platform pricing (so the marketing
    site's plans/hardware reflect the pricing defined in the admin). Merged over
    defaults so partial edits are safe."""
    row = _content_row(db)
    merged = {**DEFAULT_SITE, **(row.content or {})}
    return {"content": merged, "pricing": _public_pricing(db),
            "published": bool(row.published),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


def _public_pricing(db: Session) -> dict:
    """Public-safe slice of the platform pricing model (no secrets)."""
    from .billing import get_pricing, pricing_public
    pr = pricing_public(get_pricing(db))
    return {
        "currency": pr["currency"],
        "license_plans": pr["license_plans"],
        "cloud_price_per_tb_month": pr["cloud_price_per_tb_month"],
        "s3_price_per_tb_month": pr["s3_price_per_tb_month"],
        "azure_price_per_tb_month": pr["azure_price_per_tb_month"],
        "appliance_tiers": pr["appliance_tiers"],
    }


@admin_router.get("/site")
def admin_get_site(db: Session = Depends(get_db)):
    row = _content_row(db)
    return {"content": row.content or DEFAULT_SITE, "defaults": DEFAULT_SITE,
            "published": bool(row.published),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


class SiteUpdate(BaseModel):
    content: dict | None = None
    published: bool | None = None


@admin_router.put("/site")
def admin_put_site(body: SiteUpdate,
                   principal: security.Principal = Depends(security.require_platform_admin),
                   db: Session = Depends(get_db)):
    row = _content_row(db)
    if body.content is not None:
        row.content = body.content
    if body.published is not None:
        row.published = body.published
    db.commit()
    audit.record(db, actor=principal.user_id, action="admin.site_updated", category="admin")
    return {"ok": True, "updated_at": row.updated_at.isoformat() if row.updated_at else None}


# --------------------------------------------------------------------------- #
# Fleet node communication                                                    #
# --------------------------------------------------------------------------- #

def _fleet_secret() -> str:
    """The shared secret fleet nodes use to enroll/heartbeat. Prefers the
    explicitly-configured CV_NODE_SECRET; otherwise a persistent secret is
    generated once and stored next to the fleet signer so it survives restarts
    (no env change or DB migration required)."""
    settings = get_settings()
    if settings.node_secret:
        return settings.node_secret
    import os
    signer = os.environ.get("CV_FLEET_SIGNER", "./cv_fleet_signer.json")
    path = Path(signer).resolve().parent / "fleet_secret"
    try:
        if path.exists():
            return path.read_text().strip()
        secret = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret)
        try:
            path.chmod(0o600)
        except Exception:
            pass
        return secret
    except Exception:
        # Read-only FS fallback: derive a stable secret from the session secret.
        return (settings.session_secret or "arkive")[:64]


class NodeHeartbeat(BaseModel):
    name: str
    role: str
    version: str = ""
    endpoint: str = ""
    telemetry: dict = {}
    cloud: dict = {}


def _blueprint_for(db: Session, role: str) -> dict:
    bp = db.get(NodeBlueprint, role)
    if bp is None:
        return {"role": role, "target_version": "", "config": {}, "settings": {}}
    return {"role": bp.role, "target_version": bp.target_version or "",
            "config": bp.config or {}, "settings": bp.settings or {}}


@public_router.post("/nodes/heartbeat")
def node_heartbeat(body: NodeHeartbeat,
                   authorization: str = Header(default=""),
                   db: Session = Depends(get_db)):
    """A non-control-plane node reports in and receives its role blueprint. Auth
    is the shared fleet secret (Bearer) baked into the install command."""
    token = authorization.replace("Bearer ", "").strip()
    if not token or token != _fleet_secret():
        raise HTTPException(401, "invalid node credentials")
    # Upsert the node by (name, role) so re-registration is idempotent.
    node = (db.query(Node)
            .filter(Node.name == body.name, Node.role == body.role,
                    Node.is_self.is_(False)).first())
    if node is None:
        node = Node(name=body.name, role=body.role, is_self=False)
        db.add(node)
    node.version = body.version or node.version
    node.endpoint = body.endpoint or node.endpoint
    node.telemetry = body.telemetry or {}
    node.cloud = body.cloud or {}
    node.region = (body.cloud or {}).get("region") or node.region
    node.status = "active"
    node.last_heartbeat_at = _now()
    db.commit()
    return {"ok": True, "node_id": node.id, "blueprint": _blueprint_for(db, body.role),
            "heartbeat_interval_seconds": 60}


class BackupReport(BaseModel):
    id: str | None = None
    node_id: str | None = None
    node_name: str = ""
    role: str = ""
    kind: str = "node"
    status: str = "success"
    components: list = []
    destinations: list = []
    total_bytes: int = 0
    message: str = ""
    error: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    created_at: str | None = None


@public_router.post("/nodes/backup-report")
def node_backup_report(body: BackupReport,
                       authorization: str = Header(default=""),
                       db: Session = Depends(get_db)):
    """A node reports the result of an infrastructure backup so the control plane
    aggregates fleet-wide backup status. Auth = shared fleet secret (Bearer)."""
    token = authorization.replace("Bearer ", "").strip()
    if not token or token != _fleet_secret():
        raise HTTPException(401, "invalid node credentials")
    from ..models import BackupRun

    def _dt(v):
        if not v:
            return None
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return d.replace(tzinfo=None) if d.tzinfo else d
        except ValueError:
            return None

    run = db.get(BackupRun, body.id) if body.id else None
    if run is None:
        run = BackupRun(id=body.id) if body.id else BackupRun()
        db.add(run)
    run.node_id = body.node_id
    run.node_name = body.node_name
    run.role = body.role
    run.kind = body.kind or "node"
    run.status = body.status
    run.components = body.components or []
    run.destinations = body.destinations or []
    run.total_bytes = int(body.total_bytes or 0)
    run.message = body.message or ""
    run.error = body.error or ""
    run.started_at = _dt(body.started_at)
    run.finished_at = _dt(body.finished_at)
    if body.created_at:
        run.created_at = _dt(body.created_at)
    db.commit()
    return {"ok": True, "id": run.id}


# --------------------------------------------------------------------------- #
# Node installer — served from the control plane                              #
# --------------------------------------------------------------------------- #

@public_router.get("/nodes/bootstrap")
def node_bootstrap():
    """Serve the generic node bootstrap installer (no auth — open code). All
    node-specific values are supplied by the one-line command's env vars."""
    path = _repo_root() / "installers" / "bootstrap.sh"
    try:
        return PlainTextResponse(path.read_text())
    except Exception:
        raise HTTPException(404, "bootstrap unavailable")


# The code a fleet node needs — served from the control plane so public-web and
# customer-tenant nodes never touch GitHub. Only the dirs each node role uses.
_NODE_BUNDLE_DIRS = ("cloud", "shared", "web", "site", "installers", "infra", "updater")
_NODE_BUNDLE_EXCLUDE = (".venv", "__pycache__", "node_modules", ".git", ".pyc",
                        "web/dist", "site/dist")
_node_bundle_version_cache: str | None = None


def _node_bundle_version() -> str:
    """Stable content hash of the node bundle; changes only when code changes so
    a node's self-update timer only re-installs on real updates."""
    global _node_bundle_version_cache
    if _node_bundle_version_cache:
        return _node_bundle_version_cache
    root = _repo_root()
    h = hashlib.sha256()
    for d in _NODE_BUNDLE_DIRS:
        base = root / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and not any(x in str(f) for x in _NODE_BUNDLE_EXCLUDE):
                h.update(str(f.relative_to(root)).encode())
                try:
                    h.update(f.read_bytes())
                except Exception:
                    pass
    _node_bundle_version_cache = h.hexdigest()[:12]
    return _node_bundle_version_cache


@public_router.get("/nodes/bundle/version")
def node_bundle_version():
    return {"version": _node_bundle_version()}


@public_router.get("/nodes/bundle")
def node_bundle():
    """Serve the node install bundle (source + installers). Fleet nodes download
    this from the control plane instead of cloning the public repo."""
    root = _repo_root()
    buf = io.BytesIO()

    def _filter(ti: tarfile.TarInfo):
        return None if any(x in ti.name for x in _NODE_BUNDLE_EXCLUDE) else ti

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for d in _NODE_BUNDLE_DIRS:
            p = root / d
            if p.exists():
                tar.add(str(p), arcname=d, filter=_filter)
        version = _node_bundle_version().encode()
        vi = tarfile.TarInfo("NODE_VERSION")
        vi.size = len(version)
        tar.addfile(vi, io.BytesIO(version))
    return Response(content=buf.getvalue(), media_type="application/gzip",
                    headers={"Content-Disposition": "attachment; filename=arkive-node.tar.gz"})


class NodeInstallerRequest(BaseModel):
    role: str = "public-web"
    domain: str = ""


@admin_router.post("/nodes/installer")
def node_installer(body: NodeInstallerRequest,
                   principal: security.Principal = Depends(security.require_platform_admin),
                   db: Session = Depends(get_db)):
    """Return a single-line command a clean Ubuntu host runs to install and enroll
    a node of the given role. The command downloads the bootstrap from this
    control plane and bakes in the control-plane URL, role, domain and the fleet
    enrollment secret so the node links back automatically."""
    role = body.role
    if role not in ("control-plane", "customer-tenant", "public-web"):
        raise HTTPException(400, "unknown role")
    settings = get_settings()
    api = settings.api_base_url.rstrip("/")                 # https://host/api
    origin = settings.rp_origin.rstrip("/") or api[:-4]     # https://host
    secret = _fleet_secret()
    domain = (body.domain or "").strip()

    env = [f'CV_NODE_ROLE="{role}"']
    if domain:
        env.append(f'CV_DOMAIN="{domain}"')
    if role != "control-plane":
        env.append(f'CV_CONTROL_PLANE_URL="{origin}"')
        env.append(f'CV_NODE_SECRET="{secret}"')
    env_str = " ".join(env)
    command = (f'curl -fsSL "{api}/nodes/bootstrap" -o /tmp/arkive-node.sh && '
               f'sudo {env_str} bash /tmp/arkive-node.sh')
    audit.record(db, actor=principal.user_id, action="admin.node_installer_created",
                 category="admin", detail={"role": role, "domain": domain})
    return {"role": role, "domain": domain, "command": command,
            "control_plane_url": origin, "secret": secret}
