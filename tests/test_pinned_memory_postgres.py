"""Pinned-memory read/write regression against disposable isolated PostgreSQL."""
import os
import unittest
from unittest.mock import patch

TEST_DSN="postgresql://test:test@127.0.0.1:5432/gateway_test"


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL")==TEST_DSN,
                     "Requires disposable CI PostgreSQL")
class PinnedMemoryPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        import database
        self.db=database
        admin=await asyncpg.connect(TEST_DSN)
        await admin.execute("CREATE SCHEMA IF NOT EXISTS pinned_test")
        await admin.close()
        self.pool=await asyncpg.create_pool(
            TEST_DSN,min_size=1,max_size=2,
            server_settings={"search_path":"pinned_test"})
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS memories")
            await conn.execute("""CREATE TABLE memories(
                id SERIAL PRIMARY KEY,content TEXT NOT NULL,
                importance INTEGER DEFAULT 5,
                is_active BOOLEAN DEFAULT TRUE,
                is_pinned BOOLEAN NOT NULL DEFAULT FALSE
            )""")
            await conn.execute("""
                INSERT INTO memories(id,content,importance,is_active,is_pinned)
                VALUES (1,'active',9,TRUE,FALSE),(2,'other',3,TRUE,TRUE),
                       (3,'archived',10,FALSE,TRUE)
            """)
        self.ctx=patch.object(database,"get_pool",return_value=self.pool)
        self.ctx.start()

    async def asyncTearDown(self):
        self.ctx.stop()
        await self.pool.close()

    async def test_explicit_pin_then_query_skips_archived(self):
        result=await self.db.set_memory_pinned(1,True)
        self.assertTrue(result["is_pinned"])
        rows=await self.db.get_pinned_memories()
        self.assertEqual([row["id"] for row in rows],[1,2])
        await self.db.set_memory_pinned(1,False)
        rows=await self.db.get_pinned_memories()
        self.assertEqual([row["id"] for row in rows],[2])

    async def test_cannot_pin_inactive_or_missing(self):
        self.assertIsNone(await self.db.set_memory_pinned(3,True))
        self.assertIsNone(await self.db.set_memory_pinned(999,True))

    async def test_boolean_id_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.db.set_memory_pinned(True,True)

    async def test_query_limit_is_bounded(self):
        self.assertEqual(len(await self.db.get_pinned_memories(1)),1)
        self.assertEqual(await self.db.get_pinned_memories(0),[])


if __name__=="__main__":
    unittest.main()
