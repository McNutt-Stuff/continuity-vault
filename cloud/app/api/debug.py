"""Debug / diagnostics API — a controlled, key-gated surface to inspect, measure,
benchmark and test the live platform (and, via the fleet, its nodes).

Every endpoint is gated by the debug key set in the admin console (never exposed
without it). Query/maintenance operations are read-only or explicitly guarded so
this can be pointed at production safely. Federated calls reuse the fleet secret.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..db import engine, get_db, worker_engine
from ..models import SystemSetting

logger = logging.getLogger("cv.debug")
router = APIRouter(prefix="/debug", tags=["debug"])

_DEBUG_KEY = "debug_key"


# --------------------------------------------------------------------------- #
# Key management (shared with the admin endpoints)                            #
# --------------------------------------------------------------------------- #
def get_debug_key(db: Session) -> str:
    row = db.get(SystemSetting, _DEBUG_KEY)
    return (row.value or "") if row else ""


def set_debug_key(db: Session, value: str) -> str:
    row = db.get(SystemSetting, _DEBUG_KEY)
    if row is None:
        row = SystemSetting(key=_DEBUG_KEY, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()
    return value


def rotate_debug_key(db: Session) -> str:
    return set_debug_key(db, "dbg_" + secrets.token_urlsafe(24))


def require_debug_key(x_debug_key: str = Header(default=""),
                      db: Session = Depends(get_db)) -> bool:
    """Gate: the request must carry the admin-set debug key. Constant-time compare;
    403 if debugging is disabled (no key set) or the key doesn't match."""
    configured = get_debug_key(db)
    if not configured:
        raise HTTPException(403, "debug API is disabled — set a debug key in the admin console")
    if not x_debug_key or not hmac.compare_digest(x_debug_key, configured):
        raise HTTPException(403, "invalid debug key")
    return True


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    return str(v)


def _is_pg() -> bool:
    return engine.dialect.name == "postgresql"


# --------------------------------------------------------------------------- #
# Self-describing manifest — lets an LLM / automated agent discover and drive  #
# the debug surface without out-of-band docs.                                  #
# --------------------------------------------------------------------------- #
_MANIFEST = {
    "service": "arkive-debug",
    "auth": {"header": "X-Debug-Key",
             "how": "Send the admin-set debug key in the X-Debug-Key header on every /debug call. "
                    "403 if the key is unset or wrong."},
    "base_path": "/api/debug",
    "workflow": [
        "GET /api/debug/health — quick liveness (db ping, pools).",
        "GET /api/debug/db/stats — find bloat (high dead_ratio) or idle-in-transaction.",
        "POST /api/debug/db/prune — bound high-churn tables, then POST /api/debug/db/maintenance {action:'vacuum'} to reclaim.",
        "POST /api/debug/db/benchmark — confirm queries are fast again.",
        "POST /api/debug/query {sql} — read-only SELECT/WITH/EXPLAIN/SHOW to inspect data.",
        "GET /api/debug/nodes — per-node DB health across the fleet.",
    ],
    "endpoints": [
        {"method": "GET", "path": "/api/debug", "desc": "This manifest."},
        {"method": "GET", "path": "/api/debug/health", "desc": "DB ping, pool status, node/role."},
        {"method": "GET", "path": "/api/debug/db/stats",
         "desc": "DB size, per-table live/dead tuples + bloat ratio + size + last analyze, connection states, pools."},
        {"method": "POST", "path": "/api/debug/db/benchmark", "body": {"iterations": 3},
         "desc": "Time representative queries; returns per-query ms + a 'slow' list."},
        {"method": "POST", "path": "/api/debug/query",
         "body": {"sql": "SELECT ...", "limit": 200, "timeout_ms": 15000},
         "desc": "Run ONE read-only query (SELECT/WITH/EXPLAIN/SHOW). Returns columns + rows + ms."},
        {"method": "POST", "path": "/api/debug/db/maintenance",
         "body": {"action": "analyze|vacuum|vacuum_full", "table": "optional"},
         "desc": "Reclaim bloat / refresh planner stats (autocommit)."},
        {"method": "POST", "path": "/api/debug/db/prune",
         "desc": "Prune all bounded-retention tables (safe; never touches audit/recovery points)."},
        {"method": "POST", "path": "/api/debug/db/prune-appliance-commands",
         "desc": "Free inline-ciphertext envelopes + delete old terminal appliance commands."},
        {"method": "GET", "path": "/api/debug/nodes",
         "desc": "Fan out DB health to every fleet node (via the fleet secret) to find the slow one."},
    ],
    "notes": [
        "All responses are JSON. Query/maintenance are read-only or explicitly guarded — safe on production.",
        "Deletes/updates only mark rows dead; a VACUUM (autovacuum or the maintenance endpoint) reclaims disk.",
        "Never prunes the audit log (hash-chained) or snapshot receipts (recovery points).",
    ],
}


@router.get("", dependencies=[Depends(require_debug_key)])
def manifest():
    """Machine-readable catalog of the debug API so an LLM/automation can discover
    and drive it. Requires the debug key like every other endpoint."""
    return _MANIFEST


# --------------------------------------------------------------------------- #
# Database stats / health                                                     #
# --------------------------------------------------------------------------- #
def _pool_status(eng) -> dict:
    try:
        p = eng.pool
        return {"size": p.size(), "checked_in": p.checkedin(),
                "checked_out": p.checkedout(), "overflow": p.overflow()}
    except Exception:  # noqa: BLE001
        return {}


def _db_stats(db: Session) -> dict:
    out: dict = {"dialect": engine.dialect.name,
                 "pools": {"web": _pool_status(engine), "worker": _pool_status(worker_engine)}}
    if not _is_pg():
        # SQLite dev — row counts only.
        tables = []
        for (name,) in db.execute(text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")).all():
            try:
                n = db.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
            except Exception:  # noqa: BLE001
                n = None
            tables.append({"table": name, "rows": n})
        out["tables"] = tables
        return out
    out["database_size_bytes"] = int(db.execute(text("SELECT pg_database_size(current_database())")).scalar() or 0)
    # Per-table live/dead tuples, size and last (auto)vacuum/analyze — the key
    # signals for the "everything is slow" (bloat / stale stats) diagnosis.
    rows = db.execute(text("""
        SELECT relname,
               n_live_tup, n_dead_tup, n_mod_since_analyze,
               pg_total_relation_size(relid) AS total_bytes,
               last_vacuum, last_autovacuum, last_analyze, last_autoanalyze
        FROM pg_stat_user_tables
        ORDER BY pg_total_relation_size(relid) DESC
    """)).mappings().all()
    out["tables"] = [{
        "table": r["relname"], "live": int(r["n_live_tup"] or 0),
        "dead": int(r["n_dead_tup"] or 0),
        "dead_ratio": round((r["n_dead_tup"] or 0) / max(1, (r["n_live_tup"] or 0) + (r["n_dead_tup"] or 0)), 3),
        "mod_since_analyze": int(r["n_mod_since_analyze"] or 0),
        "total_bytes": int(r["total_bytes"] or 0),
        "last_vacuum": _jsonable(r["last_vacuum"] or r["last_autovacuum"]),
        "last_analyze": _jsonable(r["last_analyze"] or r["last_autoanalyze"]),
    } for r in rows]
    # Connection activity — idle-in-transaction is the classic autovacuum blocker.
    act = db.execute(text("""
        SELECT state, COUNT(*) AS n,
               MAX(EXTRACT(EPOCH FROM (now() - xact_start))) AS longest_xact_s
        FROM pg_stat_activity
        WHERE datname = current_database()
        GROUP BY state
    """)).mappings().all()
    out["connections"] = [{"state": r["state"] or "unknown", "count": int(r["n"]),
                           "longest_xact_s": round(float(r["longest_xact_s"] or 0), 1)} for r in act]
    try:
        out["idle_in_transaction"] = int(db.execute(text(
            "SELECT COUNT(*) FROM pg_stat_activity WHERE state = 'idle in transaction' "
            "AND datname = current_database()")).scalar() or 0)
    except Exception:  # noqa: BLE001
        out["idle_in_transaction"] = None
    return out


@router.get("/db/stats", dependencies=[Depends(require_debug_key)])
def db_stats(db: Session = Depends(get_db)):
    """Database size, per-table row counts / dead-tuple bloat / last vacuum, live
    connection states (incl. idle-in-transaction) and SQLAlchemy pool status."""
    return _db_stats(db)


class Bench(BaseModel):
    iterations: int = 1


@router.post("/db/benchmark", dependencies=[Depends(require_debug_key)])
def db_benchmark(body: Bench = Bench(), db: Session = Depends(get_db)):
    """Time a set of representative queries so slow DB paths are obvious."""
    checks = [
        ("ping", "SELECT 1"),
        ("count_users", "SELECT COUNT(*) FROM users"),
        ("count_tenants", "SELECT COUNT(*) FROM tenants"),
        ("count_search_documents", "SELECT COUNT(*) FROM search_documents"),
        ("count_snapshot_receipts", "SELECT COUNT(*) FROM snapshot_receipts"),
        ("recent_search", "SELECT id FROM search_documents ORDER BY created_at DESC LIMIT 50"),
        ("users_join_tenants",
         "SELECT u.id FROM users u LEFT JOIN tenants t ON t.id = u.tenant_id LIMIT 200"),
    ]
    iters = max(1, min(20, body.iterations))
    results = []
    for name, sql in checks:
        best = None
        rowcount = 0
        try:
            for _ in range(iters):
                t0 = time.perf_counter()
                res = db.execute(text(sql))
                rows = res.fetchall()
                dt = (time.perf_counter() - t0) * 1000
                rowcount = len(rows)
                best = dt if best is None else min(best, dt)
            results.append({"name": name, "ms": round(best or 0, 2), "rows": rowcount, "ok": True})
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            results.append({"name": name, "ms": None, "error": str(exc)[:200], "ok": False})
    slow = [r for r in results if r["ok"] and (r["ms"] or 0) > 250]
    return {"iterations": iters, "results": results, "slow": [r["name"] for r in slow]}


class Query(BaseModel):
    sql: str
    limit: int = 200
    timeout_ms: int = 15000


_FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "truncate", "create",
              "grant", "revoke", "vacuum", "copy", "call", "merge")
# Word-boundary match so column names like "created_at" / "updated_at" aren't
# mistaken for the DDL/DML keywords "create" / "update".
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(_FORBIDDEN) + r")\b", re.IGNORECASE)


@router.post("/query", dependencies=[Depends(require_debug_key)])
def run_query(body: Query, db: Session = Depends(get_db)):
    """Run a READ-ONLY SQL query (SELECT / WITH only) with a statement timeout and
    a row cap. Anything that mutates or is multi-statement is rejected."""
    sql = (body.sql or "").strip().rstrip(";")
    low = sql.lower()
    if not (low.startswith("select") or low.startswith("with") or low.startswith("explain")
            or low.startswith("show")):
        raise HTTPException(400, "only SELECT / WITH / EXPLAIN / SHOW queries are allowed")
    if ";" in sql:
        raise HTTPException(400, "only a single statement is allowed")
    if _FORBIDDEN_RE.search(sql):
        raise HTTPException(400, "query contains a forbidden keyword")
    limit = max(1, min(1000, body.limit))
    t0 = time.perf_counter()
    try:
        with engine.connect() as conn:
            if _is_pg():
                conn.exec_driver_sql(f"SET statement_timeout = {max(1000, min(60000, body.timeout_ms))}")
            res = conn.execute(text(sql))
            cols = list(res.keys()) if res.returns_rows else []
            rows = [[_jsonable(v) for v in row] for row in res.fetchmany(limit)] if res.returns_rows else []
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"query failed: {str(exc)[:300]}")
    return {"columns": cols, "rows": rows, "row_count": len(rows),
            "ms": round((time.perf_counter() - t0) * 1000, 2), "truncated": len(rows) >= limit}


class Maint(BaseModel):
    action: str = "analyze"   # analyze | vacuum | vacuum_full
    table: str | None = None  # optional single table


@router.post("/db/maintenance", dependencies=[Depends(require_debug_key)])
def db_maintenance(body: Maint):
    """Run VACUUM / ANALYZE to recover from bloat + stale planner stats (the usual
    cause of a broadly-slow database). Runs in AUTOCOMMIT (VACUUM can't be in a tx)."""
    if not _is_pg():
        return {"ok": False, "message": "maintenance is only supported on Postgres"}
    action = (body.action or "analyze").lower()
    tbl = (body.table or "").strip()
    if tbl and not tbl.replace("_", "").isalnum():
        raise HTTPException(400, "invalid table name")
    target = f" {tbl}" if tbl else ""
    if action == "analyze":
        stmt = f"ANALYZE{target}"
    elif action == "vacuum":
        stmt = f"VACUUM (ANALYZE){target}"
    elif action == "vacuum_full":
        stmt = f"VACUUM (FULL, ANALYZE){target}"
    else:
        raise HTTPException(400, "action must be analyze | vacuum | vacuum_full")
    t0 = time.perf_counter()
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql(stmt)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"maintenance failed: {str(exc)[:300]}")
    logger.warning("debug: ran %s (%.0fms)", stmt, (time.perf_counter() - t0) * 1000)
    return {"ok": True, "ran": stmt, "ms": round((time.perf_counter() - t0) * 1000, 2)}


@router.post("/db/prune-appliance-commands", dependencies=[Depends(require_debug_key)])
def prune_appliance_commands(db: Session = Depends(get_db)):
    """Free the inline-ciphertext payload from completed appliance commands and
    delete long-terminal rows — the usual reason appliance_commands is huge. Run
    VACUUM afterwards to reclaim the freed space on disk."""
    from datetime import timedelta
    from ..models import ApplianceCommand
    now = datetime.utcnow()
    freed = (db.query(ApplianceCommand)
             .filter(ApplianceCommand.status.in_(["acked", "rejected", "expired"]),
                     ApplianceCommand.envelope.isnot(None))
             .update({ApplianceCommand.envelope: {}}, synchronize_session=False))
    stale = (db.query(ApplianceCommand)
             .filter(ApplianceCommand.status.in_(["pending", "delivered"]),
                     ApplianceCommand.created_at < now - timedelta(days=1))
             .update({ApplianceCommand.status: "expired", ApplianceCommand.envelope: {}},
                     synchronize_session=False))
    deleted = (db.query(ApplianceCommand)
               .filter(ApplianceCommand.status.in_(["acked", "rejected", "expired"]),
                       ApplianceCommand.created_at < now - timedelta(days=7))
               .delete(synchronize_session=False))
    db.commit()
    return {"ok": True, "envelopes_freed": int(freed), "stale_expired": int(stale),
            "old_deleted": int(deleted),
            "note": "run VACUUM (ANALYZE) to reclaim the freed space on disk"}


@router.post("/db/prune", dependencies=[Depends(require_debug_key)])
def prune_db(db: Session = Depends(get_db)):
    """Prune every bounded-retention table (appliance_commands, sync_jobs,
    integration_runs, backup_runs, pending_actions, network_usage, node_metrics).
    Never touches the audit log or recovery points. Follow with VACUUM to reclaim."""
    from ..workers.pruning import prune_all
    counts = prune_all(db)
    return {"ok": True, "pruned": counts,
            "note": "run VACUUM (ANALYZE) to reclaim the freed space on disk"}


@router.get("/health", dependencies=[Depends(require_debug_key)])
def health(db: Session = Depends(get_db)):
    """Fast liveness snapshot: DB ping, pools, worker/scheduler state."""
    t0 = time.perf_counter()
    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    ping_ms = round((time.perf_counter() - t0) * 1000, 2)
    from ..config import get_settings
    s = get_settings()
    return {
        "db_ok": db_ok, "db_ping_ms": ping_ms,
        "dialect": engine.dialect.name,
        "pools": {"web": _pool_status(engine), "worker": _pool_status(worker_engine)},
        "node_role": s.node_role, "node_name": s.node_name or s.domain,
        "sync_enabled": s.sync_enabled,
        "federated": s.node_sync_scope,
    }


# --------------------------------------------------------------------------- #
# Federated: fan out diagnostics to fleet nodes                               #
# --------------------------------------------------------------------------- #
@router.get("/nodes", dependencies=[Depends(require_debug_key)])
def nodes_debug(db: Session = Depends(get_db)):
    """Per-node DB health across the fleet (self + each reachable customer-tenant
    node, via the fleet secret). Lets you spot which node's DB is the slow one."""
    from ..models import Node
    from ..config import get_settings
    s = get_settings()
    out = []
    for n in db.query(Node).all():
        row = {"id": n.id, "name": n.name, "role": n.role, "is_self": bool(n.is_self),
               "endpoint": n.endpoint, "reachable": None, "stats": None, "error": None}
        if n.is_self or (s.node_role == "control-plane" and not n.endpoint):
            try:
                row["stats"] = _brief_stats(db)
                row["reachable"] = True
            except Exception as exc:  # noqa: BLE001
                row["error"] = str(exc)[:200]
        elif n.endpoint and n.role != "public-web":
            try:
                data = _node_call(n, "/nodes/sync/debug")
                row["stats"] = data
                row["reachable"] = True
            except Exception as exc:  # noqa: BLE001
                row["reachable"] = False
                row["error"] = str(exc)[:200]
        out.append(row)
    return {"nodes": out}


def _brief_stats(db: Session) -> dict:
    """Compact DB health used for the per-node fleet view."""
    st = _db_stats(db)
    tables = sorted(st.get("tables", []), key=lambda t: t.get("total_bytes", 0), reverse=True)[:8]
    return {
        "dialect": st.get("dialect"),
        "database_size_bytes": st.get("database_size_bytes"),
        "idle_in_transaction": st.get("idle_in_transaction"),
        "pools": st.get("pools"),
        "top_tables": tables,
        "connections": st.get("connections"),
    }


def _node_call(node, path: str) -> dict:
    """Call a node's fleet debug endpoint with the shared fleet secret."""
    import httpx
    from . import site as _site
    base = (node.endpoint or "").rstrip("/")
    secret = _site._fleet_secret()
    r = httpx.get(f"{base}{path}", headers={"Authorization": f"Bearer {secret}"}, timeout=15.0)
    r.raise_for_status()
    return r.json()
