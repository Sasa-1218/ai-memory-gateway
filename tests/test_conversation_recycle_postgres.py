"""Real PostgreSQL recycle restore transaction tests on disposable CI database only."""
import json
import os
import unittest
from unittest.mock import patch

TEST_DSN = "postgresql://test:test@127.0.0.1:5432/gateway_test"


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL") == TEST_DSN,
                     "Requires disposable CI PostgreSQL service")
class RecyclePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        import database
        self.db = database
        admin = await asyncpg.connect(TEST_DSN)
        await admin.execute("CREATE SCHEMA IF NOT EXISTS recycle_test")
        await admin.close()
        async def setup(conn):
            await conn.execute("SET search_path TO recycle_test")
        self.pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=2, init=setup)
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS conversation_recycle_bin")
            await conn.execute("DROP TABLE IF EXISTS session_cache_state")
            await conn.execute("DROP TABLE IF EXISTS conversations")
            await conn.execute("""CREATE TABLE conversations (
                id SERIAL PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT, model TEXT, created_at TIMESTAMPTZ DEFAULT NOW(),
                metadata TEXT
            )""")
            await conn.execute("""CREATE TABLE session_cache_state (
                session_id TEXT PRIMARY KEY, summary TEXT DEFAULT '',
                a_start_round INTEGER DEFAULT 0, updated_at TIMESTAMPTZ DEFAULT NOW(),
                summary_checkpoint_user_id BIGINT
            )""")
            await conn.execute("""CREATE TABLE conversation_recycle_bin (
                id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL,
                messages JSONB NOT NULL, cache_state JSONB,
                deleted_at TIMESTAMPTZ DEFAULT NOW(), restored_at TIMESTAMPTZ
            )""")
            metadata = json.dumps({"multimodal_content": [
                {"type":"text","text":"看图"},
                {"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]})
            await conn.execute(
                "INSERT INTO conversations(id,session_id,role,content,model,metadata) "
                "VALUES(101,'s','user','看图','test',$1)", metadata)
            await conn.execute(
                "INSERT INTO conversations(id,session_id,role,content,model,metadata) "
                "VALUES(102,'s','assistant','好的','test',$1)",
                json.dumps({"tool_calls":[{"id":"tool-1"}]}))
            await conn.execute(
                "INSERT INTO session_cache_state(session_id,summary,a_start_round,summary_checkpoint_user_id) "
                "VALUES('s',$1,12,101)", json.dumps(["完整的历史摘要"],ensure_ascii=False))
        self.patch = patch.object(database, "get_pool", return_value=self.pool)
        self.patch.start()

    async def asyncTearDown(self):
        self.patch.stop()
        await self.pool.close()

    async def test_round_trip_preserves_images_tool_metadata_and_summary(self):
        archive_id = await self.db.delete_conversation("s")
        self.assertIsInstance(archive_id,int)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval(
                "SELECT count(*) FROM conversations WHERE session_id='s'"),0)
            self.assertEqual(await conn.fetchval(
                "SELECT count(*) FROM session_cache_state WHERE session_id='s'"),0)
        listing = await self.db.list_recycled_conversations()
        self.assertEqual(listing[0]["message_count"],2)
        result = await self.db.restore_recycled_conversation(archive_id)
        self.assertEqual(result["session_id"],"s")
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id,metadata FROM conversations ORDER BY id")
            self.assertEqual([row["id"] for row in rows],[101,102])
            self.assertIn("image_url",rows[0]["metadata"])
            self.assertIn("tool_calls",rows[1]["metadata"])
            cache = await conn.fetchrow("SELECT * FROM session_cache_state WHERE session_id='s'")
            self.assertEqual(cache["a_start_round"],12)
            self.assertEqual(cache["summary_checkpoint_user_id"],101)
            self.assertIn("完整的历史摘要",cache["summary"])
        self.assertIsNone(await self.db.restore_recycled_conversation(archive_id))

    async def test_restore_rejects_reused_session_without_undoing_snapshot(self):
        archive_id = await self.db.delete_conversation("s")
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO conversations(id,session_id,role,content) VALUES(103,'s','user','新的')"
            )
        with self.assertRaisesRegex(ValueError,"session_id_already_in_use"):
            await self.db.restore_recycled_conversation(archive_id)
        self.assertEqual(len(await self.db.list_recycled_conversations()),1)

    async def test_purge_is_separate_and_final(self):
        archive_id = await self.db.delete_conversation("s")
        self.assertTrue(await self.db.purge_recycled_conversation(archive_id))
        self.assertFalse(await self.db.purge_recycled_conversation(archive_id))
        self.assertIsNone(await self.db.restore_recycled_conversation(archive_id))


if __name__ == "__main__":
    unittest.main()
