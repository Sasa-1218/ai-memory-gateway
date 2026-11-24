"""Static regression guards for the opt-in shared recall budget wiring."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RecallBudgetWiringTests(unittest.TestCase):
    def test_shared_budget_is_default_off(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("SHARED_RECALL_BUDGET_ENABLED", "false")', source)

    def test_three_chat_paths_call_same_budget(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("combined = combine_recall_blocks("), 2)
        self.assertEqual(source.count("memory_block = combine_recall_blocks("), 1)

    def test_source_records_are_packed_before_formatting(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("fit_memory_entries(memory_lines, remaining)"), 2)
        self.assertIn("async def build_system_prompt_with_memories(user_message: str, max_chars:", source)
        self.assertIn("async def build_memory_text(user_message: str, max_chars:", source)

    def test_three_paths_limit_records_after_card_selection(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("SHARED_RECALL_MAX_CHARS - len(experience_text)"), 3)
        self.assertEqual(source.count("build_memory_text(user_message, max_chars=remaining)"), 2)
        self.assertIn("user_message, max_chars=remaining,", source)

    def test_ambiguous_formatted_parser_is_not_available(self):
        source = (ROOT / "recall_budget.py").read_text(encoding="utf-8")
        self.assertNotIn("def fit_memory_fact_block(", source)
        self.assertIn("def fit_memory_entries(", source)

    def test_experience_card_budget_is_independent_and_small(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('SHARED_RECALL_CARD_MAX_CHARS = max(', source)
        self.assertIn('SHARED_RECALL_MAX_CHARS // 5', source)
        self.assertIn('min(_env_int("SHARED_RECALL_CARD_MAX_CHARS", 600)', source)
        self.assertIn('600, SHARED_RECALL_MAX_CHARS // 5', source)
        self.assertIn('            1,\n            max_chars=SHARED_RECALL_CARD_MAX_CHARS', source)
        self.assertIn('max_chars=SHARED_RECALL_CARD_MAX_CHARS if _shared_recall_budget_active() else None', source)

    def test_dashboard_toggle_automatically_applies_budget(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("return bool(SHARED_RECALL_BUDGET_ENABLED or EXPERIENCE_RECALL_ENABLED)", source)
        self.assertEqual(source.count("if _shared_recall_budget_active():"), 3)
        self.assertIn("if _shared_recall_budget_active() and SHARED_RECALL_CARD_MAX_CHARS < 80:", source)

    def test_dashboard_is_single_card_and_persists_normalized_value(self):
        html = (ROOT / "templates/dashboard.html").read_text(encoding="utf-8")
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('id="set-EXPERIENCE_RECALL_MAX_CARDS" class="input" min="1" max="1" value="1" readonly', html)
        self.assertIn('"EXPERIENCE_RECALL_MAX_CARDS": 1,', source)
        self.assertIn('if key == "EXPERIENCE_RECALL_MAX_CARDS":', source)

    def test_experience_card_default_off_and_interval_unchanged(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('EXPERIENCE_RECALL_ENABLED = os.getenv("EXPERIENCE_RECALL_ENABLED", "false")', source)
        self.assertIn('_env_int("EXPERIENCE_RECALL_EVERY_ROUNDS", 4)', source)

    def test_legacy_fallback_preserves_memory_before_experience(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('memory_block = (memory_block + "\\n\\n" + experience_text) if memory_block else experience_text', source)
        self.assertIn("if mem_text:\n                parts.append(mem_text)", source)

    def test_budget_applies_to_memory_without_experience_in_legacy_mode(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("        experience_text = \"\"", source.index("# ---------- 原有逻辑"))
        end = source.index("        has_system =", start)
        code = source[start:end]
        self.assertIn("if _shared_recall_budget_active():", code)
        self.assertIn("memory_block = combine_recall_blocks(", code)


if __name__ == "__main__":
    unittest.main()
