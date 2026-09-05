---
applyTo: "appliance/**,infra/systemd/cv-appliance-*"
description: "On-prem appliance agent — sandbox, privileged ops, commands, storage."
---

# Appliance agent (on-prem)

- **Runs sandboxed as `cvagent`** under systemd: `NoNewPrivileges=true`, `ProtectSystem=strict`,
  `ReadWritePaths=/var/lib/continuity-vault-appliance -/arkive`. It CANNOT gain privileges (no `sudo`), mount,
  format, or write outside those paths. Any new host path the agent must write needs adding to `ReadWritePaths`.
- **Privileged disk ops** (partition/format/mount USB/external drives) CANNOT run in the agent. Delegate them
  to the ROOT helper: the agent drops a request in `<data>/storage-queue`, the `cv-appliance-storage.path`
  watcher starts `cv-appliance-storage.service` (root, unsandboxed) which runs `agent/storage_helper.py` and
  writes a result the agent polls for. (`cvtool` is the manual/root CLI equivalent.)
- **`import os` at module top.** `main.py` imports os/fcntl/pty locally inside functions; module-level code
  (runs at import) must not assume they're imported, or the agent crash-loops (`Restart=always`).
- **Commands are signed** (spec 5.2): `_verify_command` checks applianceId, quarantine, signature (pinned
  fleet signer bundle), policyHash. On signer drift the appliance re-pins via `/appliance/control-plane-bundle`.
  Wrap each handler in try/except → post an acked-with-error result so a failure stops redelivery and never
  crashes the heartbeat. Valid command types live in `cv_crypto/command.COMMAND_TYPES`.
- **Outbound-only** management plane: the appliance heartbeats the CP (or its assigned node), drains commands,
  posts results. It never accepts inbound management. Storage/recovery bytes stay on-device.
- **Shipping code:** the appliance bundle lists explicit files (`api/appliances.py` `_BUNDLE_FILES`/`_BUNDLE_DIRS`).
  A NEW systemd unit or installer file MUST be added there or it never reaches appliances. systemd unit changes
  need the installer to re-run (the self-update path runs `appliance-install.sh`).
- **Telemetry:** report everything the portal shows (storage kind/capacity/health, RAID/SMART, net, versions).
  Node-routed appliances need their runtime fields in `_PULL_EXCLUDE` so the pull doesn't clobber node-owned
  liveness.
