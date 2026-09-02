"""
Local storage-management CLI for the appliance (invoked by ``cvtool storage``).

Runs OUTSIDE the agent process (as root, via cvtool) so it can partition/format
a drive that the sandboxed agent can't reach directly. It mirrors the portal's
"Set up detected drive" flow: detect removable drives, format+label+mark one as
Arkive storage (optionally as a 1:1 mirror), and register it. The running agent
adopts the change on its next restart (which cvtool triggers), then reports the
new store to the cloud on the following heartbeat.

Everything destructive re-resolves the target by serial and refuses the system /
dedicated disk (see :mod:`agent.storage_ops`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .config import get_settings
from . import storage_ops, sysinfo

settings = get_settings()
EXT_BASE = Path(settings.data_dir) / "ext"
EXT_REGISTRY = Path(settings.data_dir) / "ext_stores.json"
# The unsandboxed cvtool formats/mounts as root, so hand the new volume to the
# agent's service account (which runs sandboxed and can't mount) or it couldn't
# write vault data there.
SERVICE_USER = os.environ.get("ARKIVE_SERVICE_USER", "cvagent")


def _load() -> list[dict]:
    try:
        return json.loads(EXT_REGISTRY.read_text()) or []
    except Exception:
        return []


def _save(rows: list[dict]) -> None:
    EXT_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    EXT_REGISTRY.write_text(json.dumps(rows))


def _excluded() -> set:
    return {sysinfo.system_disk_device(),
            sysinfo.dedicated_disk_device(settings.dedicated_path)}


def _fmt_bytes(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or u == "PB":
            return f"{int(n)} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return "0 B"


def cmd_list(args) -> int:
    detected = sysinfo.detect_external_storage(_excluded())
    registry = _load()
    reg_serials = {e.get("serial") for e in registry}
    if args.json:
        print(json.dumps({"configured": registry, "detected": detected}, indent=2))
        return 0
    print("Configured Arkive storage:")
    if not registry:
        print("  (none)")
    for e in registry:
        kind = e.get("kind", "external")
        mirror = f" · mirror of {e['mirror_of_id']}" if e.get("mirror_of_id") else ""
        print(f"  {e['store_id']}  {e.get('name','?')}  [{kind}{mirror}]  SN {e.get('serial','?')}")
    print("\nDetected removable drives:")
    unclaimed = [d for d in detected if d.get("serial") not in reg_serials]
    if not unclaimed:
        print("  (none)")
    for d in unclaimed:
        flags = []
        if d.get("has_filesystem"):
            lbl = d.get("label")
            flags.append(f"has data ({lbl})" if lbl else "has data")
        else:
            flags.append("blank")
        if d.get("is_arkive"):
            flags.append("Arkive-formatted")
        print(f"  SN {d.get('serial','?')}  {d.get('model') or d.get('vendor') or 'drive'}  "
              f"{_fmt_bytes(d.get('size_bytes'))}  {d.get('transport','')}  ({', '.join(flags)})")
    return 0


def cmd_setup(args) -> int:
    detected = {d.get("serial"): d for d in sysinfo.detect_external_storage(_excluded())
                if d.get("serial")}
    dev = detected.get(args.serial)
    if not dev:
        print(f"error: no removable drive with serial {args.serial!r} is detected "
              f"(run 'cvtool storage list')", file=sys.stderr)
        return 2
    store_id = uuid.uuid4().hex
    try:
        res = storage_ops.setup_device(
            device=dev.get("device", ""), serial=args.serial, store_id=store_id,
            name=args.name, mount_base=str(EXT_BASE), dedicated_path=settings.dedicated_path,
            mirror_of_id=args.mirror, kind=("mirror" if args.mirror else "external"))
    except storage_ops.StorageOpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Hand the fresh volume to the agent's service account so the sandboxed agent
    # can write vault data (it can't chown/mount itself).
    try:
        subprocess.run(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", res["mountpoint"]],
                       capture_output=True, timeout=30)
        os.chmod(res["mountpoint"], 0o750)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not set ownership on {res['mountpoint']}: {exc}", file=sys.stderr)
    # Leave it mounted: the mount (made here as root, in the host namespace)
    # propagates into the agent's sandboxed namespace, and the agent adopts it on
    # the SIGHUP reload that cvtool sends next — no restart required.
    rows = [e for e in _load()
            if e.get("store_id") != store_id and e.get("serial") != args.serial]
    rows.append({"store_id": store_id, "serial": args.serial, "name": res["name"],
                 "kind": res["kind"], "mirror_of_id": res.get("mirror_of_id")})
    _save(rows)
    print(json.dumps({"store_id": store_id, "name": res["name"], "kind": res["kind"],
                      "capacity_bytes": res["capacity_bytes"]}))
    return 0


def cmd_forget(args) -> int:
    rows = _load()
    entry = next((e for e in rows if e.get("store_id") == args.store_id), None)
    if not entry:
        print(f"error: no configured store with id {args.store_id!r}", file=sys.stderr)
        return 2
    storage_ops.unmount(str(EXT_BASE / args.store_id))
    _save([e for e in rows if e.get("store_id") != args.store_id])
    print(json.dumps({"store_id": args.store_id, "forgotten": True}))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agent.storage_cli",
                                description="Arkive appliance external-storage management")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("list", help="list configured + detected drives")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_list)
    ps = sub.add_parser("setup", help="format + register a detected drive (DESTRUCTIVE)")
    ps.add_argument("serial")
    ps.add_argument("--name", required=True)
    ps.add_argument("--mirror", default=None, help="store id to mirror (1:1)")
    ps.set_defaults(func=cmd_setup)
    pf = sub.add_parser("forget", help="unmount + deregister a store (data kept)")
    pf.add_argument("store_id")
    pf.set_defaults(func=cmd_forget)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
