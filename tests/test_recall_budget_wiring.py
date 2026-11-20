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

    def test_legacy_fallback_preserves_memory_before_experience(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('memory_block = (memory_block + "\\n\\n" + experience_text) if memory_block else experience_text', source)
        self.assertIn("if mem_text:\n                parts.append(mem_text)", source)

    def test_budget_applies_to_memory_without_experience_in_legacy_mode(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("        experience_text = \"\"", source.index("# ---------- 原有逻辑"))
        end = source.index("        has_system =", start)
        code = source[start:end]
        self.assertIn("if SHARED_RECALL_BUDGET_ENABLED:", code)
        self.assertIn("memory_block = combine_recall_blocks(", code)


if __name__ == "__main__":
    unittest.main()
