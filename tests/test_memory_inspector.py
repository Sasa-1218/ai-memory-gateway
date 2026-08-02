import pathlib
import unittest

from memory_inspector import (
    build_injection_preview,
    estimate_tokens,
    lexical_score,
    make_result,
    matched_terms,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


class MemoryInspectorHelperTests(unittest.TestCase):
    def test_token_estimate_is_stable_and_non_negative(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("表带记忆"), 2)

    def test_lexical_match_reports_only_present_terms(self):
        score, terms = lexical_score("表带", ["表带", "鞋子"], "后来买了那条表带")
        self.assertGreater(score, 0)
        self.assertEqual(terms, ["表带"])
        self.assertEqual(matched_terms("雨伞", ["雨伞"], "没有相关内容"), [])

    def test_result_exposes_only_normalized_inspector_fields(self):
        result = make_result(
            result_type="experience_card", item_id=3, title="表带",
            content="一段只读预览", score=2.5, terms=["表带"],
            source_session_id="session-a", source_message_ids=[10, 11],
            ai_visible=False, review_status="pending",
        )
        self.assertFalse(result["ai_visible"])
        self.assertEqual(result["source_message_ids"], [10, 11])
        self.assertNotIn("query", result)

    def test_preview_respects_character_cap(self):
        items = [make_result(
            result_type="memory", item_id=1, title="候选",
            content="x" * 1200, score=1, terms=[],
        )]
        preview = build_injection_preview(items, max_chars=80)
        self.assertLessEqual(preview["chars"], 80)
        self.assertEqual(preview["estimated_tokens"], estimate_tokens(preview["content"]))


class MemoryInspectorIsolationTests(unittest.TestCase):
    def test_route_disables_memory_access_timestamp_updates(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        route = source[source.index('@app.post("/api/memory-inspector/search")'):]
        route = route[:route.index('@app.get("/api/experience-cards")')]
        self.assertIn("touch_accessed=False", route)
        self.assertIn('"query_saved": False', route)
        self.assertNotIn("save_memory(", route)
        self.assertNotIn("save_conversation(", route)
        self.assertNotIn("update_experience_card(", route)
        self.assertNotIn('"archived_experience_cards"', route)
        self.assertNotIn('"deleted_experience_cards"', route)

    def test_inspector_is_not_referenced_by_experience_generation(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertEqual(source.count('@app.post("/api/memory-inspector/search")'), 1)
        self.assertNotIn("memory_inspector", (ROOT / "experience_cards.py").read_text(encoding="utf-8"))

    def test_dashboard_exposes_read_only_controls(self):
        html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn('id="section-memory-inspector"', html)
        self.assertIn('id="inspectorSourcePending"', html)
        self.assertIn("/api/memory-inspector/search", script)
        self.assertNotIn("updateExperienceCard", script[script.index("async function runMemoryInspector"):script.index("// ============================================\n// Tab")])


if __name__ == "__main__":
    unittest.main()
