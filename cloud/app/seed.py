"""Demo seed data so the prototype runs end-to-end immediately."""

from __future__ import annotations

from sqlalchemy.orm import Session

from . import keybroker
from .db import SessionLocal
from .models import Tenant, User, Vault


def seed(db: Session | None = None) -> None:
    close = False
    if db is None:
        db = SessionLocal()
        close = True
    try:
        if db.query(Tenant).first():
            return  # already seeded

        # Customer tenant (a demo family office running on Arkive).
        tenant = Tenant(
            name="Northwind Family Office",
            plan="enterprise",
            key_ownership_model="split-control",
            storage_prefix="t-northwind",
        )
        db.add(tenant)
        db.flush()

        owner = User(tenant_id=tenant.id, email="owner@northwind.example",
                     display_name="Alex Rivera", role="owner")
        secadmin = User(tenant_id=tenant.id, email="security@northwind.example",
                        display_name="Jordan Kim", role="security-admin")
        db.add_all([owner, secadmin])

        # Platform (backend) admin tenant — Arkive operations.
        platform = Tenant(name="Arkive Operations", plan="platform",
                          key_ownership_model="platform-managed",
                          storage_prefix="t-arkive-ops")
        db.add(platform)
        db.flush()
        admin = User(tenant_id=platform.id, email="admin@arkive.life",
                     display_name="Arkive Admin", role="support-admin",
                     is_platform_admin=True)
        db.add(admin)

        # Primary vault with a provisioned root key.
        vault = Vault(tenant_id=tenant.id, name="Primary Vault",
                      key_ownership_model="split-control",
                      crypto_profile_id="cvp-hybrid-2026a")
        db.add(vault)
        db.flush()
        result = keybroker.provision_vault_root_key(vault.id, vault.key_ownership_model)
        vault.wrapped_keys = [{"recipient": "primary",
                               "hash": result["record"]["rootKeyHash"]}]

        db.commit()
    finally:
        if close:
            db.close()
