"""Static integration guards for the staged memory provenance Dashboard entry."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class MemoryLineageWiringTests(unittest.TestCase):
    def test_readonly_endpoint_exists(self):
        main = (ROOT / 'main.py').read_text(encoding='utf-8')
        self.assertIn('@app.get("/api/memories/{memory_id}/lineage")', main)
        self.assertIn('get_memory_lineage_readonly(memory_id)', main)

    def test_database_adapter_uses_readonly_select(self):
        db = (ROOT / 'database.py').read_text(encoding='utf-8')
        start = db.index('async def get_memory_lineage_readonly(')
        end = db.index('async def get_memories_by_ids_readonly(', start)
        segment = db[start:end]
        self.assertIn('SELECT id, merged_from, source_session', segment)
        self.assertNotIn('UPDATE ', segment)
        self.assertNotIn('INSERT INTO', segment)
        self.assertNotIn('DELETE FROM', segment)

    def test_dashboard_has_source_preview_without_false_message_claim(self):
        js = (ROOT / 'static/js/dashboard.js').read_text(encoding='utf-8')
        self.assertIn('onclick="previewMemoryLineage(', js)
        self.assertIn('async function previewMemoryLineage(', js)
        self.assertIn('记忆编号不能当作消息编号', js)
        self.assertIn("cache: 'no-store'", js)


if __name__ == '__main__':
    unittest.main()
