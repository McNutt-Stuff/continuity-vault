#!/usr/bin/env python3
"""
End-to-end prototype demonstration / integration check.

Runs the entire flow in-process against the cloud control plane using FastAPI's
TestClient, and simulates an appliance using the shared cv_crypto library:

  1. Login + passkey enrollment + step-up unlock
  2. Link a source (Gmail) and run a backup (encrypt -> S3/local -> manifest)
  3. Unified search over the indexed metadata
  4. Provision a linking code and activate a simulated appliance
  5. Appliance heartbeat -> receive a signed OPEN_INGEST_WINDOW command
  6. Appliance verifies the hybrid signature locally, seals, and returns a
     signed seal receipt -> cloud marks the snapshot recoverable
  7. Restore request -> approve -> execute

Run from repo root:  python scripts/e2e_demo.py
"""

import os
import sys
import base64

# Make the cloud app + shared package importable when run from the repo root.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "cloud"))
sys.path.insert(0, os.path.join(ROOT, "shared"))

os.environ.setdefault("CV_DATABASE_URL", "sqlite:///./e2e_demo.db")
os.environ.setdefault("CV_ENVIRONMENT", "development")

# Fresh DB each run.
for f in ("e2e_demo.db",):
    if os.path.exists(f):
        os.remove(f)

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

from cv_crypto.signing import HybridSigner, HybridVerifier, SigPolicy  # noqa: E402
from cv_crypto.command import build_seal_receipt  # noqa: E402


def hr(title):
    print(f"\n{'='*4} {title} {'='*4}")


client = TestClient(app)
tok = None


def auth():
    return {"Authorization": f"Bearer {tok}"}


def main():
    global tok
    hr("1. Login")
    r = client.post("/api/auth/login", json={"email": "owner@northwind.example"})
    r.raise_for_status()
    tok = r.json()["token"]
    print("logged in as owner@northwind.example; has_passkey =", r.json()["has_passkey"])

    hr("2. Enroll passkey + unlock (step-up)")
    r = client.post("/api/auth/passkey/register-simulated",
                    json={"label": "Demo", "transport": "internal"}, headers=auth())
    r.raise_for_status()
    cred = r.json()["credential_id"]
    ch = client.post("/api/auth/passkey/challenge", headers=auth()).json()["challenge"]
    sig = client.post("/api/auth/passkey/sign-simulated",
                      json={"credential_id": cred, "challenge": ch}, headers=auth()).json()["signature"]
    r = client.post("/api/auth/passkey/verify",
                    json={"credential_id": cred, "challenge": ch, "signature": sig}, headers=auth())
    r.raise_for_status()
    tok = r.json()["token"]
    print("passkey verified =", r.json()["passkey_verified"])

    hr("3. Link Gmail + back it up")
    acct = client.post("/api/connectors/link",
                       json={"connector_type": "gmail", "account_label": "owner@gmail.com"},
                       headers=auth()).json()
    vault_id = client.get("/api/tenant", headers=auth()).json()["vaults"][0]["id"]
    coll = client.post("/api/collections", json={
        "vault_id": vault_id, "name": "owner@gmail.com", "source_type": "gmail",
        "connector_account_id": acct["id"], "destinations": ["cv-cloud"],
    }, headers=auth()).json()
    bk = client.post(f"/api/collections/{coll['id']}/backup",
                     json={"destinations": ["cv-cloud"]}, headers=auth()).json()
    print(f"backup: {bk['object_count']} objects, {bk['total_bytes']} bytes, "
          f"recoverable={bk['recoverable']}")
    cloud_snapshot = bk["snapshot_id"]

    hr("4. Unified search")
    s = client.get("/api/search?q=board", headers=auth()).json()
    print(f"search 'board': {s['count']} hit(s); facets={s['facets']['source']}")

    hr("5. Provision linking code + activate simulated appliance")
    code = client.post("/api/appliances/linking-code",
                       json={"model": "CV Edge 8", "name": "Home Appliance"},
                       headers=auth()).json()["code"]
    appliance_signer = HybridSigner.generate("appliance:E2E-DEMO")
    act = client.post("/api/appliance/activate", json={
        "linking_code": code, "serial": "CV-E2E-DEMO", "model": "CV Edge 8",
        "identity_bundle": appliance_signer.public_bundle(),
        "attestation": {"secure_boot": True},
    }).json()
    agent_token = act["agent_token"]
    appliance_id = act["appliance_id"]
    cloud_bundle = act["cloud_public_bundle"]
    ah = {"Authorization": f"Bearer {agent_token}"}
    print("appliance activated:", appliance_id)

    hr("6. Issue signed ingest command -> appliance verifies + seals")
    client.post(f"/api/appliances/{appliance_id}/command", json={
        "command_type": "OPEN_INGEST_WINDOW",
        "parameters": {"vaultId": vault_id, "collectionId": coll["id"],
                       "snapshotId": "appliance-snap-1", "maximumDurationSeconds": 1800},
    }, headers=auth())

    hb = client.post("/api/appliance/heartbeat", json={
        "state": "SEALED", "isolation_state": "sealed", "software_version": "1.0.0",
        "attestation": {"secure_boot": True}, "telemetry": {"drive_health": "healthy"},
    }, headers=ah).json()
    cmds = hb["commands"]
    assert cmds, "expected a delivered command"
    cmd = cmds[0]

    # Appliance-side local verification of the hybrid-signed command.
    ok = HybridVerifier.from_bundle(cloud_bundle).verify(
        cmd["payload"], cmd["signature"], SigPolicy.REQUIRE_BOTH)
    print("appliance verified command signature (Ed25519+ML-DSA):", ok)
    assert ok

    # Appliance produces a signed seal receipt and reports it.
    manifest_hash = "sha384:demo-manifest"
    receipt = build_seal_receipt(appliance_signer, appliance_id, "appliance-snap-1",
                                 manifest_hash, 5, 12345, "sealed", "verified")
    sr = client.post("/api/appliance/seal-receipt", json={
        "vault_id": vault_id, "collection_id": coll["id"],
        "snapshot_id": "appliance-snap-1", "object_count": 5, "total_bytes": 12345,
        "manifest_hash": manifest_hash, "receipt": receipt,
    }, headers=ah).json()
    print("cloud verified seal receipt; recoverable =", sr["recoverable"])
    assert sr["recoverable"]

    hr("7. Restore: request -> approve -> execute")
    req = client.post("/api/restore", json={
        "snapshot_id": cloud_snapshot, "object_ids": [], "destination": "download",
        "purpose": "demo",
    }, headers=auth()).json()
    # Owner is also security-admin-capable? Owner role approves per require_security_admin.
    ap = client.post(f"/api/restore/{req['id']}/approve", headers=auth()).json()
    ex = client.post(f"/api/restore/{req['id']}/execute", headers=auth()).json()
    print(f"restore status: requested -> {ap['status']} -> {ex['status']}")

    hr("8. Admin overview")
    # Switch to platform admin.
    at = client.post("/api/auth/login", json={"email": "admin@arkive.life"}).json()["token"]
    ov = client.get("/api/admin/overview", headers={"Authorization": f"Bearer {at}"}).json()
    print("admin overview:", ov)

    print("\nAll end-to-end steps completed successfully.")


if __name__ == "__main__":
    main()
