"""Tests for structured logging configuration."""

from __future__ import annotations

import json
import logging

from spotifyforge.logging_config import (
    JSONFormatter,
    configure_logging,
    correlation_id_var,
    get_correlation_id,
    new_correlation_id,
)


class TestCorrelationId:
    def test_default_is_empty(self):
        correlation_id_var.set("")
        assert get_correlation_id() == ""

    def test_new_generates_12_char_hex(self):
        cid = new_correlation_id()
        assert len(cid) == 12
        assert cid == get_correlation_id()
        # Valid hex
        int(cid, 16)

    def test_successive_calls_generate_different_ids(self):
        id1 = new_correlation_id()
        id2 = new_correlation_id()
        assert id1 != id2


class TestJSONFormatter:
    def test_formats_as_valid_json(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Hello %s",
            args=("world",),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["message"] == "Hello world"
        assert data["level"] == "INFO"
        assert data["logger"] == "test"
        assert "timestamp" in data

    def test_includes_correlation_id(self):
        correlation_id_var.set("abc123def456")
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="test",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["correlation_id"] == "abc123def456"
        correlation_id_var.set("")

    def test_excludes_correlation_id_when_empty(self):
        correlation_id_var.set("")
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="test",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "correlation_id" not in data

    def test_includes_extra_fields(self):
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=1,
            msg="test",
            args=(),
            exc_info=None,
        )
        record.user_id = 42  # type: ignore[attr-defined]
        record.status_code = 200  # type: ignore[attr-defined]
        output = formatter.format(record)
        data = json.loads(output)
        assert data["user_id"] == 42
        assert data["status_code"] == 200

    def test_includes_exception_info(self):
        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys

            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=1,
            msg="error",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError" in data["exception"]


class TestConfigureLogging:
    def test_text_mode(self):
        configure_logging(level="DEBUG", log_format="text")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert not isinstance(root.handlers[0].formatter, JSONFormatter)

    def test_json_mode(self):
        configure_logging(level="INFO", log_format="json")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JSONFormatter)

    def test_reconfigure_clears_old_handlers(self):
        configure_logging(level="INFO", log_format="text")
        configure_logging(level="DEBUG", log_format="json")
        root = logging.getLogger()
        assert len(root.handlers) == 1  # Old handler removed
