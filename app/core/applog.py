"""The app's own log file: `data/artdrop.log`, rotating, INFO and above.

Nothing written here is secret: callers pass paths, ids, status words and JSON
key names only — never query strings, bodies, tokens or passwords. The text the
UI shows is already scrubbed by `base.scrub`.
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_NAME = "artdrop.log"
MAX_BYTES = 1_000_000
BACKUPS = 3
_HANDLER_MARK = "_artdrop_file_handler"

# Last line of defence: a labelled secret never reaches the file, even inside a
# traceback from a third-party library (an httpx URL, a header, a query string).
_SECRET_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|api_key|app_password|bot_token|"
    r"password|secret|token|code)=([^&\s\"']+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
_REPLACEMENT = r"\1=***"


def redact(text: str) -> str:
    """Hide labelled secrets before they are written anywhere."""
    text = _SECRET_RE.sub(_REPLACEMENT, str(text))
    return _BEARER_RE.sub("Bearer ***", text)


class ScrubFormatter(logging.Formatter):
    """The app's file format, with secrets redacted — tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(data_dir: Path, level: int = logging.INFO) -> Path:
    """Attach the rotating file handler once, and return the log path."""
    path = Path(data_dir) / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("artdrop")
    logger.setLevel(level)
    wanted = str(path.resolve())
    for handler in list(logger.handlers):
        if not getattr(handler, _HANDLER_MARK, False):
            continue
        if str(getattr(handler, "baseFilename", "")) == wanted:
            return path  # already logging here
        logger.removeHandler(handler)  # the data folder moved: follow it
        handler.close()
    handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                  encoding="utf-8")
    setattr(handler, _HANDLER_MARK, True)
    handler.setLevel(level)
    handler.setFormatter(ScrubFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.info("ArtDrop log started (%s, level %s)", path, logging.getLevelName(level))
    return path
