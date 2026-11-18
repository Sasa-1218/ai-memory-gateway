"""Exercise actual chat message constructors with experience recall enabled/off."""
import unittest
from unittest.mock import AsyncMock, patch
import main

class ExperienceChatConstructorTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_chat_includes_approved_recall_and_keeps_user_text(self):
        with (
            patch.object(main, "EXPERIENCE_RECALL_ENABLED", True),
            patch.object(main, "MEMORY_ENABLED", False),
            patch.object(main, "build_time_injection", return_value="TIME"),
            patch.object(main, "get_active_session_id", return_value="6.14"),
            patch.object(main, "build_experience_recall", new=AsyncMock(return_value="【经历卡】猫耳簪")),
        ):
            result = await main._build_basic_cached([], "", "猫耳簪", {"role":"user","content":"猫耳簪"})
        content = result[-1]["content"]
        self.assertIn("【经历卡】猫耳簪", str(content))
        self.assertIn("猫耳簪", str(content))

    async def test_partitioned_chat_includes_approved_recall(self):
        history=[{"role":"user","content":"猫耳簪"}]
        with (
            patch.object(main, "EXPERIENCE_RECALL_ENABLED", True),
            patch.object(main, "MEMORY_ENABLED", False),
            patch.object(main, "CACHE_PARTITION_X", 8),
            patch.object(main, "build_time_injection", return_value="TIME"),
            patch.object(main, "get_session_cache_state",new=AsyncMock(return_value={"summary_parts":[],"a_start_round":0})),
            patch.object(main, "build_experience_recall",new=AsyncMock(return_value="【经历卡】猫耳簪")),
        ):
            result=await main.build_partitioned_messages("6.14",history,"","猫耳簪")
        self.assertIn("【经历卡】猫耳簪",str(result[-1]["content"]))

    async def test_disabled_does_not_call_retrieval(self):
        retrieve=AsyncMock(return_value="SHOULD_NOT_APPEAR")
        with (
            patch.object(main, "EXPERIENCE_RECALL_ENABLED", False),
            patch.object(main, "MEMORY_ENABLED", False),
            patch.object(main, "build_time_injection",return_value="TIME"),
            patch.object(main, "build_experience_recall",new=retrieve),
        ):
            result=await main._build_basic_cached([],"","猫耳簪",{"role":"user","content":"猫耳簪"})
        retrieve.assert_not_awaited()
        self.assertNotIn("SHOULD_NOT_APPEAR",str(result))

if __name__=="__main__":
    unittest.main()
