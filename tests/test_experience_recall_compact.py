import unittest
from experience_recall import format_experience_recall as format_card


def card(summary="一起看烟花", title="烟花日的回忆"):
    return {
        "title": title,
        "event_summary": summary,
        "event_date_start": "2026-08-08",
        "interaction_trace": "当时的回应",
    }


class CompactExperienceRecallTests(unittest.TestCase):
    def test_legacy_format_is_unchanged(self):
        old = format_card([card()])
        self.assertIn("双方当时的回应：当时的回应", old)
        self.assertIn("以下内容是经过人工批准", old)

    def test_cap_keeps_only_one_card(self):
        result = format_card([card(), card("另一段", "第二张卡")], max_chars=600)
        self.assertIn("烟花日的回忆", result)
        self.assertNotIn("第二张卡", result)
        self.assertLessEqual(len(result), 600)

    def test_long_summary_is_explicitly_excerpted(self):
        result = format_card([card("细节内容" * 300)], max_chars=200)
        self.assertTrue(result.endswith("</过去共同经历的检索线索>"))
        self.assertIn("历史经历节选", result)
        self.assertIn("……", result)
        self.assertLessEqual(len(result), 200)

    def test_empty_card_is_not_recalled(self):
        self.assertEqual(format_card([card("")], max_chars=600), "")

    def test_small_budget_skips_card(self):
        self.assertEqual(format_card([card()], max_chars=10), "")

    def test_zero_budget_skips_card(self):
        self.assertEqual(format_card([card()], max_chars=0), "")

    def test_bad_budget_is_rejected(self):
        for bad in (-1, True, "600", 1.5):
            with self.assertRaises(ValueError):
                format_card([card()], max_chars=bad)

    def test_input_cards_unchanged(self):
        item = card("细节" * 300)
        old = dict(item)
        format_card([item], max_chars=100)
        self.assertEqual(item, old)


if __name__ == "__main__":
    unittest.main()
