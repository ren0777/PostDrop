"""The app log file: created once, rotating, and never fed a secret."""
import logging

import pytest

from app.core import applog


@pytest.fixture(autouse=True)
def clean_handlers():
    """Leave no file handler behind for the next test."""
    yield
    logger = logging.getLogger("artdrop")
    for handler in list(logger.handlers):
        if getattr(handler, applog._HANDLER_MARK, False):
            logger.removeHandler(handler)
            handler.close()


def test_the_log_file_is_written(tmp_path):
    path = applog.setup_logging(tmp_path)

    assert path == tmp_path / "artdrop.log"
    logging.getLogger("artdrop.pixiv").info("hello from a test")

    text = path.read_text(encoding="utf-8")
    assert "hello from a test" in text
    assert "artdrop.pixiv" in text  # the logger name is in the line


def test_setup_logging_does_not_stack_handlers(tmp_path):
    applog.setup_logging(tmp_path)
    applog.setup_logging(tmp_path)

    handlers = [handler for handler in logging.getLogger("artdrop").handlers
                if getattr(handler, applog._HANDLER_MARK, False)]
    assert len(handlers) == 1


def test_the_handler_rotates(tmp_path):
    path = applog.setup_logging(tmp_path)
    handler = next(handler for handler in logging.getLogger("artdrop").handlers
                   if getattr(handler, applog._HANDLER_MARK, False))

    assert handler.maxBytes == applog.MAX_BYTES
    assert handler.backupCount == applog.BACKUPS
    assert path.name == "artdrop.log"


def test_labelled_secrets_never_reach_the_file(tmp_path):
    path = applog.setup_logging(tmp_path)
    logger = logging.getLogger("artdrop.instagram")

    logger.error("failed for url https://graph.instagram.com/me?fields=id&access_token=IGQV123")
    logger.error("header Bearer abc.def.ghi was rejected")
    try:
        raise RuntimeError("client_secret=dsecret987 is wrong")
    except RuntimeError:
        logger.exception("oauth exchange failed")

    text = path.read_text(encoding="utf-8")
    for secret in ("IGQV123", "abc.def.ghi", "dsecret987"):
        assert secret not in text
    assert "access_token=***" in text and "Bearer ***" in text
    assert "client_secret=***" in text  # …even inside a traceback
