import unittest

from recall_budget import combine_recall_blocks as combine
from recall_budget import fit_memory_entries, MEMORY_OPEN, MEMORY_CLOSE


def fact_block(*entries):
    return MEMORY_OPEN + "\n" + "\n".join(entries) + "\n" + MEMORY_CLOSE


class RecallBudgetTests(unittest.TestCase):
    def test_card_first_when_both_fit(self):
        self.assertEqual(combine("MEM", "CARD"), "CARD\n\nMEM")

    def test_card_first_even_if_only_one_fits(self):
        self.assertEqual(combine("memory", "card", max_chars=4), "card")

    def test_can_prefer_memory(self):
        self.assertEqual(combine("mem", "card", max_chars=4,
                                 prefer_experience=False), "mem")

    def test_no_arbitrary_block_truncation(self):
        self.assertEqual(combine("memory", "long card", max_chars=6), "memory")

    def test_empty_and_nonstring(self):
        self.assertEqual(combine(None, ""), "")
        self.assertEqual(combine("", "card", max_chars=4), "card")

    def test_repeated_identical_blocks_not_doubled(self):
        self.assertEqual(combine("same", "same"), "same")

    def test_two_complete_facts_preserved(self):
        one, two, three = "- First memory detail", "- Second memory detail", "- Third memory detail"
        result = fit_memory_entries([one, two, three], len(fact_block(one, two)))
        self.assertEqual(result, fact_block(one, two))
        self.assertNotIn(three, result)

    def test_multiline_fact_preserved_or_skipped_whole(self):
        big = "- A memory\n- embedded bullet\ncontinued"
        short = "- Short"
        result = fit_memory_entries([big, short], len(fact_block(short)))
        self.assertEqual(result, fact_block(short))
        result2 = fit_memory_entries([big, short], len(fact_block(big)))
        self.assertIn(big, result2)

    def test_unknown_records_never_injected(self):
        self.assertEqual(fit_memory_entries(["no prefix", 123, ""], 6000), "")

    def test_memory_and_card_share_budget(self):
        one, two = "- Relevant memory", "- Another memory"
        budget = len("CARD\n\n" + fact_block(one))
        memory = fit_memory_entries([one, two], budget - len("CARD\n\n"))
        combined = combine(memory, "CARD", max_chars=budget)
        self.assertEqual(combined, "CARD\n\n" + fact_block(one))
        self.assertLessEqual(len(combined), budget)

    def test_too_small_budget_skips_all_memories(self):
        self.assertEqual(fit_memory_entries(["- A"], 5), "")

    def test_memory_priority_respects_search_ranking(self):
        records = ["- Alpha one", "- Beta two", "- Gamma three"]
        self.assertEqual(
            fit_memory_entries(records, len(fact_block(*records[:2]))),
            fact_block(*records[:2])
        )

    def test_exact_budget_boundary(self):
        records = ["- Alpha one", "- Beta two"]
        cap = len(fact_block(records[0]))
        self.assertEqual(fit_memory_entries(records, cap), fact_block(records[0]))

    def test_skip_oversized_first_then_keep_later(self):
        self.assertEqual(
            fit_memory_entries(["- " + "X"*1000, "- short"], 80),
            fact_block("- short")
        )

    def test_oversized_preformatted_text_is_not_reparsed(self):
        self.assertEqual(combine(fact_block("- item1", "- item2"), "CARD", max_chars=10), "CARD")

    def test_invalid_limits(self):
        for bad in (-1, True, 1.5, None):
            with self.assertRaises(ValueError):
                combine("M", "E", max_chars=bad)
            with self.assertRaises(ValueError):
                fit_memory_entries(["- A"], bad)

    def test_separator_is_counted(self):
        self.assertEqual(combine("MEM", "CARD", max_chars=8), "CARD")
        self.assertEqual(combine("MEM", "CARD", max_chars=9), "CARD\n\nMEM")


if __name__ == "__main__":
    unittest.main()
