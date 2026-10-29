import json
import unittest
from unittest.mock import AsyncMock, patch

import database
import main


IMAGE_CONTENT = [
    {"type": "text", "text": "看看这张图"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,TEST_IMAGE"}},
]


class MultimodalMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_database_restores_multimodal_content_from_metadata(self):
        row = {
            "role": "user",
            "content": "看看这张图",
            "metadata": json.dumps({"multimodal_content": IMAGE_CONTENT}),
        }

        message = database.db_row_to_message(row)

        self.assertEqual(message["content"], IMAGE_CONTENT)

    def test_old_plain_text_row_stays_compatible(self):
        message = database.db_row_to_message({
            "role": "user",
            "content": "旧消息",
            "metadata": None,
        })

        self.assertEqual(message, {"role": "user", "content": "旧消息"})

    async def test_basic_cache_preserves_image_and_adds_injection_block(self):
        with (
            patch.object(main, "MEMORY_ENABLED", True),
            patch.object(main, "MEMORY_EXTRACT_ENABLED", True),
            patch.object(main, "build_time_injection", return_value="TIME"),
            patch.object(main, "build_memory_text", new=AsyncMock(return_value="MEMORY")),
        ):
            result = await main._build_basic_cached(
                [], "", "看看这张图", {"role": "user", "content": IMAGE_CONTENT}
            )

        content = result[-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "TIME\n\nMEMORY"})
        self.assertEqual(content[1:], IMAGE_CONTENT)

    async def test_partitioned_cache_preserves_current_image_blocks(self):
        history = [
            {"role": "user", "content": "上一轮"},
            {"role": "assistant", "content": "上一轮回复"},
            {"role": "user", "content": IMAGE_CONTENT},
        ]
        with (
            patch.object(main, "CACHE_PARTITION_X", 1),
            patch.object(main, "MEMORY_ENABLED", False),
            patch.object(main, "build_time_injection", return_value="TIME"),
            patch.object(
                main,
                "get_session_cache_state",
                new=AsyncMock(return_value={"summary_parts": [], "a_start_round": 0}),
            ),
        ):
            result = await main.build_partitioned_messages(
                "session", history, "", "看看这张图"
            )

        content = result[-1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "TIME"})
        self.assertEqual(content[1:], IMAGE_CONTENT)

    def test_text_extraction_never_includes_image_payload(self):
        extracted = main._extract_text_content(IMAGE_CONTENT)

        self.assertEqual(extracted, "看看这张图")
        self.assertNotIn("TEST_IMAGE", extracted)

    async def test_image_only_message_is_saved_with_empty_text_content(self):
        image_only = IMAGE_CONTENT[1:]
        save = AsyncMock()
        with (
            patch.object(main, "save_message", new=save),
            patch.object(main, "get_last_user_content", new=AsyncMock(return_value="")),
            patch.object(main, "_record_operational_success_safe", new=AsyncMock()),
            patch.object(main, "MEMORY_EXTRACT_ENABLED", False),
            patch.object(main, "SHADOW_MIND_RULES_ENABLED", False),
        ):
            await main.process_memories_background(
                "session", "", "我看到了", "model", user_content=image_only
            )

        user_call = save.await_args_list[0]
        self.assertEqual(user_call.args[:4], ("session", "user", "", "model"))
        metadata = json.loads(user_call.kwargs["metadata"])
        self.assertEqual(metadata["multimodal_content"], image_only)

    async def test_image_message_never_uses_text_only_reroll_dedup(self):
        save = AsyncMock()
        update = AsyncMock(return_value=True)
        with (
            patch.object(main, "save_message", new=save),
            patch.object(main, "get_last_user_content", new=AsyncMock(return_value="看看这张图")),
            patch.object(main, "update_last_assistant_message", new=update),
            patch.object(main, "_record_operational_success_safe", new=AsyncMock()),
            patch.object(main, "MEMORY_EXTRACT_ENABLED", False),
            patch.object(main, "SHADOW_MIND_RULES_ENABLED", False),
        ):
            await main.process_memories_background(
                "session", "看看这张图", "新回复", "model", user_content=IMAGE_CONTENT
            )

        update.assert_not_awaited()
        self.assertEqual(save.await_count, 2)


class ConversationBackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_allows_image_only_message_with_metadata(self):
        image_only = IMAGE_CONTENT[1:]
        metadata = json.dumps({"multimodal_content": image_only})

        class FakeConn:
            def __init__(self):
                self.executed = []

            async def fetchrow(self, *args):
                return None

            async def execute(self, query, *args):
                self.executed.append((query, args))

        class AcquireContext:
            def __init__(self, conn):
                self.conn = conn

            async def __aenter__(self):
                return self.conn

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakePool:
            def __init__(self, conn):
                self.conn = conn

            def acquire(self):
                return AcquireContext(self.conn)

        conn = FakeConn()
        with patch.object(database, "get_pool", new=AsyncMock(return_value=FakePool(conn))):
            imported, skipped = await database.import_conversations([
                {
                    "session_id": "session",
                    "role": "user",
                    "content": "",
                    "model": "model",
                    "metadata": metadata,
                }
            ])

        self.assertEqual((imported, skipped), (1, 0))
        self.assertEqual(len(conn.executed), 1)
        self.assertIn("metadata", conn.executed[0][0])
        self.assertIn(metadata, conn.executed[0][1])


if __name__ == "__main__":
    unittest.main()
