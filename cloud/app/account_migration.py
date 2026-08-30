"""
Account-type upgrade / downgrade.

Changing a user's plan tier can move them between tenants:
  * Upgrading a personal (shared-tenant) account to family/business/enterprise
    creates a NEW dedicated tenant on the SAME node, makes the user its owner, and
    re-parents ALL of their data into it.
  * Downgrading returns the user to the node's default shared tenant as a personal
    account; if they owned a dedicated tenant, its other members are also moved to
    personal accounts with a 30-day grace to set up their own billing.

The whole move runs in ONE transaction — any failure rolls everything back, so a
partially-migrated account can never result (the built-in rollback procedure).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from . import audit, billing_engine, emailer, services
from .models import (
    Appliance, ApplianceAssignment, ApplianceCommand, ApplianceStorage,
    BillingProfile, Collection, ConnectorAccount, ContactLink, CustomerStorage,
    IntegrationInstance, NetworkClient, Node, ObjectVersion, PaymentMethod,
    PendingAction, ProtectionPolicy, RecoveredItem, RestoreRequest, SearchDocument,
    SnapshotReceipt, SupportTicket, SyncJob, Tenant, User, UserAddress, UserInsights,
    Vault,
)

logger = logging.getLogger("cv.accountmigration")

TB = 1024 ** 4
DOWNGRADE_GRACE_DAYS = 30
PLAN_CHANGE_COOLDOWN_DAYS = 30
DEDICATED_PLANS = {"family", "business", "enterprise"}
SHARED_PLAN = "personal"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_dedicated_plan(plan: str | None) -> bool:
    return (plan or "").lower() in DEDICATED_PLANS


def _pricing(db: Session):
    from .api.billing import effective_plan, get_pricing
    return get_pricing(db), effective_plan


def _plan_meta(db: Session, plan_id: str) -> dict:
    p, effective_plan = _pricing(db)
    return effective_plan(p, plan_id)


# --------------------------------------------------------------------------- #
# Preview — what the change costs + what warnings apply, before it's applied.   #
# --------------------------------------------------------------------------- #

def preview(db: Session, user: User, tenant: Tenant, target_plan: str) -> dict:
    from .api.billing import plan_view
    target_plan = (target_plan or "").lower()
    cur = plan_view(db, user, tenant)
    cur_monthly = float((cur.get("costs") or {}).get("total_monthly") or 0.0)
    tp = _plan_meta(db, target_plan)
    rate = float(tp.get("price_per_tb_month", 0) or 0)
    min_tb = float(tp.get("min_tb", 0) or 0)
    used_tb = float(cur.get("used_tb") or 0)
    licensed_tb = float(cur.get("licensed_tb") or 0)
    # Dedicated plans bill on committed (licensed) capacity, floored at the tier
    # minimum; personal bills on used data.
    if is_dedicated_plan(target_plan):
        target_billable = max(min_tb, licensed_tb, used_tb)
    else:
        target_billable = max(used_tb, min_tb)
    target_monthly = round(rate * target_billable, 2)

    upgrade = is_dedicated_plan(target_plan) and not is_dedicated_plan(tenant.plan)
    downgrade = not is_dedicated_plan(target_plan) and is_dedicated_plan(tenant.plan)
    inplace = is_dedicated_plan(target_plan) and is_dedicated_plan(tenant.plan)
    other_members = _other_members(db, tenant, user) if is_dedicated_plan(tenant.plan) else []

    # One change per cooldown window.
    cooldown_days = 0
    if user.last_plan_change_at:
        elapsed = (_now() - user.last_plan_change_at).days
        cooldown_days = max(0, PLAN_CHANGE_COOLDOWN_DAYS - elapsed)

    warnings: list[str] = []
    if cooldown_days:
        warnings.append(f"You changed your plan recently — you can change it again in {cooldown_days} day(s).")
    if upgrade:
        warnings.append("You'll become the administrator of a new organization and be signed out to finish switching.")
    if downgrade:
        warnings.append("Your account returns to a personal, pay-as-you-go plan and you'll be signed out to finish switching.")
        if other_members:
            warnings.append(
                f"{len(other_members)} other member(s) in your organization will be moved to personal "
                f"accounts and have {DOWNGRADE_GRACE_DAYS} days to set up their own billing before their "
                "protected data is removed.")
    if inplace and target_monthly > cur_monthly:
        warnings.append("You'll be charged a prorated amount now for the remainder of your current billing period.")
    return {
        "current_plan": {"id": tenant.plan, "name": _plan_meta(db, tenant.plan).get("name", tenant.plan)},
        "target_plan": {"id": target_plan, "name": tp.get("name", target_plan),
                        "price_per_tb_month": rate, "min_tb": min_tb},
        "current_monthly": round(cur_monthly, 2),
        "target_monthly": target_monthly,
        "currency": cur.get("currency", "USD"),
        "is_upgrade": upgrade,
        "is_downgrade": downgrade,
        "is_inplace": inplace,
        "requires_new_tenant": upgrade,
        "requires_relogin": upgrade or downgrade,
        "affected_members": len(other_members),
        "grace_days": DOWNGRADE_GRACE_DAYS,
        "cooldown_days": cooldown_days,
        "blocked": cooldown_days > 0,
        "warnings": warnings,
    }


def _other_members(db: Session, tenant: Tenant, user: User) -> list[User]:
    return (db.query(User)
            .filter(User.tenant_id == tenant.id, User.id != user.id).all())


# --------------------------------------------------------------------------- #
# Apply the change (transactional).                                            #
# --------------------------------------------------------------------------- #

def change_plan(db: Session, user: User, target_plan: str,
                tenant_name: str | None = None, actor: str | None = None) -> dict:
    tenant = db.get(Tenant, user.tenant_id)
    target_plan = (target_plan or "").lower()
    if target_plan == (tenant.plan or "").lower():
        raise ValueError("The account is already on that plan.")
    # One plan change per cooldown window.
    if user.last_plan_change_at:
        elapsed = (_now() - user.last_plan_change_at).days
        if elapsed < PLAN_CHANGE_COOLDOWN_DAYS:
            raise ValueError(f"You can change your plan again in "
                             f"{PLAN_CHANGE_COOLDOWN_DAYS - elapsed} day(s).")
    cur_dedicated = is_dedicated_plan(tenant.plan)
    tgt_dedicated = is_dedicated_plan(target_plan)
    if tgt_dedicated and not cur_dedicated:
        return _upgrade(db, user, tenant, target_plan, tenant_name, actor)
    if not tgt_dedicated and cur_dedicated:
        return _downgrade(db, user, tenant, target_plan, actor)
    if tgt_dedicated and cur_dedicated:
        return _change_inplace(db, user, tenant, target_plan, actor)
    raise ValueError("That plan change isn't available for personal accounts.")


def _tenant_monthly_cents(db: Session, tenant: Tenant) -> int:
    from .api.billing import _compute_plan
    v = _compute_plan(db, tenant)
    return int(round(float((v.get("costs") or {}).get("total_monthly") or 0.0) * 100))


def _plan_base_cents(db: Session, plan_id: str) -> int:
    tp = _plan_meta(db, plan_id)
    return int(round(float(tp.get("price_per_tb_month", 0) or 0)
                     * float(tp.get("min_tb", 0) or 0) * 100))


def _upgrade(db: Session, user: User, old_tenant: Tenant, target_plan: str,
             tenant_name: str | None, actor: str | None) -> dict:
    from .api.billing import _user_usage
    plan_name = _plan_meta(db, target_plan).get("name", target_plan)
    name = (tenant_name or "").strip() or f"{(user.display_name or user.email or 'My').split('@')[0]}'s {plan_name}"
    # Carry the user's protection-setup selections + committed capacity so nothing
    # gets silently un-selected on the new plan.
    options = list(user.protection_options or old_tenant.protection_options or [])
    _total, used_bytes, _bk = _user_usage(db, user)
    min_bytes = int(float(_plan_meta(db, target_plan).get("min_tb", 0) or 0) * TB)
    licensed = max(min_bytes, int(used_bytes or 0), int(old_tenant.licensed_bytes or 0))
    try:
        new = Tenant(name=name, tenant_type="dedicated", plan=target_plan,
                     node_id=old_tenant.node_id,
                     storage_prefix=f"t-{secrets.token_hex(4)}",
                     key_ownership_model=old_tenant.key_ownership_model or "customer-managed",
                     licensed_bytes=licensed,
                     feature_flags={}, protection_options=options,
                     appliance_plan=list(old_tenant.appliance_plan or []))
        db.add(new)
        db.flush()
        _reparent_user_data(db, user, old_tenant.id, new.id)
        user.tenant_id = new.id
        user.role = "owner"
        user.feature_flags = {}        # full capabilities in their own tenant
        user.protection_options = []   # options now live at the org (tenant) level
        user.cloud_delete_at = None
        user.last_plan_change_at = _now()
        _seed_billing_profile(db, new, user)
        _carry_billing_cycle(db, old_tenant.id, new.id, target_plan)
        _autostart_billing(db, new)    # auto-switch billing to the new plan — no admin step
        _cancel_orphaned_billing(db, old_tenant.id)   # retire the old tenant's profile if now empty
        audit.record(db, actor=actor or user.id, action="account.upgraded",
                     tenant_id=new.id, resource=user.id,
                     detail={"from_tenant": old_tenant.id, "plan": target_plan, "name": name})
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("account upgrade failed for user %s", user.id)
        raise
    _email_change(user, "upgrade", plan_name, name)
    return {"ok": True, "requires_relogin": True, "new_tenant_id": new.id,
            "tenant_name": name, "plan": target_plan}


def _autostart_billing(db: Session, tenant: Tenant) -> None:
    """Turn recurring billing on for the new plan automatically when the tenant has
    a card + a live payment processor — so an admin never has to re-enable it. A
    profile whose cycle was already carried over (active) is left as-is."""
    from .api.billing import _live_processor
    prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant.id).first()
    if prof is None or prof.active:
        return
    has_card = (db.query(PaymentMethod)
                .filter(PaymentMethod.tenant_id == tenant.id).first() is not None)
    if not has_card:
        return
    if not _live_processor(services.tenant_payment_service(db, tenant.id)):
        return
    billing_engine.activate(db, prof)   # charges the first month on the new plan


def _cancel_orphaned_billing(db: Session, tenant_id: str) -> None:
    """When a tenant has no users left after a migration, retire its billing
    profile so the sweep never charges it and it doesn't linger in the admin
    list. Charge history is kept for revenue reporting."""
    if db.query(User).filter(User.tenant_id == tenant_id).count() > 0:
        return
    for p in db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant_id).all():
        p.active = False
        p.status = "canceled"
        p.next_charge_at = None


def _change_inplace(db: Session, user: User, tenant: Tenant, target_plan: str,
                    actor: str | None) -> dict:
    """Family ↔ Business ↔ Enterprise: change the plan on the SAME dedicated
    tenant (no move, no relogin) and prorate the delta for the remaining cycle."""
    new_meta = _plan_meta(db, target_plan)
    plan_name = new_meta.get("name", target_plan)
    prorated = 0
    try:
        old_amount = _tenant_monthly_cents(db, tenant)
        tenant.plan = target_plan
        tenant.licensed_bytes = max(int(tenant.licensed_bytes or 0),
                                    int(float(new_meta.get("min_tb", 0) or 0) * TB))
        new_amount = _tenant_monthly_cents(db, tenant)
        prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant.id).first()
        if prof is not None:
            prof.plan_id = target_plan
            prof.plan_name = plan_name
            prof.amount_cents = new_amount
            if prof.active:
                delta = billing_engine.proration_cents(old_amount, new_amount, prof.next_charge_at)
                if delta > 0:
                    billing_engine.charge_profile(db, prof, kind="proration", amount_cents=delta)
                    prorated = delta
        user.last_plan_change_at = _now()
        audit.record(db, actor=actor or user.id, action="account.plan_changed",
                     tenant_id=tenant.id, resource=user.id,
                     detail={"plan": target_plan, "prorated_cents": prorated})
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("in-place plan change failed for user %s", user.id)
        raise
    _email_change(user, "change", plan_name, tenant.name)
    return {"ok": True, "requires_relogin": False, "plan": target_plan,
            "prorated_cents": prorated}


def _carry_billing_cycle(db: Session, old_tenant_id: str, new_tenant_id: str,
                         target_plan: str) -> None:
    """When an active subscription is upgraded to a new tenant, carry the billing
    anniversary over, prorate the delta now, and retire the old profile."""
    old = db.query(BillingProfile).filter(BillingProfile.tenant_id == old_tenant_id).first()
    new = db.query(BillingProfile).filter(BillingProfile.tenant_id == new_tenant_id).first()
    if new is None:
        return
    new.amount_cents = _plan_base_cents(db, target_plan)
    new.plan_id = target_plan
    if old is not None and old.active:
        new.activated_at = old.activated_at
        new.next_charge_at = old.next_charge_at
        new.active = True
        new.status = "active"
        delta = billing_engine.proration_cents(old.amount_cents, new.amount_cents, old.next_charge_at)
        if delta > 0:
            billing_engine.charge_profile(db, new, kind="proration", amount_cents=delta)
        old.active = False
        old.status = "canceled"


def _downgrade(db: Session, user: User, old_tenant: Tenant, target_plan: str,
               actor: str | None) -> dict:
    shared = _default_shared_tenant(db, old_tenant.node_id)
    if shared is None:
        raise ValueError("No default shared account tenant is configured for this node. "
                         "Set tenant.default_shared in the node's configuration profile.")
    was_dedicated = is_dedicated_plan(old_tenant.plan) or (old_tenant.tenant_type or "") != "shared"
    others = _other_members(db, old_tenant, user) if was_dedicated else []
    plan_name = _plan_meta(db, target_plan).get("name", target_plan)
    grace = _now() + timedelta(days=DOWNGRADE_GRACE_DAYS)
    try:
        _reparent_user_data(db, user, old_tenant.id, shared.id)
        user.tenant_id = shared.id
        user.role = "member"
        # Personal accounts keep their protection selections on the user record.
        user.protection_options = list(old_tenant.protection_options or user.protection_options or [])
        user.cloud_delete_at = None    # the initiator keeps billing continuity
        user.last_plan_change_at = _now()
        moved = []
        for m in others:
            _reparent_user_data(db, m, old_tenant.id, shared.id)
            m.tenant_id = shared.id
            m.role = "member"
            m.cloud_delete_at = grace   # grace to establish their own billing
            moved.append(m)
        _cancel_orphaned_billing(db, old_tenant.id)
        audit.record(db, actor=actor or user.id, action="account.downgraded",
                     tenant_id=shared.id, resource=user.id,
                     detail={"from_tenant": old_tenant.id, "plan": target_plan,
                             "members_moved": len(moved)})
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("account downgrade failed for user %s", user.id)
        raise
    _email_change(user, "downgrade", plan_name, shared.name)
    for m in others:
        _email_orphaned_member(m, grace)
    return {"ok": True, "requires_relogin": True, "shared_tenant_id": shared.id,
            "plan": target_plan, "members_moved": len(others), "grace_days": DOWNGRADE_GRACE_DAYS}


# --------------------------------------------------------------------------- #
# Data re-parenting — every table that scopes a user's data by tenant.         #
# --------------------------------------------------------------------------- #

def _reparent_user_data(db: Session, user: User, from_tid: str, to_tid: str) -> None:
    """Move every row belonging to ``user`` from ``from_tid`` to ``to_tid``. Runs
    as bulk UPDATEs within the caller's transaction so it's atomic + fast."""
    uid = user.id
    vault_ids = [r[0] for r in db.query(Vault.id)
                 .filter(Vault.owner_user_id == uid, Vault.tenant_id == from_tid).all()]
    coll_ids = ([r[0] for r in db.query(Collection.id)
                 .filter(Collection.vault_id.in_(vault_ids)).all()] if vault_ids else [])
    appl_ids = [r[0] for r in db.query(ApplianceAssignment.appliance_id)
                .filter(ApplianceAssignment.user_id == uid,
                        ApplianceAssignment.tenant_id == from_tid,
                        ApplianceAssignment.can_manage.is_(True)).all()]

    def move(model, *filters):
        db.query(model).filter(*filters).update({model.tenant_id: to_tid},
                                                synchronize_session=False)

    if vault_ids:
        move(Vault, Vault.id.in_(vault_ids))
        move(Collection, Collection.vault_id.in_(vault_ids))
        move(SearchDocument, SearchDocument.vault_id.in_(vault_ids))
        move(SnapshotReceipt, SnapshotReceipt.vault_id.in_(vault_ids))
    if coll_ids:
        move(ObjectVersion, ObjectVersion.collection_id.in_(coll_ids))
        move(SyncJob, SyncJob.collection_id.in_(coll_ids))
        move(PendingAction, PendingAction.collection_id.in_(coll_ids))
        # Protection policies referenced ONLY by the moving collections travel too;
        # a policy shared with a staying collection is left in place.
        pol_ids = {r[0] for r in db.query(Collection.policy_id)
                   .filter(Collection.id.in_(coll_ids), Collection.policy_id.isnot(None)).all()}
        for pid in pol_ids:
            shared_use = (db.query(Collection.id)
                          .filter(Collection.policy_id == pid, ~Collection.id.in_(coll_ids)).first())
            if not shared_use:
                move(ProtectionPolicy, ProtectionPolicy.id == pid)

    # Rows keyed by the owning user directly.
    for model in (ConnectorAccount, ContactLink, CustomerStorage, IntegrationInstance, NetworkClient):
        move(model, model.owner_user_id == uid)
    for model in (UserAddress, PaymentMethod, UserInsights, ApplianceAssignment, SupportTicket):
        move(model, model.user_id == uid, model.tenant_id == from_tid)
    for model in (RecoveredItem, RestoreRequest):
        move(model, model.requested_by == uid, model.tenant_id == from_tid)

    # Appliances the user manages (and their storage/commands) come along.
    if appl_ids:
        move(Appliance, Appliance.id.in_(appl_ids))
        move(ApplianceStorage, ApplianceStorage.appliance_id.in_(appl_ids))
        move(ApplianceCommand, ApplianceCommand.appliance_id.in_(appl_ids))


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _default_shared_tenant(db: Session, node_id: str | None) -> Tenant | None:
    """The shared tenant a downgraded account lands in: the node's configured
    tenant.default_shared, else any shared tenant on the same node."""
    tid = None
    if node_id:
        node = db.get(Node, node_id)
        if node is not None:
            try:
                tid = services._node_effective(db, node).get("tenant.default_shared")
            except Exception:  # noqa: BLE001
                tid = None
    if tid:
        t = db.get(Tenant, tid)
        if t is not None and (t.tenant_type or "") == "shared":
            return t
    q = db.query(Tenant).filter(Tenant.tenant_type == "shared")
    if node_id:
        q = q.filter(Tenant.node_id == node_id)
    return q.order_by(Tenant.created_at.asc()).first()


def _seed_billing_profile(db: Session, tenant: Tenant, user: User) -> None:
    from .api.billing import _plan_amount_cents
    pm = (db.query(PaymentMethod)
          .filter(PaymentMethod.tenant_id == tenant.id, PaymentMethod.user_id == user.id)
          .order_by(PaymentMethod.is_default.desc()).first())
    prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == tenant.id).first()
    if prof is None:
        prof = BillingProfile(tenant_id=tenant.id, status="inactive", active=False)
        db.add(prof)
    amount, cur, plan_id, plan_name = _plan_amount_cents(db, user, tenant)
    prof.plan_id = plan_id or tenant.plan
    prof.plan_name = plan_name
    prof.amount_cents = amount
    prof.currency = cur
    if pm is not None:
        prof.processor = pm.processor
        prof.processor_customer = pm.processor_customer
        prof.payment_method_id = pm.id


def _email_change(user: User, kind: str, plan_name: str, tenant_name: str) -> None:
    try:
        if kind == "upgrade":
            subject = f"Your Arkive account is now {plan_name}"
            body = (f"Hi {user.display_name or ''},\n\n"
                    f"Your Arkive account has been upgraded to {plan_name}. You're now the "
                    f"administrator of \"{tenant_name}\". Sign back in to start using your new plan.\n\n"
                    "— Arkive")
        else:
            subject = "Your Arkive account plan changed"
            body = (f"Hi {user.display_name or ''},\n\n"
                    f"Your Arkive account has been moved to a personal ({plan_name}) plan. "
                    "Sign back in to continue.\n\n— Arkive")
        emailer.send_email(user.email, subject, body, category="notification")
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan-change email to %s failed: %s", user.email, exc)


def _email_orphaned_member(member: User, grace: datetime) -> None:
    try:
        emailer.send_email(
            member.email, "Action needed: set up your Arkive billing",
            (f"Hi {member.display_name or ''},\n\n"
             "Your organization changed plans and your account is now an individual personal account. "
             f"Please set up your own billing by {grace.date().isoformat()} to keep your protected data — "
             "after that date it may be removed.\n\n— Arkive"),
            category="notification")
    except Exception as exc:  # noqa: BLE001
        logger.warning("orphaned-member email to %s failed: %s", member.email, exc)
