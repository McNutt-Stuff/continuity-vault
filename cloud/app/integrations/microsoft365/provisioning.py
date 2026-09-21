"""Microsoft 365 — identity binding reconciliation (auto-suggest / map / create).

Turns discovered Entra identities into Arkive member bindings:

  * **auto-suggest** (always): match each in-scope identity to an existing Arkive
    member by email/UPN and record a *suggested* binding the admin can accept.
  * **auto-map** (opt-in ``config.auto_map``): accept those suggestions
    automatically as they're discovered.
  * **auto-create** (opt-in ``config.auto_create``): provision a new Arkive member
    (member + vault + keys) for an in-scope identity with no existing match.

Member/vault/key provisioning is a CONTROL-PLANE concern (keybroker + vault keys
live there), so ``can_provision`` gates it — a customer node passes False and the
control plane completes creation for node-hosted tenants on its own reconcile.
Mapping a Microsoft user never grants portal access; auto-created members are data
owners (status ``active``, no invite email) until an admin invites them.
"""

from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from ... import keybroker
from ...models import Tenant, User, Vault
from . import models as m

logger = logging.getLogger("cv.integrations.m365.provisioning")


def find_member(db: Session, tenant_id: str, ident: "m.ExternalIdentity") -> User | None:
    """Existing Arkive member whose email matches the identity's mail/UPN."""
    for cand in (ident.email, ident.upn):
        c = (cand or "").strip().lower()
        if not c or "@" not in c:
            continue
        u = (db.query(User)
             .filter(User.tenant_id == tenant_id, func.lower(User.email) == c).first())
        if u is not None:
            return u
    return None


def provision_member(db: Session, tenant: Tenant, email: str, display_name: str) -> User | None:
    """Create a new Arkive member (+ their vault and keys) for an Entra identity.
    Idempotent on email. CONTROL-PLANE only (provisions vault keys)."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    existing = db.query(User).filter(func.lower(User.email) == email).first()
    if existing is not None:
        return existing
    user = User(tenant_id=tenant.id, email=email,
                display_name=(display_name or "").strip() or email.split("@")[0],
                role="member", status="active")
    db.add(user)
    db.flush()
    vault = Vault(tenant_id=tenant.id, owner_user_id=user.id,
                  name=f"{user.display_name.split()[0]}'s Vault",
                  key_ownership_model=tenant.key_ownership_model or "customer-managed",
                  crypto_profile_id="cvp-hybrid-2026a")
    db.add(vault)
    db.flush()
    result = keybroker.provision_vault_root_key(vault.id, vault.key_ownership_model)
    vault.wrapped_keys = [{"recipient": "primary", "hash": result["record"]["rootKeyHash"]}]
    keybroker.provision_recovery_keypair(vault.id)
    db.flush()
    logger.info("m365 auto-created member %s (%s) in tenant %s", user.id, email, tenant.id)
    return user


# Binding statuses the admin has decided — never overridden by reconciliation.
_LOCKED = {"mapped", "excluded", "protected_only"}


def reconcile_bindings(db: Session, inst, *, can_provision: bool) -> dict:
    """Match / suggest / (optionally) auto-map / auto-create for every in-scope
    identity of one instance. Returns {suggested, mapped, created}."""
    cfg = inst.config or {}
    auto_map = bool(cfg.get("auto_map"))
    auto_create = bool(cfg.get("auto_create")) and can_provision
    tenant = db.get(Tenant, inst.tenant_id)
    if tenant is None:
        return {"suggested": 0, "mapped": 0, "created": 0}

    # Seat governance: when enforcement is on, auto-create NEVER exceeds the licensed
    # seats — extras are held as candidates for admin review (never auto-billed).
    from ... import entitlements
    _enforce = entitlements.enforcement_on(db, tenant)
    _seat_key = "family_members" if (tenant.plan or "").lower() == "family" else "protected_users"

    identities = (db.query(m.ExternalIdentity)
                  .filter(m.ExternalIdentity.integration_instance_id == inst.id,
                          m.ExternalIdentity.in_scope.is_(True)).all())
    bindings = {b.external_identity_id: b for b in db.query(m.ExternalIdentityBinding)
                .filter(m.ExternalIdentityBinding.integration_instance_id == inst.id).all()}
    suggested = mapped = created = held = 0

    for e in identities:
        binding = bindings.get(e.id)
        if binding is not None and binding.status in _LOCKED:
            continue  # respect an explicit admin decision
        member = find_member(db, tenant.id, e)
        created_now = False
        if member is None and auto_create:
            if _enforce and not entitlements.can_consume(db, tenant, _seat_key, 1):
                # Over the licensed seats — hold for admin review, never auto-bill.
                if e.state in ("discovered", "suggested_match"):
                    e.state = "new_user_candidate"
                held += 1
                continue
            member = provision_member(db, tenant, e.email or e.upn, e.display_name)
            created_now = member is not None
            if created_now:
                created += 1
        if member is None:
            # No match and not auto-creating — flag as a candidate for an account.
            if e.state in ("discovered", "suggested_match"):
                e.state = "new_user_candidate"
            continue
        if binding is None:
            binding = m.ExternalIdentityBinding(
                tenant_id=inst.tenant_id, integration_instance_id=inst.id,
                external_identity_id=e.id)
            db.add(binding)
            bindings[e.id] = binding
        binding.user_id = member.id
        binding.protected_only = False
        binding.mapping_method = ("auto_created" if created_now
                                  else (binding.mapping_method or "verified_email"))
        if auto_map or created_now:
            binding.status = "mapped"
            e.state = "mapped"
            mapped += 1
        else:
            binding.status = "suggested"
            e.state = "suggested_match"
            suggested += 1

    db.commit()
    if suggested or mapped or created or held:
        logger.info("m365 reconcile bindings (instance=%s): suggested=%d mapped=%d created=%d held=%d",
                    inst.id, suggested, mapped, created, held)
    return {"suggested": suggested, "mapped": mapped, "created": created, "held": held}
