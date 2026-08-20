"""
Detect the cloud platform, region, and instance the node runs on.

Reads the provider's Instance Metadata Service (IMDS) with short timeouts —
AWS (IMDSv2), GCP, and Azure — plus DMI/BIOS hints as a fallback. Cached for the
process. Used to enrich node health so the admin can see where each node lives.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

_cache: dict | None = None


def _dmi(name: str) -> str:
    try:
        return Path(f"/sys/class/dmi/id/{name}").read_text(errors="ignore").strip()
    except Exception:
        return ""


def _aws() -> dict | None:
    try:
        with httpx.Client(timeout=0.4) as c:
            tok = c.put("http://169.254.169.254/latest/api/token",
                        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
            h = {"X-aws-ec2-metadata-token": tok.text} if tok.status_code < 400 else {}
            r = c.get("http://169.254.169.254/latest/dynamic/instance-identity/document", headers=h)
            if r.status_code >= 400:
                return None
            d = r.json()
            return {"provider": "aws", "region": d.get("region"),
                    "zone": d.get("availabilityZone"),
                    "instance_id": d.get("instanceId"),
                    "instance_type": d.get("instanceType"),
                    "account": d.get("accountId")}
    except Exception:
        return None


def _gcp() -> dict | None:
    try:
        base = "http://metadata.google.internal/computeMetadata/v1"
        h = {"Metadata-Flavor": "Google"}
        with httpx.Client(timeout=0.4, headers=h) as c:
            zone = c.get(f"{base}/instance/zone").text.split("/")[-1]
            mt = c.get(f"{base}/instance/machine-type").text.split("/")[-1]
            return {"provider": "gcp",
                    "region": "-".join(zone.split("-")[:-1]) if zone else "",
                    "zone": zone,
                    "instance_id": c.get(f"{base}/instance/id").text,
                    "instance_type": mt,
                    "account": c.get(f"{base}/project/project-id").text}
    except Exception:
        return None


def _azure() -> dict | None:
    try:
        url = "http://169.254.169.254/metadata/instance?api-version=2021-02-01"
        with httpx.Client(timeout=0.4, headers={"Metadata": "true"}) as c:
            r = c.get(url)
            if r.status_code >= 400:
                return None
            cm = (r.json() or {}).get("compute", {})
            return {"provider": "azure", "region": cm.get("location"),
                    "zone": cm.get("zone"),
                    "instance_id": cm.get("vmId"),
                    "instance_type": cm.get("vmSize"),
                    "account": cm.get("subscriptionId")}
    except Exception:
        return None


def detect(refresh: bool = False) -> dict:
    """Return {provider, region, zone, instance_id, instance_type, account}.
    provider is 'aws'|'gcp'|'azure'|'baremetal'|'unknown'. Cached per process."""
    global _cache
    if _cache is not None and not refresh:
        return _cache
    # Quick DMI hint narrows which IMDS to try first.
    vendor = (_dmi("sys_vendor") + " " + _dmi("bios_vendor") + " " + _dmi("product_name")).lower()
    order = []
    if "amazon" in vendor or "ec2" in vendor:
        order = [_aws, _azure, _gcp]
    elif "google" in vendor:
        order = [_gcp, _aws, _azure]
    elif "microsoft" in vendor:
        order = [_azure, _aws, _gcp]
    else:
        order = [_aws, _azure, _gcp]
    result = None
    for fn in order:
        result = fn()
        if result:
            break
    if not result:
        result = {"provider": "baremetal" if vendor.strip() else "unknown",
                  "region": "", "zone": "", "instance_id": "",
                  "instance_type": _dmi("product_name"), "account": ""}
    result["detected_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _cache = result
    return result
