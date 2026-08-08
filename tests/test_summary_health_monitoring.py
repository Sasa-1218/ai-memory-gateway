import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SummaryHealthStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        ast.parse(cls.main_source)
        ast.parse(cls.database_source)

    def test_health_table_contains_no_content_columns(self):
        start = self.database_source.index("CREATE TABLE IF NOT EXISTS summary_health_status")
        end = self.database_source.index('""")', start)
        schema = self.database_source[start:end]
        self.assertIn("consecutive_failures", schema)
        self.assertIn("last_error_code", schema)
        self.assertNotIn("content", schema)
        self.assertNotIn("prompt", schema)

    def test_dashboard_route_is_read_only(self):
        start = self.main_source.index('async def api_summary_health')
        end = self.main_source.index('@app.', start)
        route = self.main_source[start:end]
        self.assertIn("get_summary_health_status", route)
        self.assertNotIn("save_session_cache_state", route)
        self.assertNotIn("deliver_summary_health_alert", route)

    def test_success_is_recorded_after_state_write(self):
        start = self.main_source.index("if rotation_count > 0:")
        end = self.main_source.index("if cache_diag is not None:", start)
        block = self.main_source[start:end]
        self.assertLess(
            block.index("await save_session_cache_state"),
            block.index("await _record_summary_success_safe"),
        )
        self.assertIn("if not rotation_failed:", block)


class SummaryHealthAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.database_source = (ROOT / "database.py").read_text(encoding="utf-8")

    def test_alert_requires_atomic_should_alert_claim(self):
        helper_start = self.main_source.index("async def _record_summary_failure_safe")
        helper_end = self.main_source.index("async def _record_summary_success_safe", helper_start)
        helper = self.main_source[helper_start:helper_end]
        self.assertIn('if SUMMARY_ALERTS_ENABLED and result.get("should_alert"):', helper)

    def test_default_threshold_and_cooldown(self):
        self.assertIn('SUMMARY_ALERT_AFTER_FAILURES", "2"', self.main_source)
        self.assertIn('SUMMARY_ALERT_COOLDOWN_HOURS", "6"', self.main_source)
        self.assertIn("FOR UPDATE", self.database_source)
        self.assertIn("last_alert_attempt_at", self.database_source)

    def test_monitoring_errors_are_caught(self):
        start = self.main_source.index("async def _record_summary_failure_safe")
        end = self.main_source.index("async def _record_summary_success_safe", start)
        helper = self.main_source[start:end]
        self.assertIn("except Exception as monitor_error:", helper)
        self.assertIn("summary_health_monitor_failed", helper)


if __name__ == "__main__":
    unittest.main()
