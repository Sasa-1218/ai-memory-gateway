import unittest
from memory_lineage_query import inspect_memory_lineage


class LineageQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_leaf_and_two_level_merge(self):
        rows = {
            3: {'id': 3, 'merged_from': [1, 2]},
            1: {'id': 1, 'source_session': 's', 'merged_from': []},
            2: {'id': 2, 'source_session': 's', 'merged_from': []},
        }
        calls = []
        async def fetch(ids):
            calls.append(list(ids))
            return [rows[i] for i in ids if i in rows]
        result = await inspect_memory_lineage(3, fetch)
        self.assertEqual(result['source_memory_ids'], [1, 2, 3])
        self.assertEqual(result['source_message_ids'], [])
        self.assertFalse(result['message_sources_verified'])
        self.assertTrue(result['lineage_complete'])
        self.assertEqual(calls, [[3], [1, 2]])

    async def test_missing_parent_flagged(self):
        async def fetch(ids):
            return [{'id': 2, 'merged_from': [99]}] if ids == [2] else []
        result = await inspect_memory_lineage(2, fetch)
        self.assertFalse(result['lineage_complete'])
        self.assertEqual(result['missing_memory_ids'], [99])

    async def test_mixed_sessions_no_claim_of_one_session(self):
        rows={3:{'id':3,'merged_from':[1,2]},
              1:{'id':1,'source_session':'a'},
              2:{'id':2,'source_session':'b'}}
        async def fetch(ids): return [rows[i] for i in ids]
        result = await inspect_memory_lineage(3,fetch)
        self.assertEqual(result['source_session'], '')

    async def test_cycle_finishes(self):
        rows={1:{'id':1,'merged_from':[2]},2:{'id':2,'merged_from':[1]}}
        async def fetch(ids): return [rows[i] for i in ids]
        result = await inspect_memory_lineage(1,fetch)
        self.assertEqual(result['source_memory_ids'],[1,2])

    async def test_node_budget_marks_truncated(self):
        rows={1:{'id':1,'merged_from':[2,3]},2:{'id':2},3:{'id':3}}
        async def fetch(ids): return [rows[i] for i in ids]
        result = await inspect_memory_lineage(1,fetch,max_nodes=2)
        self.assertTrue(result['truncated'])

    async def test_missing_root(self):
        async def fetch(ids): return []
        self.assertFalse((await inspect_memory_lineage(4,fetch))['exists'])

    async def test_invalid_input(self):
        async def fetch(ids): return []
        for bad in (0,-1,True,'1'):
            with self.assertRaises(ValueError):
                await inspect_memory_lineage(bad,fetch)

    async def test_no_database_writes_are_requested(self):
        calls=[]
        async def fetch(ids):
            calls.append(ids)
            return [{'id':1,'merged_from':[]}]
        await inspect_memory_lineage(1,fetch)
        self.assertEqual(calls,[[1]])


if __name__=='__main__': unittest.main()
