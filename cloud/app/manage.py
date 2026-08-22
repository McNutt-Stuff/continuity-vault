"""
Platform management CLI (run on the cloud host).

  python -m app.manage add-admin --email you@arkive.life --name "You"
  python -m app.manage list-admins
  python -m app.manage remove-admin --email you@arkive.life

``add-admin`` promotes an existing user or creates a new one in the platform
("Arkive Operations") tenant with ``is_platform_admin=True``. Sign-in still uses
the normal email-code flow — no password is set here.
"""

from __future__ import annotations

import argparse
import secrets

from .db import SessionLocal
from .models import Collection, ConnectorAccount, SyncJob, Tenant, User


def _platform_tenant(db) -> Tenant:
    # Reuse an existing platform-admin's tenant, then a plan="platform" tenant,
    # else create the operations tenant.
    existing = db.query(User).filter(User.is_platform_admin.is_(True)).first()
    if existing:
        t = db.get(Tenant, existing.tenant_id)
        if t:
            return t
    t = db.query(Tenant).filter(Tenant.plan == "platform").first()
    if t:
        return t
    t = Tenant(name="Arkive Operations", plan="platform",
               key_ownership_model="platform-managed",
               storage_prefix=f"t-ops-{secrets.token_hex(3)}")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def add_admin(email: str, name: str) -> None:
    email = email.strip().lower()
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        if u:
            u.is_platform_admin = True
            u.status = "active"
            db.commit()
            print(f"Promoted existing user to platform admin: {email}")
            return
        t = _platform_tenant(db)
        u = User(tenant_id=t.id, email=email, display_name=(name or email).strip(),
                 role="support-admin", is_platform_admin=True, status="active",
                 email_verified=True)
        db.add(u)
        db.commit()
        print(f"Created platform admin: {email} (tenant: {t.name})")


def remove_admin(email: str) -> None:
    email = email.strip().lower()
    with SessionLocal() as db:
        u = db.query(User).filter(User.email == email).first()
        if not u or not u.is_platform_admin:
            print(f"No platform admin with email {email}")
            return
        u.is_platform_admin = False
        db.commit()
        print(f"Revoked platform admin: {email}")


def list_admins() -> None:
    with SessionLocal() as db:
        rows = db.query(User).filter(User.is_platform_admin.is_(True)).all()
        if not rows:
            print("(no platform admins)")
        for u in rows:
            print(f"{u.email:40} {u.display_name:24} status={u.status}")


# -- Worker processes (background backup/sync jobs) --------------------------

_ACTIVE_JOB = ("queued", "running", "cancelling")


def list_jobs(all_jobs: bool = False) -> None:
    with SessionLocal() as db:
        q = db.query(SyncJob).order_by(SyncJob.created_at.desc())
        if not all_jobs:
            q = q.filter(SyncJob.status.in_(_ACTIVE_JOB))
        rows = q.limit(100).all()
        if not rows:
            print("(no active jobs)" if not all_jobs else "(no jobs)")
            return
        tenants = {t.id: t.name for t in db.query(Tenant).all()}
        colls = {c.id: c for c in db.query(Collection).all()}
        print(f"{'JOB ID':38} {'STATUS':11} {'PROGRESS':>14}  {'TENANT':22} SOURCE")
        for j in rows:
            c = colls.get(j.collection_id)
            src = (c.source_type if c else "?")
            prog = f"{j.processed or 0}/{j.total or 0}"
            print(f"{j.id:38} {j.status:11} {prog:>14}  "
                  f"{(tenants.get(j.tenant_id) or '-')[:22]:22} {src}")


def kill_job(job_id: str) -> None:
    with SessionLocal() as db:
        j = db.get(SyncJob, job_id)
        if not j:
            print(f"No job {job_id}")
            return
        if j.status in ("done", "failed", "cancelled"):
            print(f"Job already {j.status}")
            return
        # The running worker polls its DB status and stops at the next checkpoint.
        j.status = "cancelling"
        j.message = "Cancelling (CLI)…"
        db.commit()
        print(f"Requested cancel for job {job_id}. It stops at its next checkpoint.")


# -- Scheduled backup cadence (why a source does/doesn't auto-run) -----------


def _fmt_delta(minutes: float) -> str:
    m = int(round(minutes))
    if m <= 0:
        return "now"
    if m < 60:
        return f"{m}m"
    if m < 1440:
        return f"{m // 60}h{m % 60:02d}m" if m % 60 else f"{m // 60}h"
    return f"{m // 1440}d{(m % 1440) // 60:02d}h" if (m % 1440) // 60 else f"{m // 1440}d"


def list_schedule(all_rows: bool = False) -> None:
    """Print the effective backup schedule the scheduler would act on, mirroring
    its own decision logic, so you can see each mapping's cadence, last run,
    next-due and — for anything that never runs — WHY it's skipped."""
    from .config import get_settings
    from .connectors import get_connector
    from .workers.scheduler import _effective_interval, _now

    settings = get_settings()
    is_cp = (settings.node_role or "control-plane") == "control-plane"
    skip_assigned = is_cp  # the control plane never runs tenants assigned to a node
    default_minutes = max(1, settings.sync_interval_minutes)
    now = _now()

    if not settings.sync_enabled:
        print("!! scheduler DISABLED (CV_SYNC_ENABLED is false) — nothing auto-runs\n")

    with SessionLocal() as db:
        tenants = {t.id: t for t in db.query(Tenant).all()}
        assigned = {tid for tid, t in tenants.items() if t.node_id}
        rows = db.query(Collection).order_by(Collection.source_type).all()
        if not rows:
            print("(no mappings)")
            return
        print(f"{'SOURCE':16} {'TENANT':20} {'CADENCE':>8} {'LAST RUN':>10} "
              f"{'NEXT':>8}  STATUS")
        shown = 0
        for c in rows:
            tenant = tenants.get(c.tenant_id)
            tname = (tenant.name if tenant else "-")[:20]
            conn = get_connector(c.source_type)
            caps = conn.capabilities() if conn else None

            reason = None          # why it won't auto-run this cycle (or ever)
            cadence = "-"
            nxt = "-"
            if conn is None:
                reason = "unknown source"
            elif skip_assigned and c.tenant_id in assigned:
                reason = f"on node ({tenant.node_id[:8] if tenant else '?'})"
            elif caps and caps.requires_agent:
                reason = "agent push (endpoint cadence)"
            elif caps and caps.picker:
                reason = "picker (reminder only)"
            elif not c.connector_account_id:
                reason = "no linked account"
            else:
                acct = db.get(ConnectorAccount, c.connector_account_id)
                if acct is not None and acct.active is False:
                    reason = "account inactive"
                else:
                    interval = _effective_interval(c, default_minutes, caps)
                    if interval <= 0:
                        cadence = "manual"
                        reason = "manual only (interval=0)"
                    else:
                        cadence = _fmt_delta(interval)
                        if c.last_backup_run_at is None:
                            nxt = "now"
                        else:
                            last = c.last_backup_run_at
                            if last.tzinfo is not None:
                                last = last.replace(tzinfo=None)
                            due_in = interval - (now - last).total_seconds() / 60
                            nxt = "now" if due_in <= 0 else _fmt_delta(due_in)

            last_txt = "never"
            if c.last_backup_run_at is not None:
                last = c.last_backup_run_at
                if last.tzinfo is not None:
                    last = last.replace(tzinfo=None)
                last_txt = _fmt_delta((now - last).total_seconds() / 60) + " ago"

            runs = reason is None
            status = "auto" if runs else reason
            if runs or all_rows:
                print(f"{c.source_type[:16]:16} {tname:20} {cadence:>8} "
                      f"{last_txt:>10} {nxt:>8}  {status}")
                shown += 1
        if not all_rows:
            hidden = len(rows) - shown
            if hidden:
                print(f"\n({hidden} mapping(s) not auto-scheduled — pass --all to "
                      f"see them and the reason)")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="app.manage")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-admin", help="create or promote a platform admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", default="")
    r = sub.add_parser("remove-admin", help="revoke platform admin")
    r.add_argument("--email", required=True)
    sub.add_parser("list-admins", help="list platform admins")
    lj = sub.add_parser("list-jobs", help="list worker (backup/sync) jobs")
    lj.add_argument("--all", action="store_true", help="include finished jobs")
    kj = sub.add_parser("kill-job", help="cancel a running worker job")
    kj.add_argument("job_id")
    ls = sub.add_parser("list-schedule",
                        help="show the backup cadence + why sources do/don't auto-run")
    ls.add_argument("--all", action="store_true",
                    help="include mappings that never auto-run (with the reason)")
    args = p.parse_args(argv)
    if args.cmd == "add-admin":
        add_admin(args.email, args.name)
    elif args.cmd == "remove-admin":
        remove_admin(args.email)
    elif args.cmd == "list-admins":
        list_admins()
    elif args.cmd == "list-jobs":
        list_jobs(all_jobs=args.all)
    elif args.cmd == "kill-job":
        kill_job(args.job_id)
    elif args.cmd == "list-schedule":
        list_schedule(all_rows=args.all)


if __name__ == "__main__":
    main()
