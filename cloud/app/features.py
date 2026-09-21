"""Admin-controlled capability flags, resolved per user / per tenant.

A flag defaults ON; an admin turns it OFF to remove a capability. The key flag is
``purge_enabled`` — cleared to place a user or tenant under a legal hold so they
can no longer delete (purge) their protected data.

Resolution:
- Personal accounts (shared tenant) carry ONLY user-level flags.
- Org tenants (dedicated/restricted/internal) may set a flag tenant-wide; a
  tenant-level *disable* is authoritative (a legal hold can't be user-overridden),
  otherwise a user-level value applies over the tenant/default.
"""

from __future__ import annotations

# flag name -> default when neither user nor tenant sets it
FLAGS: dict[str, bool] = {
    "purge_enabled": True,   # may the account delete (purge) its protected data?
    "insights_enabled": True,  # show the Insights page (digital-footprint findings)?
    "cloud_storage_enabled": True,  # show/allow Cloud Storage (bring-your-own buckets)?
    "integrations_enabled": True,   # show/allow Integrations (network intelligence)?
    # Rules engine: declarative ingestion rules that label, restrict, obfuscate,
    # don't-index or discard data. OFF by default. NOTE: the rules engine can HELP
    # DRIVE compliance (it feeds the data_classification/data_minimization
    # capabilities) — it is NOT the compliance feature itself. Compliance posture
    # lives behind `compliance_enabled`.
    "rules_enabled": False,
    # Compliance engine: framework posture (NIST CSF, CIS, HIPAA, …) scored from
    # live evidence provided by Arkive + its integrations. OFF by default and
    # Business/Enterprise only (gated in the API alongside this flag).
    "compliance_enabled": False,
    # Advanced Ubiquiti/UniFi analytics: per-user + org-level network drilldowns,
    # device→user mapping analytics, and collection-gap detection. OFF by default.
    "advanced_ubiquiti_analytics": False,
    # Microsoft 365 Managed Integration (Business/Enterprise, admin-governed). Gated
    # by plan AND this flag; OFF by default until the collection backend ships.
    "m365_managed_integration": False,
    # Org admins may search + recover ANOTHER member's protected data (org-wide /
    # per-user scope). ON by default for org tenants; clear it for a privacy/legal
    # hold so admins are confined to their own vaults. Every cross-member access is
    # audited regardless.
    "admin_cross_member_access": True,
    # Require a second org admin to approve a cross-member RECOVERY (dual control /
    # "break glass") before the data is released. OFF by default.
    "cross_member_recovery_approval": False,
    # Hard-enforce entitlement quantities (e.g. reject inviting a member over the
    # licensed-seat allowance). OFF by default so the entitlements rollout is
    # visibility-only until a cohort is switched on. Derivation + the admin view
    # are always available regardless of this flag.
    "entitlements_enforced": False,
}

# Human labels for the admin UI.
LABELS = {
    "purge_enabled": "Allow data purge",
    "insights_enabled": "Digital-footprint Insights",
    "cloud_storage_enabled": "Cloud Storage (bring-your-own)",
    "integrations_enabled": "Integrations (network intelligence)",
    "rules_enabled": "Rules engine",
    "compliance_enabled": "Compliance engine (framework posture — Business)",
    "advanced_ubiquiti_analytics": "Advanced Ubiquiti Analytics (per-user & org)",
    "m365_managed_integration": "Microsoft 365 Managed Integration (Business/Enterprise)",
    "admin_cross_member_access": "Admin cross-member data access (search & recover)",
    "cross_member_recovery_approval": "Require approval for cross-member recovery",
    "entitlements_enforced": "Enforce entitlement/seat limits (rollout)",
}


def defaults() -> dict:
    return dict(FLAGS)


def _shared(tenant) -> bool:
    return ((tenant.tenant_type if tenant else "dedicated") or "dedicated") == "shared"


# Reverse map (built lazily): a feature-flag key -> the entitlement key that gates
# it, so the plan/add-on that grants the entitlement is what turns the flag on.
_FLAG_ENTITLEMENT: dict[str, str] | None = None


def _flag_entitlement_map() -> dict[str, str]:
    global _FLAG_ENTITLEMENT
    if _FLAG_ENTITLEMENT is None:
        try:
            from .entitlements import registry as ent_registry
            _FLAG_ENTITLEMENT = {spec["feature"]: key
                                 for key, spec in ent_registry.ENTITLEMENTS.items()
                                 if spec.get("feature")}
        except Exception:  # noqa: BLE001 — entitlements optional
            _FLAG_ENTITLEMENT = {}
    return _FLAG_ENTITLEMENT


def _plan_addon_grant(tenant, name: str, db=None):
    """Whether the tenant's PLAN / ADD-ONS grant this flag (bool), or ``None`` when
    the flag isn't entitlement-controlled. See ``_plan_addon_grant_src`` for origin."""
    return _plan_addon_grant_src(tenant, name, db)[0]


def _plan_addon_grant_src(tenant, name: str, db=None):
    """``(granted, origin)`` where origin is ``"plan"`` or ``"addon"``; ``(None, None)``
    when the flag isn't entitlement-controlled (caller falls back to the default)."""
    ent_key = _flag_entitlement_map().get(name)
    granted: bool | None = None
    origin: str | None = None
    plan = (getattr(tenant, "plan", "") or "").lower() if tenant is not None else ""
    if ent_key is not None and tenant is not None:
        try:
            from .entitlements import registry as ent_registry
            plan_grants = ent_registry.plan_grants(plan)
            if ent_key in plan_grants:                 # known plan decides explicitly
                granted = bool(plan_grants[ent_key]); origin = "plan"
        except Exception:  # noqa: BLE001
            pass
        # The catalog plan version (admin-edited) overrides the code-registry default.
        if db is not None:
            try:
                from . import catalog
                v = catalog.effective_version(db, plan)
                if v is not None and v.entitlements and ent_key in v.entitlements:
                    granted = bool(v.entitlements[ent_key]); origin = "plan"
            except Exception:  # noqa: BLE001
                pass
    # Add-ons can ENABLE a flag (via their entitlements map or a direct feature_flags
    # entry). Needs a db session; when absent, only the plan layer applies.
    if db is not None and tenant is not None:
        try:
            from .entitlements import addons
            _qty, bools, flags = addons.grants_for_tenant(db, tenant.id)
            if name in (flags or []):
                granted = True; origin = "addon"
            if ent_key is not None and bools.get(ent_key):
                granted = True; origin = "addon"
        except Exception:  # noqa: BLE001
            pass
    return granted, origin


def resolve_with_source(user, tenant, name: str, db=None) -> tuple[bool, str]:
    """The single authority for a feature flag's effective value + WHY.

    The tenant TYPE decides the scope:
      - org / dedicated tenant → TENANT-scoped: every member INHERITS the tenant's
        flags (plan feature → add-on feature → manual tenant override). Per-user
        flags do not apply to org members.
      - shared / personal pool → ACCOUNT-scoped: the account's own resolution
        (plan feature → add-on feature → manual per-user override).

    Source: ``"manual"`` (an explicit Arkive-admin override on the governing
    tenant/account), ``"plan"``, ``"addon"``, or ``"default"``."""
    default = FLAGS.get(name, False)
    shared = _shared(tenant)
    # The governing manual-override layer: the account for a shared/personal pool,
    # the tenant for an org — so org members inherit their tenant.
    overrides = (((user.feature_flags or {}) if user else {}) if shared
                 else ((tenant.feature_flags or {}) if tenant else {}))
    if name in overrides:
        return bool(overrides[name]), "manual"
    granted, origin = _plan_addon_grant_src(tenant, name, db)
    if granted is not None:
        return granted, (origin or "plan")
    return bool(default), "default"


def resolve(user, tenant, name: str, db=None) -> bool:
    """Effective value of a feature flag — THE way an account's/tenant's features are
    determined (org members inherit the tenant; shared accounts resolve per-account).
    Every authorization check flows through here. Pass ``db`` so add-on grants count."""
    return resolve_with_source(user, tenant, name, db)[0]


def effective(user, tenant, db=None) -> dict:
    return {name: resolve(user, tenant, name, db) for name in FLAGS}
