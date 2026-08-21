"""Tenant, vault, and onboarding (destination selection) endpoints (spec 15.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import audit, keybroker, security
from ..config import get_settings
from ..db import get_db
from ..models import Appliance, ApplianceStorage, Collection, Tenant, Vault

router = APIRouter(prefix="/tenant", tags=["tenant"])


DESTINATION_OPTIONS = [
    {
        "id": "cv-cloud",
        "title": "Arkive Cloud",
        "storageOwner": "Arkive",
        "keyOwner": "You (customer-managed)",
        "cvCanDecrypt": False,
        "offlineUpdate": "n/a",
        "recoveryLocation": "Vendor cloud, multi-region",
        "outageBehavior": "Managed failover",
        "hardwareCost": "$0",
    },
    {
        "id": "customer-s3",
        "title": "My Cloud Account (Amazon S3)",
        "storageOwner": "You",
        "keyOwner": "You (customer-managed or zero-knowledge)",
        "cvCanDecrypt": False,
        "offlineUpdate": "n/a",
        "recoveryLocation": "Your AWS account",
        "outageBehavior": "Independent of vendor cloud",
        "hardwareCost": "$0 + AWS usage",
    },
    {
        "id": "appliance",
        "title": "My Offline Appliance",
        "storageOwner": "You",
        "keyOwner": "You + appliance HSM",
        "cvCanDecrypt": False,
        "offlineUpdate": "Per policy (default daily)",
        "recoveryLocation": "On-premises appliance",
        "outageBehavior": "Full local operation & recovery",
        "hardwareCost": "From CV Edge 8",
    },
    {
        "id": "cloud+appliance",
        "title": "Cloud + Offline Appliance",
        "storageOwner": "Shared",
        "keyOwner": "You (split-control)",
        "cvCanDecrypt": False,
        "offlineUpdate": "Hourly cloud / daily appliance",
        "recoveryLocation": "Cloud + on-premises",
        "outageBehavior": "Cloud simplicity + physical isolation",
        "hardwareCost": "From CV Edge 8",
    },
]


@router.get("/destinations")
def destination_options():
    return DESTINATION_OPTIONS


@router.get("/storage-targets")
def storage_targets(tenant: Tenant = Depends(security.get_tenant),
                    db: Session = Depends(get_db)):
    """Concrete, selectable storage objects for source→vault mappings.

    A mapping targets a *storage* (identified by its own id): the Arkive cloud,
    a customer's own S3 bucket, or a named storage volume on an appliance
    (``store:<id>``, e.g. "My Home Appliance · Built-In Storage").
    """
    settings = get_settings()
    # Feature gating: only offer the storage tiers the tenant has enabled in
    # Protection Setup. A tenant that hasn't chosen yet (empty) sees everything.
    enabled = set(tenant.protection_options or [])
    def _on(tier: str) -> bool:
        return not enabled or tier in enabled
    targets: list[dict] = []
    if _on("cv-cloud"):
        targets.append({"id": "cv-cloud", "label": "Arkive Cloud", "kind": "cloud",
                        "detail": "Managed vendor cloud, multi-region"})
    appliances = {a.id: a for a in db.query(Appliance)
                  .filter(Appliance.tenant_id == tenant.id).all()}
    stores = (db.query(ApplianceStorage)
              .filter(ApplianceStorage.tenant_id == tenant.id).all())
    for s in sorted(stores, key=lambda s: (appliances.get(s.appliance_id).name
                                           if appliances.get(s.appliance_id) else "", s.name)):
        a = appliances.get(s.appliance_id)
        if not a or not _on("appliance"):
            continue
        targets.append({
            "id": f"store:{s.id}", "kind": "appliance",
            "label": f"{a.name} · {s.name}",
            "detail": f"{a.model} · {a.serial}",
            "appliance_id": a.id, "appliance_name": a.name,
            "store_name": s.name, "store_kind": s.kind,
            "state": a.state, "online": bool(a.last_heartbeat_at),
        })
    if _on("customer-cloud") and settings.aws_access_key_id and settings.s3_bucket:
        targets.append({
            "id": "customer-s3", "kind": "cloud",
            "label": f"Customer S3 — {settings.s3_bucket}",
            "detail": settings.s3_region or "customer-owned bucket",
        })
    return targets


@router.get("")
def get_tenant_info(principal: security.Principal = Depends(security.get_principal),
                   tenant: Tenant = Depends(security.get_tenant),
                   db: Session = Depends(get_db)):
    # A member only sees their own vaults; this drives "which vault does my data
    # live in" selections. Org-wide vault views live under /org.
    vaults = (db.query(Vault)
              .filter(Vault.tenant_id == tenant.id,
                      Vault.owner_user_id == principal.user_id).all())
    return {
        "id": tenant.id,
        "name": tenant.name,
        "org_name": tenant.name,
        "plan": tenant.plan,
        "key_ownership_model": tenant.key_ownership_model,
        "protection_options": tenant.protection_options or [],
        "licensed_bytes": int(tenant.licensed_bytes or 0),
        "role": principal.role,
        "can_admin": security.is_org_admin(principal.role) or principal.is_platform_admin,
        "is_owner": security.is_owner(principal.role),
        "vaults": [{"id": v.id, "name": v.name,
                    "key_ownership_model": v.key_ownership_model,
                    "crypto_profile_id": v.crypto_profile_id} for v in vaults],
    }


class CreateVaultRequest(BaseModel):
    name: str
    key_ownership_model: str = "customer-managed"
    crypto_profile_id: str = "cvp-hybrid-2026a"


@router.post("/vaults")
def create_vault(body: CreateVaultRequest,
                 principal: security.Principal = Depends(security.get_principal),
                 tenant: Tenant = Depends(security.get_tenant),
                 db: Session = Depends(get_db)):
    vault = Vault(
        tenant_id=tenant.id,
        owner_user_id=principal.user_id,
        name=body.name,
        key_ownership_model=body.key_ownership_model,
        crypto_profile_id=body.crypto_profile_id,
    )
    db.add(vault)
    db.commit()
    db.refresh(vault)
    # Provision the vault root key + recovery keypair in the key broker (spec 10.x).
    result = keybroker.provision_vault_root_key(vault.id, body.key_ownership_model)
    vault.wrapped_keys = [{"recipient": "primary", "hash": result["record"]["rootKeyHash"]}]
    keybroker.provision_recovery_keypair(vault.id)
    db.commit()
    audit.record(db, actor=principal.user_id, action="vault.created",
                 tenant_id=tenant.id, resource=vault.id)
    return {"id": vault.id, "name": vault.name}


@router.get("/keys")
def my_keys(principal: security.Principal = Depends(security.get_principal),
            tenant: Tenant = Depends(security.get_tenant),
            db: Session = Depends(get_db)):
    """The current user's own encryption keys (metadata only — never plaintext).
    Shows the user they hold the keys: type, strength, status and fingerprint."""
    vaults = (db.query(Vault)
              .filter(Vault.tenant_id == tenant.id,
                      Vault.owner_user_id == principal.user_id).all())
    return [{"vault_id": v.id, "vault_name": v.name, **keybroker.key_metadata(v.id)}
            for v in vaults]
