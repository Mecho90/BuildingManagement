from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from core.middleware import EnsureCoreSchemaMiddleware


class EnsureCoreSchemaMiddlewareTests(SimpleTestCase):
    def setUp(self):
        from core import middleware as middleware_module

        middleware_module._bootstrapped = False
        self.factory = RequestFactory()

    @override_settings(AUTO_FIX_CORE_SCHEMA=True, DEBUG=True)
    def test_auto_fix_runs_migrate_in_debug(self):
        middleware = EnsureCoreSchemaMiddleware(lambda req: req)
        request = self.factory.get("/")

        with self.assertLogs("core.middleware", level="WARNING") as logs, \
                self._mock_schema(return_value=({"core_building"}, {})), \
                self._mock_migrate() as migrate_cmd:
            middleware(request)

        migrate_cmd.assert_called_once_with("migrate", interactive=False, verbosity=1)
        self.assertIn("Auto-migrate complete", " ".join(logs.output))

    @override_settings(AUTO_FIX_CORE_SCHEMA=True, DEBUG=False)
    def test_auto_fix_logs_error_when_not_debug(self):
        middleware = EnsureCoreSchemaMiddleware(lambda req: req)
        request = self.factory.get("/")

        with self.assertLogs("core.middleware", level="ERROR") as logs, \
                self._mock_schema(return_value=({"core_building"}, {})), \
                self._mock_migrate() as migrate_cmd:
            middleware(request)

        migrate_cmd.assert_not_called()
        self.assertIn("python manage.py migrate", " ".join(logs.output))

    @override_settings(AUTO_FIX_CORE_SCHEMA=True, DEBUG=True)
    def test_noop_when_schema_matches(self):
        middleware = EnsureCoreSchemaMiddleware(lambda req: req)
        request = self.factory.get("/")

        with self._mock_schema(return_value=(set(), {})), self._mock_migrate() as migrate_cmd:
            middleware(request)

        migrate_cmd.assert_not_called()

    @contextmanager
    def _mock_schema(self, return_value):
        with patch("core.middleware._missing_schema_for_core", return_value=return_value):
            yield

    @contextmanager
    def _mock_migrate(self):
        with patch("core.middleware.call_command") as mocked:
            yield mocked
