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

import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse
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
    """Public marketing content. Merged over defaults so partial edits are safe."""
    row = _content_row(db)
    merged = {**DEFAULT_SITE, **(row.content or {})}
    return {"content": merged, "published": bool(row.published),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


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
