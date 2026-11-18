"""Database adapter and API tests for read-only provenance inspection."""
import unittest
from unittest.mock import AsyncMock, patch

import database
import main


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetch(self, sql, ids):
        self.queries.append((sql, list(ids)))
        assert sql.strip().upper().startswith('SELECT')
        return [self.rows[i] for i in ids if i in self.rows]


class FakeAcquire:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *_): return False


class FakePool:
    def __init__(self, conn): self.conn = conn
    def acquire(self): return FakeAcquire(self.conn)


class LineageDBTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_only_queries_and_no_false_message_sources(self):
        conn = FakeConn({
            4: {'id': 4, 'merged_from': [2], 'source_session': None},
            2: {'id': 2, 'merged_from': None, 'source_session': '6.14'},
        })
        with patch.object(database, 'get_pool', new=AsyncMock(return_value=FakePool(conn))):
            result = await database.get_memory_lineage_readonly(4)
        self.assertTrue(result['lineage_complete'])
        self.assertEqual(result['source_memory_ids'], [2, 4])
        self.assertEqual(result['source_session'], '6.14')
        self.assertEqual(result['source_message_ids'], [])
        self.assertTrue(all(query.strip().upper().startswith('SELECT') for query, _ in conn.queries))

    async def test_missing_root(self):
        conn = FakeConn({})
        with patch.object(database, 'get_pool', new=AsyncMock(return_value=FakePool(conn))):
            result = await database.get_memory_lineage_readonly(888)
        self.assertFalse(result['exists'])

    async def test_endpoint_found_and_missing(self):
        hit = {'exists': True, 'memory_id': 1, 'source_memory_ids': [1]}
        with patch.object(main._db_module, 'get_memory_lineage_readonly', new=AsyncMock(return_value=hit)):
            self.assertEqual(await main.api_get_memory_lineage(1), hit)
        missing = {'exists': False}
        with patch.object(main._db_module, 'get_memory_lineage_readonly', new=AsyncMock(return_value=missing)):
            with self.assertRaises(main.HTTPException) as cm:
                await main.api_get_memory_lineage(999)
        self.assertEqual(cm.exception.status_code, 404)

    async def test_endpoint_rejects_invalid_memory_id(self):
        with self.assertRaises(main.HTTPException) as cm:
            await main.api_get_memory_lineage(0)
        self.assertEqual(cm.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
