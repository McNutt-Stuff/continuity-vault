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


if __name__ == "__main__":
    main()
