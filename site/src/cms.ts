// Runtime CMS bridge. The public site ships with bundled default content
// (content.ts) and, at load, applies the editable content published from the
// Control Plane admin CMS. Resolution order:
//   1. /site.json      — a local, same-origin mirror written by this node's
//                        heartbeat (fast, works even if the control plane is down)
//   2. control plane   — GET {appUrl}/api/site (cross-origin, if the mirror is absent)
//   3. bundled defaults (content.ts) — offline / unpublished
// So the Public Web Node needs no database of its own: content originates in the
// control plane (which has the DB) and is mirrored to a static JSON file here.

import { site, home, pricing, about } from "./content";
import { applyPricing } from "./pricing";
import { injectAnalytics } from "./analytics";

const LOCAL_URL = "/site.json";
const REMOTE_URL = `${site.appUrl}/api/site`;

function applyCms(data: any) {
  if (!data || typeof data !== "object") return;
  if (typeof data.brand === "string") site.brand = data.brand;
  if (typeof data.tagline === "string") site.tagline = data.tagline;

  const h = data.hero || {};
  if (h.eyebrow) home.eyebrow = h.eyebrow;
  if (h.h1) home.h1 = h.h1;
  if (h.lead) home.lead = h.lead;
  if (h.ctaPrimary) home.ctaPrimary.label = h.ctaPrimary;
  if (h.ctaSecondary) home.ctaSecondary.label = h.ctaSecondary;
  if (Array.isArray(h.badges) && h.badges.length) home.badges = h.badges;
  if (Array.isArray(data.stats) && data.stats.length) home.stats = data.stats;

  const p = data.pricing || {};
  if (p.note) pricing.note = p.note;
  if (Array.isArray(p.plans) && p.plans.length) {
    pricing.plans = p.plans.map((plan: any, i: number) => ({
      ...(pricing.plans[i] || {}),
      ...plan,
      features: Array.isArray(plan.features) && plan.features.length
        ? plan.features
        : pricing.plans[i]?.features || [],
    }));
  }

  const a = data.about || {};
  if (a.h1) about.h1 = a.h1;
  if (a.lead) about.lead = a.lead;
}

async function fetchJson(url: string): Promise<any | null> {
  try {
    const res = await fetch(url, { credentials: "omit", cache: "no-cache" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

// Applies content inlined into index.html by the node's heartbeat
// (window.__ARKIVE_CMS__). Synchronous, so calling it before the first render
// paints real content with zero network query. Returns true if it applied.
export function applyInlineCms(): boolean {
  const g = (globalThis as any).__ARKIVE_CMS__;
  if (!g || typeof g !== "object") return false;
  injectAnalytics(g.analytics_id);
  applyPricing(g.pricing);
  if (g.published === false) return false;
  applyCms(g.content);
  return true;
}

// Loads published content and applies it over the bundled defaults. Resolves
// regardless of outcome so the caller renders defaults immediately and updates
// on success.
export async function loadCms(): Promise<void> {
  const body = (await fetchJson(LOCAL_URL)) ?? (await fetchJson(REMOTE_URL));
  if (!body) return; // offline / no source → keep bundled defaults
  injectAnalytics(body.analytics_id);
  applyPricing(body.pricing);
  if (body.published === false) return; // unpublished → keep bundled defaults
  applyCms(body.content);
}
