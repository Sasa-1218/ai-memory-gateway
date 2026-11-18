import unittest
from memory_provenance import trace_memory_provenance as trace

class MemoryProvenanceTests(unittest.TestCase):
    def test_single_explicit_source(self):
        self.assertEqual(trace({'id':1,'source_session':'s','source_message_ids':[3,2,3]}, {}),
                         {'source_session':'s','source_message_ids':[2,3],'complete':True})
    def test_merge_inherits_parent_source(self):
        leaf={'id':1,'source_session':'s','source_message_ids':[2,3]}
        root={'id':2,'merged_from':[1]}
        self.assertEqual(trace(root,{1:leaf}), {'source_session':'s','source_message_ids':[2,3],'complete':True})
    def test_cross_session_fails_closed(self):
        root={'id':3,'merged_from':[1,2]}
        parents={1:{'id':1,'source_session':'a','source_message_ids':[1]},
                 2:{'id':2,'source_session':'b','source_message_ids':[1]}}
        self.assertEqual(trace(root,parents)['source_message_ids'], [])
    def test_missing_parent_flagged(self):
        root={'id':2,'source_session':'s','source_message_ids':[4],'merged_from':[5]}
        self.assertFalse(trace(root,{})['complete'])
    def test_legacy_session_only_is_unverified(self):
        self.assertEqual(trace({'id':1,'source_session':'s'},{}),
                         {'source_session':'s','source_message_ids':[],'complete':False})
    def test_unscoped_ids_do_not_appear(self):
        self.assertEqual(trace({'id':1,'source_message_ids':[5]}, {})['source_message_ids'], [])
    def test_cycle_is_bounded(self):
        a={'id':1,'source_session':'s','source_message_ids':[1],'merged_from':[2]}
        b={'id':2,'source_session':'s','source_message_ids':[2],'merged_from':[1]}
        self.assertFalse(trace(a,{1:a,2:b})['complete'])
    def test_bad_parent_type_rejected(self):
        self.assertFalse(trace({'id':1,'source_session':'s','source_message_ids':[1], 'merged_from':'2'}, {})['complete'])
    def test_node_budget(self):
        root={'id':100,'source_session':'s','merged_from':list(range(1,20))}
        rows={i:{'id':i,'source_session':'s','source_message_ids':[i]} for i in range(1,20)}
        self.assertFalse(trace(root,rows,max_nodes=2)['complete'])
    def test_does_not_mutate_input(self):
        row={'id':1,'source_session':'s','source_message_ids':[2,1]}
        original=dict(row);trace(row,{});self.assertEqual(row,original)

if __name__=='__main__': unittest.main()
