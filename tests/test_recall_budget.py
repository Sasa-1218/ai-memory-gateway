import unittest
from recall_budget import combine_recall_blocks as combine


class RecallBudgetTests(unittest.TestCase):
    def test_card_first_when_both_fit(self):
        self.assertEqual(combine("MEM", "CARD"), "CARD\n\nMEM")

    def test_keeps_whole_card_when_memory_would_overflow(self):
        self.assertEqual(combine("m"*10, "card", max_chars=4), "card")

    def test_can_prefer_memory(self):
        self.assertEqual(combine("mem", "card", max_chars=4, prefer_experience=False), "mem")

    def test_no_text_truncation(self):
        self.assertEqual(combine("memory", "long card", max_chars=6), "memory")

    def test_empty_and_nonstring(self):
        self.assertEqual(combine(None, ""), "")
        self.assertEqual(combine("", "card", max_chars=4), "card")

    def test_repeated_identical_blocks_are_not_doubled(self):
        self.assertEqual(combine("same", "same"), "same")

    def test_preserve_two_complete_memories_without_truncating(self):
        from recall_budget import fit_memory_fact_block
        source = (
            "<脑海中浮现的既往事实>\n"
            "- First memory detail\n"
            "- Second memory detail\n"
            "- Third memory detail\n"
            "</脑海中浮现的既往事实>"
        )
        budget = len(
            "<脑海中浮现的既往事实>\n"
            "- First memory detail\n"
            "- Second memory detail\n"
            "</脑海中浮现的既往事实>"
        )
        result = fit_memory_fact_block(source, budget)
        self.assertIn("- First memory detail", result)
        self.assertIn("- Second memory detail", result)
        self.assertNotIn("- Third memory detail", result)
        self.assertEqual(len(result), budget)
        self.assertTrue(result.endswith("</脑海中浮现的既往事实>"))

    def test_preserve_multiline_fact_intact(self):
        from recall_budget import fit_memory_fact_block
        source = (
            "<脑海中浮现的既往事实>\n"
            "- A memory\n"
            "a continuation\n"
            "- Short\n"
            "</脑海中浮现的既往事实>"
        )
        budget = len(
            "<脑海中浮现的既往事实>\n"
            "- Short\n"
            "</脑海中浮现的既往事实>"
        )
        result = fit_memory_fact_block(source, budget)
        self.assertNotIn("a continuation", result)
        self.assertIn("- Short", result)
        self.assertNotIn("- A memory", result)

    def test_unrecognized_memory_format_skipped_without_truncation(self):
        from recall_budget import fit_memory_fact_block
        self.assertEqual(fit_memory_fact_block("unrecognized format", 4), "")

    def test_shares_budget_with_card_and_keeps_whole_memory(self):
        facts = (
            "<脑海中浮现的既往事实>\n"
            "- Relevant memory\n"
            "- Another memory\n"
            "</脑海中浮现的既往事实>"
        )
        combined = combine(facts, "CARD", max_chars=70)
        self.assertTrue(combined.startswith("CARD\n\n"))
        self.assertIn("- Relevant memory", combined)
        self.assertNotIn("- Another memory", combined)
        self.assertLessEqual(len(combined), 70)

    def test_too_small_memory_wrapper_budget_skips_all(self):
        from recall_budget import fit_memory_fact_block
        source = "<脑海中浮现的既往事实>\n- A\n</脑海中浮现的既往事实>"
        self.assertEqual(fit_memory_fact_block(source, 5), "")

    def test_invalid_budget_rejected(self):
        for bad in (-1, True, 1.5, None):
            with self.assertRaises(ValueError):
                combine("M", "E", max_chars=bad)

    def test_separator_is_counted(self):
        self.assertEqual(combine("MEM", "CARD", max_chars=8), "CARD")
        self.assertEqual(combine("MEM", "CARD", max_chars=9), "CARD\n\nMEM")


if __name__ == "__main__":
    unittest.main()
