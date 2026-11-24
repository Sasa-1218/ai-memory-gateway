"""Verified source IDs may be linked only after DB validates original messages."""
import os
import unittest
from unittest.mock import patch

DSN="postgresql://test:test@127.0.0.1:5432/gateway_test"


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL")==DSN, "Disposable CI database required")
class VerifiedSourcesPostgres(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        import database
        self.db=database
        admin=await asyncpg.connect(DSN)
        await admin.execute("CREATE SCHEMA IF NOT EXISTS provenance_test")
        await admin.close()
        self.pool=await asyncpg.create_pool(
            DSN,min_size=1,max_size=2,
            server_settings={"search_path":"provenance_test"})
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS memories")
            await conn.execute("DROP TABLE IF EXISTS conversations")
            await conn.execute("CREATE TABLE conversations(id BIGINT PRIMARY KEY,session_id TEXT NOT NULL)")
            await conn.execute("""CREATE TABLE memories(
                id INTEGER PRIMARY KEY, source_session TEXT,
                source_message_ids BIGINT[]
            )""")
            await conn.execute("INSERT INTO memories VALUES(1,'s',NULL)")
            await conn.execute("INSERT INTO conversations VALUES(11,'s'),(12,'s'),(13,'other')")
        self.ctx=patch.object(database,"get_pool",return_value=self.pool)
        self.ctx.start()

    async def asyncTearDown(self):
        self.ctx.stop()
        await self.pool.close()

    async def test_link_then_read_verified_message_ids(self):
        result=await self.db.attach_verified_memory_sources(1,"s",[12,11,12])
        self.assertEqual(result["source_message_ids"],[11,12])
        report=await self.db.get_verified_memory_sources_readonly(1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["source_message_ids"],[11,12])
        info=await self.db.get_memory_source_info([1])
        self.assertEqual(info[1]["source_message_ids"],[11,12])

    async def test_cross_session_source_rejected_before_any_update(self):
        with self.assertRaisesRegex(ValueError,"source_messages_do_not_match_session"):
            await self.db.attach_verified_memory_sources(1,"s",[11,13])
        report=await self.db.get_verified_memory_sources_readonly(1)
        self.assertFalse(report["complete"])

    async def test_deleted_source_fails_closed(self):
        await self.db.attach_verified_memory_sources(1,"s",[11,12])
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM conversations WHERE id=12")
        report=await self.db.get_verified_memory_sources_readonly(1)
        self.assertFalse(report["complete"])
        info=await self.db.get_memory_source_info([1])
        self.assertIsNone(info[1]["source_message_ids"])

    async def test_no_automatic_backfill_for_legacy(self):
        self.assertFalse((await self.db.get_verified_memory_sources_readonly(1))["complete"])


if __name__=="__main__":
    unittest.main()
