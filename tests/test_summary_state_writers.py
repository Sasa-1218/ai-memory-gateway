"""Behavioral mock tests for exact summary writers extracted from database.py.

No Supabase credentials, service startup, or network required.
"""
import ast
import json
import unittest
from pathlib import Path


def load_writers():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "database.py").read_text(encoding="utf-8"))
    wanted = {
        "save_partition_summary_if_unchanged",
        "replace_manual_summary_if_unchanged",
    }
    methods = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
               and node.name in wanted]
    assert len(methods) == len(wanted), "missing summary writer"
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])),
                 str(root / "database.py"), "exec"), namespace)
    return namespace


class _Context:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _Context(self.connection)


class _Connection:
    def __init__(self, state=None, has_history=False):
        self.state = dict(state) if state else None
        self.has_history = has_history
        self.snapshots = []

    def transaction(self):
        return _Context(self)

    async def fetchrow(self, sql, *args):
        return dict(self.state) if self.state is not None else None

    async def fetchval(self, sql, *args):
        if "SELECT EXISTS" in sql:
            return self.has_history
        if "UPDATE session_cache_state" in sql:
            sid, old_json, old_cursor, old_checkpoint, new_json, new_cursor, new_checkpoint, raw_old, revision = args
            row = self.state
            if row is None:
                return None
            matches_summary = (
                row["summary"] == old_json or
                (not row["summary"].strip() and old_json == "[]") or
                (raw_old is not None and row["summary"] == raw_old)
            )
            if not (matches_summary and row["a_start_round"] == old_cursor
                    and row["summary_checkpoint_user_id"] == old_checkpoint
                    and row["updated_at"] == revision):
                return None
            row.update(summary=new_json, a_start_round=new_cursor,
                       summary_checkpoint_user_id=new_checkpoint,
                       updated_at=revision + 1)
            return sid
        if "INSERT INTO session_cache_state" in sql and "DO NOTHING" in sql:
            if self.state is not None:
                return None
            sid, summary, cursor, checkpoint = args
            self.state = dict(summary=summary, a_start_round=cursor,
                              summary_checkpoint_user_id=checkpoint, updated_at=1)
            return sid
        raise AssertionError("unexpected fetchval SQL: " + sql[:100])

    async def execute(self, sql, *args):
        if "INSERT INTO historical_summaries" in sql:
            self.snapshots.append(json.loads(args[2]))
            return "INSERT 0 1"
        if "INSERT INTO session_cache_state" in sql:
            self.state = dict(summary=args[1], a_start_round=0,
                              summary_checkpoint_user_id=args[2], updated_at=1)
            return "INSERT 0 1"
        if "UPDATE session_cache_state" in sql:
            self.state.update(summary=args[1], a_start_round=args[2],
                              summary_checkpoint_user_id=args[3],
                              updated_at=self.state["updated_at"] + 1)
            return "UPDATE 1"
        raise AssertionError("unexpected execute SQL: " + sql[:100])


class SummaryStateWriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_writers()

    def connect(self, connection):
        async def pool():
            return _Pool(connection)
        self.ns["get_pool"] = pool
        return connection

    async def test_first_rotation_creates_missing_cache_row(self):
        conn = self.connect(_Connection())
        ok = await self.ns["save_partition_summary_if_unchanged"](
            "s", [], 0, None, ["block"], 8, 29, expected_updated_at=None)
        self.assertTrue(ok)
        self.assertEqual(json.loads(conn.state["summary"]), ["block"])
        self.assertEqual(conn.state["summary_checkpoint_user_id"], 29)

    async def test_old_empty_string_summary_does_not_require_jsonb_cast(self):
        conn = self.connect(_Connection(dict(
            summary="", a_start_round=0,
            summary_checkpoint_user_id=None, updated_at=4)))
        ok = await self.ns["save_partition_summary_if_unchanged"](
            "s", [], 0, None, ["block"], 8, 29, expected_updated_at=4)
        self.assertTrue(ok)
        self.assertEqual(conn.state["a_start_round"], 8)

    async def test_stale_content_cannot_overwrite(self):
        original = dict(summary=json.dumps(["newer"]), a_start_round=8,
                        summary_checkpoint_user_id=29, updated_at=4)
        conn = self.connect(_Connection(original))
        ok = await self.ns["save_partition_summary_if_unchanged"](
            "s", ["older"], 8, 29, ["stale write"], 16, 61,
            expected_updated_at=4)
        self.assertFalse(ok)
        self.assertEqual(conn.state["summary"], original["summary"])

    async def test_changed_revision_cannot_overwrite(self):
        conn = self.connect(_Connection(dict(
            summary=json.dumps(["same"]), a_start_round=8,
            summary_checkpoint_user_id=29, updated_at=5)))
        ok = await self.ns["save_partition_summary_if_unchanged"](
            "s", ["same"], 8, 29, ["stale"], 16, 61,
            expected_updated_at=4)
        self.assertFalse(ok)

    async def test_manual_replace_keeps_checkpoint_and_makes_snapshot(self):
        conn = self.connect(_Connection(dict(
            summary=json.dumps(["old"]), a_start_round=8,
            summary_checkpoint_user_id=29, updated_at=5)))
        ok = await self.ns["replace_manual_summary_if_unchanged"](
            "s", ["old"], 8, 29, ["new"])
        self.assertTrue(ok)
        self.assertEqual(conn.state["summary_checkpoint_user_id"], 29)
        self.assertEqual(conn.snapshots[0]["summary_checkpoint_user_id"], 29)
        self.assertEqual(conn.snapshots[0]["summary_parts"], ["old"])

    async def test_manual_conflict_never_overwrites_or_snapshots(self):
        conn = self.connect(_Connection(dict(
            summary=json.dumps(["changed"]), a_start_round=8,
            summary_checkpoint_user_id=29, updated_at=5)))
        ok = await self.ns["replace_manual_summary_if_unchanged"](
            "s", ["old"], 8, 29, ["replacement"])
        self.assertFalse(ok)
        self.assertEqual(conn.snapshots, [])
        self.assertEqual(json.loads(conn.state["summary"]), ["changed"])

    async def test_manual_clear_resets_both_watermarks(self):
        conn = self.connect(_Connection(dict(
            summary=json.dumps(["old"]), a_start_round=8,
            summary_checkpoint_user_id=29, updated_at=5)))
        ok = await self.ns["replace_manual_summary_if_unchanged"](
            "s", ["old"], 8, 29, [])
        self.assertTrue(ok)
        self.assertEqual(conn.state["a_start_round"], 0)
        self.assertIsNone(conn.state["summary_checkpoint_user_id"])

    async def test_manual_clean_thread_starts_with_zero_watermark(self):
        conn = self.connect(_Connection())
        ok = await self.ns["replace_manual_summary_if_unchanged"](
            "s", [], 0, None, ["preface"])
        self.assertTrue(ok)
        self.assertEqual(conn.state["summary_checkpoint_user_id"], 0)

    async def test_manual_existing_history_has_no_assumed_boundary(self):
        conn = self.connect(_Connection(has_history=True))
        ok = await self.ns["replace_manual_summary_if_unchanged"](
            "s", [], 0, None, ["preface"])
        self.assertTrue(ok)
        self.assertIsNone(conn.state["summary_checkpoint_user_id"])


if __name__ == "__main__":
    unittest.main()
