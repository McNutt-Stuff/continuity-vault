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
3. **Versioned catalog + price-book** — `Product`/`Plan`/`PlanVersion`/`Price` (+
   effective dates, immutable codes, minor-unit money, provider mappings). Migrate
   `PricingConfig` behind a compatibility adapter (dual-read).
4. **Subscription items** — `Subscription` + `SubscriptionItem` (base/seats/capacity/
   add-ons/appliance/usage), each price-version-referenced; adapter over `BillingProfile`.
5. **Billing calc service** — deterministic, minor-units, included-vs-billable split,
   preview API; reproducible line items referencing exact price versions.
6. **Add-on management** — `AddOn`/`AddOnVersion`, eligibility, pricing models
   (flat/per-user/per-TB/tiered/metered), entitlement + flag mappings.
7. **Usage metering** — `UsageMeter`/`UsageRecord` with idempotency keys; protected
   TB / cloud / seats; feeds the calc.
8. **Admin experience** — Plan Management, Add-on Management, per-org subscription +
   entitlement management (extend `Admin.tsx`).
9. **Customer signup + Protection Setup** — seat/capacity/cloud/appliance/add-on
   pricing from server calc; preview before confirm. No prices in the frontend.
10. **Org/user enforcement** — turn on seat limits per cohort; M365 auto-discovery →
    review/mapping, never auto-billable seats.
11. **Payment-provider enhancements** — multi-item subscriptions, proration, metered
    usage report; **idempotent, signature-verified webhooks** + reconciliation job.
12. **Legacy compatibility & migration** — grandfather existing subscriptions; dry-run
    "Upgrade Legacy Customer" with outbox/compensating actions.
13. **Federated entitlement sync** — signed entitlement snapshot pushed to nodes
    (org/tenant/values/version/issued/expires), validated before enforcement, safe
    cached behavior offline.
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
