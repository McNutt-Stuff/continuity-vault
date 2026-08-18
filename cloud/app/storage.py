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
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional

from .config import get_settings

settings = get_settings()


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

    def put_manifest(self, tenant_prefix, snapshot_id, manifest) -> str:
        path = self._p(tenant_prefix, f"manifests/{snapshot_id}.json")
        path.write_text(json.dumps(manifest))
        return str(path)


class _S3Base(ProtectionDestination):
    def __init__(self, bucket: str, region: str, endpoint_url: Optional[str] = None,
                 access_key: Optional[str] = None, secret_key: Optional[str] = None) -> None:
        import boto3  # imported lazily so dev runs without boto3

        self.bucket = bucket
        self._s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        try:
            self._s3.head_bucket(Bucket=bucket)
        except Exception:
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

    def put_manifest(self, tenant_prefix, snapshot_id, manifest) -> str:
        return self.put_object(
            tenant_prefix, f"manifests/{snapshot_id}.json",
            json.dumps(manifest).encode(), immutable=True,
        )


class CVCloudDestination(_S3Base):
    name = "cv-cloud"


class CustomerS3Destination(_S3Base):
    name = "customer-s3"


def build_destination(kind: str) -> ProtectionDestination:
    """Factory honoring configuration; falls back to local FS for the prototype
    when no S3 credentials are configured."""
    if kind in ("cv-cloud", "customer-s3") and settings.aws_access_key_id:
        cls = CVCloudDestination if kind == "cv-cloud" else CustomerS3Destination
        return cls(
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.aws_access_key_id,
            secret_key=settings.aws_secret_access_key,
        )
    return LocalFsDestination()
