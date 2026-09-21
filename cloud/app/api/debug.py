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
from sqlalchemy import func, text
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
        {"method": "GET", "path": "/api/debug/costs",
         "desc": "Cloud cost diagnostics: RAW provider breakdown (by_service/by_role/by_resource), "
                 "category rollup, and per-node/per-bucket mapping analysis with notes explaining "
                 "identical or misattributed prices. Optional ?provider=aws|azure."},
        {"method": "GET", "path": "/api/debug/integrations",
         "desc": "Integration collection diagnostics: run history, last success, DPI note, day-coverage "
                 "gaps, client/app counts, unmapped apps, stale devices. Optional ?tenant=&itype=ubiquiti."},
        {"method": "GET", "path": "/api/debug/billing",
         "desc": "Billing/entitlement diagnostics. No ?tenant → PARITY overview (legacy charge vs new "
                 "billing_calc recurring total per tenant, with a match flag) — the cutover gate. "
                 "?tenant=<id> → full dump: plan + pricing source, legacy vs calc, itemized lines, "
                 "persisted subscription + items, add-ons, overrides, entitlements vs usage, metered usage."},
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
    """Expire never-acked stragglers and delete long-terminal appliance commands.
    Envelopes are freed on-transition and drain with the DELETE, so this does NOT
    run a full-table envelope UPDATE (that detoasts the whole table — a WAL storm).
    Run VACUUM (FULL) afterwards to reclaim the freed space on disk."""
    from datetime import timedelta
    from ..models import ApplianceCommand
    now = datetime.utcnow()
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
    return {"ok": True, "stale_expired": int(stale), "old_deleted": int(deleted),
            "note": "run VACUUM (FULL) to reclaim the freed space on disk"}


@router.post("/db/prune", dependencies=[Depends(require_debug_key)])
def prune_db(db: Session = Depends(get_db)):
    """Prune every bounded-retention table (appliance_commands, sync_jobs,
    integration_runs, backup_runs, pending_actions, network_usage, node_metrics).
    Never touches the audit log or recovery points. Follow with VACUUM to reclaim."""
    from ..workers.pruning import prune_all
    counts = prune_all(db)
    return {"ok": True, "pruned": counts,
            "note": "run VACUUM (ANALYZE) to reclaim the freed space on disk"}


@router.get("/billing", dependencies=[Depends(require_debug_key)])
def billing_debug(tenant: str = "", limit: int = 100, db: Session = Depends(get_db)):
    """Billing / entitlement diagnostics — the migration + go-live troubleshooting
    surface. Without ?tenant it returns a PARITY overview across tenants: the legacy
    charge (``_price_breakdown`` → BillingProfile.amount_cents) vs the new
    deterministic ``billing_calc`` recurring total, with a mismatch flag — so you can
    prove the new engine reproduces today's prices before cutover. With ?tenant=<id>
    it dumps everything for one tenant: plan + pricing source (catalog version or
    legacy fallback), legacy vs calc, the itemized calc lines, the persisted
    subscription + items, active add-ons, entitlement overrides, derived entitlements
    vs usage, and current-period metered usage."""
    from ..models import Tenant, User, BillingProfile
    from .. import billing_calc, subscriptions, metering, catalog, entitlements
    from ..entitlements.models import TenantAddOn, EntitlementOverride

    def _legacy_cents(t: "Tenant"):
        """Legacy authoritative recurring cents (what actually bills today), or None."""
        from .billing import _plan_amount_cents
        owner = (db.query(User).filter(User.tenant_id == t.id)
                 .order_by(User.created_at.asc()).first())
        if owner is None:
            return None
        try:
            cents, _cur, _pid, _name = _plan_amount_cents(db, owner, t)
            return int(cents)
        except Exception as exc:  # noqa: BLE001
            logger.warning("debug/billing legacy calc failed for %s: %s", t.id, exc)
            return None

    def _calc(t: "Tenant"):
        try:
            return billing_calc.calculate(db, t)
        except Exception as exc:  # noqa: BLE001
            logger.warning("debug/billing calc failed for %s: %s", t.id, exc)
            return None

    # ---- Overview: parity sweep across tenants (the cutover gate) --------------
    if not tenant:
        rows = []
        n_match = n_mismatch = n_unknown = 0
        for t in db.query(Tenant).order_by(Tenant.created_at.asc()).limit(max(1, min(1000, limit))).all():
            legacy = _legacy_cents(t)
            c = _calc(t)
            calc_cents = c.recurring_cents if c else None
            prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == t.id).first()
            if legacy is None or calc_cents is None:
                match = None
                n_unknown += 1
            else:
                match = (legacy == calc_cents)
                n_match += int(match)
                n_mismatch += int(not match)
            rows.append({
                "tenant_id": t.id, "name": t.name, "plan": t.plan,
                "tenant_type": getattr(t, "tenant_type", ""),
                "legacy_cents": legacy, "calc_cents": calc_cents,
                "delta_cents": (None if (legacy is None or calc_cents is None) else calc_cents - legacy),
                "match": match,
                "profile_amount_cents": (int(prof.amount_cents or 0) if prof else None),
                "profile_status": (prof.status if prof else None),
                "profile_active": (bool(prof.active) if prof else None),
            })
        # Worst mismatches first so the parity gaps are obvious.
        rows.sort(key=lambda r: (r["match"] is not False, -abs(r["delta_cents"] or 0)))
        return {"mode": "parity_overview",
                "counts": {"match": n_match, "mismatch": n_mismatch,
                           "unknown": n_unknown, "total": len(rows)},
                "note": "match=true means the new billing_calc reproduces today's legacy charge to the "
                        "cent. Resolve all mismatches before flipping billing_source=calc.",
                "tenants": rows}

    # ---- Detail: one tenant, full picture -------------------------------------
    t = db.get(Tenant, tenant)
    if not t:
        raise HTTPException(404, "tenant not found")
    owner = (db.query(User).filter(User.tenant_id == t.id)
             .order_by(User.created_at.asc()).first())
    c = _calc(t)
    legacy = _legacy_cents(t)
    pricing = catalog.plan_pricing(db, str(t.plan or "").lower())
    prof = db.query(BillingProfile).filter(BillingProfile.tenant_id == t.id).first()
    try:
        ents = {k: {"value": e.value, "type": e.type, "source": e.source}
                for k, e in entitlements.derive(db, t, owner).items()}
    except Exception as exc:  # noqa: BLE001
        ents = {"_error": str(exc)[:200]}
    usage = {k: entitlements.get_usage(db, t, k) for k in ("protected_users", "protected_data_tb")}
    active_addons = [{"code": ta.addon_code, "quantity": ta.quantity, "status": ta.status,
                      "price_cents_snapshot": ta.price_cents_snapshot, "version": ta.addon_version}
                     for ta in db.query(TenantAddOn).filter(TenantAddOn.tenant_id == t.id).all()]
    overrides = [{"key": o.key, "value": o.value, "active": bool(o.active),
                  "reason": o.reason, "expires_at": _jsonable(o.expires_at)}
                 for o in db.query(EntitlementOverride).filter(EntitlementOverride.tenant_id == t.id).all()]
    return {
        "mode": "tenant_detail",
        "tenant": {"id": t.id, "name": t.name, "plan": t.plan,
                   "tenant_type": getattr(t, "tenant_type", ""),
                   "licensed_bytes": int(getattr(t, "licensed_bytes", 0) or 0),
                   "appliance_plan": getattr(t, "appliance_plan", None) or []},
        "pricing_source": {"source": pricing.get("source"), "version": pricing.get("version"),
                           "currency": pricing.get("currency")},
        "parity": {"legacy_cents": legacy,
                   "calc_recurring_cents": (c.recurring_cents if c else None),
                   "delta_cents": (None if (legacy is None or not c) else c.recurring_cents - legacy),
                   "match": (None if (legacy is None or not c) else legacy == c.recurring_cents)},
        "calc": (c.as_dict() if c else None),
        "billing_profile": ({"amount_cents": int(prof.amount_cents or 0), "currency": prof.currency,
                             "status": prof.status, "active": bool(prof.active),
                             "plan_id": prof.plan_id, "plan_name": prof.plan_name,
                             "next_charge_at": _jsonable(prof.next_charge_at),
                             "dunning_attempts": prof.dunning_attempts or 0} if prof else None),
        "subscription": subscriptions.view(db, t),
        "entitlements": ents, "usage": usage,
        "active_addons": active_addons, "overrides": overrides,
        "metered_usage": metering.tenant_view(db, t),
    }


@router.get("/integrations", dependencies=[Depends(require_debug_key)])
def integrations_debug(tenant: str = "", itype: str = "", db: Session = Depends(get_db)):
    """Integration collection diagnostics: per instance, the run history, last
    success, DPI note, day-coverage gaps, client/app counts, unmapped apps and
    stale devices — so you can see WHY telemetry (e.g. UniFi) stopped or is partial.
    Optional ?tenant=<id>&itype=<ubiquiti>."""
    from datetime import timedelta, timezone
    from ..models import (IntegrationInstance, IntegrationRun, NetworkApp,
                          NetworkClient, NetworkSample)
    from ..integrations.source_map import map_app_to_source
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    q = db.query(IntegrationInstance)
    if tenant:
        q = q.filter(IntegrationInstance.tenant_id == tenant)
    if itype:
        q = q.filter(IntegrationInstance.integration_type == itype)
    out = []
    for inst in q.order_by(IntegrationInstance.updated_at.desc()).limit(200).all():
        runs = (db.query(IntegrationRun)
                .filter(IntegrationRun.integration_id == inst.id)
                .order_by(IntegrationRun.created_at.desc()).limit(20).all())
        ok = sum(1 for r in runs if r.status == "ok")
        err = sum(1 for r in runs if r.status == "error")
        since = today - timedelta(days=13)
        present = {d for (d,) in db.query(NetworkSample.day).filter(
            NetworkSample.integration_id == inst.id, NetworkSample.dim == "total",
            NetworkSample.day >= since).all()}
        missing = []
        d = since
        while d <= today:
            if d not in present:
                missing.append(d.strftime("%m-%d"))
            d += timedelta(days=1)
        clients = db.query(NetworkClient).filter(NetworkClient.integration_id == inst.id).all()
        apps = db.query(NetworkApp).filter(NetworkApp.integration_id == inst.id).all()
        stale_clients = sum(1 for c in clients if c.last_seen and (now - c.last_seen).days >= 2)
        unmapped_apps = sum(1 for a in apps
                            if not (a.source_type or "") and not map_app_to_source(a.name, a.category))
        mapped_apps = sum(1 for a in apps if a.source_type)
        out.append({
            "id": inst.id, "tenant_id": inst.tenant_id, "type": inst.integration_type,
            "label": inst.label, "enabled": inst.enabled, "status": inst.status,
            "provision_state": inst.provision_state, "appliance_id": inst.appliance_id,
            "last_run_at": inst.last_run_at.isoformat() if inst.last_run_at else None,
            "last_success_at": inst.last_success_at.isoformat() if inst.last_success_at else None,
            "last_error": (inst.last_error or "")[:300],
            "last_stats": inst.last_stats or {},
            "dpi_note": (inst.last_stats or {}).get("note", ""),
            "runs_recent": {"ok": ok, "error": err, "total": len(runs)},
            "run_errors": [{"at": r.created_at.isoformat() if r.created_at else None,
                            "error": (r.error or "")[:200]}
                           for r in runs if r.status == "error"][:5],
            "clients": len(clients), "apps": len(apps),
            "mapped_apps": mapped_apps, "unmapped_apps": unmapped_apps,
            "assigned_devices": sum(1 for c in clients if c.owner_user_id),
            "stale_devices": stale_clients,
            "coverage_14d": {"present": len(present), "missing_days": missing},
        })
    return {"generated_at": now.isoformat(), "count": len(out), "integrations": out}


@router.get("/costs", dependencies=[Depends(require_debug_key)])
def costs_debug(provider: str = "", db: Session = Depends(get_db)):
    """Cloud cost diagnostics: live-call the provider cost API for each Cloud
    Billing service object and return the RAW breakdown (by_service/by_role/
    by_resource) + per-object mapping analysis so you can see WHY prices are
    identical or misattributed. Optional ?provider=aws|azure."""
    from .. import cloud_costs
    logger.info("debug: cost diagnostics requested (provider=%s)", provider or "all")
    return cloud_costs.diagnose(db, provider_filter=provider)


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


# --------------------------------------------------------------------------- #
# Notification testing (admin) — send any notification type to a chosen user   #
# --------------------------------------------------------------------------- #

@router.get("/notifications", dependencies=[Depends(require_debug_key)])
def notif_overview(db: Session = Depends(get_db)):
    from .. import notifications as notif
    from ..models import NotificationLog
    recent = (db.query(NotificationLog)
              .order_by(NotificationLog.created_at.desc()).limit(20).all())
    return {
        "types": notif.NOTIFICATION_TYPES,
        "settings": {
            "source_repeat_hours": notif.source_repeat_hours(db),
            "enabled_insights": notif.enabled_insights(db),
        },
        "recent": [{"type": r.type, "to": r.to_email, "subject": r.subject,
                    "ok": bool(r.ok),
                    "at": r.created_at.isoformat() if r.created_at else None} for r in recent],
    }


@router.get("/notifications/users", dependencies=[Depends(require_debug_key)])
def notif_users(q: str = "", db: Session = Depends(get_db)):
    """Search accounts to pick a test recipient."""
    from ..models import User
    query = db.query(User)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.filter(func.lower(User.email).like(like))
    rows = query.order_by(User.created_at.desc()).limit(25).all()
    return {"users": [{"id": u.id, "email": u.email, "name": u.full_name,
                       "tenant_id": u.tenant_id, "role": u.role} for u in rows]}


class NotifTest(BaseModel):
    type: str
    user_id: str


@router.post("/notifications/test", dependencies=[Depends(require_debug_key)])
def notif_test(body: NotifTest, db: Session = Depends(get_db)):
    from .. import notifications as notif
    from ..models import User
    user = db.get(User, body.user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    if body.type not in {t["key"] for t in notif.NOTIFICATION_TYPES}:
        raise HTTPException(400, "unknown notification type")
    result = notif.send_test(db, user, body.type)
    logger.warning("debug: test notification %s -> %s (%s)", body.type, user.email,
                   result.get("ok"))
    return result


class NotifSettings(BaseModel):
    source_repeat_hours: int | None = None
    enabled_insights: list[str] | None = None


@router.put("/notifications/settings", dependencies=[Depends(require_debug_key)])
def notif_settings(body: NotifSettings, db: Session = Depends(get_db)):
    def _set(key: str, value: str):
        row = db.get(SystemSetting, key)
        if row is None:
            db.add(SystemSetting(key=key, value=value))
        else:
            row.value = value
    if body.source_repeat_hours is not None:
        _set("notif.source_repeat_hours", str(max(1, int(body.source_repeat_hours))))
    if body.enabled_insights is not None:
        _set("notif.enabled_insights", ",".join(x.strip() for x in body.enabled_insights if x.strip()))
    db.commit()
    from .. import notifications as notif
    return {"ok": True, "settings": {
        "source_repeat_hours": notif.source_repeat_hours(db),
        "enabled_insights": notif.enabled_insights(db),
    }}
