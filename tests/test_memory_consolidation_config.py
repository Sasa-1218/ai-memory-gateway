import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class MemoryConsolidationConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_source = (ROOT / "main.py").read_text(encoding="utf-8")

    def test_consolidation_uses_dedicated_memory_config(self):
        start = self.main_source.index("async def consolidate_memories_for_date_range")
        end = self.main_source.index("@app.post(\"/api/memories/consolidate\")", start)
        block = self.main_source[start:end]
        self.assertIn("get_memory_config()", block)
        self.assertIn("_apply_memory_thinking_option(payload)", block)
        self.assertIn("_apply_memory_thinking_option(fix_payload)", block)
        self.assertNotIn("consolidation_model = MEMORY_MODEL", block)
        self.assertNotIn("memory_url = get_memory_api_base_url()", block)

    def test_empty_model_content_reports_specific_error(self):
        start = self.main_source.index("async def consolidate_memories_for_date_range")
        end = self.main_source.index("@app.post(\"/api/memories/consolidate\")", start)
        block = self.main_source[start:end]
        self.assertIn("_extract_chat_completion_text(data).strip()", block)
        self.assertIn("memory_consolidation_empty_response", block)
        self.assertIn("finish_reason=", block)


if __name__ == "__main__":
    unittest.main()
