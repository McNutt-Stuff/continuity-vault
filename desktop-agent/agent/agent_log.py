"""Structured, rotating file logging for the desktop agent.

Writes directly to a rotating file (flushed per record) so logs are visible
immediately, independent of launchd's block-buffered stdout. A tail helper feeds
recent lines into the heartbeat telemetry shipped to the cloud.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_file: Path) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("arkive")
    if _CONFIGURED:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S%z")
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass  # fall back to stream-only if the file can't be opened
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    _CONFIGURED = True
    return logger


def set_verbose(verbose: bool) -> None:
    """Toggle DEBUG logging (driven by the cloud advanced setting)."""
    logging.getLogger("arkive").setLevel(logging.DEBUG if verbose else logging.INFO)


_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR}


def set_level(name: str) -> None:
    """Set the log level by name (cloud config CV_LOG_LEVEL / agent log_level)."""
    logging.getLogger("arkive").setLevel(_LEVELS.get((name or "").lower(), logging.INFO))


def tail(log_file: Path, lines: int = 50) -> list[str]:
    try:
        return log_file.read_text(errors="replace").splitlines()[-lines:]
    except FileNotFoundError:
        return []
    except Exception:
        return []
