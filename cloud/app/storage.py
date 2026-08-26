"""
ProtectionDestination interface (spec LLM build-instruction 1).

Cloud and appliance storage destinations sit behind one common interface so a
protection policy can target any combination without the ingest pipeline caring
where bytes land. Concrete destinations:

- ``CVCloudDestination``      Arkive public cloud (S3 / MinIO), Model A
- ``CustomerS3Destination``   customer-owned S3 bucket, Model B
- ``ApplianceDestination``    hands off to the appliance agent, Model C
- ``LocalFsDestination``      developer fallback (no cloud creds required)

Objects are always written already-encrypted. Recovery storage uses
tenant-scoped key prefixes; object-lock/immutability is requested where the
backend supports it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

from .config import get_settings

settings = get_settings()
log = logging.getLogger("arkive.storage")


class ProtectionDestination(ABC):
    name: str = "base"

    @abstractmethod
    def put_object(self, tenant_prefix: str, key: str, data: bytes,
                   immutable: bool = True) -> str:
        ...

    @abstractmethod
    def get_object(self, tenant_prefix: str, key: str) -> bytes:
        ...

    @abstractmethod
    def put_manifest(self, tenant_prefix: str, snapshot_id: str, manifest: dict) -> str:
        ...

    def delete_object(self, tenant_prefix: str, key: str) -> None:
        """Remove an object. Best-effort; overridden by concrete backends."""
        raise NotImplementedError

    def probe(self) -> str:
        """Write, read back and remove a small probe object to prove the
        destination is writable. Raises on any failure; returns a short
        description of what was verified on success."""
        token = secrets.token_hex(8)
        key = f"healthcheck/{token}"
        payload = b"arkive-storage-check-" + token.encode()
        self.put_object("_platform", key, payload, immutable=False)
        got = self.get_object("_platform", key)
        if got != payload:
            raise RuntimeError("read-back mismatch: stored bytes differ from what was written")
        try:
            self.delete_object("_platform", key)
        except Exception:
            pass  # cleanup is best-effort — the write + read already proved writeability
        return "wrote, read back and removed a probe object"


class LocalFsDestination(ProtectionDestination):
    name = "local-fs"

    def __init__(self, root: str | None = None) -> None:
        # Configurable so it can live on a writable path (the service mounts
        # the code directory read-only).
        self.root = Path(root or os.environ.get("CV_OBJECT_STORE", "./cv_object_store"))
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, tenant_prefix: str, key: str) -> Path:
        p = self.root / tenant_prefix / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put_object(self, tenant_prefix, key, data, immutable=True) -> str:
        path = self._p(tenant_prefix, key)
        path.write_bytes(data)
        if immutable:
            os.chmod(path, 0o444)  # emulate object-lock
        return str(path)

    def get_object(self, tenant_prefix, key) -> bytes:
        return self._p(tenant_prefix, key).read_bytes()

    def delete_object(self, tenant_prefix, key) -> None:
        path = self._p(tenant_prefix, key)
        try:
            os.chmod(path, 0o644)  # clear the emulated object-lock before unlink
        except Exception:
            pass
        path.unlink(missing_ok=True)

    def put_manifest(self, tenant_prefix, snapshot_id, manifest) -> str:
        path = self._p(tenant_prefix, f"manifests/{snapshot_id}.json")
        path.write_text(json.dumps(manifest))
        return str(path)


class _S3Base(ProtectionDestination):
    def __init__(self, bucket: str, region: str, endpoint_url: Optional[str] = None,
                 access_key: Optional[str] = None, secret_key: Optional[str] = None,
                 storage_class: Optional[str] = None) -> None:
        import boto3  # imported lazily so dev runs without boto3

        self.bucket = bucket
        # Low-cost tier for cloud archival copies (e.g. INTELLIGENT_TIERING,
        # STANDARD_IA). Left unset for MinIO / local S3-compatible dev.
        self.storage_class = storage_class
        self._s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        try:
            self._s3.head_bucket(Bucket=bucket)
        except Exception as exc:
            # Only auto-create when the bucket genuinely doesn't exist (404).
            # A 403 / SignatureDoesNotMatch / region redirect means bad creds or
            # wrong region — re-raise so the real cause surfaces instead of a
            # misleading CreateBucket error (which also needs s3:CreateBucket).
            status = None
            try:
                status = exc.response["ResponseMetadata"]["HTTPStatusCode"]
            except Exception:
                pass
            if status != 404:
                raise
            create_args = {"Bucket": bucket}
            if region != "us-east-1":
                create_args["CreateBucketConfiguration"] = {"LocationConstraint": region}
            self._s3.create_bucket(**create_args)
            try:
                self._s3.put_bucket_versioning(
                    Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
                )
            except Exception:
                pass

    def put_object(self, tenant_prefix, key, data, immutable=True) -> str:
        full = f"{tenant_prefix}/{key}"
        args = {"Bucket": self.bucket, "Key": full, "Body": data}
        if self.storage_class:
            args["StorageClass"] = self.storage_class
        if immutable:
            # Object-lock governance retention (spec 3.1: object-lock enabled).
            args["ObjectLockMode"] = "GOVERNANCE"
        try:
            self._s3.put_object(**args)
        except Exception:
            args.pop("ObjectLockMode", None)
            self._s3.put_object(**args)
        return f"s3://{self.bucket}/{full}"

    def get_object(self, tenant_prefix, key) -> bytes:
        full = f"{tenant_prefix}/{key}"
        return self._s3.get_object(Bucket=self.bucket, Key=full)["Body"].read()

    def delete_object(self, tenant_prefix, key) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=f"{tenant_prefix}/{key}")

    def put_manifest(self, tenant_prefix, snapshot_id, manifest) -> str:
        return self.put_object(
            tenant_prefix, f"manifests/{snapshot_id}.json",
            json.dumps(manifest).encode(), immutable=True,
        )


class CVCloudDestination(_S3Base):
    name = "cv-cloud"


class CustomerS3Destination(_S3Base):
    name = "customer-s3"


class AzureBlobDestination(ProtectionDestination):
    """Arkive Cloud storage backed by Azure Blob (low-cost Cool/Cold tiers).
    Objects are written already-encrypted, mirroring the S3 destinations."""

    name = "cv-cloud"

    def __init__(self, container: str, connection_string: Optional[str] = None,
                 account_url: Optional[str] = None, account_name: Optional[str] = None,
                 account_key: Optional[str] = None, access_tier: str = "Cool") -> None:
        from azure.storage.blob import BlobServiceClient  # lazy import

        self.container = container
        self.access_tier = access_tier or "Cool"
        if connection_string:
            self._svc = BlobServiceClient.from_connection_string(connection_string)
        elif account_url and account_key:
            self._svc = BlobServiceClient(account_url=account_url, credential=account_key)
        elif account_name and account_key:
            url = f"https://{account_name}.blob.core.windows.net"
            self._svc = BlobServiceClient(account_url=url, credential=account_key)
        else:
            raise ValueError("azure storage requires a connection string or account name + key")
        try:
            self._svc.create_container(container)
        except Exception:
            pass  # container already exists

    def _blob(self, tenant_prefix: str, key: str):
        return self._svc.get_blob_client(self.container, f"{tenant_prefix}/{key}")

    def _tier(self):
        try:
            from azure.storage.blob import StandardBlobTier
            return getattr(StandardBlobTier, (self.access_tier or "Cool").upper(), None)
        except Exception:
            return None

    def put_object(self, tenant_prefix, key, data, immutable=True) -> str:
        blob = self._blob(tenant_prefix, key)
        kwargs = {"overwrite": True}
        tier = self._tier()
        if tier is not None:
            kwargs["standard_blob_tier"] = tier
        blob.upload_blob(data, **kwargs)
        return f"azure://{self.container}/{tenant_prefix}/{key}"

    def get_object(self, tenant_prefix, key) -> bytes:
        return self._blob(tenant_prefix, key).download_blob().readall()

    def delete_object(self, tenant_prefix, key) -> None:
        self._blob(tenant_prefix, key).delete_blob()

    def put_manifest(self, tenant_prefix, snapshot_id, manifest) -> str:
        return self.put_object(
            tenant_prefix, f"manifests/{snapshot_id}.json",
            json.dumps(manifest).encode(), immutable=True,
        )


class GCSDestination(ProtectionDestination):
    """Google Cloud Storage destination. Objects are written already-encrypted,
    mirroring the S3/Azure destinations. Auth via a service-account key JSON."""

    name = "gcs"

    def __init__(self, bucket: str, project: Optional[str] = None,
                 credentials_json: Optional[dict] = None, location: str = "US") -> None:
        from google.cloud import storage as gcs  # lazy import
        if credentials_json:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(credentials_json)
            self._client = gcs.Client(project=project or credentials_json.get("project_id"),
                                      credentials=creds)
        else:
            self._client = gcs.Client(project=project)
        self.bucket_name = bucket
        b = self._client.bucket(bucket)
        try:
            if not b.exists():
                b = self._client.create_bucket(bucket, location=location or "US")
        except Exception:
            pass  # bucket may exist but the caller lacks buckets.get — writes still work
        self._bucket = b

    def put_object(self, tenant_prefix, key, data, immutable=True) -> str:
        blob = self._bucket.blob(f"{tenant_prefix}/{key}")
        blob.upload_from_string(data)
        return f"gs://{self.bucket_name}/{tenant_prefix}/{key}"

    def get_object(self, tenant_prefix, key) -> bytes:
        return self._bucket.blob(f"{tenant_prefix}/{key}").download_as_bytes()

    def delete_object(self, tenant_prefix, key) -> None:
        try:
            self._bucket.blob(f"{tenant_prefix}/{key}").delete()
        except Exception:
            pass

    def put_manifest(self, tenant_prefix, snapshot_id, manifest) -> str:
        return self.put_object(
            tenant_prefix, f"manifests/{snapshot_id}.json",
            json.dumps(manifest).encode(), immutable=True,
        )


def destination_from_customer_storage(provider: str, config: dict,
                                      credentials: dict) -> Optional[ProtectionDestination]:
    """Build a live destination for a customer's own storage (bring-your-own).
    ``config`` = non-secret routing (bucket/container/region/…); ``credentials`` =
    the decrypted access keys (write OR read set, depending on the operation)."""
    provider = (provider or "").lower()
    cfg = config or {}
    creds = credentials or {}
    if provider == "aws":
        bucket = (cfg.get("bucket") or "").strip().rstrip("/")
        if not bucket:
            return None
        if "arn" in bucket.lower() and ":" in bucket:
            bucket = bucket.rsplit(":", 1)[-1]
        return CustomerS3Destination(
            bucket=bucket,
            region=cfg.get("region") or "us-east-1",
            endpoint_url=cfg.get("endpoint_url") or None,
            access_key=creds.get("access_key_id") or None,
            secret_key=creds.get("secret_access_key") or None,
            storage_class=cfg.get("storage_class") or "INTELLIGENT_TIERING",
        )
    if provider == "azure":
        container = (cfg.get("container") or "").strip()
        if not container:
            return None
        return AzureBlobDestination(
            container=container,
            connection_string=creds.get("connection_string") or None,
            account_url=cfg.get("account_url") or None,
            account_name=cfg.get("account_name") or None,
            account_key=creds.get("account_key") or None,
            access_tier=cfg.get("access_tier") or "Cool",
        )
    if provider == "gcp":
        bucket = (cfg.get("bucket") or "").strip().rstrip("/")
        if not bucket:
            return None
        sa = creds.get("service_account_json")
        if isinstance(sa, str) and sa.strip():
            try:
                sa = json.loads(sa)
            except Exception:
                sa = None
        return GCSDestination(
            bucket=bucket,
            project=cfg.get("project_id") or None,
            credentials_json=sa if isinstance(sa, dict) else None,
            location=cfg.get("location") or "US",
        )
    return None


def destination_from_service(kind: str, cfg: dict) -> Optional[ProtectionDestination]:
    """Build a storage destination from a ServiceObject's merged config (linked
    ConfigObject credentials + non-secret settings). Returns None if the kind is
    unknown or required routing is missing."""
    if kind == "storage-s3":
        bucket = cfg.get("bucket")
        if not bucket:
            return None
        # Tolerate a pasted ARN — S3 wants the bare bucket name.
        bucket = str(bucket).strip().rstrip("/")
        if "arn" in bucket.lower() and ":" in bucket:
            bucket = bucket.rsplit(":", 1)[-1]
        return CVCloudDestination(
            bucket=bucket,
            region=cfg.get("region") or "us-east-1",
            endpoint_url=cfg.get("endpoint_url") or None,
            access_key=cfg.get("aws_access_key_id") or None,
            secret_key=cfg.get("aws_secret_access_key") or None,
            storage_class=cfg.get("storage_class") or "INTELLIGENT_TIERING",
        )
    if kind == "storage-azure":
        container = cfg.get("container")
        if not container:
            return None
        return AzureBlobDestination(
            container=container,
            connection_string=cfg.get("connection_string") or None,
            account_url=cfg.get("account_url") or None,
            account_name=cfg.get("account_name") or None,
            account_key=cfg.get("account_key") or None,
            access_tier=cfg.get("access_tier") or "Cool",
        )
    return None


def _cv_cloud_from_service() -> Optional[ProtectionDestination]:
    """Resolve the Arkive Cloud destination from the running node's selected
    storage service object, if any."""
    try:
        from .services import self_storage_service
        svc = self_storage_service()
        if not svc:
            return None
        return destination_from_service(svc.get("kind", ""), svc.get("config") or {})
    except Exception:
        log.exception("failed to build cloud storage from node service object")
        return None


def build_destination(kind: str) -> ProtectionDestination:
    """Factory honoring configuration. Arkive Cloud (``cv-cloud``) resolves to the
    storage service object selected on the running node (Amazon S3 / Azure Blob);
    falls back to env-configured S3, then local FS for the prototype."""
    if kind == "cv-cloud":
        dest = _cv_cloud_from_service()
        if dest is not None:
            return dest
        if settings.aws_access_key_id:
            return CVCloudDestination(
                bucket=settings.s3_bucket,
                region=settings.s3_region,
                endpoint_url=settings.s3_endpoint_url,
                access_key=settings.aws_access_key_id,
                secret_key=settings.aws_secret_access_key,
            )
        return LocalFsDestination()
    if kind == "customer-s3" and settings.aws_access_key_id:
        return CustomerS3Destination(
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.aws_access_key_id,
            secret_key=settings.aws_secret_access_key,
        )
    return LocalFsDestination()
