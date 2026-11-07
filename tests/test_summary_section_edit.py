"""Regression tests for date-section parsing and targeted replacements."""
import unittest

from summary_section_edit import DATE_HEADING, _digest, _validate_selection, split_sections


class DateSectionEditTests(unittest.TestCase):
    def test_multiple_sections_inside_one_archived_part(self):
        original = (
            "# 对话摘要\n"
            "【2026-10-31】万圣节与柚子\n旧日记。\n\n"
            "【2026-11-01】南瓜灯\n情绪细节。\n\n"
            "【2026-11-01】花园\n另一件事。\n"
        )
        sections = split_sections([original, "【2026-11-05】羽绒服\n待修复"])
        self.assertEqual(len(sections), 4)
        self.assertEqual([s["section_index"] for s in sections], [0, 1, 2, 0])
        self.assertEqual([s["part_index"] for s in sections], [0, 0, 0, 1])
        self.assertEqual(sections[1]["heading"], "【2026-11-01】南瓜灯")
        s = sections[1]
        replacement = "【2026-11-01】南瓜灯\n增加一点感情细节。\n\n"
        result = original[:s["start"]] + replacement + original[s["end"]:]
        self.assertTrue(result.startswith("# 对话摘要\n【2026-10-31】万圣节与柚子\n旧日记"))
        self.assertIn("【2026-11-01】花园\n另一件事。", result)
        self.assertNotIn("情绪细节。", result)

    def test_time_range_heading_is_one_section(self):
        parts = ["【2026-11-02 至 2026-11-03】亲吻\n正文\n"
                 "【2026-11-03 至 2026-11-04】主心骨\n正文"]
        sections = split_sections(parts)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[1]["heading"], "【2026-11-03 至 2026-11-04】主心骨")

    def test_year_month_chinese_heading_and_plain_part(self):
        sections = split_sections([
            "【2025年8月8日】烟花日\n正文",
            "不带日期标题的摘要内容"
        ])
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[1]["heading"], "整段（无日期标题）")

    def test_selection_and_revision_token(self):
        parts = ["【2026-11-01】日记\n内容"]
        selected = _validate_selection(parts, 0, 0)
        self.assertEqual(selected["text"], parts[0])
        self.assertNotEqual(_digest(parts, 2324), _digest(parts, 2332))
        self.assertNotEqual(_digest(parts, 2324),
                            _digest(["【2026-11-01】日记\n改后"], 2324))

    def test_heading_match_is_anchored_to_line(self):
        self.assertFalse(DATE_HEADING.search("今天提到【2026-11-01】的故事"))
        self.assertTrue(DATE_HEADING.search("\n【2026-11-01】南瓜灯"))


if __name__ == "__main__":
    unittest.main()
