"""Demo seed data so the prototype runs end-to-end immediately."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
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
            tenant_type="dedicated",
            key_ownership_model="split-control",
            storage_prefix="t-northwind",
        )
        db.add(tenant)
        db.flush()

        owner = User(tenant_id=tenant.id, email="owner@northwind.example",
                     display_name="Alex Rivera", role="owner")
        secadmin = User(tenant_id=tenant.id, email="security@northwind.example",
                        display_name="Jordan Kim", role="admin")
        member = User(tenant_id=tenant.id, email="member@northwind.example",
                      display_name="Sam Chen", role="member")
        db.add_all([owner, secadmin, member])
        db.flush()

        # Platform (backend) admin tenant — Arkive operations.
        platform = Tenant(name="Arkive Operations", plan="platform",
                          tenant_type="internal",
                          key_ownership_model="platform-managed",
                          storage_prefix="t-arkive-ops")
        db.add(platform)
        db.flush()
        admin = User(tenant_id=platform.id, email="admin@arkive.life",
                     display_name="Arkive Admin", role="support-admin",
                     is_platform_admin=True)
        db.add(admin)

        # Each member owns their own vault — the demarcation of their data.
        for u in (owner, secadmin, member):
            vault = Vault(tenant_id=tenant.id, owner_user_id=u.id,
                          name=f"{u.display_name.split()[0]}'s Vault",
                          key_ownership_model="split-control",
                          crypto_profile_id="cvp-hybrid-2026a")
            db.add(vault)
            db.flush()
            result = keybroker.provision_vault_root_key(vault.id, vault.key_ownership_model)
            vault.wrapped_keys = [{"recipient": "primary",
                                   "hash": result["record"]["rootKeyHash"]}]
            keybroker.provision_recovery_keypair(vault.id)

        db.commit()
    except IntegrityError:
        # Another worker/instance seeded concurrently — safe to ignore.
        db.rollback()
    finally:
        if close:
            db.close()
