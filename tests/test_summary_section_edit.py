"""Regression tests for date-section parsing and targeted replacements."""
import unittest

from summary_section_edit import DATE_HEADING, INLINE_DATE, _digest, _validate_selection, split_sections, splice_section, _range_edit


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

    def test_merge_without_final_newline_keeps_following_day_separate(self):
        original = (
            "【2026-10-31】樱花照\\n原文A\\n"
            "【2026-10-31】汉服\\n原文B\\n"
            "【2026-11-01】南瓜灯\\n下一天原文\\n"
        )
        parts = [original]
        sections = split_sections(parts)
        replacement = "【2026-10-31】樱花照与汉服\\n合并稿没有末尾换行"
        after = splice_section(original, sections[0]["start"],
                               sections[1]["end"], replacement)
        parsed = split_sections([after])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["heading"], "【2026-10-31】樱花照与汉服")
        self.assertEqual(parsed[1]["heading"], "【2026-11-01】南瓜灯")
        self.assertTrue(parsed[1]["text"].endswith("下一天原文\\n"))
        self.assertIn("合并稿没有末尾换行\\n\\n【2026-11-01】", after)

    def test_two_consecutive_saves_do_not_consume_next_day(self):
        original = (
            "【2026-10-31】原始标题\\n正文A\\n"
            "【2026-11-01】次日标题\\n正文B"
        )
        first_section = split_sections([original])[0]
        after_first = splice_section(original, first_section["start"],
                                     first_section["end"],
                                     "【2026-10-31】新标题\\n第一次修改")
        first_after = split_sections([after_first])
        self.assertEqual(len(first_after), 2)
        after_second = splice_section(after_first, first_after[0]["start"],
                                      first_after[0]["end"],
                                      "【2026-10-31】新标题\\n第二次修改")
        second_after = split_sections([after_second])
        self.assertEqual(len(second_after), 2)
        self.assertEqual(second_after[1]["text"], "【2026-11-01】次日标题\\n正文B")
        self.assertEqual(after_second.count("【2026-11-01】"), 1)

    def test_diagnostic_flags_joined_date_only(self):
        damaged = "【2026-10-31】合并稿\\n正文没有换行【2026-11-01】下一天\\n原文"
        matches = [(m.start(), m.group(0)) for m in INLINE_DATE.finditer(damaged)
                   if m.start() > 0 and damaged[m.start()-1] != "\\n"]
        self.assertEqual(len(matches), 1)
        self.assertIn("2026-11-01", matches[0][1])
        healthy = "【2026-10-31】合并稿\\n正文\\n【2026-11-01】下一天\\n原文"
        self.assertEqual(
            [m.group(0) for m in INLINE_DATE.finditer(healthy)
             if m.start() > 0 and healthy[m.start()-1] != "\\n"], []
        )

    def test_date_range_replaces_two_days_without_touching_neighbors(self):
        parts = [
            "开头保留\\n【2026-10-29】旧事\\n不要改\\n"
            "【2026-10-30】A\\n旧A\\n"
            "【2026-10-31】B\\n旧B\\n"
            "【2026-11-01】C\\n旧C\\n"
            "【2026-11-02】D\\n保留D"
        ]
        sections = split_sections(parts)
        original = "".join(x["text"] for x in sections[1:4])
        replacement = ("【2026-10-30】新标题\\n新版A\\n\\n"
                       "【2026-10-31】新标题\\n新版B\\n"
                       "【2026-11-01】新标题\\n新版C")
        after, audit = _range_edit(parts, 0, 1, 0, 3, original, replacement)
        self.assertEqual(len(after), 1)
        self.assertIn("【2026-10-29】旧事\\n不要改", after[0])
        self.assertIn("【2026-11-02】D\\n保留D", after[0])
        self.assertIn("新版C\\n\\n【2026-11-02】D", after[0])
        self.assertNotIn("旧B", after[0])
        self.assertEqual(audit["selected_sections"], 3)
        self.assertEqual(len(split_sections(after)), 5)

    def test_date_range_across_parts_preserves_outside_text(self):
        parts = [
            "【2026-10-29】旧事\\n保留\\n【2026-10-30】A\\n旧A\\n",
            "【2026-10-31】B\\n旧B\\n【2026-11-01】C\\n旧C\\n",
            "【2026-11-02】D\\n保留D\\n【2026-11-03】E\\n保留E",
        ]
        selected = split_sections(parts)
        original = "".join(x["text"] for x in selected[1:4])
        new = ("【2026-10-30】合并第一天\\n内容1\\n"
               "【2026-10-31】合并第二天\\n内容2\\n"
               "【2026-11-01】合并第三天\\n内容3")
        result, audit = _range_edit(parts, 0, 1, 1, 1, original, new)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1], parts[2])
        self.assertEqual(result[0].count("【2026-11-01】"), 1)
        self.assertIn("【2026-10-29】旧事\\n保留", result[0])
        self.assertEqual(len(split_sections(result)), 6)
        self.assertEqual(audit["old_chars"], len(original))

    def test_date_range_rejects_unselected_dates_and_stale_text(self):
        from fastapi import HTTPException
        parts = ["【2026-10-30】A\\n旧A\\n【2026-10-31】B\\n旧B"]
        selected = split_sections(parts)
        original = selected[0]["text"]
        with self.assertRaises(HTTPException):
            _range_edit(parts, 0, 0, 0, 0, original, "【2026-11-01】错日期\\n正文")
        with self.assertRaises(HTTPException):
            _range_edit(parts, 0, 0, 0, 0, "旧内容和实际不符", "【2026-10-30】A\\n改了")

    def test_date_range_merges_duplicate_day_headings(self):
        parts = ["【2026-10-31】樱花\\n旧A\\n【2026-10-31】汉服\\n旧B\\n"
                 "【2026-11-01】下一天\\n不修改"]
        secs = split_sections(parts)
        result, _ = _range_edit(parts, 0, 0, 0, 1,
                                secs[0]["text"] + secs[1]["text"],
                                "【2026-10-31】樱花与汉服\\n合并后的新稿")
        self.assertEqual(len(split_sections(result)), 2)
        self.assertTrue(result[0].endswith("【2026-11-01】下一天\\n不修改"))
        self.assertIn("合并后的新稿\\n\\n【2026-11-01】", result[0])

    def test_range_heading_includes_end_date(self):
        parts = ["【2026-11-06 至 2026-11-07】电子花束\\n旧事"]
        result, _ = _range_edit(
            parts, 0, 0, 0, 0, parts[0],
            "【2026-11-06】电子花束\\n新内容\\n"
            "【2026-11-07】蛋糕\\n新内容")
        self.assertEqual(len(split_sections(result)), 2)
        self.assertIn("【2026-11-07】蛋糕", result[0])

    def test_heading_match_is_anchored_to_line(self):
        self.assertFalse(DATE_HEADING.search("今天提到【2026-11-01】的故事"))
        self.assertTrue(DATE_HEADING.search("\n【2026-11-01】南瓜灯"))


if __name__ == "__main__":
    unittest.main()
