"""File logging: nothing is written unless --log or --debug is given, so the TUI screen stays clean."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import platformdirs

LOGGER = "earthwave"


def default_log_path() -> Path:
    return Path(platformdirs.user_log_dir("earthwave", appauthor=False)) / "earthwave.log"


def setup_logging(path: Path | None = None, debug: bool = False) -> Path | None:
    """Attach a rotating file handler (WARNING, or DEBUG with `debug`); returns the log path, or None when disabled."""
    logger = logging.getLogger(LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.DEBUG if debug else logging.WARNING)  # also when disabled: a stale DEBUG must not linger
    if path is None and not debug:
        logger.addHandler(logging.NullHandler())
        return None
    target = Path(path) if path else default_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(target, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return target
