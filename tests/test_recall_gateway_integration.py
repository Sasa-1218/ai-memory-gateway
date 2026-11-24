"""Mocked gateway-path regression for opt-in experience and recall budgets.

These cases do not connect to PostgreSQL or call model providers.
"""
import unittest
from unittest.mock import AsyncMock, patch

import main


class GatewayRecallIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_toggle_turns_on_budget_and_preserves_image(self):
        image_block = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
        current = {"role": "user", "content": [
            {"type": "text", "text": "还记得烟花吗？"}, image_block,
        ]}
        memory = "<脑海中浮现的既往事实>\n- 重要的日常细节\n</脑海中浮现的既往事实>"
        with (patch.object(main, "SHARED_RECALL_BUDGET_ENABLED", False),
              patch.object(main, "EXPERIENCE_RECALL_ENABLED", True),
              patch.object(main, "MEMORY_ENABLED", True),
              patch.object(main, "MEMORY_EXTRACT_ENABLED", True),
              patch.object(main, "SHARED_RECALL_MAX_CHARS", 120),
              patch.object(main, "get_active_session_id", return_value="6.14"),
              patch.object(main, "build_time_injection", return_value="NOW"),
              patch.object(main, "build_experience_recall", new=AsyncMock(return_value="CARD")) as card,
              patch.object(main, "build_memory_text", new=AsyncMock(return_value=memory)) as mem):
            result = await main._build_basic_cached([], "", "烟花", current)
        self.assertEqual(len(result), 1)
        blocks = result[0]["content"]
        self.assertEqual(blocks[-1], image_block)
        self.assertEqual(blocks[1], current["content"][0])
        self.assertIn("CARD", blocks[0]["text"])
        self.assertIn("- 重要的日常细节", blocks[0]["text"])
        self.assertEqual(mem.await_args.kwargs["max_chars"], 114)
        self.assertEqual(card.await_count, 1)

    async def test_memory_only_default_uses_legacy_path(self):
        current = {"role": "user", "content": "晚饭吃什么"}
        with (patch.object(main, "SHARED_RECALL_BUDGET_ENABLED", False),
              patch.object(main, "EXPERIENCE_RECALL_ENABLED", False),
              patch.object(main, "MEMORY_ENABLED", True),
              patch.object(main, "MEMORY_EXTRACT_ENABLED", True),
              patch.object(main, "build_time_injection", return_value="NOW"),
              patch.object(main, "build_experience_recall", new=AsyncMock(return_value="")) as card,
              patch.object(main, "build_memory_text", new=AsyncMock(return_value="MEM")) as mem):
            result = await main._build_basic_cached([], "", "晚饭", current)
        self.assertIn("MEM", result[-1]["content"])
        self.assertNotIn("max_chars", mem.await_args.kwargs)
        self.assertEqual(card.await_count, 0)

    async def test_recall_helper_uses_one_card_with_cap_when_dashboard_enabled(self):
        with (patch.object(main, "SHARED_RECALL_BUDGET_ENABLED", False),
              patch.object(main, "EXPERIENCE_RECALL_ENABLED", True),
              patch.object(main, "SHARED_RECALL_CARD_MAX_CHARS", 600),
              patch("database.retrieve_experience_recall", new=AsyncMock(return_value="CARD")) as retrieve):
            result = await main.build_experience_recall("烟花日", "6.14")
        self.assertEqual(result, "CARD")
        self.assertEqual(retrieve.await_args.args[3], 1)
        self.assertEqual(retrieve.await_args.kwargs["max_chars"], 600)

    async def test_pinned_memory_floats_when_normal_search_empty(self):
        rows = [{"id": 7, "content": "记住我的小细节", "importance": 10,
                 "is_active": True, "is_pinned": True}]
        with (patch.object(main, "MEMORY_PINNED_ENABLED", True),
              patch.object(main, "MAX_MEMORIES_INJECT", 15),
              patch.object(main, "MEMORY_ENABLED", True),
              patch.object(main, "MEMORY_EXTRACT_ENABLED", True),
              patch("database.get_pinned_memories", new=AsyncMock(return_value=rows)),
              patch.object(main, "search_memories", new=AsyncMock(return_value=[]))):
            result = await main.build_memory_text("不相关内容", max_chars=6000)
        self.assertIn("记住我的小细节", result)
        self.assertIn("<固定记忆", result)

    async def test_rerank_success_or_provider_failure(self):
        original = [{"id": 1, "content": "A"}, {"id": 2, "content": "B"}]
        with (patch.object(main, "MEMORY_RERANK_ENABLED", True),
              patch.object(main, "MEMORY_RERANK_MODEL", "demo-model"),
              patch("recall_rerank.rerank", new=AsyncMock(return_value=[original[1]])) as ranked):
            filtered = await main._rerank_memory_candidates("B是什么", original)
            self.assertEqual(filtered, [original[1]])
            self.assertEqual(ranked.await_args.args[-1], "memory")
        with (patch.object(main, "MEMORY_RERANK_ENABLED", True),
              patch.object(main, "MEMORY_RERANK_MODEL", "demo-model"),
              patch("recall_rerank.rerank", new=AsyncMock(return_value=None))):
            filtered = await main._rerank_memory_candidates("B是什么", original)
            self.assertEqual(filtered, original)

    async def test_rerank_default_off_avoids_paid_call(self):
        with (patch.object(main, "MEMORY_RERANK_ENABLED", False),
              patch("recall_rerank.rerank", new=AsyncMock()) as ranked):
            rows = [{"id": 1, "content": "A"}]
            result = await main._rerank_memory_candidates("A", rows)
            self.assertEqual(result, rows)
            ranked.assert_not_awaited()

    async def test_verified_source_dedup_keeps_earliest_rank(self):
        records=[{"id":1,"content":"one important fact"},
                 {"id":2,"content":"one important fact"}]
        sources={1:{"source_session":"s","source_message_ids":[11]},
                 2:{"source_session":"s","source_message_ids":[11]}}
        with (patch.object(main,"MEMORY_DEDUP_ENABLED",True),
              patch("database.get_memory_source_info",new=AsyncMock(return_value=sources))):
            output=await main._deduplicate_verified_memory_candidates(records)
        self.assertEqual([r["id"] for r in output],[1])

    async def test_dedup_unknown_sessions_keeps_distinct_facts(self):
        records=[{"id":1,"content":"the same text"},
                 {"id":2,"content":"the same text"}]
        with (patch.object(main,"MEMORY_DEDUP_ENABLED",True),
              patch("database.get_memory_source_info",new=AsyncMock(return_value={}))):
            output=await main._deduplicate_verified_memory_candidates(records)
        self.assertEqual([r["id"] for r in output],[1,2])

    async def test_dedup_off_has_no_source_db_read(self):
        rows=[{"id":1,"content":"fact"}]
        with (patch.object(main,"MEMORY_DEDUP_ENABLED",False),
              patch("database.get_memory_source_info",new=AsyncMock()) as get):
            self.assertEqual(await main._deduplicate_verified_memory_candidates(rows),rows)
        get.assert_not_awaited()

    async def test_recall_helper_does_not_fetch_when_off(self):
        with (patch.object(main, "SHARED_RECALL_BUDGET_ENABLED", False),
              patch.object(main, "EXPERIENCE_RECALL_ENABLED", False),
              patch("database.retrieve_experience_recall", new=AsyncMock(return_value="CARD")) as retrieve):
            result = await main.build_experience_recall("烟花日", "6.14")
        self.assertEqual(result, "")
        self.assertEqual(retrieve.await_count, 0)


if __name__ == "__main__":
    unittest.main()
