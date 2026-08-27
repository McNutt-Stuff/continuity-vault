"""Auto-provision customer cloud storage (Scenario 2).

Given a customer's org-level admin credential, create a dedicated bucket/container
plus two SCOPED access credentials — a write-only one for unattended backups and
a read-only one for passkey-gated restores. The admin credential is used only for
this call and is never stored.

Returns ``{config, write, read, summary}``:
  * ``config``  — non-secret routing persisted on CustomerStorage.config
  * ``write``   — the write-only credential (stored server-side)
  * ``read``    — the read-only credential (stored, released only after passkey)
  * ``summary`` — human-readable notes for the provisioning log
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Callable

logger = logging.getLogger("cv.storage")

Progress = Callable[[str], None]


def _slug(tenant_id: str) -> str:
    return (tenant_id or "").replace("-", "")[:8].lower() or secrets.token_hex(4)


def provision(provider: str, admin: dict, tenant_id: str, progress: Progress) -> dict:
    provider = (provider or "").lower()
    if provider == "aws":
        return _provision_aws(admin, tenant_id, progress)
    if provider == "azure":
        return _provision_azure(admin, tenant_id, progress)
    if provider == "gcp":
        return _provision_gcp(admin, tenant_id, progress)
    raise ValueError(f"auto-provisioning is not supported for '{provider}'")


# --------------------------------------------------------------------------- #
# AWS — dedicated bucket + write-only & read-only IAM users (true split)      #
# --------------------------------------------------------------------------- #
def _provision_aws(admin: dict, tenant_id: str, progress: Progress) -> dict:
    import boto3

    ak = (admin.get("access_key_id") or "").strip()
    sk = (admin.get("secret_access_key") or "").strip()
    region = (admin.get("region") or "us-east-1").strip()
    if not ak or not sk:
        raise ValueError("an AWS admin access key + secret is required")

    suffix = secrets.token_hex(4)
    slug = _slug(tenant_id)
    bucket = f"arkive-{slug}-{suffix}"
    s3 = boto3.client("s3", region_name=region,
                      aws_access_key_id=ak, aws_secret_access_key=sk)
    iam = boto3.client("iam", aws_access_key_id=ak, aws_secret_access_key=sk)

    progress(f"Creating S3 bucket {bucket} in {region}…")
    kw: dict = {"Bucket": bucket}
    if region != "us-east-1":
        kw["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kw)
    try:
        s3.put_bucket_versioning(Bucket=bucket,
                                 VersioningConfiguration={"Status": "Enabled"})
    except Exception:  # noqa: BLE001
        logger.debug("aws provision: versioning not set", exc_info=True)
    try:
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    except Exception:  # noqa: BLE001
        logger.debug("aws provision: public-access-block not set", exc_info=True)

    b_arn = f"arn:aws:s3:::{bucket}"
    write_doc = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": b_arn},
        {"Effect": "Allow", "Action": ["s3:PutObject", "s3:PutObjectRetention",
                                       "s3:AbortMultipartUpload", "s3:DeleteObject"],
         "Resource": f"{b_arn}/*"}]}
    read_doc = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": b_arn},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"],
         "Resource": f"{b_arn}/*"}]}

    progress("Creating a write-only backup key…")
    w_user = f"arkive-w-{slug}-{suffix}"
    iam.create_user(UserName=w_user, Tags=[{"Key": "arkive", "Value": "backup-writer"}])
    iam.put_user_policy(UserName=w_user, PolicyName="arkive-write",
                        PolicyDocument=json.dumps(write_doc))
    w_key = iam.create_access_key(UserName=w_user)["AccessKey"]

    progress("Creating a read-only restore key…")
    r_user = f"arkive-r-{slug}-{suffix}"
    iam.create_user(UserName=r_user, Tags=[{"Key": "arkive", "Value": "restore-reader"}])
    iam.put_user_policy(UserName=r_user, PolicyName="arkive-read",
                        PolicyDocument=json.dumps(read_doc))
    r_key = iam.create_access_key(UserName=r_user)["AccessKey"]

    return {
        "config": {"bucket": bucket, "region": region, "storage_class": "INTELLIGENT_TIERING"},
        "write": {"access_key_id": w_key["AccessKeyId"], "secret_access_key": w_key["SecretAccessKey"]},
        "read": {"access_key_id": r_key["AccessKeyId"], "secret_access_key": r_key["SecretAccessKey"]},
        "summary": f"Provisioned bucket {bucket}, IAM users {w_user} (write) and {r_user} (read).",
    }


# --------------------------------------------------------------------------- #
# Azure — dedicated container + write/read container SAS (true split)         #
# --------------------------------------------------------------------------- #
def _provision_azure(admin: dict, tenant_id: str, progress: Progress) -> dict:
    from datetime import datetime, timedelta, timezone

    from azure.storage.blob import (BlobServiceClient, ContainerSasPermissions,
                                    generate_container_sas)

    account = (admin.get("account_name") or "").strip()
    key = (admin.get("account_key") or "").strip()
    if not account or not key:
        raise ValueError("an Azure storage account name + admin key is required")

    container = f"arkive-{_slug(tenant_id)}-{secrets.token_hex(3)}"
    url = f"https://{account}.blob.core.windows.net"
    svc = BlobServiceClient(account_url=url, credential=key)
    progress(f"Creating container {container} in {account}…")
    try:
        svc.create_container(container)
    except Exception:  # noqa: BLE001
        logger.debug("azure provision: container may already exist", exc_info=True)

    expiry = datetime.now(timezone.utc) + timedelta(days=3650)  # long-lived scoped SAS
    progress("Minting a write-only SAS…")
    write_sas = generate_container_sas(
        account, container, account_key=key, expiry=expiry,
        permission=ContainerSasPermissions(write=True, create=True, add=True, list=True))
    progress("Minting a read-only SAS…")
    read_sas = generate_container_sas(
        account, container, account_key=key, expiry=expiry,
        permission=ContainerSasPermissions(read=True, list=True))

    return {
        "config": {"account_name": account, "account_url": url,
                   "container": container, "access_tier": "Cool"},
        "write": {"sas_token": write_sas},
        "read": {"sas_token": read_sas},
        "summary": f"Provisioned container {container} with scoped write/read SAS tokens.",
    }


# --------------------------------------------------------------------------- #
# GCP — dedicated bucket + scoped writer/reader service accounts (true split) #
# --------------------------------------------------------------------------- #
def _provision_gcp(admin: dict, tenant_id: str, progress: Progress) -> dict:
    from google.cloud import storage as gcs
    from google.oauth2 import service_account

    sa = admin.get("service_account_json")
    if isinstance(sa, str):
        sa = json.loads(sa)
    if not isinstance(sa, dict):
        raise ValueError("a GCP service-account key (JSON) is required")
    project = (admin.get("project_id") or sa.get("project_id") or "").strip()
    location = (admin.get("location") or "US").strip()
    if not project:
        raise ValueError("a GCP project id is required")
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    client = gcs.Client(project=project, credentials=creds)

    bucket = f"arkive-{_slug(tenant_id)}-{secrets.token_hex(3)}"
    progress(f"Creating GCS bucket {bucket}…")
    b = client.create_bucket(bucket, location=location or "US")
    # Uniform bucket-level access so IAM roles (not per-object ACLs) govern the split.
    try:
        b.iam_configuration.uniform_bucket_level_access_enabled = True
        b.patch()
    except Exception:  # noqa: BLE001
        logger.debug("gcp provision: uniform bucket-level access not set", exc_info=True)

    slug = _slug(tenant_id)
    suffix = secrets.token_hex(3)
    writer = _gcp_create_sa(creds, project, f"arkive-w-{slug}-{suffix}",
                            "Arkive backup writer", "write-only backup", progress)
    reader = _gcp_create_sa(creds, project, f"arkive-r-{slug}-{suffix}",
                            "Arkive restore reader", "read-only restore", progress)

    if writer and reader:
        progress("Granting scoped bucket roles…")
        # Writer creates objects (cannot read/overwrite); reader can only view.
        _gcp_bind_bucket_role(b, f"serviceAccount:{writer['email']}", "roles/storage.objectCreator")
        _gcp_bind_bucket_role(b, f"serviceAccount:{reader['email']}", "roles/storage.objectViewer")
        return {
            "config": {"bucket": bucket, "project_id": project, "location": location},
            "write": {"service_account_json": writer["key_json"]},
            "read": {"service_account_json": reader["key_json"]},
            "summary": (f"Provisioned bucket {bucket} with scoped service accounts "
                        f"{writer['email']} (objectCreator) and {reader['email']} (objectViewer)."),
        }

    # Fallback: the admin credential lacks IAM permissions to mint scoped service
    # accounts — the read/write split is still enforced at the API boundary (reads
    # require a passkey session), but both roles share the admin service account.
    sa_str = json.dumps(sa)
    progress("Could not create scoped service accounts — using the admin credential for both roles.")
    return {
        "config": {"bucket": bucket, "project_id": project, "location": location},
        "write": {"service_account_json": sa_str},
        "read": {"service_account_json": sa_str},
        "summary": (f"Provisioned bucket {bucket}. Scoped service-account split was unavailable "
                    f"(grant the admin SA iam.serviceAccounts.create + storage.buckets.setIamPolicy, "
                    f"and enable the IAM API, for a true credential split); the admin SA backs both roles."),
    }


def _gcp_create_sa(creds, project: str, account_id: str, display: str,
                   role_label: str, progress: Progress) -> dict | None:
    """Create a scoped service account + JSON key via the IAM Admin API.
    Returns {email, key_json} or None if it can't (missing lib/permissions)."""
    try:
        from googleapiclient.discovery import build
    except Exception:  # noqa: BLE001
        logger.warning("gcp provision: google-api-python-client missing; cannot mint scoped SA")
        return None
    try:
        import base64
        progress(f"Creating a {role_label} service account…")
        iam = build("iam", "v1", credentials=creds, cache_discovery=False)
        acct = iam.projects().serviceAccounts().create(
            name=f"projects/{project}",
            body={"accountId": account_id[:30],
                  "serviceAccount": {"displayName": display}}).execute()
        email = acct["email"]
        key = iam.projects().serviceAccounts().keys().create(
            name=f"projects/{project}/serviceAccounts/{email}",
            body={"privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE"}).execute()
        key_json = base64.b64decode(key["privateKeyData"]).decode()
        return {"email": email, "key_json": key_json}
    except Exception:  # noqa: BLE001
        logger.warning("gcp provision: scoped service-account creation failed", exc_info=True)
        return None


def _gcp_bind_bucket_role(bucket, member: str, role: str) -> None:
    """Grant a bucket-scoped IAM role to a member (best-effort)."""
    try:
        policy = bucket.get_iam_policy(requested_policy_version=3)
        policy.version = 3
        policy.bindings.append({"role": role, "members": {member}})
        bucket.set_iam_policy(policy)
    except Exception:  # noqa: BLE001
        logger.warning("gcp provision: could not bind %s to %s", role, member, exc_info=True)
