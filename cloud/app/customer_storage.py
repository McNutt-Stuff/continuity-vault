"""Bring-your-own cloud storage (CustomerStorage) — the third backup tier.

A customer points Arkive at their own AWS S3 / Azure Blob / GCS bucket. Data is
written already-encrypted (the same quantum-safe envelope as every destination),
so the provider only ever holds ciphertext.

Credential split (the security model the customer asked for):
  * the WRITE credential lives server-side (credstore-encrypted) so unattended
    backups keep flowing — ideally a write-only key that cannot read data back;
  * the READ credential is only decrypted to serve a RESTORE, which the API gates
    behind a passkey-verified session.

This module is import-safe for both the API layer and the sync/restore workers
(no FastAPI/app imports), so destination resolution has one home.
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional, Tuple

from . import credstore
from .models import CustomerStorage
from .storage import ProtectionDestination, destination_from_customer_storage

logger = logging.getLogger("cv.storage")

# Destination-id prefix used in Collection.destinations / SnapshotReceipt.destination.
DEST_PREFIX = "byos:"

# Provider field specs driving the "existing storage" setup dialog. config =
# non-secret routing; write/read = the two credential sets (Read vs Write).
PROVIDERS: dict[str, dict] = {
    "aws": {
        "provider": "aws", "display_name": "Amazon S3", "icon": "cloud", "color": "#FF9900",
        "config": [
            {"name": "bucket", "label": "Bucket name", "placeholder": "my-arkive-backups", "required": True},
            {"name": "region", "label": "Region", "placeholder": "us-east-1", "required": True},
            {"name": "storage_class", "label": "Storage class", "placeholder": "INTELLIGENT_TIERING", "required": False},
            {"name": "endpoint_url", "label": "Custom endpoint (S3-compatible, optional)", "placeholder": "", "required": False},
        ],
        "write": [
            {"name": "access_key_id", "label": "Write access key ID", "required": True},
            {"name": "secret_access_key", "label": "Write secret access key", "required": True, "secret": True},
        ],
        "read": [
            {"name": "access_key_id", "label": "Read access key ID", "required": False},
            {"name": "secret_access_key", "label": "Read secret access key", "required": False, "secret": True},
        ],
    },
    "azure": {
        "provider": "azure", "display_name": "Azure Blob Storage", "icon": "cloud", "color": "#0089D6",
        "config": [
            {"name": "account_name", "label": "Storage account name", "placeholder": "myarkivestore", "required": True},
            {"name": "container", "label": "Container name", "placeholder": "arkive-backups", "required": True},
            {"name": "access_tier", "label": "Access tier", "placeholder": "Cool", "required": False},
        ],
        "write": [
            {"name": "account_key", "label": "Write access key", "required": False, "secret": True},
            {"name": "connection_string", "label": "…or a write connection string", "required": False, "secret": True},
        ],
        "read": [
            {"name": "account_key", "label": "Read access key", "required": False, "secret": True},
            {"name": "connection_string", "label": "…or a read connection string", "required": False, "secret": True},
        ],
    },
    "gcp": {
        "provider": "gcp", "display_name": "Google Cloud Storage", "icon": "cloud", "color": "#4285F4",
        "config": [
            {"name": "bucket", "label": "Bucket name", "placeholder": "my-arkive-backups", "required": True},
            {"name": "project_id", "label": "Project ID", "placeholder": "my-gcp-project", "required": True},
            {"name": "location", "label": "Location", "placeholder": "US", "required": False},
        ],
        "write": [
            {"name": "service_account_json", "label": "Write service-account key (JSON)", "required": True, "secret": True},
        ],
        "read": [
            {"name": "service_account_json", "label": "Read service-account key (JSON)", "required": False, "secret": True},
        ],
    },
}


def provider_spec(provider: str) -> Optional[dict]:
    return PROVIDERS.get((provider or "").lower())


def enc_credentials(tenant_id: str, creds: dict) -> Optional[str]:
    creds = {k: v for k, v in (creds or {}).items() if v not in (None, "")}
    return credstore.encrypt(tenant_id, creds) if creds else None


def dec_credentials(tenant_id: str, blob: Optional[str]) -> dict:
    if not blob:
        return {}
    try:
        return credstore.decrypt(tenant_id, blob)
    except Exception:  # noqa: BLE001
        return {}


def _prefix(cs: CustomerStorage) -> str:
    """Object-key namespace inside the customer's bucket. A configured prefix lets
    one bucket safely hold multiple things; tenant id keeps snapshots isolated."""
    p = (cs.config or {}).get("prefix") or ""
    p = str(p).strip("/ ")
    return f"{p}/{cs.tenant_id}" if p else cs.tenant_id


def build_destination(db, cs: CustomerStorage, mode: str = "write") -> Optional[ProtectionDestination]:
    """Live destination for this storage. mode='write' uses the write credential
    (automated backups); mode='read' uses the read credential, falling back to
    the write credential when a separate read key wasn't supplied."""
    if mode == "read":
        creds = dec_credentials(cs.tenant_id, cs.read_credentials) \
            or dec_credentials(cs.tenant_id, cs.write_credentials)
    else:
        creds = dec_credentials(cs.tenant_id, cs.write_credentials)
    return destination_from_customer_storage(cs.provider, cs.config or {}, creds)


def object_prefix(cs: CustomerStorage) -> str:
    return _prefix(cs)


def storage_id_from_dest(dest_kind: str) -> Optional[str]:
    if dest_kind and dest_kind.startswith(DEST_PREFIX):
        return dest_kind[len(DEST_PREFIX):]
    return None


def get_for_tenant(db, tenant_id: str, storage_id: str) -> Optional[CustomerStorage]:
    cs = db.get(CustomerStorage, storage_id)
    if not cs or cs.tenant_id != tenant_id:
        return None
    return cs


def test_storage(db, cs: CustomerStorage) -> Tuple[bool, str]:
    """Round-trip a tiny probe: PUT with the write credential (and, when a read
    credential exists, read it back with that). Returns (ok, error)."""
    key = f"_arkive_healthcheck/{secrets.token_hex(6)}"
    probe = secrets.token_hex(16).encode()
    prefix = _prefix(cs)
    try:
        w = build_destination(db, cs, "write")
        if w is None:
            return False, "storage is not fully configured"
        w.put_object(prefix, key, probe, immutable=False)
    except Exception as exc:  # noqa: BLE001
        return False, f"write failed: {str(exc)[:240]}"
    read_ok = True
    read_err = ""
    if cs.read_credentials:
        try:
            r = build_destination(db, cs, "read")
            got = r.get_object(prefix, key) if r else None
            read_ok = got == probe
            if not read_ok:
                read_err = "read-back mismatch"
        except Exception as exc:  # noqa: BLE001
            read_ok = False
            read_err = f"read failed: {str(exc)[:240]}"
    try:
        w.delete_object(prefix, key)
    except Exception:  # noqa: BLE001
        pass
    if not read_ok:
        return False, read_err or "read verification failed"
    return True, ""
