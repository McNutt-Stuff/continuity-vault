"""Local HTML status page for the desktop agent, opened from the menu bar.

Renders a self-contained, styled page (no network/assets) showing cloud
connectivity, version, collectors, quantum-safe status, host details, and recent
logs — a production-quality status window without needing a native UI toolkit.
"""

from __future__ import annotations

import html
import time


def _ago(epoch: float) -> str:
    if not epoch:
        return "never"
    d = time.time() - epoch
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{html.escape(str(text))}</span>'


def _row(label: str, value: str) -> str:
    return (f'<div class="row"><div class="k">{html.escape(label)}</div>'
            f'<div class="v">{value}</div></div>')


def render(snap: dict) -> str:
    t = snap.get("telemetry", {}) or {}
    crypto = t.get("crypto", {}) or {}
    hb = snap.get("last_heartbeat_epoch", 0) or 0
    online = bool(hb) and (time.time() - hb) < 120
    conn = _pill("Connected", "ok") if online else _pill("Reconnecting…", "warn")

    op_auth = t.get("op_auth", "unknown")
    op_map = {
        "service-account": ("Service account", "ok"),
        "interactive": ("App integration", "ok"),
        "unauthenticated": ("Interactive only", "muted"),
        "absent": ("CLI not installed", "danger"),
    }
    op_text, op_tone = op_map.get(op_auth, (str(op_auth), "muted"))
    op_installed = t.get("op_available") is not False

    pq = crypto.get("pq_available")
    pq_pill = _pill("ML-KEM / ML-DSA", "ok") if pq else _pill("Classical fallback", "warn")
    escrow = crypto.get("recovery_escrow", "pending")
    escrow_pill = _pill(f"Escrowed · {crypto.get('recovery_kem_alg') or 'KEM'}", "ok") \
        if escrow == "escrowed" else _pill("Pending", "warn")

    logs = t.get("recent_logs", []) or []
    log_text = html.escape("\n".join(logs[-120:])) or "No logs yet."

    name = html.escape(str(snap.get("name") or "Arkive Agent"))
    version = html.escape(str(snap.get("version") or "—"))
    cloud = html.escape(str(snap.get("cloud_url") or "—"))
    reg = "Registered" if snap.get("registered") else "Not linked"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Arkive Agent — Status</title>
<style>
  :root {{
    --bg:#0b0f17; --panel:#141b2b; --elev:#1a2234; --border:#24304a; --soft:#1c2740;
    --text:#e6ebf5; --dim:#9aa7bf; --faint:#64728f; --brand:#4f7cff; --brand2:#7aa2ff;
    --ok:#35d0a5; --warn:#f5a623; --danger:#f2545b;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 -apple-system,
    BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:28px; }}
  .wrap {{ max-width:760px; margin:0 auto; }}
  .head {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:20px; }}
  .brand {{ display:flex; align-items:center; gap:12px; }}
  .logo {{ width:42px; height:42px; border-radius:11px; background:linear-gradient(135deg,#7a5cff,#4f7cff);
    display:flex; align-items:center; justify-content:center; font-weight:800; font-size:20px; color:#fff; }}
  h1 {{ font-size:18px; margin:0; }}
  .sub {{ color:var(--faint); font-size:12px; font-family:ui-monospace,SFMono-Regular,monospace; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:14px;
    padding:18px 20px; margin-bottom:16px; box-shadow:0 8px 30px rgba(0,0,0,.35); }}
  .card h2 {{ font-size:11.5px; text-transform:uppercase; letter-spacing:.6px; color:var(--dim);
    margin:0 0 14px; font-weight:700; }}
  .row {{ display:flex; justify-content:space-between; align-items:center; gap:16px;
    padding:9px 0; border-bottom:1px solid var(--soft); }}
  .row:last-child {{ border-bottom:none; }}
  .k {{ color:var(--dim); font-size:13px; }}
  .v {{ font-weight:600; text-align:right; font-size:13px; }}
  .pill {{ display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:999px;
    font-size:12px; font-weight:600; border:1px solid var(--border); }}
  .pill:before {{ content:""; width:7px; height:7px; border-radius:50%; background:currentColor; }}
  .pill.ok {{ color:var(--ok); border-color:rgba(53,208,165,.4); background:rgba(53,208,165,.08); }}
  .pill.warn {{ color:var(--warn); border-color:rgba(245,166,35,.4); background:rgba(245,166,35,.08); }}
  .pill.danger {{ color:var(--danger); border-color:rgba(242,84,91,.4); background:rgba(242,84,91,.08); }}
  .pill.muted {{ color:var(--faint); }}
  pre {{ background:rgba(0,0,0,.28); border:1px solid var(--soft); border-radius:10px; padding:14px;
    font:11px/1.5 ui-monospace,SFMono-Regular,monospace; white-space:pre-wrap; word-break:break-all;
    max-height:280px; overflow:auto; margin:0; color:var(--dim); }}
  .foot {{ color:var(--faint); font-size:11px; text-align:center; margin-top:8px; }}
</style></head>
<body><div class="wrap">
  <div class="head">
    <div class="brand">
      <div class="logo">A</div>
      <div><h1>{name}</h1><div class="sub">v{version} · {reg}</div></div>
    </div>
    {conn}
  </div>

  <div class="card">
    <h2>Cloud connectivity</h2>
    {_row("Status", conn)}
    {_row("Last heartbeat", html.escape(_ago(hb)))}
    {_row("Endpoint", f'<span class="sub">{cloud}</span>')}
    {_row("Last report", html.escape(_ago(hb)))}
  </div>

  <div class="card">
    <h2>Collectors</h2>
    {_row("1Password (op CLI)", _pill(op_text, op_tone) if op_installed else _pill("CLI not installed", "danger"))}
    {_row("Last collection", html.escape(_ago(snap.get("last_collect_epoch", 0) or 0)))}
  </div>

  <div class="card">
    <h2>Security</h2>
    {_row("Client-side encryption", _pill(crypto.get("content_alg", "AES-256-GCM"), "ok"))}
    {_row("Quantum-safe crypto", pq_pill)}
    {_row("Recovery escrow", escrow_pill)}
  </div>

  <div class="card">
    <h2>Host &amp; communication</h2>
    {_row("Hostname", html.escape(str(t.get("hostname", "—"))))}
    {_row("Local IP", html.escape(str(t.get("local_ip", "—"))))}
    {_row("Local user", html.escape(str(t.get("local_user", "—"))))}
    {_row("OS", html.escape(str(t.get("os", "—"))))}
    {_row("Agent ID", f'<span class="sub">{html.escape(str(snap.get("agent_id") or "—"))}</span>')}
  </div>

  <div class="card">
    <h2>Recent activity</h2>
    <pre>{log_text}</pre>
  </div>

  <div class="foot">Generated {html.escape(time.strftime("%Y-%m-%d %H:%M:%S"))} · Arkive desktop agent</div>
</div></body></html>"""
