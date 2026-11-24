"""Upgrade an actual legacy memories table in disposable CI PostgreSQL."""
import os
import unittest
from unittest.mock import patch

DSN="postgresql://test:test@127.0.0.1:5432/gateway_test"


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL")==DSN,
                     "Requires explicitly isolated gateway_test database")
class PawwakeMigrationPostgres(unittest.IsolatedAsyncioTestCase):
    async def test_old_memory_table_migrates_idempotently(self):
        import asyncpg
        import database
        admin=await asyncpg.connect(DSN)
        await admin.execute("DROP SCHEMA IF EXISTS pawwake_migration_test CASCADE")
        await admin.execute("CREATE SCHEMA pawwake_migration_test")
        await admin.close()
        pool=await asyncpg.create_pool(
            DSN,min_size=1,max_size=2,
            server_settings={"search_path":"pawwake_migration_test"})
        try:
            async with pool.acquire() as conn:
                await conn.execute("""CREATE TABLE memories(
                    id SERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    importance INTEGER DEFAULT 5,
                    source_session TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_accessed TIMESTAMPTZ DEFAULT NOW()
                )""")
                await conn.execute(
                    "INSERT INTO memories(content,importance,source_session) "
                    "VALUES('legacy memory',7,'s')"
                )
            with patch.object(database,"get_pool",return_value=pool):
                await database.init_tables()
                await database.init_tables()
            async with pool.acquire() as conn:
                row=await conn.fetchrow(
                    "SELECT content,is_active,is_pinned,source_message_ids "
                    "FROM memories WHERE content='legacy memory'"
                )
                self.assertEqual(row["content"],"legacy memory")
                self.assertTrue(row["is_active"])
                self.assertFalse(row["is_pinned"])
                self.assertIsNone(row["source_message_ids"])
                self.assertTrue(await conn.fetchval(
                    "SELECT to_regclass('pawwake_migration_test.conversation_recycle_bin') IS NOT NULL"))
                self.assertTrue(await conn.fetchval(
                    "SELECT to_regclass('pawwake_migration_test.idx_memories_pinned_active') IS NOT NULL"))
        finally:
            await pool.close()


if __name__=="__main__":
    unittest.main()
