"""Server-side Google Analytics (gtag.js) injection.

The portal (web/dist) and public site (site/dist) are static SPAs served by Caddy;
the measurement ID comes from the node's config profile (CV_GA_MEASUREMENT_ID), so
we inject the gtag snippet directly into each index.html's <head> — right after the
opening tag, on every page — rather than relying only on runtime JS. Idempotent:
the snippet lives between markers so it can be replaced or removed cleanly when the
ID changes or is cleared.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger("cv.analytics")

_GA_RE = re.compile(r"^(?:G|GT|AW|UA|DC)-[A-Za-z0-9-]+$", re.IGNORECASE)
_START = "<!--ARKIVE_GA_START-->"
_END = "<!--ARKIVE_GA_END-->"
_HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)


def snippet(ga_id: str) -> str:
    """The gtag.js loader for a GA4 measurement ID, or "" when unset/invalid."""
    ga_id = (ga_id or "").strip()
    if not _GA_RE.match(ga_id):
        return ""
    return (
        "<!-- Google tag (gtag.js) -->"
        f'<script async src="https://www.googletagmanager.com/gtag/js?id={ga_id}"></script>'
        "<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}"
        f"gtag('js',new Date());gtag('config','{ga_id}');</script>"
    )


def inject_index(path: Path, ga_id: str) -> bool:
    """Insert (or update/remove) the GA block right after <head> in an index.html.
    Returns True when the file changed."""
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return False
    block = _START + snippet(ga_id) + _END
    if _START in html and _END in html:
        new = re.sub(re.escape(_START) + ".*?" + re.escape(_END),
                     lambda _m: block, html, count=1, flags=re.S)
    else:
        m = _HEAD_RE.search(html)
        if not m:
            return False
        new = html[:m.end()] + block + html[m.end():]
    if new == html:
        return False
    try:
        tmp = path.with_name(path.name + ".ga.tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(path)  # atomic swap
        return True
    except OSError as exc:
        logger.warning("could not write %s: %s", path, exc)
        return False


def _webroot_indexes() -> list[Path]:
    root = Path(__file__).resolve().parents[2]  # INSTALL_DIR
    return [root / "web" / "dist" / "index.html",
            root / "site" / "dist" / "index.html"]


def apply(db) -> None:
    """Re-inject the configured GA snippet into every served index.html this node
    hosts. Called at startup and whenever the node config changes."""
    ga = ""
    try:
        from . import node_config
        raw = node_config.get(db, "CV_GA_MEASUREMENT_ID", "")
        ga = (raw or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not resolve CV_GA_MEASUREMENT_ID: %s", exc)
        raw = None
    # Explain WHY nothing will be injected, so an assigned-but-not-working profile
    # is diagnosable from the journal (the key resolves per-node — the profile must
    # be assigned+enabled on THIS node).
    if not ga:
        try:
            from . import node_config
            present = "CV_GA_MEASUREMENT_ID" in node_config.effective(db)
        except Exception:  # noqa: BLE001
            present = False
        logger.info("Google Analytics not applied: measurement ID is %s on this node "
                    "(assign an enabled config profile carrying CV_GA_MEASUREMENT_ID to "
                    "the node that serves this site)",
                    "empty" if present else "not set")
    elif not snippet(ga):
        logger.warning("Google Analytics measurement ID %r is not a recognized tag "
                       "format (expected e.g. G-XXXXXXXXXX) — not injected", ga)
    for p in _webroot_indexes():
        if not p.exists():
            continue
        if inject_index(p, ga):
            logger.info("applied Google Analytics (%s) to %s",
                        ga or "cleared", p)
