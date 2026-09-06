// Minimal, dependency-free Markdown → HTML for the support docs. Content is
// authored by trusted admins in the CMS, but we HTML-escape first regardless, so
// the output is safe to inject. Supports the subset the docs use: headings,
// bold/italic, inline code, links, unordered/ordered lists, blockquotes, and
// paragraphs. Links to /support/* stay in-app (handled by the renderer's click
// interception in Support.tsx).

function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Plan-gating labels — mirror the license tiers in cloud/app/api/billing.py.
const PLAN_LABELS: Record<string, string> = {
  personal: "Personal", consumer: "Consumer", family: "Family",
  business: "Business", enterprise: "Enterprise",
};
export function planLabel(tier: string): string {
  const k = (tier || "").toLowerCase();
  return PLAN_LABELS[k] || (k ? k.charAt(0).toUpperCase() + k.slice(1) : "Upgrade");
}
function planBadge(tier: string): string {
  const k = (tier || "").toLowerCase().replace(/[^a-z0-9_-]/g, "");
  return `<span class="plan-gate-badge" data-plan="${k}">${planLabel(k)} plan</span>`;
}

function inline(s: string): string {
  let t = esc(s);
  // inline code first so its contents aren't further transformed
  t = t.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`);
  // plan badge [plan:business] (no parens, so it can't collide with links)
  t = t.replace(/\[plan:([a-z0-9_-]+)\]/gi, (_m, p) => planBadge(p));
  // links [text](url)
  t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, text, url) => {
    const safe = /^(https?:|\/)/i.test(url) ? url : "#";
    return `<a href="${safe}">${text}</a>`;
  });
  // bold then italic
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  return t;
}

export function renderMarkdown(md: string): string {
  const lines = (md || "").replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let i = 0;
  let para: string[] = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${inline(para.join(" "))}</p>`);
      para = [];
    }
  };

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      flushPara();
      i++;
      continue;
    }
    // Plan-gate callout: ":::plan <tier>" … ":::"
    const pg = /^:::\s*plan\s+([a-z0-9_-]+)\s*$/i.exec(trimmed);
    if (pg) {
      flushPara();
      const tier = pg[1];
      const inner: string[] = [];
      i++;
      while (i < lines.length && lines[i].trim() !== ":::") { inner.push(lines[i]); i++; }
      i++; // skip the closing :::
      out.push(`<div class="plan-gate" data-plan="${tier.toLowerCase()}">`
        + `<div class="plan-gate-head">${planBadge(tier)}`
        + `<span class="plan-gate-note">Available on ${planLabel(tier)} and above</span></div>`
        + `${renderMarkdown(inner.join("\n"))}</div>`);
      continue;
    }
    // Heading
    const h = /^(#{1,4})\s+(.*)$/.exec(trimmed);
    if (h) {
      flushPara();
      const level = h[1].length;
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }
    // Blockquote (consecutive > lines)
    if (/^>\s?/.test(trimmed)) {
      flushPara();
      const quote: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i].trim())) {
        quote.push(lines[i].trim().replace(/^>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      continue;
    }
    // Unordered list
    if (/^[-*]\s+/.test(trimmed)) {
      flushPara();
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(`<li>${inline(lines[i].trim().replace(/^[-*]\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    // Ordered list
    if (/^\d+\.\s+/.test(trimmed)) {
      flushPara();
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(`<li>${inline(lines[i].trim().replace(/^\d+\.\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<ol>${items.join("")}</ol>`);
      continue;
    }
    // Paragraph text (accumulate)
    para.push(trimmed);
    i++;
  }
  flushPara();
  return out.join("\n");
}

// A stored body is HTML (authored in the rich-text editor) if it contains any
// HTML tag; otherwise it's Markdown. renderDoc picks the right path.
export function isHtml(body: string): boolean {
  return /<[a-z][^>]*>/i.test(body || "");
}

export function renderDoc(body: string): string {
  return isHtml(body) ? (body || "") : renderMarkdown(body);
}
