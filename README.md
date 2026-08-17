# Arkive — Prototype

Cloud-managed digital continuity and cyber-recovery platform with a hybrid public
cloud + customer S3 + offline appliance architecture, multi-tenant isolation,
user-owned keys, passkey/hardware-token unlock, unified search, and a turnkey
cloud-managed appliance. Post-quantum (hybrid) cryptography is the default.

Arkive (`arkive.life`) runs the platform; the cloud vault app lives at
`vault.arkive.life`. This repository implements an **end-to-end prototype** of the
architecture in `Continuity Vault Product Specification …md`.

```
┌──────────────────────────────────────────── vault.arkive.life ──────────────────────────────────────────────┐
│                                                                                                              │
│   Web Portal (React/TS)  ──/api──►  Cloud Control Plane (FastAPI)  ──►  PostgreSQL                            │
│      • onboarding                      • multi-tenant identity + passkeys        • S3 / MinIO (recovery)      │
│      • unified search                  • connector orchestration (sync workers)  • Key broker (HSM stand-in)  │
│      • appliance dashboard             • appliance fleet manager (signed cmds)   • Tamper-evident audit       │
│      • restore + approvals             • crypto-profile registry (PQC)                                        │
│      • admin console                                                                                         │
│                                            ▲  outbound-only, signed commands                                 │
│                                            │  (Ed25519 + ML-DSA), sealed receipts                            │
│   ┌───────────────┐   ┌───────────────┐    │                                                                 │
│   │ Appliance #1  │   │ Appliance #2  │────┘   Offline appliances: strict local state machine, sealed vault, │
│   │ CV Edge 8     │   │ CV Vault 64   │        controlled unseal, local policy enforcement, attestation.     │
│   └───────────────┘   └───────────────┘                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Repository layout

| Path | What it is |
|---|---|
| `shared/cv_crypto/` | Crypto-agile hybrid crypto: profiles registry, provider (classical + PQC via liboqs), envelope encryption, hybrid signing, signed command envelopes |
| `cloud/app/` | Cloud control plane (FastAPI): models, security/passkeys, key broker, audit ledger, fleet manager, storage destinations, connectors, sync worker, REST API |
| `appliance/agent/` | Offline appliance agent: state machine, sealed vault store, identity/attestation, outbound-only management loop |
| `web/` | Enterprise web portal (React + TypeScript + Vite) |
| `installers/` | Turnkey Ubuntu 26 installers (cloud + appliance) |
| `updater/` | Cloud-triggered signed update scripts (cloud + appliance) + release builder |
| `infra/` | Caddyfile (Let's Encrypt), systemd units, Dockerfiles |
| `scripts/e2e_demo.py` | In-process end-to-end integration demonstration |
| `docker-compose.yml` | Local topology: 1 cloud + 2 appliances + MinIO (S3) + Postgres |

## Quick start (Docker)

```bash
docker compose up --build
# Portal:  http://localhost:5173     (sign in as owner@northwind.example)
# API:     http://localhost:8000/api/health
# MinIO:   http://localhost:9001     (minioadmin / minioadmin)
```

Activate the two appliances: in the portal go to **Appliances → Generate linking
code**, then:

```bash
curl -XPOST localhost:8091/activate -H 'content-type: application/json' -d '{"linking_code":"CV-XXXX-YYYY"}'
curl -XPOST localhost:8092/activate -H 'content-type: application/json' -d '{"linking_code":"CV-XXXX-YYYY"}'
```

## Quick start (local, no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ./shared -r cloud/requirements.txt
# Terminal 1 — control plane
(cd cloud && uvicorn app.main:app --reload)
# Terminal 2 — web portal
(cd web && npm install && npm run dev)
# Optional — an appliance agent
pip install -r appliance/requirements.txt
(cd appliance && CVA_CLOUD_BASE_URL=http://localhost:8000/api uvicorn agent.main:app --port 8090)
```

Verify the whole flow without any servers:

```bash
python scripts/e2e_demo.py
```

## Demo identities (seeded)

| Email | Role |
|---|---|
| `owner@northwind.example` | Vault owner |
| `security@northwind.example` | Security admin |
| `admin@arkive.life` | Platform (backend) admin |

Sign in with the email, then **enroll a passkey** and **unlock** — the simulated
authenticator completes the WebAuthn-style ceremony in-browser.

## Security model highlights (per the specification)

- **Separated planes** — control, data, recovery, key, and management planes are
  distinct. The cloud fleet manager never mounts appliance storage.
- **Signed commands** — every appliance command is hybrid-signed (Ed25519 +
  ML-DSA), sequenced, expiring, replay-protected, and re-evaluated against local
  appliance policy. No fail-open for privileged operations.
- **Offline = inaccessible** — the appliance protected data path opens only in
  explicit `UNSEALED_*` states; the management controller can never obtain a
  handle to it.
- **User-owned keys** — layered envelope encryption (root → vault → collection →
  snapshot → object), AES-256-GCM content, ML-KEM-wrapped recovery recipients,
  customer-managed / split-control / zero-knowledge ownership models.
- **Passkey-gated interfaces** — unified search, source linking, and restore
  require passkey/hardware-token step-up.
- **Verified recovery points** — a snapshot is only marked recoverable once its
  destination confirms and signs the commit.
- **Tamper-evident audit** — append-only, hash-chained ledger.
- **Crypto-agility** — every artifact records its crypto profile; algorithms can
  migrate without rewriting business logic or re-encrypting content.

## Post-quantum note

Real ML-KEM / ML-DSA / SLH-DSA come from **liboqs** (`pip install oqs`). If liboqs
is absent, the provider uses a **clearly flagged** software fallback so the
prototype still runs — `pq_available` reports the honest state everywhere and no
fallback artifact is ever presented as quantum-safe. Install `oqs` for genuine
post-quantum operation.

## Production deployment (Ubuntu 26)

```bash
# Cloud all-in-one server (clean Ubuntu 26)
sudo CV_DOMAIN=vault.arkive.life ./installers/cloud-install.sh

# Each offline appliance (clean Ubuntu 26)
sudo CV_CLOUD_URL=https://vault.arkive.life/api ./installers/appliance-install.sh
```

Caddy automatically obtains and renews a Let's Encrypt certificate for the
domain. Cloud-triggered updates: publish a release in **Admin → Updates**, then
the cloud updater timer (`cv-cloud-update.timer`) applies it with rollback, and
appliance updates are delivered as signed `STAGE_UPDATE` commands.
