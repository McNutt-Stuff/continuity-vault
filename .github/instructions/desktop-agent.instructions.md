---
applyTo: "desktop-agent/**"
description: "macOS endpoint (desktop) agent — collectors, push, self-update."
---

# Desktop agent (macOS)

- **Runs as the logged-in user via a per-user LaunchAgent** (`~/Library/LaunchAgents/com.arkive.agent.plist`,
  `gui/<uid>`). NEVER install/run with `sudo`: as root `Path.home()` = `/var/root`, so collectors can't see
  the user's Outlook/iMessage/Library data, the menu-bar icon can't draw (no Aqua session), and the Keychain
  data key differs. `main.py` logs a loud ROOT warning when `geteuid()==0`.
- **Paths:** data/logs live under `ARKIVE_AGENT_DIR` (`~/.arkive-agent`), set by the plist. Collectors use
  `Path.home()`-based paths. Logs write to `~/.arkive-agent/agent.log` (RotatingFileHandler + launchd stdout).
- **Collectors** (`agent/collectors/*.py`) expose `available()` + `collect(config, state) -> (objects, state)`.
  Collection is incremental: persist a per-source `state` (cursor or content signatures) so a run only pushes
  new/changed objects; the first run is a full backfill. Register a new collector in `main.py` `_collectors()`
  + `collect_and_push()` and the cloud connector registry (`requires_agent`).
- **Objects:** `{object_id, kind, title, content_b64, preview, meta, labels, size_bytes}`. Attachments are
  their own file objects linked via `meta.message_object_id`. The agent computes `content_hash = sha256(plaintext)`
  by default — override with a stable hash if the plaintext changes every run.
- **Push model:** the agent owns its cadence (pulls mappings on heartbeat, runs due collects); `_push_objects`
  client-encrypts and batches by size. Heartbeat telemetry can carry advisory notices
  (`telemetry["collector_notices"]`) surfaced on the source in the portal.
- **Bundled native tools** (e.g. `hxprobe` for New Outlook) ship a prebuilt binary in the bundle; exclude the
  build tree from the cloud bundle (`api/agents.py` `_EXCLUDE`) and add a cargo build fallback in `update.sh`.
- **Self-update:** the agent self-updates from the CP bundle when `latest_version` differs. Code changes ship
  automatically; only a plist change needs a reinstall.
