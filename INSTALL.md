# Arkive — Installation Guide

Deploy Arkive to remote systems: one cloud all-in-one server (`vault.arkive.life`)
plus one or more offline appliances. Installs are performed over SSH by running
the bundled installer scripts on each host.

> **Remote install note:** the installers run **on the target hosts**. Copy the
> repository to each server, open an SSH session, and run the script there.
> Password / sudo / passphrase prompts must be answered directly in your own
> terminal.

---

## 1. Prerequisites

**DNS** (at your registrar for `arkive.life`):

| Record | Type | Value |
|---|---|---|
| `vault.arkive.life` | A | Cloud server's public IP |

**Firewall (cloud server):** inbound **TCP 80 and 443** must be reachable so
Caddy can obtain and renew the Let's Encrypt certificate. Appliances need only
**outbound** HTTPS to the cloud — no inbound ports.

**Hosts:** clean **Ubuntu 26.04 LTS** with a sudo-capable user.

**Topology:** 1 cloud all-in-one + 2 appliances (this guide covers two; add more
by repeating Step 3).

---

## 2. Copy the code to each remote host

Run from your workstation for **every** host (cloud + each appliance).

**Option A — rsync (recommended):**

```bash
# from the repository root
rsync -az --exclude '.venv' --exclude 'node_modules' --exclude 'web/dist' \
  --exclude '*.db' --exclude 'cv_*' ./ user@HOST_IP:~/arkive/
```

**Option B — git:**

```bash
ssh user@HOST_IP 'git clone https://your-git-host/arkive.git ~/arkive'
```

---

## 3. Install the cloud server

```bash
ssh user@CLOUD_IP
cd ~/arkive
sudo CV_DOMAIN=vault.arkive.life ./installers/cloud-install.sh
```

This installs Python, Node.js, PostgreSQL, and Caddy; builds the web portal;
writes `/etc/continuity-vault.env`; starts `cv-cloud.service`; and configures
Caddy to automatically obtain a Let's Encrypt certificate for `vault.arkive.life`.

**Verify:**

```bash
curl -s https://vault.arkive.life/api/health      # {"status":"ok",...}
systemctl status cv-cloud caddy --no-pager
```

Open `https://vault.arkive.life` and sign in as `owner@northwind.example`
(demo seed data).

**Optional — enable cloud auto-updates** (polls the admin *Updates* console every
5 minutes and applies releases with automatic rollback):

```bash
sudo cp ~/arkive/infra/systemd/cv-cloud-update.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cv-cloud-update.timer
```

---

## 4. Install each appliance

In the portal, go to **Appliances → Generate linking code** (one code per
appliance). Then on each appliance host:

```bash
ssh user@APPLIANCE_IP
cd ~/arkive
sudo CV_CLOUD_URL=https://vault.arkive.life/api ./installers/appliance-install.sh
# prompts: Enter the linking code from your Arkive portal: CV-XXXX-YYYY
```

Non-interactive alternative:

```bash
sudo CV_CLOUD_URL=https://vault.arkive.life/api \
     CV_LINKING_CODE=CV-XXXX-YYYY ./installers/appliance-install.sh
```

**Verify (on the appliance):**

```bash
curl -s http://127.0.0.1:8090/status      # activated=true, state=SEALED
systemctl status cv-appliance-agent --no-pager
```

Within ~30 seconds the appliance appears **online & attested** in the portal's
Appliances page. Repeat for the second appliance with its own linking code.

---

## 5. One-shot bootstrap (paste into a fresh SSH session)

**Cloud:**

```bash
cd ~/arkive && sudo CV_DOMAIN=vault.arkive.life ./installers/cloud-install.sh \
  && curl -s https://vault.arkive.life/api/health
```

**Appliance:**

```bash
cd ~/arkive && sudo CV_CLOUD_URL=https://vault.arkive.life/api \
  CV_LINKING_CODE=CV-XXXX-YYYY ./installers/appliance-install.sh \
  && curl -s http://127.0.0.1:8090/status
```

---

## 6. Installer behavior & recovering from a failed install

Both installers show a clean step-by-step progress display; each step's command
output is hidden and written to a timestamped log at
`/var/log/arkive-install-*.log`.

**The installers are resumable.** Every completed step is recorded under the
data directory (`.../install-state/`). If a step fails, the installer prints the
last 25 lines of the log and stops — fix the underlying issue (e.g. DNS, network,
disk) and **run the exact same command again**; completed steps are skipped and
it continues from the failed one.

| Flag | Effect |
|---|---|
| _(default)_ | Hidden output, spinner, resume from last failure |
| `CV_FORCE=1` | Ignore saved state and re-run **every** step |
| `CV_VERBOSE=1` | Stream live command output instead of the spinner |

```bash
# Resume after fixing an issue — just re-run:
sudo CV_DOMAIN=vault.arkive.life ./installers/cloud-install.sh

# Watch full output for a specific problem:
sudo CV_VERBOSE=1 CV_DOMAIN=vault.arkive.life ./installers/cloud-install.sh

# Force a clean, full re-install:
sudo CV_FORCE=1 CV_DOMAIN=vault.arkive.life ./installers/cloud-install.sh
```

Steps are idempotent (packages, database role, service user, config, and
services are only created/updated as needed), so re-running is always safe. The
appliance linking code can be left blank to install now and link later from the
portal.

---

## 7. Production hardening (before real data)

The cloud installer seeds demo data and generates a random PostgreSQL password
(stored in `/etc/continuity-vault.env`). Before production use, on the cloud
server:

1. Edit `/etc/continuity-vault.env`:
   - set `CV_SEED_DEMO_DATA=false`
   - confirm `CV_DATABASE_URL`, `CV_SESSION_SECRET`, and `CV_KEK_SECRET` hold the
     random values the installer generated (never reuse across environments)
2. Restart the service:
   ```bash
   sudo systemctl restart cv-cloud
   ```
3. Create your real platform-admin and customer accounts, then remove the demo
   tenants.

---

## 8. Service reference

| Component | Service | Port (local) | Notes |
|---|---|---|---|
| Cloud control plane | `cv-cloud.service` | 8000 | Behind Caddy on 443 |
| Reverse proxy / TLS | `caddy.service` | 80, 443 | Auto Let's Encrypt |
| Cloud auto-update | `cv-cloud-update.timer` | — | Optional |
| Appliance agent | `cv-appliance-agent.service` | 8090 | Outbound-only to cloud |

**Logs:**

```bash
journalctl -u cv-cloud -f            # cloud
journalctl -u cv-appliance-agent -f  # appliance
```

---

## 9. Troubleshooting

| Symptom | Check |
|---|---|
| TLS cert not issued | DNS A record resolves to this host; ports 80/443 open; `journalctl -u caddy` |
| Portal 502 | `systemctl status cv-cloud`; `curl -s http://127.0.0.1:8000/api/health` |
| Appliance not appearing | Correct `CV_CLOUD_URL`; valid/unexpired linking code; `journalctl -u cv-appliance-agent` |
| Appliance shows attestation failed | Restart agent; confirm system time is correct (signature expiry) |
