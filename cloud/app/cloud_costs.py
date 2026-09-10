"""
Cloud cost / billing integration.

Reads month-to-date cloud spend from a **Cloud Billing** service object
(``cloud-billing-aws`` → AWS Cost Explorer, ``cloud-billing-azure`` → Azure Cost
Management) and maps it to the platform entities that incur it — nodes (compute),
Arkive Cloud storage, backups, and other micro-services. The cost worker samples
hourly and records ``CloudCostSample`` rows so the admin can see a per-object
"this month" badge and trends, and compare cost vs. revenue.

Cloud APIs are called lazily + best-effort: a credential/permission/outage error
is logged (with the provider's message) and never crashes the worker.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("cv.costs")

_BILLING_KINDS = ("cloud-billing-aws", "cloud-billing-azure")
CATEGORIES = ("nodes", "storage", "backups", "microservices", "other")


def provider_of(kind: str) -> str:
    return {"cloud-billing-aws": "aws", "cloud-billing-azure": "azure"}.get(kind or "", "")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _period(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


# --------------------------------------------------------------------------- #
# IAM / access guidance (mirrors the auto-provision guidance)                 #
# --------------------------------------------------------------------------- #

IAM_GUIDANCE = {
    "aws": {
        "title": "AWS IAM policy — Cloud Billing (Cost Explorer)",
        "summary": "Create an IAM user (programmatic access) and attach this "
                   "read-only Cost Explorer policy. Use its access key id + secret "
                   "as the service object credentials. Cost Explorer must be enabled "
                   "on the payer account.",
        "credentials": ["aws_access_key_id", "aws_secret_access_key"],
        "policy": {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "ArkiveCostRead", "Effect": "Allow", "Action": [
                    "ce:GetCostAndUsage", "ce:GetCostForecast",
                    "ce:GetDimensionValues", "ce:GetTags"], "Resource": "*"},
            ],
        },
        "notes": [
            "Enable Cost Explorer in Billing → Cost Explorer (one-time; data appears within ~24h).",
            "Tag your resources with arkive:role (auto-provisioned nodes already are) and activate "
            "'arkive:role' as a Cost Allocation Tag in the Billing console so per-node costs resolve.",
            "Cost Explorer is a global (us-east-1) service and read-only — no write access is granted.",
        ],
    },
    "azure": {
        "title": "Azure role assignment — Cloud Billing (Cost Management)",
        "summary": "Register an App (service principal) and grant it Cost Management "
                   "Reader on the subscription. Use its tenant ID, client ID, a client "
                   "secret and the subscription ID as the credentials.",
        "credentials": ["tenant_id", "client_id", "client_secret", "subscription_id"],
        "roles": [
            {"role": "Cost Management Reader", "scope": "the subscription whose spend you want to read"},
        ],
        "notes": [
            "Cost Management Reader is read-only — it grants no access to resources or data.",
            "Tag resources with arkive-role so per-node costs resolve (grouping by ServiceName always works).",
        ],
    },
}


# --------------------------------------------------------------------------- #
# Service-name → category mapping                                             #
# --------------------------------------------------------------------------- #

def _categorize(provider: str, service_name: str) -> str:
    n = (service_name or "").lower()
    if any(t in n for t in ("elastic compute", "ec2", "virtual machines", "compute")):
        return "nodes"
    if any(t in n for t in ("simple storage", "s3", "blob", "storage")):
        return "storage"  # split into storage/backups later by byte share
    if any(t in n for t in ("route 53", "dns", "simple email", "ses", "email",
                            "cloudwatch", "monitor", "data transfer", "bandwidth",
                            "key management", "key vault", "load balanc")):
        return "microservices"
    return "other"


# --------------------------------------------------------------------------- #
# Provider fetchers (best-effort)                                             #
# --------------------------------------------------------------------------- #

def fetch_costs(provider: str, config: dict) -> dict:
    """Return normalized month-to-date spend:
    ``{currency, period, total, by_service:{name:amt}, by_role:{role:amt}}``.
    Raises on hard credential/permission errors so the caller can record detail."""
    provider = (provider or "").lower()
    if provider == "aws":
        return _aws_costs(config or {})
    if provider == "azure":
        return _azure_costs(config or {})
    raise ValueError(f"unsupported billing provider '{provider}'")


def _aws_costs(config: dict) -> dict:
    import boto3
    ak = (config.get("aws_access_key_id") or "").strip()
    sk = (config.get("aws_secret_access_key") or "").strip()
    if not ak or not sk:
        raise ValueError("AWS access key id + secret are required")
    session = boto3.session.Session(aws_access_key_id=ak, aws_secret_access_key=sk,
                                    region_name="us-east-1")  # Cost Explorer is global
    ce = session.client("ce")
    now = datetime.now(timezone.utc)
    start = now.replace(day=1).strftime("%Y-%m-%d")
    end = (now + timedelta(days=1)).strftime("%Y-%m-%d")  # CE end is exclusive
    tp = {"Start": start, "End": end}
    by_service: dict[str, float] = {}
    resp = ce.get_cost_and_usage(TimePeriod=tp, Granularity="MONTHLY", Metrics=["UnblendedCost"],
                                 GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
    currency = "USD"
    for r in resp.get("ResultsByTime", []):
        for g in r.get("Groups", []):
            name = g["Keys"][0]
            m = g["Metrics"]["UnblendedCost"]
            currency = m.get("Unit", currency)
            by_service[name] = by_service.get(name, 0.0) + float(m.get("Amount", 0) or 0)
    by_role: dict[str, float] = {}
    try:
        resp2 = ce.get_cost_and_usage(TimePeriod=tp, Granularity="MONTHLY", Metrics=["UnblendedCost"],
                                      GroupBy=[{"Type": "TAG", "Key": "arkive:role"}])
        for r in resp2.get("ResultsByTime", []):
            for g in r.get("Groups", []):
                key = g["Keys"][0]
                role = key.split("$", 1)[1] if "$" in key else key
                by_role[role or "untagged"] = by_role.get(role or "untagged", 0.0) + \
                    float(g["Metrics"]["UnblendedCost"].get("Amount", 0) or 0)
    except Exception as exc:  # noqa: BLE001 — tag grouping is optional (needs activated cost tag)
        logger.info("cost: arkive:role tag grouping unavailable (%s)", str(exc)[:120])
    return {"currency": currency, "period": _period(now),
            "total": round(sum(by_service.values()), 4), "by_service": by_service, "by_role": by_role}


def _azure_costs(config: dict) -> dict:
    from azure.identity import ClientSecretCredential
    from azure.mgmt.costmanagement import CostManagementClient
    for k in ("tenant_id", "client_id", "client_secret", "subscription_id"):
        if not (config.get(k) or "").strip():
            raise ValueError(f"Azure {k} is required")
    cred = ClientSecretCredential(config["tenant_id"].strip(), config["client_id"].strip(),
                                  config["client_secret"].strip())
    client = CostManagementClient(cred)
    scope = f"/subscriptions/{config['subscription_id'].strip()}"
    query = {
        "type": "ActualCost", "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ServiceName"}],
        },
    }
    result = client.query.usage(scope, query)
    cols = [c.name for c in (result.columns or [])]
    i_cost = next((i for i, c in enumerate(cols) if c.lower() in ("pretaxcost", "cost", "costusd")), 0)
    i_svc = next((i for i, c in enumerate(cols) if c.lower() == "servicename"), None)
    i_cur = next((i for i, c in enumerate(cols) if c.lower() in ("currency", "billingcurrency")), None)
    by_service: dict[str, float] = {}
    currency = "USD"
    now = datetime.now(timezone.utc)
    for row in (result.rows or []):
        try:
            amt = float(row[i_cost] or 0)
        except (TypeError, ValueError, IndexError):
            amt = 0.0
        name = str(row[i_svc]) if (i_svc is not None and i_svc < len(row)) else "Azure"
        if i_cur is not None and i_cur < len(row) and row[i_cur]:
            currency = str(row[i_cur])
        by_service[name] = by_service.get(name, 0.0) + amt
    return {"currency": currency, "period": _period(now),
            "total": round(sum(by_service.values()), 4), "by_service": by_service, "by_role": {}}


# --------------------------------------------------------------------------- #
# Mapping to entities + sampling                                              #
# --------------------------------------------------------------------------- #

def _storage_backup_split(db) -> tuple[float, float]:
    """Fraction of object-storage cost to attribute to (storage, backups) by their
    stored-byte share. Falls back to all-storage when there are no backup bytes."""
    from .models import BackupRun
    from sqlalchemy import func
    try:
        cloud_bytes = _cloud_stored_bytes(db)
        backup_bytes = int(db.query(func.coalesce(func.sum(BackupRun.total_bytes), 0))
                           .filter(BackupRun.status.in_(["success", "partial"])).scalar() or 0)
        tot = cloud_bytes + backup_bytes
        if tot <= 0:
            return 1.0, 0.0
        return cloud_bytes / tot, backup_bytes / tot
    except Exception:  # noqa: BLE001
        return 1.0, 0.0


def _cloud_stored_bytes(db) -> int:
    from .models import SnapshotReceipt
    from sqlalchemy import func
    try:
        return int(db.query(func.coalesce(func.sum(SnapshotReceipt.total_bytes), 0))
                   .filter(SnapshotReceipt.destination == "cv-cloud").scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


def _map_samples(db, provider: str, svc_id: str, breakdown: dict, ts: datetime) -> list:
    """Turn a normalized breakdown into CloudCostSample rows: a grand total, one
    per category, per-node (compute split by role/count) and per-storage-service."""
    from .models import CloudCostSample, Node, ServiceObject
    currency = breakdown.get("currency", "USD")
    period = breakdown.get("period", _period(ts))
    by_service = breakdown.get("by_service", {}) or {}
    by_role = breakdown.get("by_role", {}) or {}

    cat_totals = {c: 0.0 for c in CATEGORIES}
    for name, amt in by_service.items():
        cat_totals[_categorize(provider, name)] += float(amt or 0)
    # Split object-storage cost into storage (Arkive Cloud) vs backups by byte share.
    storage_total = cat_totals["storage"]
    if storage_total > 0:
        s_frac, b_frac = _storage_backup_split(db)
        cat_totals["storage"] = round(storage_total * s_frac, 4)
        cat_totals["backups"] = round(cat_totals["backups"] + storage_total * b_frac, 4)

    total = round(sum(cat_totals.values()), 4)
    rows: list = []

    def _row(category, etype, eid, label, amount):
        rows.append(CloudCostSample(
            ts=ts, provider=provider, service_object_id=svc_id, category=category,
            entity_type=etype, entity_id=eid, entity_label=label,
            amount=round(float(amount or 0), 4), currency=currency, period=period,
            meta={} if etype != "total" else {"by_service": by_service}))

    _row("other", "total", None, "All cloud spend", total)
    for c in CATEGORIES:
        _row(c, "category", None, c.title(), cat_totals[c])

    # Per-node: distribute the compute cost across nodes, by role when tags resolve.
    nodes = db.query(Node).all()
    node_cost = cat_totals["nodes"]
    if node_cost > 0 and nodes:
        role_counts: dict[str, int] = {}
        for n in nodes:
            role_counts[n.role] = role_counts.get(n.role, 0) + 1
        has_role_data = any(v for v in by_role.values())
        for n in nodes:
            if has_role_data and n.role in by_role and role_counts.get(n.role):
                amt = by_role[n.role] / role_counts[n.role]
            else:
                amt = node_cost / len(nodes)
            _row("nodes", "node", n.id, n.name, amt)

    # Per storage service: distribute the storage-category cost by stored bytes.
    storage_svcs = (db.query(ServiceObject)
                    .filter(ServiceObject.kind.in_(("storage-s3", "storage-azure"))).all())
    if cat_totals["storage"] > 0 and storage_svcs:
        share = cat_totals["storage"] / len(storage_svcs)
        for s in storage_svcs:
            _row("storage", "storage_service", s.id, s.name, share)
    return rows


def sample_all(db) -> int:
    """Fetch + record hourly cost samples for every Cloud Billing service object.
    Deduped per (hour × service). Best-effort — records the provider error detail
    on failure. Returns the number of service objects sampled."""
    from .models import CloudCostSample, ServiceObject
    from . import services, audit
    ts = _now().replace(minute=0, second=0, microsecond=0)
    sampled = 0
    for svc in db.query(ServiceObject).filter(ServiceObject.kind.in_(_BILLING_KINDS),
                                              ServiceObject.enabled.is_(True)).all():
        exists = db.query(CloudCostSample.id).filter(
            CloudCostSample.service_object_id == svc.id, CloudCostSample.ts == ts).first()
        if exists:
            continue
        provider = provider_of(svc.kind)
        cfg = (services.resolve_service(db, svc.id) or {}).get("config", {}) or {}
        try:
            breakdown = fetch_costs(provider, cfg)
        except Exception as exc:  # noqa: BLE001 — never crash the worker on a billing error
            detail = str(exc)[:200]
            logger.warning("cost sample failed for %s (%s): %s", svc.name, provider, detail)
            try:
                audit.record(db, actor="system", action="costs.fetch_failed", category="system",
                             severity="warning", resource=svc.id,
                             detail={"provider": provider, "service": svc.name, "error": detail})
            except Exception:  # noqa: BLE001
                db.rollback()
            continue
        try:
            for row in _map_samples(db, provider, svc.id, breakdown, ts):
                db.add(row)
            db.commit()
            sampled += 1
            logger.info("cost sample: %s (%s) MTD total %.2f %s",
                        svc.name, provider, breakdown.get("total", 0), breakdown.get("currency", "USD"))
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("cost sample store failed for %s", svc.id)
    _INVALIDATE()
    return sampled


# --------------------------------------------------------------------------- #
# Read helpers (cached latest snapshot)                                       #
# --------------------------------------------------------------------------- #

_latest_cache: dict = {}
_latest_at: float = 0.0
_LATEST_TTL = 120.0


def _INVALIDATE() -> None:
    global _latest_at
    _latest_at = 0.0


def latest_costs(db) -> dict:
    """Most recent month-to-date cost per (entity_type, entity_id) for the current
    period, keyed 'etype:eid'. Cached briefly (hot on node/storage list views)."""
    global _latest_cache, _latest_at
    if _latest_cache and time.time() - _latest_at < _LATEST_TTL:
        return _latest_cache
    from .models import CloudCostSample
    out: dict = {}
    period = _period(_now())
    latest_ts = (db.query(CloudCostSample.ts)
                 .filter(CloudCostSample.period == period)
                 .order_by(CloudCostSample.ts.desc()).first())
    if latest_ts:
        rows = (db.query(CloudCostSample)
                .filter(CloudCostSample.period == period, CloudCostSample.ts == latest_ts[0]).all())
        for r in rows:
            key = f"{r.entity_type}:{r.entity_id or ''}"
            out[key] = out.get(key, 0.0) + float(r.amount or 0)
        out["_ts"] = latest_ts[0].isoformat()
        out["_currency"] = rows[0].currency if rows else "USD"
    _latest_cache, _latest_at = out, time.time()
    return out


def entity_cost(db, entity_type: str, entity_id: str) -> float:
    """Latest month-to-date cost attributed to one entity (node / storage service),
    or 0 when no cost data. Cheap after the first call (cached map)."""
    try:
        return round(float(latest_costs(db).get(f"{entity_type}:{entity_id or ''}", 0.0)), 2)
    except Exception:  # noqa: BLE001
        return 0.0


def summary(db) -> dict:
    """Month-to-date cost by category + total, and the most recent sample time."""
    m = latest_costs(db)
    cats = {}
    from .models import CloudCostSample
    period = _period(_now())
    lt = m.get("_ts")
    if lt:
        rows = (db.query(CloudCostSample)
                .filter(CloudCostSample.period == period,
                        CloudCostSample.entity_type == "category").all())
        latest = {}
        for r in rows:
            if r.ts.isoformat() == lt:
                latest[r.category] = round(float(r.amount or 0), 2)
        cats = latest
    return {
        "currency": m.get("_currency", "USD"),
        "period": period,
        "updated_at": m.get("_ts"),
        "total": round(float(m.get("total:", 0.0)), 2),
        "by_category": {c: cats.get(c, 0.0) for c in CATEGORIES},
        "configured": _has_billing_service(db),
    }


def _has_billing_service(db) -> bool:
    from .models import ServiceObject
    return db.query(ServiceObject.id).filter(
        ServiceObject.kind.in_(_BILLING_KINDS), ServiceObject.enabled.is_(True)).first() is not None


def trends(db, days: int = 30) -> dict:
    """Total + per-category month-to-date cost over time (one point per sample hour)."""
    from .models import CloudCostSample
    since = _now() - timedelta(days=max(1, min(365, days)))
    rows = (db.query(CloudCostSample)
            .filter(CloudCostSample.created_at >= since,
                    CloudCostSample.entity_type.in_(("total", "category")))
            .order_by(CloudCostSample.ts.asc()).all())
    by_ts: dict[str, dict] = {}
    for r in rows:
        key = r.ts.isoformat()
        pt = by_ts.setdefault(key, {"ts": key, "total": 0.0})
        if r.entity_type == "total":
            pt["total"] = round(float(r.amount or 0), 2)
        else:
            pt[r.category] = round(float(r.amount or 0), 2)
    return {"points": list(by_ts.values()), "categories": list(CATEGORIES)}
