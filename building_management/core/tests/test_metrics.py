from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from core.utils.metrics import log_duration


class _LoggerStub:
    def __init__(self):
        self.records: list[tuple[str, str, dict]] = []

    def info(self, message, *args, extra=None, **kwargs):
        self.records.append(("info", message, extra or {}))

    def error(self, message, *args, extra=None, **kwargs):
        self.records.append(("error", message, extra or {}))


class LogDurationTests(SimpleTestCase):
    def setUp(self):
        self.logger = _LoggerStub()

    def test_scrubs_sensitive_identifiers(self):
        with log_duration(
            self.logger,
            "demo.metric",
            extra={"user_id": 42, "safe_value": "ok"},
        ):
            pass
        self.assertEqual(len(self.logger.records), 1)
        _, _, payload = self.logger.records[0]
        self.assertEqual(payload["metric"], "demo.metric")
        self.assertTrue(payload["user_id"].startswith("hash:"))
        self.assertEqual(payload["safe_value"], "ok")
        self.assertEqual(payload["status"], "ok")

    @override_settings(TIMING_LOG_MIN_DURATION_MS=10_000)
    def test_short_successful_spans_can_be_suppressed(self):
        with log_duration(self.logger, "demo.metric"):
            pass
        self.assertEqual(self.logger.records, [])

    @override_settings(TIMING_LOG_MIN_DURATION_MS=10_000)
    def test_errors_always_emit_records(self):
        with self.assertRaises(RuntimeError):
            with log_duration(self.logger, "demo.metric"):
                raise RuntimeError("boom")
        self.assertEqual(len(self.logger.records), 1)
        level, _, payload = self.logger.records[0]
        self.assertEqual(level, "error")
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_type"], "RuntimeError")
