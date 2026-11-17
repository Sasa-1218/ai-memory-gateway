"""Disposable PostgreSQL checks of bulk archived-fragment restoration.

The DSN guard prevents these destructive fixture resets from running on a
real Supabase or user database. No production data is accessed.
"""
import os
import unittest
from unittest.mock import patch
from fastapi import HTTPException

TEST_DSN = "postgresql://test:test@127.0.0.1:5432/gateway_test"


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL") == TEST_DSN, "disposable CI PostgreSQL only")
class BulkRestorePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        import memory_bulk_restore as bulk
        self.bulk = bulk
        self.pool = await asyncpg.create_pool(TEST_DSN, min_size=1, max_size=2)
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS memories")
            await conn.execute("""
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY, content TEXT NOT NULL,
                    layer INTEGER DEFAULT 1, is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    merged_from INTEGER[]
                )
            """)
            await conn.execute("""
                INSERT INTO memories(id,content,layer,is_active,merged_from)
                VALUES
                (1,'亲密细节A',1,FALSE,NULL),
                (2,'亲密细节B',1,FALSE,NULL),
                (3,'原本就活跃',1,TRUE,NULL),
                (4,'合并事件',2,TRUE,ARRAY[1,2]::integer[]),
                (5,'另一个事件',2,TRUE,ARRAY[2]::integer[])
            """)
        self._p = patch.object(bulk, "get_pool", return_value=self.pool)
        self._p.start()

    async def asyncTearDown(self):
        self._p.stop()
        await self.pool.close()

    async def test_restore_two_preserves_original_and_both_events(self):
        before = await self.bulk.preview([1, 2])
        self.assertEqual(before["restorable"], 2)
        self.assertEqual(len(before["associated_events"]), 2)
        result = await self.bulk.restore([1, 2], before["state_token"], "确认批量恢复")
        self.assertEqual(result["restored_ids"], [1, 2])
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT id,content,layer,is_active,merged_from FROM memories ORDER BY id")
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4, 5])
        self.assertTrue(rows[0]["is_active"] and rows[1]["is_active"])
        self.assertEqual(rows[0]["content"], "亲密细节A")
        self.assertEqual(rows[3]["merged_from"], [1, 2])
        self.assertEqual(rows[4]["merged_from"], [2])

    async def test_missing_id_is_reported_and_atomic_restore_rejected(self):
        preview = await self.bulk.preview([1, 9999])
        self.assertEqual(preview["missing_ids"], [9999])
        with self.assertRaises(HTTPException) as cm:
            await self.bulk.restore([1, 9999], preview["state_token"], "确认批量恢复")
        self.assertEqual(cm.exception.status_code, 409)
        async with self.pool.acquire() as conn:
            self.assertFalse(await conn.fetchval("SELECT is_active FROM memories WHERE id=1"))

    async def test_active_or_event_id_rejected_without_changes(self):
        preview = await self.bulk.preview([1, 3, 4])
        with self.assertRaises(HTTPException):
            await self.bulk.restore([1, 3, 4], preview["state_token"], "确认批量恢复")
        async with self.pool.acquire() as conn:
            self.assertFalse(await conn.fetchval("SELECT is_active FROM memories WHERE id=1"))

    async def test_stale_preview_rejected(self):
        preview = await self.bulk.preview([1, 2])
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE memories SET content='已编辑' WHERE id=2")
        with self.assertRaises(HTTPException) as cm:
            await self.bulk.restore([1, 2], preview["state_token"], "确认批量恢复")
        self.assertEqual(cm.exception.status_code, 409)
        async with self.pool.acquire() as conn:
            self.assertFalse(await conn.fetchval("SELECT is_active FROM memories WHERE id=1"))

    async def test_parent_event_change_rejected(self):
        preview = await self.bulk.preview([1, 2])
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE memories SET merged_from=ARRAY[1]::integer[] WHERE id=4")
        with self.assertRaises(HTTPException) as cm:
            await self.bulk.restore([1, 2], preview["state_token"], "确认批量恢复")
        self.assertEqual(cm.exception.status_code, 409)

    async def test_duplicate_or_invalid_ids_and_confirm_rejected(self):
        for ids in ([1, 1], [True], [], list(range(1, 502))):
            with self.assertRaises(HTTPException):
                await self.bulk.preview(ids)
        preview = await self.bulk.preview([1])
        with self.assertRaises(HTTPException):
            await self.bulk.restore([1], preview["state_token"], "yes")

    async def test_repeat_restore_refused(self):
        preview = await self.bulk.preview([1])
        await self.bulk.restore([1], preview["state_token"], "确认批量恢复")
        with self.assertRaises(HTTPException):
            await self.bulk.restore([1], preview["state_token"], "确认批量恢复")
