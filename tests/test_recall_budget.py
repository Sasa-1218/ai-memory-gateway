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

    def test_invalid_budget_rejected(self):
        for bad in (-1, True, 1.5, None):
            with self.assertRaises(ValueError):
                combine("M", "E", max_chars=bad)

    def test_separator_is_counted(self):
        self.assertEqual(combine("MEM", "CARD", max_chars=8), "CARD")
        self.assertEqual(combine("MEM", "CARD", max_chars=9), "CARD\n\nMEM")


if __name__ == "__main__":
    unittest.main()
