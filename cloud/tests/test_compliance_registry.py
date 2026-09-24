"""Compliance registry — capability / framework integrity (app Python 3.14).

No pytest harness is wired yet (see the compliance-engine roadmap); these are
dependency-light and meant to run under the app env. Run: ``pytest cloud/tests``.
"""

from cloud.app.compliance import registry


def test_every_framework_capability_is_defined():
    caps = set(registry.CAPABILITIES)
    for fw, spec in registry.FRAMEWORKS.items():
        for c in spec["controls"]:
            for cap in c["capabilities"]:
                assert cap in caps, f"{fw}/{c['id']} references undefined capability {cap}"


def test_capabilities_have_required_keys():
    for key, spec in registry.CAPABILITIES.items():
        assert spec.get("title"), key
        assert spec.get("description"), key
        assert spec.get("domain"), key


def test_scope_aware_and_posture_capabilities_present_and_mapped():
    new = ["coverage_completeness", "backup_freshness", "phishing_resistant_mfa",
           "privileged_mfa", "privileged_access_review", "guest_access",
           "integrity_verified", "restore_test",
           # Phase 2 — deepened M365 automated posture.
           "security_posture", "device_compliance", "device_encryption", "password_policy"]
    for cap in new:
        assert cap in registry.CAPABILITIES, f"{cap} missing from CAPABILITIES"
        mapped = sum(1 for _fw, s in registry.FRAMEWORKS.items()
                     for c in s["controls"] if cap in c["capabilities"])
        assert mapped >= 1, f"{cap} not mapped into any control"


def test_framework_ids_stable():
    # Control ids + posture history depend on these framework ids staying stable.
    assert set(registry.FRAMEWORKS) >= {"nist_csf", "cis", "hipaa", "iso_27001", "soc2", "gdpr"}


def test_control_ids_unique_within_framework():
    for fw, spec in registry.FRAMEWORKS.items():
        ids = [c["id"] for c in spec["controls"]]
        assert len(ids) == len(set(ids)), f"duplicate control id in {fw}"


def test_attestable_capabilities_present_and_mapped():
    attestable = [k for k, s in registry.CAPABILITIES.items() if s.get("attestable")]
    # The Phase-1 procedural capabilities must exist and each be mapped into a control.
    expected = {"security_policy", "risk_assessment", "security_training",
                "incident_response_plan", "continuity_plan", "vendor_risk_management",
                "access_review", "change_management", "vulnerability_management",
                "penetration_testing", "physical_security", "personnel_security"}
    assert expected <= set(attestable), expected - set(attestable)
    for cap in attestable:
        mapped = sum(1 for _fw, s in registry.FRAMEWORKS.items()
                     for c in s["controls"] if cap in c["capabilities"])
        assert mapped >= 1, f"attestable {cap} not mapped into any control"

