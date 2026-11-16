"""Mocked tests of the legacy-resume transaction (not a real PostgreSQL test)."""
import ast
import json
import unittest
from pathlib import Path


def writer():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "database.py").read_text())
    method = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                  and n.name == "resume_legacy_partition_if_unchanged")
    scope = {}
    exec(compile(ast.fix_missing_locations(ast.Module([method], [])), "database.py", "exec"), scope)
    return scope


class Context:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *exc): return False


class Conn:
    def __init__(self, row):
        self.row = row
        self.snapshots = []
    def transaction(self): return Context(self)
    async def fetchrow(self, sql, *args): return dict(self.row) if self.row else None
    async def execute(self, sql, *args):
        assert "historical_summaries" in sql
        self.snapshots.append(json.loads(args[2]))
    async def fetchval(self, sql, *args):
        assert "UPDATE session_cache_state" in sql
        if self.row["summary_checkpoint_user_id"] is not None: return None
        self.row["summary_checkpoint_user_id"] = args[1]
        self.row["a_start_round"] = args[2]
        self.row["updated_at"] = 2
        return args[0]


class Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return Context(self.conn)


class LegacyResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = writer()
        self.row = dict(summary=json.dumps(["old"]), a_start_round=2420,
                        summary_checkpoint_user_id=None, updated_at=1)
        self.plan = dict(checkpoint_user_id=120, replayed_recent_rounds=8,
                         first_replay_round_index=2408,
                         history_rounds_at_migration=2416)
    async def run_writer(self, conn, plan=None):
        async def pool(): return Pool(conn)
        self.scope["get_pool"] = pool
        return await self.scope["resume_legacy_partition_if_unchanged"](
            "s", ["old"], 2420, 1, plan or self.plan)
    async def test_success_snapshots_and_preserves_summary(self):
        c = Conn(dict(self.row))
        self.assertTrue(await self.run_writer(c))
        self.assertEqual(c.row["summary"], self.row["summary"])
        self.assertEqual(c.row["a_start_round"], 2408)
        self.assertEqual(c.snapshots[0]["a_start_round"], 2420)
        self.assertEqual(c.row["summary_checkpoint_user_id"], 120)
        self.assertEqual(c.snapshots[0]["summary_parts"], ["old"])
    async def test_repeated_resume_is_noop(self):
        c = Conn(dict(self.row))
        self.assertTrue(await self.run_writer(c))
        self.assertFalse(await self.run_writer(c))
        self.assertEqual(len(c.snapshots), 1)
    async def test_stale_summary_is_noop(self):
        c = Conn(dict(self.row, summary=json.dumps(["changed"])))
        self.assertFalse(await self.run_writer(c))
        self.assertFalse(c.snapshots)
    async def test_stale_cursor_is_noop(self):
        c = Conn(dict(self.row, a_start_round=2400))
        self.assertFalse(await self.run_writer(c))
    async def test_stale_revision_is_noop(self):
        c = Conn(dict(self.row, updated_at=3))
        self.assertFalse(await self.run_writer(c))
    async def test_empty_state_is_noop(self):
        self.assertFalse(await self.run_writer(Conn(None)))
    async def test_invalid_replay_cursor_rejected(self):
        c = Conn(dict(self.row))
        with self.assertRaises(ValueError):
            await self.run_writer(c, dict(self.plan, first_replay_round_index=3000))
        self.assertEqual(c.row["a_start_round"], 2420)

    async def test_invalid_checkpoint_rejected(self):
        c = Conn(dict(self.row))
        with self.assertRaises(ValueError):
            await self.run_writer(c, {"checkpoint_user_id": -1})
        self.assertFalse(c.snapshots)


if __name__ == "__main__": unittest.main()
