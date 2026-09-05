// Loads Google Analytics (gtag.js) with the measurement ID configured on the
// node (via the config profile key CV_GA_MEASUREMENT_ID, delivered through the
// public /api/site response). No-op when unset or already loaded.

let injected = false;

export function injectAnalytics(id?: string | null): void {
  if (!id || injected || typeof document === "undefined") return;
  if (!/^G-[A-Z0-9]+$/i.test(id)) return; // GA4 measurement id shape
  injected = true;
  const s = document.createElement("script");
  s.async = true;
  s.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(id)}`;
  document.head.appendChild(s);
  const w = window as unknown as { dataLayer: unknown[]; gtag: (...a: unknown[]) => void };
  w.dataLayer = w.dataLayer || [];
  w.gtag = function gtag() { w.dataLayer.push(arguments); };
  w.gtag("js", new Date());
  w.gtag("config", id);
}
