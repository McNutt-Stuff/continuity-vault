---
applyTo: "cloud/app/api/**/*.py"
description: "Adding or changing FastAPI routers on the control plane / nodes."
---

# Cloud API routers

- **Imports are DOUBLE-dot relative:** `from ..models import X`, `from .. import audit`. A single-dot
  import compiles but fails at runtime import — always use `..`.
- Register every new router in `cloud/app/main.py` `include_router(...)` (prefix `API`). Public
  (unauthenticated) endpoints go on a `public_router`; agent/appliance endpoints use bearer-token auth.
- **Auth dependencies:** `security.get_principal` (session), `security.get_tenant`, `security.require_passkey`
  (step-up for content/recovery/destructive), `security.require_org_admin`, `security.require_platform_admin`,
  `security.require_feature("<flag>")`. Any dependency fn param typed `Session` MUST default to `= Depends(get_db)`.
- **Ownership scoping is mandatory.** Filter by `tenant_id` (and `owner_user_id` for personal/shared tenants)
  on every read/write. Never trust a client-supplied id without an ownership check → 404 if it isn't theirs.
- **Federation:** file operations (search, retrieve, recovered, restore, fs, purge) must run on the node that
  owns the tenant. `api/node_proxy.py` transparently forwards matching paths CP→node; billing and control
  actions stay on the CP. If you add a path that touches a tenant's data/index, confirm it's proxied or runs
  on the node.
- **Audit destructive/security actions:** `audit.record(db, actor=..., action="noun.verb", tenant_id=...,
  category="security", severity="warning", detail={...})`. Vocabulary: severity info|notice|warning|critical.
- **ALWAYS log failures so they reach Platform Logs — never a silent "something went wrong".** Every
  unhandled exception is already caught by the global handler in `main.py` (`@app.exception_handler(Exception)`)
  which logs `cv.api` ERROR + traceback to the unified store and returns `{detail, ref}` to the client. But
  do NOT rely on it as the only net:
  - When you `try/except` in a route/worker, LOG the failure via a `cv.*` logger (`logging.getLogger("cv.<area>")`)
    at `warning`/`error` (with `logger.exception(...)` to capture the traceback) — a bare `except: pass` or
    `except: return []` hides the problem and is a bug.
  - RAISE `HTTPException(status, "<actionable message>")` for expected 4xx (validation, missing consent, plan
    gate) so the client shows a real reason. For provider/integration failures, include the HTTP status +
    provider error code/reason + a bounded snippet (see connectors.instructions.md "Logging & error detail").
  - For tenant-attributed failures also `audit.record(...)` (dual-writes a LogEntry) so it shows per-tenant.
  - Frontend `notify({...})` MUST pass a non-empty `message` (fallback string) — an empty message renders the
    bare "Something went wrong" dialog. Prefer `(e as {message?:string}).message || "Couldn't <do X>"`.
- Use naive-UTC time helpers (`datetime.now(timezone.utc).replace(tzinfo=None)`) for any DateTime compare.
- After editing: `python3 -c "import ast; ast.parse(open('<file>').read())"` and `get_errors`.
