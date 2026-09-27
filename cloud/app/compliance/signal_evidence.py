"""Signal-backed compliance evidence.

Maps the shared Signal Platform's device-posture + collection signals onto the
compliance capabilities the engine already knows (device_encryption,
device_compliance, security_posture, monitoring). This is the "Signal → automated
test → control result → evidence" path (spec §25): Compliance CONSUMES the shared
signals rather than collecting its own. Contributes nothing when a tenant has no
signals, so it never regresses a tenant that isn't using the Signal Platform.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .providers import CapabilityEvidence, register_provider


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _signals_provider(db: Session, tenant, scope: dict) -> list[CapabilityEvidence]:
    from ..models import Signal, SignalProviderHealth
    tid = tenant.id
    ev: list[CapabilityEvidence] = []

    def posture_pop(signal_type: str) -> tuple[int, int]:
        """(covered=true, total) for an endpoint boolean posture signal."""
        rows = (db.query(Signal.subject_id, Signal.normalized_value)
                .filter(Signal.tenant_id == tid, Signal.status == "active",
                        Signal.signal_type == signal_type,
                        Signal.freshness_state == "fresh").all())
        total = len(rows)
        covered = sum(1 for _, v in rows if v == "true")
        return covered, total

    # device_encryption — endpoints with disk encryption on.
    cov, tot = posture_pop("endpoint.disk_encryption.enabled")
    if tot:
        status = "met" if cov == tot else ("partial" if cov else "unmet")
        ev.append(CapabilityEvidence(
            capability="device_encryption", status=status, provider="signals",
            summary=f"{cov}/{tot} endpoints have disk encryption enabled",
            scope_type="agent", expected=tot, covered=cov, failed=tot - cov,
            evidence_level="observed",
            remediation="Enable FileVault/BitLocker on endpoints reporting it off."))

    # device_compliance — firewall + secure boot + auto-update all on.
    fw_c, fw_t = posture_pop("endpoint.firewall.enabled")
    sb_c, sb_t = posture_pop("endpoint.secure_boot.enabled")
    au_c, au_t = posture_pop("endpoint.auto_update.enabled")
    pop = max(fw_t, sb_t, au_t)
    if pop:
        # An endpoint is "compliant" only if every REPORTED posture control is on.
        worst = min([c for c, t in ((fw_c, fw_t), (sb_c, sb_t), (au_c, au_t)) if t] or [pop])
        status = "met" if worst == pop else ("partial" if worst else "unmet")
        ev.append(CapabilityEvidence(
            capability="device_compliance", status=status, provider="signals",
            summary=f"{worst}/{pop} endpoints meet firewall/secure-boot/auto-update baseline",
            scope_type="agent", expected=pop, covered=worst, failed=pop - worst,
            evidence_level="observed",
            remediation="Enable firewall, secure boot and automatic updates on non-compliant endpoints."))

    # monitoring + security_posture — is the Signal Platform actively collecting?
    healthy = [h for h in db.query(SignalProviderHealth)
               .filter(SignalProviderHealth.tenant_id == tid).all()
               if h.connection_status == "ok"]
    total_signals = int(db.query(Signal.id).filter(
        Signal.tenant_id == tid, Signal.status == "active").count())
    stale = int(db.query(Signal.id).filter(
        Signal.tenant_id == tid, Signal.status == "active",
        Signal.freshness_state.in_(["stale", "expired"])).count())
    if total_signals:
        fresh_ratio = (total_signals - stale) / total_signals if total_signals else 0
        status = "met" if (healthy and fresh_ratio >= 0.9) else "partial"
        ev.append(CapabilityEvidence(
            capability="monitoring", status=status, provider="signals",
            summary=f"{len(healthy)} provider(s) reporting; {total_signals:,} signals "
                    f"({int(fresh_ratio * 100)}% fresh)",
            evidence_level="observed", detail={"providers": len(healthy), "stale": stale}))
        ev.append(CapabilityEvidence(
            capability="security_posture", status=status, provider="signals",
            summary="Continuous signal collection across the environment",
            evidence_level="observed"))
    return ev


register_provider("signals", _signals_provider)
