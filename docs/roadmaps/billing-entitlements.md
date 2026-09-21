# Billing, Entitlements & Catalog — program roadmap

Turning Arkive's existing (mature) billing into a data-driven SaaS catalog +
entitlement platform. **Reuse and extend — do NOT build a parallel system.**

## Current state (assessment)
Already production-grade and reused:
- **Plans/pricing:** `PricingConfig` (single row, `id="default"`) — currency, per-TB
  rates (protection/cloud/s3/azure), `license_plans` (tiers), `appliance_tiers`,
  `data_value_per_type`. Editable via `GET/PUT /admin/pricing`. `Tenant.plan`
  (personal|consumer|family|business|enterprise), `Tenant.licensed_bytes`.
- **Billing engine:** `billing_engine.py` (`activate`/`start_trial`/`charge_profile`/
  `run_due_charges`, 30-day anniversary, dunning ×4) + `workers/billing.py` (hourly).
  Canonical calc `api/billing.py::_price_breakdown`.
- **Payments:** `payments.py` — Stripe (+ PayPal, test mode). `BillingProfile`,
  `BillingCharge`, `PaymentMethod`, `UserAddress`. Provider IDs stored on the profile.
- **Signup/checkout:** `api/signup.py` (verify → tenant/user/vault + trial).
- **Plan change:** `account_migration.py` (upgrade/downgrade, 30-day cooldown).
- **Feature flags:** `features.py` (booleans, per-user/per-tenant, `resolve`).
- **Federation:** `Tenant` + `PricingConfig` replicate CP→node (`_PULL_ORDER`).

Gaps (the work): no **entitlements/quantities** (only booleans), no **add-ons**, no
**versioned catalog/price-book** (single float row), no **subscription items**, no
**seat enforcement**, no **Enterprise/contract overrides**, no **usage-event meter**,
no **legacy grandfathering**, **float money** (should be minor-units), no **webhook
idempotency/reconciliation**, **no test harness**.

## Target architecture
`Catalog (Product/Plan/AddOn) → PriceBook (versioned) → Subscription + Items →
Entitlements → Feature flags / limit enforcement`, with `Usage meters → Billing
calc → Invoice → Payment provider` and `Contract/Override` for Enterprise.
Entitlements are the seam: every feature/seat check goes through one service so
plan names are never hard-coded at call-sites.

## Phased plan
1. **Repo assessment** — done (this doc).
2. **Entitlements backbone** — **DONE (Phase 1, this change).** `entitlements/`
   package: `registry` (definitions + per-plan grants), `engine` (deterministic
   `derive` from plan + PricingConfig included qty + `licensed_bytes` + flags +
   overrides; `has_entitlement`/`get_limit`/`get_usage`/`can_consume`/`require_seat`/
   `view`), `EntitlementOverride` table (documented, time-boxed, audited). Data-driven
   included quantities on plan tiers (`included_users`/`included_members`). Admin API
   (`GET /admin/tenants/{id}/entitlements`, `POST/DELETE …/override`), customer
   `GET /billing/entitlements`. Seat check wired into org invite, **gated by the new
   `entitlements_enforced` flag (OFF)** so nothing changes for existing customers.
3. **Versioned catalog + price-book** — **DONE (Phase 2, this change).** `catalog/`
   package: `Plan` (immutable `code`, family, status) + `PlanVersion` (immutable,
   effective-dated, **integer minor-unit** prices: base/protection-per-TB/cloud-per-TB/
   cloud-plus-per-TB/per-user/per-member, included users/members/TB, features,
   entitlements, compatible add-ons, appliance tiers in cents, provider mappings).
   Seeded from `PricingConfig` (dual-read); `service.plan_pricing` adapter resolves
   the effective version and **falls back to the legacy `PricingConfig`** so existing
   billing is untouched. `publish_version` closes the current version (kept as
   immutable history) and opens a new one — prior invoices stay reproducible. Admin
   catalog API (`GET /admin/catalog`, `POST …/plans`, `POST …/plans/{code}/versions`,
   `GET …/plans/{code}/pricing`) + **Plan Catalog** admin section. Catalog + entitlement/
   add-on tables replicate CP→node (`_PULL_ORDER`).
4. **Subscription items** — **DONE (Phase 4, this change).** `subscriptions.py`:
   `Subscription` (one active row/tenant: plan+version+status+recurring/one-time
   totals) + `SubscriptionItem` (one priced component per calc `Line`, pinning
   unit price + `price_version`). `sync_from_calc` materializes/refreshes the
   record from the Phase 5 calc (idempotent); `view`/`cancel`. APIs: customer
   `GET /billing/subscription`; admin `GET /admin/tenants/{id}/subscription` +
   `POST …/subscription/sync`. CP-authoritative → replicates CP→node
   (`_PULL_ORDER`). Adapter over `BillingProfile` (reserved `provider_*` cols);
   doesn't move money.
5. **Billing calc service** — **DONE (Phase 5, this change).** `billing_calc.py`:
   deterministic, minor-unit `calculate(db, tenant, overrides=)` → itemized `Line`s
   (base, protected data, protected users, family members, non-seat add-ons,
   appliance lease + one-time setup) with the **included-vs-billable** split
   (`billable = max(0, licensed − included)`), each line referencing its price
   version. Seat-granting add-ons fold into the plan-priced seat count (never
   double-charged). `preview(db, tenant, changes)` returns current vs proposed +
   delta. APIs: `GET /billing/estimate`, `POST /billing/estimate/preview`,
   `GET /admin/tenants/{id}/billing-estimate`. Reads catalog `plan_pricing`
   (Phase 2) so it's reproducible. *(Customer billing-page rendering = Phase 9.)*
6. **Add-on management** — **DONE (Phase 3, this change).** `AddOn` (immutable
   code, `pricing_model`, minor-unit `price_cents`, `eligible_plans`, `entitlements`
   map, `feature_flags`, provider mappings) + `TenantAddOn` (per-tenant assignment;
   pins price + version for reproducible invoicing). Seeded real defaults (M365
   per-user, Arkive Cloud Plus per-cloud-TB, Compliance flat, extra users/members).
   `engine.derive` folds active add-on grants into entitlements (quantity increments;
   booleans enable, respecting a legal-hold disable). Admin **Add-on Management**
   section (catalog CRUD) + per-tenant assign/cancel API; customer `GET /billing/
   addons` (eligible + active, priced from the catalog). Integer minor-units money.
7. **Usage metering** — **DONE (Phase 7, this change).** `metering.py`:
   `UsageMeter` (definition: unit + aggregation max/sum/last + entitlement) +
   `UsageRecord` (period-bucketed, **globally-unique `idempotency_key`** so a
   retried event / repeated snapshot never double-counts). `record` (idempotent
   event), `observe` (monotonic peak snapshot, idempotent per meter/period/source),
   `current` (period aggregate) + `current_or_live` (metered value else live count
   → identical to before while empty). Hourly billing worker calls `snapshot_all`;
   the calc reads `cloud_stored_tb` via `current_or_live` for metered add-ons.
   Views: customer `GET /billing/usage`; admin `GET /admin/tenants/{id}/usage` +
   `POST …/usage/snapshot`. Seeded meters: protected data TB, users, cloud stored TB.
8. **Admin experience** — Plan Management + Add-on Management **DONE** (Phases 2/3);
   **per-tenant subscription line-item panel DONE (Phase 8, this change)** —
   `TenantSubscriptionBreakdown` on the tenant Subscription tab (included/licensed/
   billable columns, recurring + one-time totals, per-line price version, "Re-sync"
   from the calc). *(Full subscription-item editor is a follow-up.)*
9. **Customer signup + Protection Setup** — **DONE (Phase 9, this change) for the
   in-app Protection Setup.** Server-authoritative "Your bill, itemized" card reads
   `GET /billing/estimate` (included-vs-billable line items, recurring + one-time),
   and **Save previews before confirm** via `POST /billing/estimate/preview`
   (new recurring total + delta) — prices come from the server, never the UI.
   *(Signup-flow parity is a follow-up.)*
10. **Org/user enforcement** — **DONE (this change).** Seat enforcement is wired on
    EVERY seat-creating path (org invite, admin add-user) via `require_seat` (402 over
    the licensed seats), gated by the now tenant-scoped `entitlements_enforced` flag —
    so the cohort rollout is simply flipping that flag per tenant on the Account
    Features tab. M365 auto-create respects the seat limit when enforcement is on:
    identities over the allowance are HELD as `new_user_candidate` for admin review/
    mapping and never silently provisioned into a billable member (discovery/binding
    was already non-billable).
11. **Payment-provider enhancements** — **DONE (this change; simplified — bill the
    single monthly total, no processor line items).** `billing_source` cutover
    (global + per-tenant) makes `billing_calc.recurring_cents` the charged amount;
    `refresh_active_amounts` keeps each active profile current before the sweep.
    **Signature-verified + idempotent Stripe webhooks** (`POST /billing/webhooks/
    stripe`, HMAC verify + `ProcessedWebhook` ledger) confirm charge outcomes;
    `reconcile_charges` (querying the PaymentIntent) is the safety net for missed
    deliveries. *(Multi-item subscriptions/proration intentionally skipped — one
    total is billed monthly.)*
12. **Legacy compatibility & migration** — **DONE (this change).** `billing_migration.py`:
    `preview` (dry-run legacy-vs-calc parity + recommendation, no writes), `apply`
    (mode `calc`=re-price / `grandfather`=price-neutral lock via a `billing_price_lock`
    SystemSetting that `_plan_amount_cents` honors first), `rollback` (restore source +
    amount from the compensating `BillingMigration` snapshot), `backfill_all`
    (materialize every tenant's subscription; idempotent, no charge). Admin API +
    a **Billing engine** panel on the tenant Subscription tab (migrate/grandfather/
    rollback + source toggle + history). The go-live cutover is now safe + reversible.
13. **Federated entitlement sync** — **DONE (this change).** `entitlement_snapshot.py`:
    the CP mints a **signed** (hybrid classical+PQ fleet signer), effective-dated
    snapshot per tenant `{plan, entitlements, flags, version, issued/expires}` →
    `EntitlementSnapshot` table replicated CP→node. `verify` (CP public bundle) +
    `get_valid` (signature + expiry, **offline-safe**: a cached non-expired snapshot
    is trusted even if the CP is unreachable; None → fall back to local derive). The
    CP billing worker refreshes them (role-guarded); admin refresh + `/debug/features`
    `signed_snapshot` status.
14. **Testing, docs, rollout** — add a pytest harness (currently NONE), unit/integration/
    e2e per the spec; cohort rollout flags.

## Key decisions / conventions
- **Included vs billable:** store total licensed qty + included qty; billable =
  `max(0, licensed - included)`; show both. Registry + `PricingConfig` tiers carry
  `included_users`/`included_members`.
- **Money:** new catalog/price-book uses integer minor-units + decimal; never float.
  (Existing `PricingConfig` floats stay until Phase 3 migrates behind an adapter.)
- **Enforcement is gated** (`entitlements_enforced`, OFF) — visibility first, no
  surprise for existing customers.
- **Overrides ≠ billing:** `EntitlementOverride` adjusts ACCESS only, labelled + audited.

## Risks / open decisions
- No test harness in-repo → Phase 14 must add one before broad refactors.
- Float→minor-unit money migration needs a compatibility window (Phase 3).
- Stripe multi-item subscription mapping + webhook idempotency is the highest-risk slice.
- Federation: entitlement snapshot signing/verification design (Phase 13).
- Protected-data billing basis (provisioned vs used vs peak) must be made explicit + configurable.
