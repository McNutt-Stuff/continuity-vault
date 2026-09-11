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
CATEGORIES = ("nodes", "storage", "backups", "network", "microservices", "other")


def provider_of(kind: str) -> str:
    return {"cloud-billing-aws": "aws", "cloud-billing-azure": "azure"}.get(kind or "", "")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _period(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def _normalize_cloud_error(exc) -> dict:
    """Best-effort extraction of the provider error CODE + HTTP STATUS + a bounded
    message snippet from a botocore / azure exception, so a billing failure is
    triageable from Platform Logs without cloud console access."""
    code, status, msg = "", None, str(exc)
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):  # botocore ClientError
        err = resp.get("Error") or {}
        code = err.get("Code") or code
        status = (resp.get("ResponseMetadata") or {}).get("HTTPStatusCode") or status
        msg = err.get("Message") or msg
    azerr = getattr(exc, "error", None)  # azure HttpResponseError
    if azerr is not None and getattr(azerr, "code", None):
        code = azerr.code or code
    if getattr(exc, "status_code", None):
        status = exc.status_code
    return {"code": code or "", "status": status, "error": str(msg)[:200]}


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
            "For precise per-object mapping, enable Cost Explorer → Preferences → 'Hourly and "
            "resource-level data'. Then set each node/storage service's cloud resource id (EC2 "
            "instance id, S3 bucket) under Revenue & Costs → Cloud resource mapping.",
            "Network / data-transfer spend is tracked as its own category and attributed to the "
            "objects that drive it.",
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
            "For precise per-object mapping, set each node/storage service's cloud resource id "
            "(the Azure resource id, e.g. /subscriptions/…/virtualMachines/<name>) under "
            "Revenue & Costs → Cloud resource mapping.",
            "Network / data-transfer spend is tracked as its own category and attributed to the "
            "objects that drive it.",
        ],
    },
}


# --------------------------------------------------------------------------- #
# Service-name → category mapping                                             #
# --------------------------------------------------------------------------- #

def _categorize(provider: str, service_name: str) -> str:
    n = (service_name or "").lower()
    # Network / data-transfer first — many providers fold bandwidth into a
    # compute/"other" service name, so match the transfer signal explicitly.
    if any(t in n for t in ("data transfer", "bandwidth", "cloudfront", "content delivery",
                            "vpc", "nat gateway", "networking", "network", "ec2-other")):
        return "network"
    if any(t in n for t in ("elastic compute", "ec2", "virtual machines", "compute")):
        return "nodes"
    if any(t in n for t in ("simple storage", "s3", "blob", "storage")):
        return "storage"  # split into storage/backups later by byte share
    if any(t in n for t in ("route 53", "dns", "simple email", "ses", "email",
                            "cloudwatch", "monitor", "key management", "key vault",
                            "load balanc")):
        return "microservices"
    return "other"


def _is_network_usage(usage_type: str) -> bool:
    """True when an AWS usage type / Azure meter denotes network data transfer."""
    u = (usage_type or "").lower()
    return any(t in u for t in ("bytes", "data-transfer", "datatransfer", "data transfer",
                                "bandwidth", "-out", "-in", "egress", "ingress"))


# --------------------------------------------------------------------------- #
# Provider fetchers (best-effort)                                             #
# --------------------------------------------------------------------------- #

def fetch_costs(provider: str, config: dict) -> dict:
    """Return normalized month-to-date spend:
    ``{currency, period, total, by_service:{name:amt}, by_role:{role:amt},
    by_resource:{resource_id:amt}}``. ``by_resource`` is best-effort (resource-level
    cost data must be enabled; AWS resource granularity is limited to ~14d and is
    used as an attribution *share* basis, not the MTD total). Raises on hard
    credential/permission errors so the caller can record detail."""
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
    by_resource = _aws_resource_costs(ce, now)
    return {"currency": currency, "period": _period(now),
            "total": round(sum(by_service.values()), 4), "by_service": by_service,
            "by_role": by_role, "by_resource": by_resource}


def _aws_resource_costs(ce, now: datetime) -> dict:
    """Best-effort per-resource cost (resourceId → cost) via the resource-level
    Cost Explorer API. Requires resource-level data enabled and only supports the
    last ~14 days, so we clamp to that window and to the current month; used as an
    attribution SHARE basis (which resource incurred which fraction), not the MTD
    total. Silent (info) on failure — the feature still works via distribution."""
    by_resource: dict[str, float] = {}
    try:
        start_dt = max(now.replace(day=1), now - timedelta(days=13))
        tp = {"Start": start_dt.strftime("%Y-%m-%d"),
              "End": (now + timedelta(days=1)).strftime("%Y-%m-%d")}
        token = None
        for _ in range(10):  # bounded pagination
            kwargs = dict(TimePeriod=tp, Granularity="DAILY", Metrics=["UnblendedCost"],
                          GroupBy=[{"Type": "DIMENSION", "Key": "RESOURCE_ID"}])
            if token:
                kwargs["NextPageToken"] = token
            resp = ce.get_cost_and_usage_with_resources(**kwargs)
            for r in resp.get("ResultsByTime", []):
                for g in r.get("Groups", []):
                    rid = g["Keys"][0]
                    if not rid or rid == "NoResourceId":
                        continue
                    by_resource[rid] = by_resource.get(rid, 0.0) + \
                        float(g["Metrics"]["UnblendedCost"].get("Amount", 0) or 0)
            token = resp.get("NextPageToken")
            if not token:
                break
    except Exception as exc:  # noqa: BLE001 — resource-level data may be disabled
        logger.info("cost: AWS resource-level grouping unavailable (%s)", str(exc)[:140])
    return by_resource


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
    by_resource = _azure_resource_costs(client, scope)
    return {"currency": currency, "period": _period(now),
            "total": round(sum(by_service.values()), 4), "by_service": by_service,
            "by_role": {}, "by_resource": by_resource}


def _azure_resource_costs(client, scope: str) -> dict:
    """Best-effort per-resource cost (ResourceId → MTD cost) via Cost Management
    grouped by the ResourceId dimension. Info-level on failure."""
    by_resource: dict[str, float] = {}
    try:
        query = {
            "type": "ActualCost", "timeframe": "MonthToDate",
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "PreTaxCost", "function": "Sum"}},
                "grouping": [{"type": "Dimension", "name": "ResourceId"}],
            },
        }
        result = client.query.usage(scope, query)
        cols = [c.name for c in (result.columns or [])]
        i_cost = next((i for i, c in enumerate(cols) if c.lower() in ("pretaxcost", "cost", "costusd")), 0)
        i_rid = next((i for i, c in enumerate(cols) if c.lower() == "resourceid"), None)
        for row in (result.rows or []):
            if i_rid is None or i_rid >= len(row) or not row[i_rid]:
                continue
            try:
                by_resource[str(row[i_rid])] = by_resource.get(str(row[i_rid]), 0.0) + float(row[i_cost] or 0)
            except (TypeError, ValueError):
                continue
    except Exception as exc:  # noqa: BLE001
        logger.info("cost: Azure ResourceId grouping unavailable (%s)", str(exc)[:140])
    return by_resource


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
    per category, and per-entity rows (nodes, storage services, network). When
    resource-level cost data is available it drives per-object attribution by
    matching each entity's ``cloud_resource_id``; otherwise cost is distributed by
    role/byte/count. Resources that match no platform object are emitted as
    ``resource`` rows so the admin can associate them."""
    from .models import CloudCostSample, Node, ServiceObject
    currency = breakdown.get("currency", "USD")
    period = breakdown.get("period", _period(ts))
    by_service = breakdown.get("by_service", {}) or {}
    by_role = breakdown.get("by_role", {}) or {}
    by_resource = breakdown.get("by_resource", {}) or {}

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
    matched_rids: set = set()

    def _row(category, etype, eid, label, amount, resource_id="", meta=None):
        rows.append(CloudCostSample(
            ts=ts, provider=provider, service_object_id=svc_id, category=category,
            entity_type=etype, entity_id=eid, entity_label=label,
            cloud_resource_id=resource_id or "",
            amount=round(float(amount or 0), 4), currency=currency, period=period,
            meta=meta or {}))

    _row("other", "total", None, "All cloud spend", total,
         meta={"by_service": by_service, "resource_mapped": bool(by_resource),
               "resource_count": len(by_resource)})
    for c in CATEGORIES:
        _row(c, "category", None, c.replace("_", " ").title(), cat_totals[c])

    nodes = db.query(Node).all()
    storage_svcs = (db.query(ServiceObject)
                    .filter(ServiceObject.kind.in_(("storage-s3", "storage-azure"))).all())

    # --- compute (nodes) — resource-matched share, else role/count distribution ---
    node_amounts: dict[str, float] = {}
    node_cost = cat_totals["nodes"]
    if node_cost > 0 and nodes:
        matched = {n.id: by_resource[n.cloud_resource_id]
                   for n in nodes if (n.cloud_resource_id or "") in by_resource}
        matched_sum = sum(v for v in matched.values() if v) if matched else 0.0
        if matched_sum > 0:
            for n in nodes:
                node_amounts[n.id] = (matched.get(n.id, 0.0) / matched_sum) * node_cost
        else:
            role_counts: dict[str, int] = {}
            for n in nodes:
                role_counts[n.role] = role_counts.get(n.role, 0) + 1
            has_role = any(by_role.values())
            for n in nodes:
                if has_role and n.role in by_role and role_counts.get(n.role):
                    node_amounts[n.id] = by_role[n.role] / role_counts[n.role]
                else:
                    node_amounts[n.id] = node_cost / len(nodes)
        for n in nodes:
            rid = n.cloud_resource_id or ""
            mapped = bool(rid in by_resource and matched_sum > 0)
            if mapped:
                matched_rids.add(rid)
            _row("nodes", "node", n.id, n.name, node_amounts.get(n.id, 0.0),
                 resource_id=rid, meta={"mapped": mapped})

    # --- storage services — resource-matched share, else even by count ---
    stor_amounts: dict[str, float] = {}
    stor_cost = cat_totals["storage"]
    if stor_cost > 0 and storage_svcs:
        matched = {s.id: by_resource[s.cloud_resource_id]
                   for s in storage_svcs if (s.cloud_resource_id or "") in by_resource}
        matched_sum = sum(v for v in matched.values() if v) if matched else 0.0
        if matched_sum > 0:
            for s in storage_svcs:
                stor_amounts[s.id] = (matched.get(s.id, 0.0) / matched_sum) * stor_cost
        else:
            share = stor_cost / len(storage_svcs)
            for s in storage_svcs:
                stor_amounts[s.id] = share
        for s in storage_svcs:
            rid = s.cloud_resource_id or ""
            mapped = bool(rid in by_resource and matched_sum > 0)
            if mapped:
                matched_rids.add(rid)
            _row("storage", "storage_service", s.id, s.name, stor_amounts.get(s.id, 0.0),
                 resource_id=rid, meta={"mapped": mapped})

    # --- network (data in/out/transfer) — follows the compute/storage weights so a
    #     busier object carries proportionally more of the shared network spend ---
    net_cost = cat_totals["network"]
    if net_cost > 0 and (node_amounts or stor_amounts):
        weights: dict = {("node", nid): a for nid, a in node_amounts.items()}
        weights.update({("storage_service", sid): a for sid, a in stor_amounts.items()})
        wsum = sum(weights.values())
        if wsum <= 0:  # attribution weights all zero → spread evenly
            for k in weights:
                weights[k] = 1.0
            wsum = float(len(weights)) or 1.0
        label_by = {("node", n.id): n.name for n in nodes}
        label_by.update({("storage_service", s.id): s.name for s in storage_svcs})
        for (etype, eid), w in weights.items():
            _row("network", etype, eid, label_by.get((etype, eid), eid),
                 net_cost * (w / wsum))

    # --- unmapped cloud resources — real cost with no associated platform object ---
    if by_resource:
        unmapped = sorted(((rid, c) for rid, c in by_resource.items()
                           if rid not in matched_rids and c > 0),
                          key=lambda x: x[1], reverse=True)[:25]
        for rid, c in unmapped:
            _row("other", "resource", rid, rid, c, resource_id=rid, meta={"unmapped": True})
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
            info = _normalize_cloud_error(exc)
            op = "ce:GetCostAndUsage" if provider == "aws" else "CostManagement.query.usage"
            logger.warning("cost sample failed for %s (%s) op=%s status=%s code=%s: %s",
                           svc.name, provider, op, info.get("status"), info.get("code"), info.get("error"))
            try:
                audit.record(db, actor="system", action="costs.fetch_failed", category="system",
                             severity="warning", resource=svc.id,
                             detail={"provider": provider, "service": svc.name, "operation": op,
                                     "status": info.get("status"), "reason": info.get("code"),
                                     "error": info.get("error")})
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
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.exception("cost sample store failed for %s", svc.id)
            try:
                audit.record(db, actor="system", action="costs.store_failed", category="system",
                             severity="warning", resource=svc.id,
                             detail={"provider": provider, "service": svc.name, "error": str(exc)[:200]})
            except Exception:  # noqa: BLE001
                db.rollback()
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


def detail(db) -> dict:
    """Per-object cost breakdown for the latest sample (the Revenue & Costs
    drilldown): every category with its total and the platform objects/resources
    that make it up, plus unmapped cloud resources the admin should associate to
    an object. Reflects resource-level attribution when available."""
    from .models import CloudCostSample
    period = _period(_now())
    latest_ts = (db.query(CloudCostSample.ts).filter(CloudCostSample.period == period)
                 .order_by(CloudCostSample.ts.desc()).first())
    if not latest_ts:
        return {"period": period, "updated_at": None, "currency": "USD",
                "resource_mapped": False, "categories": [], "unmapped": []}
    ts = latest_ts[0]
    rows = (db.query(CloudCostSample)
            .filter(CloudCostSample.period == period, CloudCostSample.ts == ts).all())
    currency = rows[0].currency if rows else "USD"
    resource_mapped = False
    cat_total: dict[str, float] = {}
    cat_entities: dict[str, list] = {}
    unmapped: list = []
    for r in rows:
        if r.entity_type == "total":
            resource_mapped = bool((r.meta or {}).get("resource_mapped"))
        elif r.entity_type == "category":
            cat_total[r.category] = round(float(r.amount or 0), 2)
        elif r.entity_type in ("node", "storage_service"):
            cat_entities.setdefault(r.category, []).append({
                "entity_type": r.entity_type, "entity_id": r.entity_id,
                "label": r.entity_label, "amount": round(float(r.amount or 0), 2),
                "cloud_resource_id": r.cloud_resource_id or "",
                "mapped": bool((r.meta or {}).get("mapped"))})
        elif r.entity_type == "resource":
            unmapped.append({"cloud_resource_id": r.cloud_resource_id or r.entity_id or "",
                             "amount": round(float(r.amount or 0), 2)})
    categories = []
    for c in CATEGORIES:
        ents = sorted(cat_entities.get(c, []), key=lambda e: e["amount"], reverse=True)
        categories.append({"category": c, "amount": cat_total.get(c, 0.0), "entities": ents})
    return {"period": period, "updated_at": ts.isoformat(), "currency": currency,
            "resource_mapped": resource_mapped, "categories": categories,
            "unmapped": sorted(unmapped, key=lambda u: u["amount"], reverse=True)}


def _provider_hint(cloud: dict) -> str:
    p = ((cloud or {}).get("provider") or "").lower()
    if "aws" in p or "amazon" in p:
        return "aws"
    if "azure" in p or "microsoft" in p:
        return "azure"
    return p


def mappings(db) -> dict:
    """Every billable platform object (compute nodes + storage services) with its
    editable hyperscaler resource id and latest month-to-date cost, so an admin can
    add or fix a missing association. Also lists unmapped cloud resources seen in
    billing that don't yet map to any object."""
    from .models import Node, ServiceObject
    d = detail(db)
    mapped_flag: dict[str, bool] = {}
    for cat in d.get("categories", []):
        for e in cat.get("entities", []):
            key = f'{e["entity_type"]}:{e["entity_id"]}'
            mapped_flag[key] = mapped_flag.get(key, False) or bool(e.get("mapped"))
    items: list = []
    for n in db.query(Node).order_by(Node.name.asc()).all():
        items.append({"entity_type": "node", "entity_id": n.id, "label": n.name,
                      "kind": n.role, "provider": _provider_hint(n.cloud),
                      "cloud_resource_id": n.cloud_resource_id or "",
                      "cost_mtd": entity_cost(db, "node", n.id),
                      "matched": bool(mapped_flag.get(f"node:{n.id}"))})
    for s in (db.query(ServiceObject)
              .filter(ServiceObject.kind.in_(("storage-s3", "storage-azure")))
              .order_by(ServiceObject.name.asc()).all()):
        items.append({"entity_type": "storage_service", "entity_id": s.id, "label": s.name,
                      "kind": s.kind, "provider": "aws" if s.kind == "storage-s3" else "azure",
                      "cloud_resource_id": s.cloud_resource_id or "",
                      "cost_mtd": entity_cost(db, "storage_service", s.id),
                      "matched": bool(mapped_flag.get(f"storage_service:{s.id}"))})
    return {"items": items, "unmapped": d.get("unmapped", []),
            "resource_mapped": d.get("resource_mapped", False),
            "currency": d.get("currency", "USD"), "updated_at": d.get("updated_at")}


def set_mapping(db, entity_type: str, entity_id: str, resource_id: str) -> dict:
    """Set or clear the hyperscaler resource id on a node or storage service so its
    cloud spend can be associated. Returns the updated mapping."""
    from .models import Node, ServiceObject
    rid = (resource_id or "").strip()
    if entity_type == "node":
        obj = db.get(Node, entity_id)
    elif entity_type == "storage_service":
        obj = db.get(ServiceObject, entity_id)
    else:
        raise ValueError("entity_type must be 'node' or 'storage_service'")
    if not obj:
        raise ValueError("entity not found")
    obj.cloud_resource_id = rid
    db.commit()
    _INVALIDATE()
    return {"entity_type": entity_type, "entity_id": entity_id,
            "label": getattr(obj, "name", ""), "cloud_resource_id": rid}
