import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class TransparencyDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        cls.html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "static" / "js" / "dashboard.js").read_text(encoding="utf-8")

    def test_dashboard_exposes_transparency_panels(self):
        self.assertIn('data-section="memory-extraction"', self.html)
        self.assertIn('data-section="io-context"', self.html)
        self.assertIn('id="section-memory-extraction"', self.html)
        self.assertIn('id="section-io-context"', self.html)
        self.assertIn('id="memoryExtractionCard"', self.html)
        self.assertIn('id="ioContextCard"', self.html)
        self.assertIn('id="memoryExtractionItems"', self.html)
        self.assertIn('id="ioContextItems"', self.html)
        self.assertIn('id="ioContextSummary"', self.html)
        self.assertIn('聊天接入状态', self.html)
        self.assertIn('加工后预览', self.script)
        self.assertIn('原始字段摘要', self.script)
        self.assertIn('loadMemoryExtractionTrail()', self.script)
        self.assertIn('loadIoContextTrail()', self.script)
        self.assertIn('/api/memory/extraction/recent', self.script)
        self.assertIn('/api/io/context/recent', self.script)

    def test_recent_memory_and_io_routes_are_read_only(self):
        memory_start = self.main_source.index('@app.get("/api/memory/extraction/recent")')
        memory_end = self.main_source.index('@app.get("/api/io/context/recent")', memory_start)
        memory_route = self.main_source[memory_start:memory_end]
        self.assertIn("get_recent_memories_detail", memory_route)
        self.assertNotIn("save_memory(", memory_route)
        self.assertNotIn("save_message(", memory_route)

        io_start = self.main_source.index('@app.get("/api/io/context/recent")')
        io_end = self.main_source.index('@app.get("/api/shadow/mind/status")', io_start)
        io_route = self.main_source[io_start:io_end]
        self.assertIn("get_recent_io_context_events", io_route)
        self.assertNotIn("save_io_context_events(", io_route)
        self.assertNotIn("save_message(", io_route)
        self.assertIn("payload_preview", io_route)
        self.assertIn("payload_details", io_route)
        self.assertIn("chat_preview", io_route)
        self.assertIn("chat_integration_enabled", io_route)

    def test_io_recent_query_includes_payload_for_dashboard_preview(self):
        start = self.main_source.index('@app.get("/api/io/context/recent")')
        end = self.main_source.index('@app.get("/api/shadow/mind/status")', start)
        io_route = self.main_source[start:end]
        self.assertIn("_format_io_payload_details", io_route)

        database_source = (ROOT / "database.py").read_text(encoding="utf-8")
        query_start = database_source.index("async def get_recent_io_context_events")
        query_end = database_source.index("# ============================================================", query_start)
        query = database_source[query_start:query_end]
        self.assertIn("payload", query)

    def test_io_payload_string_is_parsed_for_dashboard_preview(self):
        self.assertIn("def _io_payload_value", self.main_source)
        self.assertIn("json.loads(value)", self.main_source)
        self.assertIn("payload = _io_payload_value(row.get(\"payload\"))", self.main_source)
        self.assertIn("payload = _io_payload_value(payload)", self.main_source)

    def test_io_preview_handles_common_perception_fields(self):
        for field in ("local_time", "now_playing", "charging", "user_state", "city"):
            self.assertIn(field, self.main_source)
        self.assertIn('user_state.lower() not in ("default", "unknown", "normal")', self.main_source)
        self.assertIn("已接收该类感知事件，但本次字段为空", self.main_source)
        self.assertIn("位置字段：", self.main_source)
        self.assertIn("仅收到时区：", self.main_source)
        self.assertIn("def _io_percent_text", self.main_source)
        self.assertIn("def _io_motion_text", self.main_source)
        self.assertIn('"still": "静止"', self.main_source)
        self.assertIn("电量：{_io_percent_text", self.main_source)
        self.assertIn("已接收该类感知事件，但这些字段暂时没有转换成聊天预览。", self.main_source)
        self.assertIn("overflow-wrap:anywhere", self.script)

    def test_recent_routes_stay_under_dashboard_access(self):
        self.assertIn('"/api/memory/extraction/recent"', self.main_source)
        self.assertIn('"/api/io/context/recent"', self.main_source)
        self.assertIn('"/api/memory/extraction/recent"', self.main_source)
        self.assertIn('"/api/io/context/recent"', self.main_source)


if __name__ == "__main__":
    unittest.main()
