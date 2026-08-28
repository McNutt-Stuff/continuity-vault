// Markdown → HTML for the portal (mirrors site/src/md.ts) plus HTML detection.
// Used by the docs editor to load legacy Markdown pages into the rich-text
// editor. Content is escaped before transformation, so output is safe to inject.

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function inline(s: string): string {
  let t = esc(s);
  t = t.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`);
  t = t.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, text, url) => {
    const safe = /^(https?:|\/)/i.test(url) ? url : "#";
    return `<a href="${safe}">${text}</a>`;
  });
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  return t;
}

export function renderMarkdown(md: string): string {
  const lines = (md || "").replace(/\r\n/g, "\n").split("\n");
  const out: string[] = [];
  let i = 0;
  let para: string[] = [];
  const flush = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  while (i < lines.length) {
    const line = lines[i];
    const t = line.trim();
    if (!t) { flush(); i++; continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(t);
    if (h) { flush(); const l = h[1].length; out.push(`<h${l}>${inline(h[2])}</h${l}>`); i++; continue; }
    if (/^>\s?/.test(t)) {
      flush(); const q: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i].trim())) { q.push(lines[i].trim().replace(/^>\s?/, "")); i++; }
      out.push(`<blockquote>${inline(q.join(" "))}</blockquote>`); continue;
    }
    if (/^[-*]\s+/.test(t)) {
      flush(); const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) { items.push(`<li>${inline(lines[i].trim().replace(/^[-*]\s+/, ""))}</li>`); i++; }
      out.push(`<ul>${items.join("")}</ul>`); continue;
    }
    if (/^\d+\.\s+/.test(t)) {
      flush(); const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) { items.push(`<li>${inline(lines[i].trim().replace(/^\d+\.\s+/, ""))}</li>`); i++; }
      out.push(`<ol>${items.join("")}</ol>`); continue;
    }
    para.push(t); i++;
  }
  flush();
  return out.join("\n");
}

// A stored body is HTML (from the rich editor) if it contains any HTML tag;
// otherwise it's legacy Markdown.
export function isHtml(body: string): boolean {
  return /<[a-z][^>]*>/i.test(body || "");
}

// Normalize any stored body to HTML for the rich-text editor.
export function toEditorHtml(body: string): string {
  return isHtml(body) ? body : renderMarkdown(body);
}
