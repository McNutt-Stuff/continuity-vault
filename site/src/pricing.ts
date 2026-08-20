// Platform pricing for the public site, fetched from the control plane via
// /api/site (see cms.ts) so the marketing plans, per-TB rates, Arkive Cloud
// price and hardware all reflect what's defined in the admin. Ships with
// defaults that match the backend so the site renders before any fetch.

export interface LicensePlan { id: string; name: string; price_per_tb_month: number; min_tb: number; }
export interface ApplianceTier { capacity_tb: number; monthly: number; setup: number; model: string; }
export interface PlatformPricing {
  currency: string;
  license_plans: LicensePlan[];
  cloud_price_per_tb_month: number;
  s3_price_per_tb_month: number;
  azure_price_per_tb_month: number;
  appliance_tiers: ApplianceTier[];
}

export const platformPricing: PlatformPricing = {
  currency: "USD",
  license_plans: [
    { id: "consumer", name: "Consumer", price_per_tb_month: 8, min_tb: 1 },
    { id: "family", name: "Family", price_per_tb_month: 6, min_tb: 2 },
    { id: "business", name: "Business", price_per_tb_month: 5, min_tb: 5 },
    { id: "enterprise", name: "Enterprise", price_per_tb_month: 4, min_tb: 25 },
  ],
  cloud_price_per_tb_month: 10,
  s3_price_per_tb_month: 23,
  azure_price_per_tb_month: 18,
  appliance_tiers: [
    { capacity_tb: 1, monthly: 25, setup: 99, model: "CV Edge 1" },
    { capacity_tb: 5, monthly: 59, setup: 149, model: "CV Edge 5" },
    { capacity_tb: 10, monthly: 99, setup: 199, model: "CV Edge 10" },
    { capacity_tb: 25, monthly: 199, setup: 299, model: "CV Edge 25" },
    { capacity_tb: 100, monthly: 499, setup: 499, model: "CV Edge 100" },
  ],
};

export function applyPricing(p: any): void {
  if (!p || typeof p !== "object") return;
  if (typeof p.currency === "string") platformPricing.currency = p.currency;
  if (Array.isArray(p.license_plans) && p.license_plans.length) platformPricing.license_plans = p.license_plans;
  if (typeof p.cloud_price_per_tb_month === "number") platformPricing.cloud_price_per_tb_month = p.cloud_price_per_tb_month;
  if (typeof p.s3_price_per_tb_month === "number") platformPricing.s3_price_per_tb_month = p.s3_price_per_tb_month;
  if (typeof p.azure_price_per_tb_month === "number") platformPricing.azure_price_per_tb_month = p.azure_price_per_tb_month;
  if (Array.isArray(p.appliance_tiers) && p.appliance_tiers.length) platformPricing.appliance_tiers = p.appliance_tiers;
}

export function planById(id?: string): LicensePlan | undefined {
  return platformPricing.license_plans.find((x) => x.id === id);
}

// "Starting at" monthly price for a plan = its per-TB rate × its minimum TB.
export function startingMonthly(id?: string): number | null {
  const pl = planById(id);
  return pl ? Math.round(pl.price_per_tb_month * pl.min_tb) : null;
}

export function money(n: number): string {
  return "$" + Math.round(n).toLocaleString();
}
