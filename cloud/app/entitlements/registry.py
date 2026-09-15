"""Entitlement registry — the catalog of grantable rights and quantities.

An *entitlement* is a right (boolean), quantity, or capacity a tenant is
commercially authorized to use. Entitlements are the layer between the commercial
model (plans, add-ons, contracts) and enforcement (feature flags, seat limits):

    plan / add-on / contract  →  ENTITLEMENT  →  feature flag / limit enforcement

Phase 1 derives entitlements deterministically from the tenant's existing plan +
PricingConfig + feature flags + admin overrides (``entitlements.engine.derive``).
Later phases feed subscription items, add-ons and Enterprise contracts into the
same derivation without changing enforcement call-sites.

Definitions live here (a versioned code registry, like the compliance registry)
so they can't drift; per-tenant *overrides* are the only DB state.
"""

from __future__ import annotations

# key -> {title, description, type, unit, scope, feature}
#   type:  bool | quantity | capacity
#   scope: org | account   (account = Personal/shared-tenant scope)
#   feature: the feature-flag key this entitlement gates (optional)
ENTITLEMENTS: dict[str, dict] = {
    "protected_users": {
        "title": "Protected users", "type": "quantity", "unit": "users", "scope": "org",
        "description": "Licensed members whose data Arkive protects."},
    "family_members": {
        "title": "Family members", "type": "quantity", "unit": "members", "scope": "org",
        "description": "Members included on a Family plan."},
    "protected_data_tb": {
        "title": "Protected data capacity", "type": "capacity", "unit": "TB", "scope": "org",
        "description": "Committed protected-data capacity in terabytes."},
    "arkive_cloud_access": {
        "title": "Arkive Cloud", "type": "bool", "scope": "org", "feature": "cloud_storage_enabled",
        "description": "Store protected data in Arkive's managed cloud."},
    "arkive_cloud_plus_access": {
        "title": "Arkive Cloud Plus", "type": "bool", "scope": "org",
        "description": "Higher-tier Arkive Cloud storage."},
    "appliance_management": {
        "title": "Appliance management", "type": "bool", "scope": "org",
        "description": "Deploy and manage on-prem Arkive appliances."},
    "m365_managed_integration": {
        "title": "Microsoft 365 Managed Integration", "type": "bool", "scope": "org",
        "feature": "m365_managed_integration",
        "description": "Admin-governed Microsoft 365 discovery + collection."},
    "compliance": {
        "title": "Compliance engine", "type": "bool", "scope": "org", "feature": "compliance_enabled",
        "description": "Framework posture scoring (NIST/CIS/HIPAA/…)."},
    "rules_engine": {
        "title": "Rules engine", "type": "bool", "scope": "org", "feature": "rules_enabled",
        "description": "Declarative ingest governance rules."},
    "integrations": {
        "title": "Integrations", "type": "bool", "scope": "org", "feature": "integrations_enabled",
        "description": "Network-intelligence integrations."},
    "insights": {
        "title": "Insights", "type": "bool", "scope": "org", "feature": "insights_enabled",
        "description": "Digital-footprint insights."},
    "admin_cross_member_access": {
        "title": "Admin cross-member access", "type": "bool", "scope": "org",
        "feature": "admin_cross_member_access",
        "description": "Org admins may search/recover other members' data (gated + audited)."},
}


# Per-plan defaults: the entitlements a plan family GRANTS out of the box. Quantity
# values are the plan's *included* allowance (the free tier); purchased seats /
# add-ons / contract overrides add to these in later phases. These are safe
# defaults — an admin can override any tenant, and PricingConfig plan tiers can
# carry ``included_users`` / ``included_members`` to tune them without code.
PLAN_ENTITLEMENTS: dict[str, dict] = {
    "personal": {
        "protected_users": 1, "family_members": 1, "protected_data_tb": 0,
        "arkive_cloud_access": True, "appliance_management": True,
        "integrations": True, "insights": True,
        "arkive_cloud_plus_access": False, "m365_managed_integration": False,
        "compliance": False, "rules_engine": False, "admin_cross_member_access": False},
    "consumer": {
        "protected_users": 1, "family_members": 1, "protected_data_tb": 1,
        "arkive_cloud_access": True, "appliance_management": True,
        "integrations": True, "insights": True,
        "arkive_cloud_plus_access": False, "m365_managed_integration": False,
        "compliance": False, "rules_engine": False, "admin_cross_member_access": True},
    "family": {
        "protected_users": 5, "family_members": 5, "protected_data_tb": 2,
        "arkive_cloud_access": True, "appliance_management": True,
        "integrations": True, "insights": True,
        "arkive_cloud_plus_access": False, "m365_managed_integration": False,
        "compliance": False, "rules_engine": False, "admin_cross_member_access": True},
    "business": {
        "protected_users": 1, "family_members": 0, "protected_data_tb": 5,
        "arkive_cloud_access": True, "arkive_cloud_plus_access": True, "appliance_management": True,
        "integrations": True, "insights": True, "m365_managed_integration": True,
        "compliance": True, "rules_engine": True, "admin_cross_member_access": True},
    "enterprise": {
        "protected_users": 1, "family_members": 0, "protected_data_tb": 25,
        "arkive_cloud_access": True, "arkive_cloud_plus_access": True, "appliance_management": True,
        "integrations": True, "insights": True, "m365_managed_integration": True,
        "compliance": True, "rules_engine": True, "admin_cross_member_access": True},
}


def definition(key: str) -> dict | None:
    return ENTITLEMENTS.get(key)


def plan_grants(plan: str) -> dict:
    """The default entitlement values a plan grants (empty for an unknown plan)."""
    return dict(PLAN_ENTITLEMENTS.get((plan or "").lower(), {}))
