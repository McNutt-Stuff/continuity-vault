"""Rotating file logging for the appliance agent, with a tail helper so recent
logs can be forwarded to the cloud in the heartbeat (like the endpoint agent)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_file: Path) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("arkive.appliance")
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
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    _CONFIGURED = True
    return logger


def set_verbose(verbose: bool) -> None:
    logging.getLogger("arkive.appliance").setLevel(
        logging.DEBUG if verbose else logging.INFO)


_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO,
           "warning": logging.WARNING, "error": logging.ERROR}


def set_level(name: str) -> None:
    """Set the log level by name (cloud config CV_LOG_LEVEL / assigned profile)."""
    logging.getLogger("arkive.appliance").setLevel(
        _LEVELS.get((name or "").lower(), logging.INFO))


def tail(log_file: Path, lines: int = 50) -> list[str]:
    try:
        return log_file.read_text(errors="replace").splitlines()[-lines:]
    except Exception:
        return []
