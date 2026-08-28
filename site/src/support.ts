// Loads the published support/documentation content. Mirrors the marketing CMS
// pattern (see cms.ts): prefer the node's local mirror (/support.json, written by
// the Public Web Node heartbeat) and fall back to the control plane. Cached for
// the session so navigation between docs is instant.

import { site } from "./content";

export interface DocNavItem {
  slug: string;
  title: string;
  icon: string;
  summary: string;
  nav_order: number;
}
export interface DocSection {
  section: string;
  order: number;
  docs: DocNavItem[];
}
export interface Doc {
  slug: string;
  title: string;
  section: string;
  icon: string;
  summary: string;
  body: string;
  help_routes: string[];
  updated_at?: string | null;
}
export interface SupportContent {
  tree: DocSection[];
  docs: Record<string, Doc>;
  updated_at?: string | null;
}

const LOCAL_URL = "/support.json";
const REMOTE_URL = `${site.appUrl}/api/support/content`;

let cache: SupportContent | null = null;

async function fetchJson(url: string): Promise<any | null> {
  try {
    const res = await fetch(url, { credentials: "omit", cache: "no-cache" });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function loadSupport(): Promise<SupportContent> {
  if (cache) return cache;
  const body = (await fetchJson(LOCAL_URL)) ?? (await fetchJson(REMOTE_URL));
  cache = body && Array.isArray(body.tree) ? (body as SupportContent) : { tree: [], docs: {} };
  return cache;
}

// Resolve the doc that serves as contextual help for a portal route (the portal
// Help icon deep-links here with ?for=<route>). Returns a slug or null.
export function slugForRoute(content: SupportContent, route: string): string | null {
  const r = (route || "").trim();
  if (!r) return null;
  for (const d of Object.values(content.docs)) {
    if ((d.help_routes || []).includes(r)) return d.slug;
  }
  return null;
}
