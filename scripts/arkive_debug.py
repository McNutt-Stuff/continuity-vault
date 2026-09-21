#!/usr/bin/env python3
"""Arkive debug CLI — drive the key-gated /debug API from a shell (usable by an
operator or an automated LLM agent). Stdlib only; prints JSON.

Auth: pass --key or set ARKIVE_DEBUG_KEY (the debug key from Admin → Debug).
Base: pass --base or set ARKIVE_DEBUG_BASE (e.g. https://vault.arkive.life).

Discover everything first:
    python3 scripts/arkive_debug.py manifest

Typical slow-DB triage:
    python3 scripts/arkive_debug.py stats
    python3 scripts/arkive_debug.py prune
    python3 scripts/arkive_debug.py vacuum
    python3 scripts/arkive_debug.py benchmark

Ad-hoc read-only SQL:
    python3 scripts/arkive_debug.py query "SELECT relname, n_dead_tup FROM pg_stat_user_tables ORDER BY n_dead_tup DESC LIMIT 10"

Billing migration parity (legacy charge vs new billing_calc):
    python3 scripts/arkive_debug.py billing                 # parity overview across tenants
    python3 scripts/arkive_debug.py billing <tenant_id>     # full per-tenant billing dump
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _call(base: str, key: str, method: str, path: str, body: dict | None = None) -> dict:
    url = base.rstrip("/") + "/api/debug" + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Debug-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        return {"error": f"HTTP {e.code}", "detail": detail}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def main() -> int:
    p = argparse.ArgumentParser(description="Arkive debug API client")
    p.add_argument("--base", default=os.environ.get("ARKIVE_DEBUG_BASE", ""))
    p.add_argument("--key", default=os.environ.get("ARKIVE_DEBUG_KEY", ""))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("manifest")
    sub.add_parser("health")
    sub.add_parser("stats")
    sub.add_parser("nodes")
    sub.add_parser("prune")
    sub.add_parser("prune-commands")
    b = sub.add_parser("benchmark"); b.add_argument("iterations", nargs="?", type=int, default=3)
    q = sub.add_parser("query"); q.add_argument("sql"); q.add_argument("--limit", type=int, default=200)
    v = sub.add_parser("vacuum"); v.add_argument("table", nargs="?", default=None)
    a = sub.add_parser("analyze"); a.add_argument("table", nargs="?", default=None)
    bl = sub.add_parser("billing"); bl.add_argument("tenant", nargs="?", default="")
    bl.add_argument("--limit", type=int, default=100)
    fe = sub.add_parser("features"); fe.add_argument("tenant", nargs="?", default=""); fe.add_argument("--user", default="")
    co = sub.add_parser("costs"); co.add_argument("--provider", default="")
    ig = sub.add_parser("integrations"); ig.add_argument("--tenant", default=""); ig.add_argument("--itype", default="")
    args = p.parse_args()

    if not args.base or not args.key:
        print("error: set --base/--key or ARKIVE_DEBUG_BASE/ARKIVE_DEBUG_KEY", file=sys.stderr)
        return 2

    routes = {
        "manifest": ("GET", "", None),
        "health": ("GET", "/health", None),
        "stats": ("GET", "/db/stats", None),
        "nodes": ("GET", "/nodes", None),
        "prune": ("POST", "/db/prune", {}),
        "prune-commands": ("POST", "/db/prune-appliance-commands", {}),
    }
    if args.cmd in routes:
        method, path, body = routes[args.cmd]
    elif args.cmd == "benchmark":
        method, path, body = "POST", "/db/benchmark", {"iterations": args.iterations}
    elif args.cmd == "query":
        method, path, body = "POST", "/query", {"sql": args.sql, "limit": args.limit}
    elif args.cmd == "vacuum":
        method, path, body = "POST", "/db/maintenance", {"action": "vacuum", "table": args.table}
    elif args.cmd == "analyze":
        method, path, body = "POST", "/db/maintenance", {"action": "analyze", "table": args.table}
    elif args.cmd == "billing":
        qs = f"?tenant={args.tenant}&limit={args.limit}" if args.tenant else f"?limit={args.limit}"
        method, path, body = "GET", "/billing" + qs, None
    elif args.cmd == "features":
        qs = f"?tenant={args.tenant}&user={args.user}"
        method, path, body = "GET", "/features" + qs, None
    elif args.cmd == "costs":
        method, path, body = "GET", "/costs" + (f"?provider={args.provider}" if args.provider else ""), None
    elif args.cmd == "integrations":
        method, path, body = "GET", f"/integrations?tenant={args.tenant}&itype={args.itype}", None
    else:  # pragma: no cover
        print(f"unknown command {args.cmd}", file=sys.stderr)
        return 2

    out = _call(args.base, args.key, method, path, body)
    print(json.dumps(out, indent=2, default=str))
    return 1 if isinstance(out, dict) and out.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
