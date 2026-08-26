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
import time
from typing import Optional, Tuple

from . import credstore
from .models import CustomerStorage
from .storage import ProtectionDestination, destination_from_customer_storage

logger = logging.getLogger("cv.storage")

# Destination-id prefix used in Collection.destinations / SnapshotReceipt.destination.
DEST_PREFIX = "byos:"

# Region / location option lists (value → friendly label) for the setup dropdowns.
AWS_REGIONS = [
    {"value": "us-east-1", "label": "US East (N. Virginia)"},
    {"value": "us-east-2", "label": "US East (Ohio)"},
    {"value": "us-west-1", "label": "US West (N. California)"},
    {"value": "us-west-2", "label": "US West (Oregon)"},
    {"value": "ca-central-1", "label": "Canada (Central)"},
    {"value": "eu-west-1", "label": "Europe (Ireland)"},
    {"value": "eu-west-2", "label": "Europe (London)"},
    {"value": "eu-west-3", "label": "Europe (Paris)"},
    {"value": "eu-central-1", "label": "Europe (Frankfurt)"},
    {"value": "eu-north-1", "label": "Europe (Stockholm)"},
    {"value": "eu-south-1", "label": "Europe (Milan)"},
    {"value": "ap-south-1", "label": "Asia Pacific (Mumbai)"},
    {"value": "ap-southeast-1", "label": "Asia Pacific (Singapore)"},
    {"value": "ap-southeast-2", "label": "Asia Pacific (Sydney)"},
    {"value": "ap-northeast-1", "label": "Asia Pacific (Tokyo)"},
    {"value": "ap-northeast-2", "label": "Asia Pacific (Seoul)"},
    {"value": "ap-east-1", "label": "Asia Pacific (Hong Kong)"},
    {"value": "sa-east-1", "label": "South America (São Paulo)"},
    {"value": "me-south-1", "label": "Middle East (Bahrain)"},
    {"value": "af-south-1", "label": "Africa (Cape Town)"},
]
GCP_LOCATIONS = [
    {"value": "US", "label": "United States (multi-region)"},
    {"value": "EU", "label": "European Union (multi-region)"},
    {"value": "ASIA", "label": "Asia (multi-region)"},
    {"value": "us-central1", "label": "Iowa (us-central1)"},
    {"value": "us-east1", "label": "South Carolina (us-east1)"},
    {"value": "us-east4", "label": "N. Virginia (us-east4)"},
    {"value": "us-west1", "label": "Oregon (us-west1)"},
    {"value": "us-west2", "label": "Los Angeles (us-west2)"},
    {"value": "europe-west1", "label": "Belgium (europe-west1)"},
    {"value": "europe-west2", "label": "London (europe-west2)"},
    {"value": "europe-west3", "label": "Frankfurt (europe-west3)"},
    {"value": "europe-north1", "label": "Finland (europe-north1)"},
    {"value": "asia-east1", "label": "Taiwan (asia-east1)"},
    {"value": "asia-northeast1", "label": "Tokyo (asia-northeast1)"},
    {"value": "asia-southeast1", "label": "Singapore (asia-southeast1)"},
    {"value": "australia-southeast1", "label": "Sydney (australia-southeast1)"},
]

# Provider field specs driving the setup dialogs. config = non-secret routing;
# write/read = the two credential sets; provision = the org-admin credential used
# once to auto-create everything; help = step-by-step for where to find it.
PROVIDERS: dict[str, dict] = {
    "aws": {
        "provider": "aws", "display_name": "Amazon S3", "icon": "cloud", "color": "#FF9900",
        "help": [
            "Sign in to the AWS Console and open IAM.",
            "Go to Users → Create user (or pick an existing admin user).",
            "Attach a policy granting S3 + IAM access (e.g. AdministratorAccess).",
            "Open the user → Security credentials → Create access key → \u201cApplication running outside AWS\u201d.",
            "Copy the Access key ID and Secret access key below, then pick your region.",
        ],
        "config": [
            {"name": "bucket", "label": "Bucket name", "placeholder": "my-arkive-backups", "required": True},
            {"name": "region", "label": "Region", "required": True, "options": AWS_REGIONS},
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
        "provision": [
            {"name": "access_key_id", "label": "Admin access key ID", "required": True},
            {"name": "secret_access_key", "label": "Admin secret access key", "required": True, "secret": True},
            {"name": "region", "label": "Region", "required": True, "options": AWS_REGIONS},
        ],
    },
    "azure": {
        "provider": "azure", "display_name": "Azure Blob Storage", "icon": "cloud", "color": "#0089D6",
        "help": [
            "Open the Azure Portal → Storage accounts.",
            "Select an existing storage account (or create one).",
            "Go to Security + networking → Access keys → Show keys.",
            "Copy the Storage account name and key1 below.",
        ],
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
        "provision": [
            {"name": "account_name", "label": "Storage account name", "required": True},
            {"name": "account_key", "label": "Admin account key", "required": True, "secret": True},
        ],
    },
    "gcp": {
        "provider": "gcp", "display_name": "Google Cloud Storage", "icon": "cloud", "color": "#4285F4",
        "help": [
            "Open the Google Cloud Console → IAM & Admin → Service Accounts.",
            "Create a service account and grant it the \u201cStorage Admin\u201d role.",
            "Open it → Keys → Add key → Create new key → JSON, and download the file.",
            "Paste the JSON file contents below, and enter your Project ID.",
        ],
        "config": [
            {"name": "bucket", "label": "Bucket name", "placeholder": "my-arkive-backups", "required": True},
            {"name": "project_id", "label": "Project ID", "placeholder": "my-gcp-project", "required": True},
            {"name": "location", "label": "Location", "required": False, "options": GCP_LOCATIONS},
        ],
        "write": [
            {"name": "service_account_json", "label": "Write service-account key (JSON)", "required": True, "secret": True},
        ],
        "read": [
            {"name": "service_account_json", "label": "Read service-account key (JSON)", "required": False, "secret": True},
        ],
        "provision": [
            {"name": "service_account_json", "label": "Admin service-account key (JSON)", "required": True, "secret": True},
            {"name": "project_id", "label": "Project ID", "required": True},
            {"name": "location", "label": "Location", "required": False, "options": GCP_LOCATIONS},
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


def apply_provision_result(db, cs: CustomerStorage, result: dict) -> None:
    """Persist an auto-provisioning result onto the instance: the resolved routing
    config + the two SCOPED credentials (write-only, read-only). Does not commit."""
    cs.config = {**(cs.config or {}), **(result.get("config") or {})}
    cs.write_credentials = enc_credentials(cs.tenant_id, result.get("write") or {})
    cs.read_credentials = enc_credentials(cs.tenant_id, result.get("read") or {})


def test_storage_retry(db, cs: CustomerStorage, attempts: int = 6,
                       delay: float = 5.0) -> Tuple[bool, str]:
    """Health test with retries — freshly minted cloud keys can take a few seconds
    to become active (e.g. AWS IAM eventual consistency)."""
    last = ""
    for i in range(attempts):
        ok, err = test_storage(db, cs)
        if ok:
            return True, ""
        last = err
        if i < attempts - 1:
            time.sleep(delay)
    return False, last


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
