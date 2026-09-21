"""Signed federated entitlement snapshot (Phase 13).

The control plane mints a signed, effective-dated snapshot of each tenant's
entitlements + feature flags and replicates it to the tenant's node. A node
validates the control-plane fleet signature AND the expiry before trusting a
snapshot for enforcement, and may keep using a non-expired cached copy while the
control plane is unreachable (offline-safe).

This is a tamper-evident layer on top of the already-replicated raw tables: a node
derives the same values locally, but the signed snapshot lets it PROVE the values
came from the control plane and detect drift/tampering.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from .models import EntitlementSnapshot, Tenant

# How long a snapshot stays valid — the offline window a node can enforce on a
# cached copy without a fresh control-plane push.
SNAPSHOT_TTL_HOURS = 48
_REFRESH_WHEN_UNDER_HOURS = 12   # re-mint when less than this remains


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_payload(db: Session, tenant: Tenant, version: int) -> dict:
    """The signed body: plan + effective entitlements + effective feature flags
    (tenant-scoped — org members inherit these)."""
    from . import entitlements, features
    ents: dict = {}
    try:
        for k, e in entitlements.derive(db, tenant).items():
            ents[k] = bool(e.value) if e.type == "bool" else int(e.value or 0)
    except Exception:  # noqa: BLE001
        ents = {}
    flags = {name: bool(features.resolve(None, tenant, name, db)) for name in features.FLAGS}
    now = _now()
    return {
        "tenant_id": tenant.id, "plan": (tenant.plan or ""),
        "entitlements": ents, "flags": flags, "version": int(version),
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=SNAPSHOT_TTL_HOURS)).isoformat(),
    }


def build_and_sign(db: Session, tenant: Tenant) -> EntitlementSnapshot:
    """Mint + sign a fresh snapshot for a tenant (CONTROL PLANE only — uses the
    fleet signer). Upserts the row; caller need not commit (this commits)."""
    from . import fleet
    row = db.get(EntitlementSnapshot, tenant.id)
    version = (int(row.version) + 1) if row else 1
    payload = _build_payload(db, tenant, version)
    signature = fleet.fleet_signer().sign(payload)
    if row is None:
        row = EntitlementSnapshot(tenant_id=tenant.id)
        db.add(row)
    row.version = version
    row.payload = payload
    row.signature = signature
    row.signer_fingerprint = fleet.signer_fingerprint()
    row.issued_at = _now()
    row.expires_at = _now() + timedelta(hours=SNAPSHOT_TTL_HOURS)
    db.commit()
    return row


def verify(payload: dict, signature: dict) -> bool:
    """Verify a snapshot's control-plane signature (hybrid classical + PQ). Works on
    the CP and on any node that has adopted the control-plane fleet signer."""
    from . import fleet
    from cv_crypto.signing import HybridVerifier, SigPolicy
    try:
        return HybridVerifier.from_bundle(fleet.cloud_public_bundle()).verify(
            payload, signature, SigPolicy.REQUIRE_BOTH)
    except Exception:  # noqa: BLE001
        return False


def _expired(payload: dict, row: EntitlementSnapshot) -> bool:
    if row.expires_at is not None:
        return row.expires_at < _now()
    exp = (payload or {}).get("expires_at")
    try:
        return exp is not None and datetime.fromisoformat(exp).replace(tzinfo=None) < _now()
    except (TypeError, ValueError):
        return True


def get_valid(db: Session, tenant_id: str) -> dict | None:
    """The validated, non-expired snapshot payload for a tenant, or ``None``.
    Offline-safe: a cached snapshot within its expiry is trusted even if the control
    plane is unreachable. ``None`` means the caller should fall back to local derive."""
    row = db.get(EntitlementSnapshot, tenant_id)
    if row is None or not row.payload or not row.signature:
        return None
    if _expired(row.payload, row):
        return None
    if not verify(row.payload, row.signature):
        return None
    return row.payload


def status(db: Session, tenant_id: str) -> dict:
    """Diagnostic view: presence, version, validity, expiry + the signed payload."""
    row = db.get(EntitlementSnapshot, tenant_id)
    if row is None:
        return {"tenant_id": tenant_id, "present": False}
    sig_ok = verify(row.payload or {}, row.signature or {})
    return {
        "tenant_id": tenant_id, "present": True, "version": row.version,
        "signature_valid": sig_ok, "expired": _expired(row.payload or {}, row),
        "signer_fingerprint": row.signer_fingerprint,
        "issued_at": row.issued_at.isoformat() if row.issued_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "payload": row.payload,
    }


def refresh_all(db: Session, *, force: bool = False) -> int:
    """Mint/refresh snapshots for every tenant (CONTROL PLANE only). Skips snapshots
    that are still comfortably fresh unless ``force``. Returns how many were minted."""
    cutoff = _now() + timedelta(hours=_REFRESH_WHEN_UNDER_HOURS)
    n = 0
    for t in db.query(Tenant).all():
        row = db.get(EntitlementSnapshot, t.id)
        if not force and row is not None and row.expires_at and row.expires_at > cutoff:
            continue
        try:
            build_and_sign(db, t)
            n += 1
        except Exception:  # noqa: BLE001 — one tenant must not stop the sweep
            db.rollback()
    return n
