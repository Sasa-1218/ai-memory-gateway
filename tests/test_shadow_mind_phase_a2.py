import ast
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shadow_mind_rules import BASE_STATE, EVENT_TYPES, STATE_FIELDS, settle_elapsed, settle_normal_chat


ROOT = Path(__file__).resolve().parents[1]


class ShadowMindRulesTests(unittest.TestCase):
    def test_normal_chat_only_changes_timing_backed_fields(self):
        state = dict(BASE_STATE, hurt=71, tension=68, connection=40)
        result, deltas, reason, confidence = settle_normal_chat(
            state, datetime(2026, 8, 2, 12, tzinfo=timezone.utc), recent_turns=1, new_burst=True
        )
        self.assertEqual(result["hurt"], 71)
        self.assertEqual(result["tension"], 68)
        self.assertEqual(result["connection"], 40)
        self.assertNotIn("warmth", deltas)
        self.assertNotIn("valence", deltas)
        self.assertNotIn("concern", deltas)
        self.assertEqual(reason, "normal_chat_burst_started")
        self.assertEqual(confidence, 1.0)

    def test_continuous_chat_does_not_repeatedly_satisfy_longing(self):
        state = dict(BASE_STATE, longing=60)
        first, first_deltas, _, _ = settle_normal_chat(
            state, datetime(2026, 8, 2, 12, tzinfo=timezone.utc), recent_turns=1, new_burst=True
        )
        second, second_deltas, reason, _ = settle_normal_chat(
            first, datetime(2026, 8, 2, 12, 5, tzinfo=timezone.utc), recent_turns=2, new_burst=False
        )
        self.assertLess(first_deltas["longing"], 0)
        self.assertNotIn("longing", second_deltas)
        self.assertEqual(reason, "normal_chat_density_updated")

    def test_fatigue_load_starts_after_four_turns(self):
        now = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
        state = dict(BASE_STATE, fatigue=20)
        quiet, quiet_deltas, _, _ = settle_normal_chat(state, now, recent_turns=4, new_burst=False)
        busy, busy_deltas, _, _ = settle_normal_chat(quiet, now, recent_turns=5, new_burst=False)
        same_band, same_band_deltas, _, _ = settle_normal_chat(
            busy, now, recent_turns=6, new_burst=False
        )
        self.assertNotIn("fatigue", quiet_deltas)
        self.assertEqual(busy_deltas["fatigue"], 2)
        self.assertNotIn("fatigue", same_band_deltas)

    def test_normal_chat_no_longer_saturates_positive_emotions(self):
        state = dict(BASE_STATE)
        now = datetime(2026, 8, 2, 12, tzinfo=timezone.utc)
        for turn in range(1, 40):
            state, _, _, _ = settle_normal_chat(state, now, recent_turns=turn, new_burst=turn == 1)
        self.assertEqual(state["warmth"], BASE_STATE["warmth"])
        self.assertEqual(state["valence"], BASE_STATE["valence"])
        self.assertEqual(state["connection"], BASE_STATE["connection"])

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

    def test_elapsed_brings_mechanical_saturation_back_toward_baseline(self):
        state = dict(BASE_STATE, curiosity=100, share=100, warmth=100, valence=100, arousal=100)
        start = datetime(2026, 8, 1, 0, tzinfo=timezone.utc)
        result, deltas, _, _ = settle_elapsed(state, start, start + timedelta(hours=6))
        for field in ("curiosity", "share", "warmth", "valence", "arousal"):
            self.assertLess(result[field], state[field])
            self.assertLess(deltas[field], 0)

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

    def test_interaction_density_uses_message_metadata_not_event_log(self):
        settle_start = self.database_source.index("async def settle_shadow_mind_rules")
        settle_end = self.database_source.index("async def get_shadow_mind_a2_events", settle_start)
        block = self.database_source[settle_start:settle_end]
        self.assertIn("SELECT COUNT(*) FROM conversations", block)
        self.assertIn("role = 'user'", block)
        self.assertNotIn("SELECT COUNT(*) FROM shadow_mind_event_log", block)


if __name__ == "__main__":
    unittest.main()
