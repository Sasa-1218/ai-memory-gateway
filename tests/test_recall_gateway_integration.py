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

    async def test_recall_helper_does_not_fetch_when_off(self):
        with (patch.object(main, "SHARED_RECALL_BUDGET_ENABLED", False),
              patch.object(main, "EXPERIENCE_RECALL_ENABLED", False),
              patch("database.retrieve_experience_recall", new=AsyncMock(return_value="CARD")) as retrieve):
            result = await main.build_experience_recall("烟花日", "6.14")
        self.assertEqual(result, "")
        self.assertEqual(retrieve.await_count, 0)


if __name__ == "__main__":
    unittest.main()
