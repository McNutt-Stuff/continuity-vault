# cvtool — Arkive appliance CLI

`cvtool` is the on-appliance troubleshooting CLI, installed to `/usr/local/bin/cvtool`
on every appliance (hardware + VM) by `installers/appliance-install.sh`. It is
runnable by `root` and, via a `NOPASSWD` sudoers rule, by the `cvagent` service
account — so it also works from the **reverse-tunnel SSH shell** (admin → Appliance
→ Remote terminal). Run `cvtool help` for the built-in reference.

The script lives at `installers/cvtool` and ships in the appliance bundle
(`_BUNDLE_FILES` in `cloud/app/api/appliances.py`).

## Commands

| Command | What it does | Privileged |
|---|---|---|
| `cvtool info` | Identity, model/type, version, cloud routing, storage kind + path | no |
| `cvtool stats` | Live storage (dedicated + OS disk), CPU/memory, uptime, recovery points, drive health | no |
| `cvtool update` | Pull + apply the latest appliance software (version-checked) | yes |
| `cvtool update --force` | Same, ignoring the version check | yes |
| `cvtool restart service` | Restart the appliance agent (`cv-appliance-agent`) | yes |
| `cvtool restart system` | Reboot the appliance host | yes |
| `cvtool link [CODE]` | Link to an account with a portal linking code; no `CODE` prints the pairing code | yes |
| `cvtool unlink` | Forget the cloud link (keeps serial/keys); re-registers for pairing | yes |
| `cvtool re-link [CODE]` | Unlink, then link again (optionally with a new `CODE`) | yes |
| `cvtool help` | Command reference | no |

Privileged commands auto-elevate with `sudo -n`; from the reverse tunnel this is
transparent thanks to `/etc/sudoers.d/cvtool`.

## Data sources

- `/etc/continuity-vault/appliance.env` — cloud URL, model, data dir, linking code.
- The agent's local API `http://127.0.0.1:8090/status` and `/pairing` — live
  telemetry (storage, CPU, memory, drive health), serial, activation state,
  pairing code.
- `<data_dir>/registration.json` + `pending.json` — cloud link state (cleared by
  `unlink`).

## Adding a new command (the convention — future commands drop in here)

When a new appliance troubleshooting action makes sense, add it as a `cvtool`
subcommand by touching these in sync:

1. **`installers/cvtool`** — add a `cmd_<name>()` function.
2. **`installers/cvtool` → `main()` dispatch** — add `<name>) cmd_<name> "$@" ;;`
   to the final `case`.
3. **`installers/cvtool` → privileged `case`** — if the command changes the
   system (systemctl/reboot/writes/network mutations), add its bare name to the
   auto-elevate `case` so it runs as root.
4. **`installers/cvtool` → `cmd_help()`** — add a one-line description.
5. **This file** — add a row to the Commands table above.

Guidelines: read from the agent's `/status` (JSON, parse with `python3` which is
always present) rather than re-collecting metrics; keep each command idempotent
and safe to run repeatedly; print a short human-readable result. No bundle,
installer, or cloud change is needed to add a command — it ships automatically in
the next appliance self-update because `installers/cvtool` is in the bundle.
