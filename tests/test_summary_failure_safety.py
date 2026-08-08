import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SummaryFailureSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.main_source)

    def test_summary_requests_disable_thinking(self):
        self.assertGreaterEqual(
            self.main_source.count('"thinking": {"type": "disabled"}'),
            2,
        )

    def test_empty_summary_stops_before_cursor_advance(self):
        loop_start = self.main_source.index("while _should_rotate")
        loop_end = self.main_source.index("if rotation_count > 0", loop_start)
        block = self.main_source[loop_start:loop_end]
        empty_guard = block.index("if not new_summary:")
        stop = block.index("break", empty_guard)
        cursor_advance = block.index("a_start_round += X")
        self.assertLess(empty_guard, stop)
        self.assertLess(stop, cursor_advance)

    def test_success_count_increments_only_after_nonempty_guard(self):
        loop_start = self.main_source.index("while _should_rotate")
        loop_end = self.main_source.index("if rotation_count > 0", loop_start)
        block = self.main_source[loop_start:loop_end]
        self.assertLess(block.index("if not new_summary:"), block.index("rotation_count += 1"))

    def test_shadow_mind_timestamp_parameter_is_explicit(self):
        self.assertIn(
            "created_at >= $2::timestamptz - INTERVAL '60 minutes'",
            self.database_source,
        )


if __name__ == "__main__":
    unittest.main()
