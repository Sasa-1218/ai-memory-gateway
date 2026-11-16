"""Read-only preview + explicit legacy cutoff alignment: SQL mocked; no live DB."""
import ast
import datetime as dt
import hashlib
import json
import sys
import types
import unittest
from pathlib import Path
from fastapi import HTTPException


def _load():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "summary_boundary_realign.py").read_text(encoding="utf-8"))
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    target = {"_parts", "_token", "_rounds", "_find_start", "preview", "confirm"}
    assert {n.name for n in funcs} == target
    scope = {"hashlib": hashlib, "json": json, "HTTPException": HTTPException}
    exec(compile(ast.fix_missing_locations(ast.Module(body=funcs, type_ignores=[])), "summary_boundary_realign.py", "exec"), scope)
    scope["CONFIRM_BOUNDARY"] = "确认摘要截止位置"
    return scope


def group_by_rounds(messages):
    grouped, current = [], []
    for message in messages:
        if message["role"] == "user" and current:
            grouped.append(current)
            current = []
        current.append(message)
    if current:
        grouped.append(current)
    return grouped


class _Connection:
    def __init__(self):
        self.state = {
            "summary": json.dumps(["已完成摘要"], ensure_ascii=False),
            "a_start_round": 999,
            "summary_checkpoint_user_id": None,
            "updated_at": dt.datetime(2026, 11, 17, tzinfo=dt.timezone.utc),
        }
        self.rows = [
            {"id": id, "role": "user", "content": f"msg {id}", "metadata": None,
             "created_at": dt.datetime(2026, 11, 17, minute=i, tzinfo=dt.timezone.utc)}
            for i, id in enumerate([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120])
        ]
        self.snapshots = []
        self.updates = 0

    def transaction(self):
        return _Context(self)

    async def fetchrow(self, sql, *args):
        if "historical_summaries" in sql:
            self.snapshots.append(json.loads(args[2]))
            return {"id": len(self.snapshots)}
        return dict(self.state)

    async def fetch(self, sql, *args):
        return list(self.rows)

    async def execute(self, sql, *args):
        if "INSERT INTO historical_summaries" in sql:
            self.snapshots.append(json.loads(args[2]))
        elif "UPDATE session_cache_state" in sql:
            self.state["summary_checkpoint_user_id"] = args[1]
            self.state["a_start_round"] = args[2]
            self.state["updated_at"] += dt.timedelta(seconds=1)
            self.updates += 1
        else:
            raise AssertionError(sql)


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


class BoundaryRealignTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.scope = _load()
        self.conn = _Connection()
        async def pool():
            return _Pool(self.conn)
        self.scope["get_pool"] = pool
        self.scope["db_row_to_message"] = lambda row: {"role": row["role"], "content": row["content"]}
        self._old_main = sys.modules.get("main")
        sys.modules["main"] = types.SimpleNamespace(group_by_rounds=group_by_rounds)

    def tearDown(self):
        if self._old_main is None:
            sys.modules.pop("main", None)
        else:
            sys.modules["main"] = self._old_main

    async def _preview(self):
        return await self.scope["preview"]("s")

    async def _confirm(self, first_id=50, token=None, confirmation="确认摘要截止位置"):
        if token is None:
            token = (await self._preview())["state_token"]
        return await self.scope["confirm"]("s", first_id, token, confirmation)

    async def test_preview_is_read_only_and_paginates(self):
        p = await self.scope["preview"]("s", limit=3)
        self.assertEqual([x["id"] for x in p["candidates"]], [120, 110, 100])
        self.assertEqual(p["next_before_id"], 100)
        self.assertEqual(self.conn.updates, 0)
        self.assertEqual(self.conn.snapshots, [])
        q = await self.scope["preview"]("s", before_id=100, limit=2)
        self.assertEqual([x["id"] for x in q["candidates"]], [90, 80])

    async def test_confirm_preserves_summary_and_history_and_makes_snapshot(self):
        original_summary = self.conn.state["summary"]
        original_rows = list(self.conn.rows)
        r = await self._confirm()
        self.assertEqual(r["status"], "aligned")
        self.assertEqual(self.conn.state["summary"], original_summary)
        self.assertEqual(self.conn.rows, original_rows)
        self.assertEqual(self.conn.state["summary_checkpoint_user_id"], 49)
        self.assertEqual(self.conn.state["a_start_round"], 4)
        self.assertEqual(self.conn.snapshots[0]["a_start_round"], 999)
        self.assertEqual(self.conn.snapshots[0]["summary_checkpoint_user_id"], None)
        self.assertEqual(r["snapshot_id"], 1)

    async def test_explicit_confirmation_required(self):
        with self.assertRaises(HTTPException) as cm:
            await self._confirm(confirmation="yes")
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(self.conn.updates, 0)

    async def test_missing_id_rejected_without_snapshot(self):
        with self.assertRaises(HTTPException) as cm:
            await self._confirm(first_id=55)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.conn.snapshots, [])

    async def test_stale_history_token_blocks(self):
        token = (await self._preview())["state_token"]
        self.conn.rows.pop(0)
        with self.assertRaises(HTTPException) as cm:
            await self._confirm(token=token)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.conn.updates, 0)

    async def test_changed_message_body_invalidates_preview(self):
        token = (await self._preview())["state_token"]
        self.conn.rows[0]["content"] = "edited after preview"
        with self.assertRaises(HTTPException) as cm:
            await self._confirm(token=token)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(self.conn.snapshots, [])

    async def test_changed_message_metadata_invalidates_preview(self):
        token = (await self._preview())["state_token"]
        self.conn.rows[0]["metadata"] = '{"changed":true}'
        with self.assertRaises(HTTPException) as cm:
            await self._confirm(token=token)
        self.assertEqual(cm.exception.status_code, 409)

    async def test_stale_summary_token_blocks(self):
        token = (await self._preview())["state_token"]
        self.conn.state["summary"] = json.dumps(["new content"])
        with self.assertRaises(HTTPException) as cm:
            await self._confirm(token=token)
        self.assertEqual(cm.exception.status_code, 409)

    async def test_second_confirmation_rejected(self):
        await self._confirm()
        with self.assertRaises(HTTPException) as cm:
            await self._confirm()
        self.assertEqual(cm.exception.status_code, 409)
        self.assertEqual(len(self.conn.snapshots), 1)

    async def test_nonmonotonic_user_ids_rejected(self):
        self.conn.rows[5]["id"] = 15
        with self.assertRaises(HTTPException) as cm:
            await self._confirm()
        self.assertEqual(cm.exception.status_code, 409)

    async def test_missing_summary_rejected(self):
        self.conn.state["summary"] = ""
        with self.assertRaises(HTTPException) as cm:
            await self._preview()
        self.assertEqual(cm.exception.status_code, 409)

    async def test_round_counter_after_confirm(self):
        await self._confirm(first_id=50)
        self.assertEqual(len(self.conn.rows)-self.conn.state["a_start_round"], 8)
        from partition_checkpoint import first_unsummarized_round
        grouped = group_by_rounds([
            {"role": "user", "_history_id": r["id"]} for r in self.conn.rows
        ])
        self.assertEqual(first_unsummarized_round(grouped, 49), 4)
        # Eight new turns become B; the existing eight are the next A block.
        grouped.extend([[{"role": "user", "_history_id": i}] for i in range(130, 210, 10)])
        self.assertEqual(len(grouped) - (4+8), 8)


if __name__ == "__main__":
    unittest.main()
