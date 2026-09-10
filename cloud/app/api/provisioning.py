"""Infrastructure auto-provisioning API (platform-admin).

Single-click node deployment: launch a VM in AWS or Azure using a Hyperscaler
Auto-Provision service object, run the bootstrap so the node installs + registers
itself, publish DNS, and watch it come online. Plus the IAM/access-policy guidance
the credentials for each provider require.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, hyperscaler, security, services
from ..config import get_settings
from ..db import SessionLocal, get_db
from ..models import Cluster, Node, ProvisioningJob, ServiceObject

router = APIRouter(prefix="/admin/provisioning", tags=["provisioning"],
                   dependencies=[Depends(security.require_platform_admin)])
logger = logging.getLogger("cv.provisioning")
settings = get_settings()

_HYPERSCALER_KINDS = ("hyperscaler-aws", "hyperscaler-azure")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _job_view(db: Session, j: ProvisioningJob) -> dict:
    node = db.get(Node, j.node_id) if j.node_id else None
    return {
        "id": j.id, "kind": j.kind, "provider": j.provider, "status": j.status,
        "message": j.message, "log": j.log or [], "params": j.params or {},
        "result": j.result or {}, "node_id": j.node_id,
        "node_name": (node.name if node else (j.params or {}).get("name")),
        "node_online": bool(node and node.last_heartbeat_at and
                            (_now() - node.last_heartbeat_at).total_seconds() < 90),
        "cluster_id": j.cluster_id,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "updated_at": j.updated_at.isoformat() if j.updated_at else None,
    }


@router.get("/services")
def list_services(db: Session = Depends(get_db)):
    """The configured Hyperscaler Auto-Provision service objects."""
    out = []
    for s in db.query(ServiceObject).filter(ServiceObject.kind.in_(_HYPERSCALER_KINDS)).all():
        cfg = services.resolve_service(db, s.id) or {}
        merged = cfg.get("config", {})
        provider = hyperscaler.provider_of(s.kind)
        # "Configured" = we at least have credentials to act.
        if provider == "aws":
            configured = bool(merged.get("aws_access_key_id") and merged.get("aws_secret_access_key"))
        else:
            configured = bool(merged.get("subscription_id") and merged.get("client_secret"))
        out.append({"id": s.id, "name": s.name, "kind": s.kind, "provider": provider,
                    "enabled": bool(s.enabled), "configured": configured,
                    "region": merged.get("region") or merged.get("location") or "",
                    "domain_suffix": merged.get("domain_suffix") or ""})
    return out


@router.get("/iam/{provider}")
def iam_guidance(provider: str):
    g = hyperscaler.IAM_GUIDANCE.get((provider or "").lower())
    if not g:
        raise HTTPException(404, "unknown provider")
    return g


@router.get("/catalog")
def catalog(provider: str):
    """Instance-size + disk presets for the deploy form (per provider)."""
    return hyperscaler.catalog(provider)


@router.get("/suggest-name")
def suggest_name(role: str = "customer-tenant", cluster_id: str = "",
                 service_object_id: str = "", db: Session = Depends(get_db)):
    """Suggest the next node name + FQDN following the platform convention
    ``<clustercode>-<roleabbr><NNN>-<letter>.<domain-suffix>`` (e.g.
    nam1-ct001-a.arkive.life)."""
    cluster = db.get(Cluster, cluster_id) if cluster_id else None
    ccode = re.sub(r"[^a-z0-9]", "", ((cluster.code if cluster else "") or "nam1").lower()) or "nam1"
    abbr = hyperscaler.ROLE_ABBR.get(role, "nd")
    prefix = f"{ccode}-{abbr}"
    nums: list[int] = []
    for (nm,) in db.query(Node.name).filter(Node.name.like(f"{prefix}%")).all():
        m = re.match(rf"^{re.escape(prefix)}(\d+)-[a-z]$", nm or "")
        if m:
            nums.append(int(m.group(1)))
    nxt = (max(nums) + 1) if nums else 1
    name = f"{prefix}{nxt:03d}-a"
    suffix = ""
    if service_object_id:
        svc = services.resolve_service(db, service_object_id)
        suffix = ((svc or {}).get("config", {}) or {}).get("domain_suffix", "").strip().lstrip(".")
    return {"name": name, "fqdn": f"{name}.{suffix}" if suffix else name, "domain_suffix": suffix}


@router.get("/check-name")
def check_name(name: str = "", role: str = "customer-tenant", service_object_id: str = "",
               db: Session = Depends(get_db)):
    """Live validation for the deploy form: flag a node-name / FQDN conflict with an
    existing node or an in-flight deployment so the operator catches it before launch."""
    slug = hyperscaler._slug((name or "").strip())
    suffix = ""
    if service_object_id:
        svc = services.resolve_service(db, service_object_id)
        suffix = ((svc or {}).get("config", {}) or {}).get("domain_suffix", "").strip().lstrip(".")
    fqdn = f"{slug}.{suffix}" if (slug and suffix) else slug
    if not slug:
        return {"ok": True, "conflict": False, "fqdn": fqdn, "reason": ""}
    conflict, reason = False, ""
    existing = db.query(Node).filter(Node.name.ilike(slug)).first()
    if existing:
        conflict, reason = True, f"A node named “{slug}” already exists."
    elif fqdn:
        ep = db.query(Node).filter(Node.endpoint.ilike(f"%{fqdn}%")).first()
        if ep:
            conflict, reason = True, f"{fqdn} is already used by node “{ep.name}”."
    if not conflict:
        for jj in (db.query(ProvisioningJob)
                   .filter(ProvisioningJob.status.in_(["pending", "provisioning", "bootstrapping"])).all()):
            if hyperscaler._slug((jj.params or {}).get("name") or "") == slug:
                conflict, reason = True, "A deployment for this name is already in progress."
                break
    return {"ok": True, "conflict": conflict, "fqdn": fqdn, "reason": reason}


class DeployNodeBody(BaseModel):
    service_object_id: str
    name: str
    role: str = "customer-tenant"
    region: str | None = None
    size: str | None = None
    disk_gb: int | None = None
    cluster_id: str | None = None
    config_profile_id: str | None = None
    ssh_config_object_id: str | None = None


@router.post("/deploy-node")
def deploy_node(body: DeployNodeBody,
                principal: security.Principal = Depends(security.require_platform_admin),
                db: Session = Depends(get_db)):
    svc = db.get(ServiceObject, body.service_object_id)
    if svc is None or svc.kind not in _HYPERSCALER_KINDS:
        raise HTTPException(404, "hyperscaler service object not found")
    provider = hyperscaler.provider_of(svc.kind)
    if body.cluster_id and not db.get(Cluster, body.cluster_id):
        raise HTTPException(404, "cluster not found")
    job = ProvisioningJob(
        kind="deploy_node", provider=provider, service_object_id=svc.id,
        status="pending", message="Queued",
        params={"name": body.name.strip(), "role": body.role,
                "region": body.region, "size": body.size, "disk_gb": body.disk_gb,
                "config_profile_id": body.config_profile_id,
                "ssh_config_object_id": body.ssh_config_object_id},
        cluster_id=body.cluster_id, created_by=principal.user_id, log=[])
    db.add(job)
    db.commit()
    db.refresh(job)
    audit.record(db, actor=principal.user_id, action="provisioning.deploy_node",
                 resource=job.id, detail={"provider": provider, "name": body.name},
                 severity="notice")
    threading.Thread(target=_run_deploy, args=(job.id,), daemon=True).start()
    return _job_view(db, job)


@router.get("/jobs")
def list_jobs(db: Session = Depends(get_db)):
    rows = (db.query(ProvisioningJob)
            .order_by(ProvisioningJob.created_at.desc()).limit(50).all())
    return [_job_view(db, j) for j in rows]


@router.get("/jobs/{jid}")
def get_job(jid: str, db: Session = Depends(get_db)):
    j = db.get(ProvisioningJob, jid)
    if j is None:
        raise HTTPException(404, "job not found")
    return _job_view(db, j)


@router.post("/jobs/{jid}/abort")
def abort_job(jid: str,
              principal: security.Principal = Depends(security.require_platform_admin),
              db: Session = Depends(get_db)):
    """Abort a failed / stuck deployment: best-effort terminate the cloud VM and
    delete the platform node record + its provisioning link."""
    j = db.get(ProvisioningJob, jid)
    if j is None:
        raise HTTPException(404, "job not found")
    from ..models import Tenant
    vm = None
    svc = services.resolve_service(db, j.service_object_id) if j.service_object_id else None
    res = j.result or {}
    inst = res.get("instance_id") or ""
    if svc and inst:
        vm = hyperscaler.terminate_node(provider=j.provider, config=svc.get("config", {}),
                                        instance_id=inst)
    # Prune the DNS A record we published so a same-name re-deploy can't collide
    # with a stale record pointing at a now-dead box.
    dns = None
    fqdn = res.get("fqdn") or res.get("dns_name") or ""
    if svc and fqdn:
        dns = hyperscaler.remove_dns(provider=j.provider, config=svc.get("config", {}),
                                     fqdn=fqdn, ip=res.get("public_ip") or "",
                                     name_relative=res.get("node_name") or "")
    if j.node_id:
        node = db.get(Node, j.node_id)
        if node is not None and not node.is_self:
            db.query(Tenant).filter(Tenant.node_id == node.id)\
                .update({Tenant.node_id: None}, synchronize_session=False)
            db.delete(node)
    j.status = "aborted"
    j.node_id = None
    parts = []
    if (vm or {}).get("terminated"):
        parts.append("cloud VM terminated")
    if (dns or {}).get("removed"):
        parts.append("DNS record removed")
    j.message = ("Aborted — " + ", ".join(parts)) if parts else "Aborted by administrator"
    log = list(j.log or [])
    log.append({"ts": _now().isoformat(), "msg": j.message})
    j.log = log[-100:]
    db.commit()
    audit.record(db, actor=principal.user_id, action="provisioning.aborted",
                 resource=j.id, severity="warning",
                 detail={"terminated_vm": bool((vm or {}).get("terminated")),
                         "dns_removed": bool((dns or {}).get("removed"))})
    return {"ok": True, "vm": vm, "dns": dns}


# --------------------------------------------------------------------------- #
# Background deploy worker                                                     #
# --------------------------------------------------------------------------- #

def _run_deploy(job_id: str) -> None:
    with SessionLocal() as db:
        job = db.get(ProvisioningJob, job_id)
        if job is None:
            return

        def progress(msg: str) -> None:
            job.message = msg
            log = list(job.log or [])
            log.append({"ts": _now().isoformat(), "msg": msg})
            job.log = log[-100:]
            db.commit()
            logger.info("provision %s: %s", job_id, msg)

        try:
            svc = services.resolve_service(db, job.service_object_id)
            if not svc:
                raise ValueError("service object not found")
            # The bootstrap script + build_userdata append '/api/...', so hand them
            # the control-plane ORIGIN (strip a trailing '/api').
            import re as _re
            cp_url = _re.sub(r"/api/?$", "", (settings.api_base_url or "")).rstrip("/")
            fleet_secret = _fleet_secret()
            if not fleet_secret:
                raise ValueError("fleet node secret is not configured on the control plane")

            job.status = "provisioning"
            progress("Starting deployment…")
            res = hyperscaler.deploy_node(
                provider=job.provider, config=_deploy_config(db, svc, job), opts=job.params or {},
                cp_url=cp_url, fleet_secret=fleet_secret, progress=progress,
                progress_token=job.id)
            job.result = res
            db.commit()

            # Register the node row now so it appears in the cluster while it boots.
            node = _ensure_node(db, job, res)
            job.node_id = node.id
            job.status = "bootstrapping"
            progress(f"Instance up ({res.get('public_ip') or 'no public IP'}). "
                     "Waiting for the node to install and register…")

            # Wait for the node to heartbeat (installed + online).
            if _wait_online(db, node.id, progress):
                job.status = "online"
                progress("Node is online. ✓")
            else:
                progress("VM is running and installing. The node will appear online "
                         "once it finishes bootstrapping (this can take a few minutes).")
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            job = db.get(ProvisioningJob, job_id)
            if job is not None:
                job.status = "error"
                job.message = str(exc)[:400]
                log = list(job.log or [])
                log.append({"ts": _now().isoformat(), "msg": f"Error: {exc}"})
                job.log = log[-100:]
                db.commit()
            logger.exception("provision %s failed", job_id)


def _fleet_secret() -> str:
    try:
        from .site import _fleet_secret as fs
        return fs() or ""
    except Exception:  # noqa: BLE001
        return (settings.node_secret or "")


def _deploy_config(db: Session, svc: dict, job: ProvisioningJob) -> dict:
    """The service's config plus the PUBLIC key from the selected key-pair config
    object. The private key is never stored — only the public key reaches the VM."""
    cfg = dict((svc or {}).get("config", {}) or {})
    oid = (job.params or {}).get("ssh_config_object_id")
    if oid:
        from ..models import ConfigObject
        from .. import credstore
        ko = db.get(ConfigObject, oid)
        if ko and ko.encrypted_values:
            try:
                kv = credstore.decrypt("platform", ko.encrypted_values) or {}
                pub = (kv.get("ssh_public_key") or kv.get("public_key") or "").strip()
                if pub:
                    cfg["ssh_public_key"] = pub
            except Exception:  # noqa: BLE001
                logger.warning("could not read key-pair config object %s", oid)
    return cfg



def _ensure_node(db: Session, job: ProvisioningJob, res: dict) -> Node:
    name = res.get("node_name") or (job.params or {}).get("name") or "node"
    role = res.get("role") or "customer-tenant"
    fqdn = res.get("fqdn") or ""
    endpoint = f"https://{fqdn}/api" if fqdn else ""
    node = (db.query(Node)
            .filter(Node.name == name, Node.role == role).first())
    if node is None:
        node = Node(name=name, role=role, status="active")
        db.add(node)
    node.endpoint = endpoint or node.endpoint
    node.region = res.get("region") or node.region
    node.public_ip = res.get("public_ip") or node.public_ip
    node.cloud = {"provider": job.provider, "region": res.get("region") or "",
                  "public_ip": res.get("public_ip") or ""}
    if job.cluster_id:
        node.cluster_id = job.cluster_id
    profile_id = (job.params or {}).get("config_profile_id")
    if profile_id:
        node.config_profile_id = profile_id
    db.commit()
    db.refresh(node)
    return node


def _wait_online(db: Session, node_id: str, progress, timeout_s: int = 900) -> bool:
    import time as _t
    deadline = _t.time() + timeout_s
    announced = False
    while _t.time() < deadline:
        _t.sleep(15)
        db.expire_all()
        node = db.get(Node, node_id)
        if node and node.last_heartbeat_at and \
                (_now() - node.last_heartbeat_at).total_seconds() < 120:
            return True
        if not announced and _t.time() - (deadline - timeout_s) > 120:
            progress("Still bootstrapping — installing the node software…")
            announced = True
    return False
