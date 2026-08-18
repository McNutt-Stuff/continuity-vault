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

**Optional — updates from GitHub:** see [Updating from GitHub](#updating-from-github).

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

---

## Connecting data sources

Sources use real provider authorization. OAuth providers (Gmail, Outlook,
OneDrive, Dropbox) need an OAuth app configured on the server; token providers
(1Password, iCloud) accept a pasted token / app-password. Until a provider is
configured, the portal shows it as **"Needs setup"** with the exact steps.

**OAuth redirect URI** to register with every provider:

```
https://vault.arkive.life/api/connectors/oauth/callback
```

**Per provider**, create an OAuth app and set the credentials in
`/etc/continuity-vault.env`, then `sudo systemctl restart cv-cloud`:

| Provider | Where to create the app | Env vars |
|---|---|---|
| Gmail | Google Cloud Console → OAuth client (Web); enable Gmail API | `CV_GOOGLE_CLIENT_ID`, `CV_GOOGLE_CLIENT_SECRET` |
| Outlook / OneDrive | Microsoft Entra ID → App registration; delegated `Mail.Read` / `Files.Read.All` + `offline_access` | `CV_MICROSOFT_CLIENT_ID`, `CV_MICROSOFT_CLIENT_SECRET` |
| Dropbox | dropbox.com/developers → scoped app | `CV_DROPBOX_CLIENT_ID`, `CV_DROPBOX_CLIENT_SECRET` |
| 1Password | 1Password Connect server + Connect token (enter host + token when connecting) | — |
| iCloud | App-specific password + `pip install pyicloud` on the server | — |

**What each connector pulls:** Gmail/Outlook = messages; OneDrive/Dropbox =
files; 1Password = vault items (secret values encrypted, only metadata indexed);
iCloud = Contacts + Drive listing (best-effort; interactive-2FA accounts can't be
synced automatically).

In the portal: **Sources → click a service → authorize** on the provider's
consent screen → you're redirected back linked. Then **Back up now** pulls live
data, which is encrypted before it enters any destination.

---

## Desktop agents (macOS)

Some sources (1Password) require a local agent because data is extracted with a
native CLI (`op`). The desktop agent registers with a linking code like an
appliance, **encrypts on the endpoint** (the cloud never sees plaintext),
collects locally, pushes to the cloud (or an appliance), reports telemetry, and
self-updates on command. It runs in the background as a menu-bar app and bundles
the 1Password CLI.

**Install on a Mac (one-click):**

1. In the portal: **Desktop Agents → Download Mac installer** (the linking code
   and cloud URL are baked into the downloaded `arkive-agent-installer.command`).
2. On the Mac, run it (right-click → Open, or `bash ~/Downloads/arkive-agent-installer.command`).
   It clones the agent, builds its Python env, bundles the `op` CLI, installs a
   **launchd** menu-bar app, escrows its client-side key, and registers.
3. For unattended collection, provide a **1Password service-account token** when
   prompted; otherwise it uses the interactive 1Password app + CLI integration.

Client-side encryption: the agent holds a data key in the macOS Keychain, wraps
each item under it, and escrows a copy wrapped to the vault recovery key — so
secrets are unreadable by the cloud yet recoverable by an authorized party.

Manage from **Desktop Agents**: **Collect now**, **Update**, **Reconfigure**.
Status/logs on the Mac:

```bash
tail -f ~/.arkive-agent/agent.log
```

---

## Updating from GitHub

Updates pull the latest code from your GitHub repo into a source checkout at
`/opt/arkive-src`, then redeploy via the idempotent installer with **automatic
rollback** — if the new revision fails to start / health-check, it resets to the
previous commit and redeploys.

**One-time setup** (cloud server and/or each appliance):

```bash
sudo cp ~/arkive/updater/arkive-update.env.example /etc/arkive-update.env
sudo nano /etc/arkive-update.env      # set CV_REPO_URL and CV_REPO_BRANCH
```

**Manual update:**

```bash
# cloud (first run clones the repo to /opt/arkive-src)
sudo CV_REPO_URL=https://github.com/mcnutter1/continuity-vault.git \
     CV_DOMAIN=vault.arkive.life ~/arkive/updater/git-update.sh cloud

# after the first run, config comes from /etc/arkive-update.env:
sudo /opt/arkive-src/updater/git-update.sh cloud
# appliance:
sudo /opt/arkive-src/updater/git-update.sh appliance
```

Nothing to do if already up to date; `CV_FORCE=1` redeploys anyway.

**Automatic updates on a timer** (checks GitHub every 10 min cloud / 30 min appliance):

```bash
sudo cp /opt/arkive-src/infra/systemd/cv-cloud-update.* /etc/systemd/system/   # cloud
sudo cp /opt/arkive-src/infra/systemd/cv-appliance-update.* /etc/systemd/system/ # appliance
sudo systemctl daemon-reload
sudo systemctl enable --now cv-cloud-update.timer        # or cv-appliance-update.timer
```

**Private repos:** use a token URL in `CV_REPO_URL`
(`https://<user>:<token>@github.com/...`) or install an SSH deploy key on the host.

The appliance can also be updated cloud-side via signed `STAGE_UPDATE` commands
(admin *Updates* console) for environments that shouldn't reach GitHub directly.
