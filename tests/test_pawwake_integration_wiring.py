"""Static regression guards: Pawwake adaptations must stay wired end to end."""
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


class PawwakeIntegrationWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main=(ROOT/"main.py").read_text(encoding="utf-8")
        cls.database=(ROOT/"database.py").read_text(encoding="utf-8")
        cls.js=(ROOT/"static/js/dashboard.js").read_text(encoding="utf-8")
        cls.html=(ROOT/"templates/dashboard.html").read_text(encoding="utf-8")

    def test_pins_have_schema_write_ui_and_injection(self):
        self.assertIn("ALTER TABLE memories ADD COLUMN IF NOT EXISTS is_pinned",self.database)
        self.assertIn("async def set_memory_pinned(",self.database)
        self.assertIn("@app.put(\"/api/memories/{memory_id}/pin\")",self.main)
        self.assertIn("togglePinnedMemory(",self.js)
        self.assertIn("set-MEMORY_PINNED_ENABLED",self.html)
        self.assertEqual(self.main.count("pinned_ids = await _read_pinned_memory_text("),2)

    def test_recall_alias_rerank_dedup_in_both_paths(self):
        self.assertEqual(self.main.count("_memory_query_with_aliases(user_message)"),2)
        self.assertEqual(self.main.count("_rerank_memory_candidates(user_message, memories)"),2)
        self.assertEqual(self.main.count("_deduplicate_verified_memory_candidates(memories)"),2)
        for key in ("MEMORY_RERANK_ENABLED","MEMORY_DEDUP_ENABLED","MEMORY_QUERY_ALIASES_JSON"):
            self.assertIn("set-"+key,self.html)

    def test_recycle_restore_and_confirmed_purge(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS conversation_recycle_bin",self.database)
        self.assertIn("async def restore_recycled_conversation(",self.database)
        self.assertIn('confirm: bool = False',self.main)
        self.assertIn('conversationRecycleList',self.html)
        self.assertIn('restoreConversationFromRecycle(',self.js)

    def test_provenance_migration_is_nullable_and_checked(self):
        self.assertIn("ALTER TABLE memories ADD COLUMN IF NOT EXISTS source_message_ids BIGINT[]",self.database)
        self.assertIn("async def attach_verified_memory_sources(",self.database)
        self.assertIn("source_messages_do_not_match_session",self.database)
        self.assertIn("@app.get(\"/api/memories/{memory_id}/sources\")",self.main)

    def test_mcp_remains_opt_in_and_requires_secret(self):
        self.assertIn('MCP_READONLY_ENABLED = os.getenv("MCP_READONLY_ENABLED", "false")',self.main)
        self.assertIn("if not GATEWAY_SECRET:",self.main)
        self.assertIn("secrets.compare_digest(presented, GATEWAY_SECRET)",self.main)
        self.assertIn("from mcp_readonly import dispatch",self.main)

    def test_migration_index_after_legacy_is_active(self):
        self.assertLess(
            self.database.index("ALTER TABLE memories ADD COLUMN is_active BOOLEAN DEFAULT TRUE;"),
            self.database.index("CREATE INDEX IF NOT EXISTS idx_memories_pinned_active"),
        )


if __name__=="__main__":
    unittest.main()
