import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shadow_mind_rules import BASE_STATE, EVENT_TYPES, STATE_FIELDS, settle_elapsed, settle_normal_chat


ROOT = Path(__file__).resolve().parents[1]


class ShadowMindRulesTests(unittest.TestCase):
    def test_normal_chat_is_bounded_and_preserves_hurt(self):
        state = dict(BASE_STATE, hurt=71, tension=68, connection=40)
        result, deltas, reason, confidence = settle_normal_chat(
            state, datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(result["hurt"], 71)
        self.assertLessEqual(abs(deltas["connection"]), 1)
        self.assertLessEqual(max(abs(value) for value in deltas.values()), 4)
        self.assertEqual(reason, "normal_chat_completed")
        self.assertEqual(confidence, 1.0)

    def test_elapsed_does_not_reduce_connection_or_concern(self):
        state = dict(BASE_STATE, connection=83, concern=31, tension=60, hurt=50)
        start = datetime(2026, 8, 1, 0, tzinfo=timezone.utc)
        result, deltas, reason, _ = settle_elapsed(state, start, start + timedelta(hours=12))
        self.assertEqual(result["connection"], 83)
        self.assertEqual(result["concern"], 31)
        self.assertLess(result["tension"], 60)
        self.assertLess(result["hurt"], 50)
        self.assertGreater(result["longing"], state["longing"])
        self.assertNotIn("connection", deltas)
        self.assertEqual(reason, "elapsed_time_settlement")

    def test_short_elapsed_time_does_not_write_a_change(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        result, deltas, reason, _ = settle_elapsed(BASE_STATE, start, start + timedelta(minutes=44))
        self.assertEqual(result, BASE_STATE)
        self.assertEqual(deltas, {})
        self.assertEqual(reason, "elapsed_below_change_threshold")

    def test_all_values_stay_in_range(self):
        extreme = {field: 10000 for field in STATE_FIELDS}
        extreme["valence"] = -10000
        result, _, _, _ = settle_normal_chat(extreme, datetime.now(timezone.utc))
        for field, value in result.items():
            minimum = -100 if field == "valence" else 0
            self.assertGreaterEqual(value, minimum)
            self.assertLessEqual(value, 100)

    def test_event_types_are_fixed(self):
        self.assertEqual(EVENT_TYPES, {
            "normal_chat", "user_replied", "warm_exchange", "topic_continued",
            "correction_detected", "boundary_mentioned", "conflict_possible",
            "repair_possible", "silence_elapsed",
        })


class ShadowMindIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.rules_source = (ROOT / "shadow_mind_rules.py").read_text(encoding="utf-8")
        cls.database_source = (ROOT / "database.py").read_text(encoding="utf-8")

    def test_rules_module_has_no_model_or_database_imports(self):
        tree = ast.parse(self.rules_source)
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_from = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("database", imports | imported_from)
        self.assertNotIn("httpx", imports | imported_from)
        self.assertNotIn("openai", imports | imported_from)

    def test_switch_defaults_to_disabled(self):
        self.assertIn('SHADOW_MIND_RULES_ENABLED = os.getenv("SHADOW_MIND_RULES_ENABLED", "false")', self.main_source)

    def test_chat_hook_is_before_memory_extraction_checks(self):
        hook = self.main_source.index("shadow_mind_normal_turn_saved")
        extraction = self.main_source.index("# 2. 检查是否需要提取记忆", hook)
        settle = self.main_source.index("await settle_shadow_mind_rules", hook)
        self.assertLess(settle, extraction)

    def test_schema_contains_only_redacted_event_fields(self):
        block_start = self.database_source.index("CREATE TABLE IF NOT EXISTS shadow_mind_event_log")
        block_end = self.database_source.index("CREATE INDEX IF NOT EXISTS idx_shadow_mind_event_session_time")
        block = self.database_source[block_start:block_end]
        for forbidden in ("content", "prompt", "system_prompt", "api_key", "secret"):
            self.assertNotIn(forbidden, block.lower())
        for required in ("event_type", "source_message_ids", "deltas", "reason_code", "confidence", "computed_at"):
            self.assertIn(required, block)


if __name__ == "__main__":
    unittest.main()
