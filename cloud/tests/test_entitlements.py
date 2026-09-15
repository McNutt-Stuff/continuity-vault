"""Entitlements — Phase 1 unit tests (pure logic; run under the app's Python 3.14).

Note: the repository currently has no pytest harness / conftest (see the billing
roadmap — a test harness is a Phase 14 deliverable). These tests are dependency-
light so they can run as soon as a runner is wired up. Run: ``pytest cloud/tests``.
"""

from cloud.app.entitlements import registry
from cloud.app.entitlements.engine import _coerce


def test_plan_grants_included_quantities():
    # Family includes 5 members; Business includes 1 user by default.
    assert registry.plan_grants("family")["family_members"] == 5
    assert registry.plan_grants("business")["protected_users"] == 1
    # Unknown plan → empty grants (safe default).
    assert registry.plan_grants("nope") == {}


def test_plan_boolean_entitlements():
    biz = registry.plan_grants("business")
    assert biz["compliance"] is True
    assert biz["m365_managed_integration"] is True
    fam = registry.plan_grants("family")
    assert fam["compliance"] is False
    assert fam["arkive_cloud_access"] is True


def test_every_registry_entitlement_has_type():
    for key, spec in registry.ENTITLEMENTS.items():
        assert spec["type"] in ("bool", "quantity", "capacity"), key
        assert spec.get("scope") in ("org", "account"), key


def test_coerce():
    assert _coerce("bool", "true") is True
    assert _coerce("bool", "0") is False
    assert _coerce("quantity", "12") == 12
    assert _coerce("capacity", "5.9") == 5      # truncates to int TB
    assert _coerce("quantity", "junk") == 0
