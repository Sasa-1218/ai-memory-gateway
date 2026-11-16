"""Real PostgreSQL transaction tests; CI-only disposable gateway_test database.

Never use a production DSN. The exact local test DSN is required on purpose.
"""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import HTTPException

TEST_DSN = "postgresql://test:test@127.0.0.1:5432/gateway_test"


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL") == TEST_DSN,
                     "disposable local PostgreSQL test service not available")
class PostgresBoundaryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        import summary_boundary_realign as boundary
        self.boundary = boundary
        self.pool = await asyncpg.create_pool(dsn=TEST_DSN, min_size=1, max_size=2)
        # Only the hardcoded gateway_test CI database can reach this code.
        async with self.pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS historical_summaries")
            await conn.execute("DROP TABLE IF EXISTS session_cache_state")
            await conn.execute("DROP TABLE IF EXISTS conversations")
            await conn.execute("""
                CREATE TABLE conversations (
                    id BIGINT PRIMARY KEY, session_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT,
                    metadata TEXT, created_at TIMESTAMPTZ NOT NULL
                )
            """)
            await conn.execute("""
                CREATE TABLE session_cache_state (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT DEFAULT '',
                    a_start_round INTEGER DEFAULT 0,
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    summary_checkpoint_user_id BIGINT
                )
            """)
            await conn.execute("""
                CREATE TABLE historical_summaries (
                    id BIGSERIAL PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    period_label TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual_backup',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            self.original = json.dumps(["existing complete summary"], ensure_ascii=False)
            await conn.execute(
                "INSERT INTO session_cache_state(session_id,summary,a_start_round) VALUES($1,$2,$3)",
                "test-session", self.original, 999,
            )
            for i in range(1, 13):
                await conn.execute(
                    "INSERT INTO conversations(id,session_id,role,content,metadata,created_at) "
                    "VALUES($1,$2,'user',$3,NULL,$4)",
                    i * 10, "test-session", f"turn {i}",
                    datetime(2026, 11, 17, tzinfo=timezone.utc) + timedelta(minutes=i),
                )
        self.pool_patch = patch.object(self.boundary, "get_pool")
        self.get_pool = self.pool_patch.start()
        self.get_pool.return_value = self.pool

    async def asyncTearDown(self):
        self.pool_patch.stop()
        await self.pool.close()

    async def test_snapshot_confirm_resume_and_rollback_real_sql(self):
        import summary_repair
        before = await self.boundary.preview("test-session")
        self.assertEqual(before["total_persisted_users"], 12)
        self.assertEqual(before["stored_cursor"], 999)
        self.assertTrue(before["requires_alignment"])
        result = await self.boundary.confirm(
            "test-session", 50, before["state_token"], "确认摘要截止位置")
        self.assertEqual(result["checkpoint_user_id"], 40)
        self.assertEqual(result["effective_a_start_round"], 4)
        self.assertFalse(result["summary_content_changed"])
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT summary,a_start_round,summary_checkpoint_user_id FROM session_cache_state "
                "WHERE session_id='test-session'")
            self.assertEqual(row["summary"], self.original)
            self.assertEqual(row["a_start_round"], 4)
            self.assertEqual(row["summary_checkpoint_user_id"], 40)
            self.assertEqual(await conn.fetchval(
                "SELECT COUNT(*) FROM conversations WHERE session_id='test-session'"), 12)
            snapshot = await conn.fetchrow(
                "SELECT content,source FROM historical_summaries WHERE id=$1", result["snapshot_id"])
            self.assertEqual(snapshot["source"], "repair_snapshot")
            snap = json.loads(snapshot["content"])
            self.assertEqual(snap["a_start_round"], 999)
            self.assertIsNone(snap["summary_checkpoint_user_id"])
        # Verify the existing rollback API restores the exact pre-alignment state.
        with patch.object(summary_repair, "get_pool", return_value=self.pool):
            rolled = await summary_repair.rollback(
                "test-session", result["snapshot_id"], summary_repair.ROLLBACK_CONFIRM)
        self.assertEqual(rolled["status"], "rolled_back")
        async with self.pool.acquire() as conn:
            restored = await conn.fetchrow(
                "SELECT summary,a_start_round,summary_checkpoint_user_id FROM session_cache_state "
                "WHERE session_id='test-session'")
            self.assertEqual(restored["summary"], self.original)
            self.assertEqual(restored["a_start_round"], 999)
            self.assertIsNone(restored["summary_checkpoint_user_id"])

    async def test_changed_history_rejects_commit_without_snapshot(self):
        before = await self.boundary.preview("test-session")
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE conversations SET content=$1 WHERE id=20",
                               "edited after preview")
        with self.assertRaises(HTTPException) as failure:
            await self.boundary.confirm(
                "test-session", 50, before["state_token"], "确认摘要截止位置")
        self.assertEqual(failure.exception.status_code, 409)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval(
                "SELECT COUNT(*) FROM historical_summaries"), 0)
            self.assertIsNone(await conn.fetchval(
                "SELECT summary_checkpoint_user_id FROM session_cache_state "
                "WHERE session_id='test-session'"))

    async def test_double_confirmation_is_rejected_transactionally(self):
        before = await self.boundary.preview("test-session")
        await self.boundary.confirm(
            "test-session", 50, before["state_token"], "确认摘要截止位置")
        with self.assertRaises(HTTPException) as failure:
            await self.boundary.confirm(
                "test-session", 50, before["state_token"], "确认摘要截止位置")
        self.assertEqual(failure.exception.status_code, 409)
        async with self.pool.acquire() as conn:
            self.assertEqual(await conn.fetchval(
                "SELECT COUNT(*) FROM historical_summaries"), 1)


if __name__ == "__main__":
    unittest.main()
