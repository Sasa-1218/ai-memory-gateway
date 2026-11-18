import unittest
from recall_selection import select_recall_items


def memory(id, text, ids=None, session='a', active=True):
    return dict(id=id, content=text, source_session=session, source_message_ids=ids or [], is_active=active)


def card(id, text, ids=None, session='a', visible=True, status='approved'):
    return dict(id=id, event_summary=text, source_session_id=session, source_message_ids=ids or [], ai_visible=visible, review_status=status)


class SelectionTests(unittest.TestCase):
    def test_card_preferred_for_same_source(self):
        got = select_recall_items([memory(1, 'fragment', [10,11])], [card(2, 'whole event', [10,11])])
        self.assertEqual([(x['kind'], x['id']) for x in got], [('experience', 2)])

    def test_distinct_sources_are_preserved(self):
        got = select_recall_items([memory(1, 'fragment', [10])], [card(2, 'whole event', [10,11,12])])
        self.assertEqual(len(got), 2)

    def test_same_source_id_different_sessions_not_deduplicated(self):
        got = select_recall_items([memory(1, 'fragment', [10], 'b')], [card(2, 'whole event', [10], 'a')])
        self.assertEqual(len(got), 2)

    def test_exact_text_dedup_when_source_unknown(self):
        got = select_recall_items([memory(1, ' same  text ')], [card(2, 'sametext')])
        self.assertEqual(len(got), 1)

    def test_pending_and_hidden_cards_never_included(self):
        got = select_recall_items([], [card(1,'secret',visible=False),card(2,'pending',status='pending')])
        self.assertEqual(got, [])

    def test_inactive_memory_never_included(self):
        self.assertEqual(select_recall_items([memory(1,'archived',active=False)], []), [])

    def test_shared_budget_does_not_truncate_and_skips_oversized(self):
        got = select_recall_items([memory(3,'OK')], [card(1,'x' * 11),card(2,'abcd')], max_items=2,max_chars=6)
        self.assertEqual([x['id'] for x in got], [2,3])

    def test_limits_are_validated(self):
        for kwargs in ({'max_items':-1},{'max_chars':-1},{'max_items':True}):
            with self.assertRaises(ValueError): select_recall_items([], [], **kwargs)

    def test_bad_source_type_is_ignored(self):
        a=memory(1,'a'); a['source_message_ids']='unsafe'
        self.assertEqual(len(select_recall_items([a],[])),1)

    def test_no_mutation(self):
        a=memory(1,'a',[1]); b=card(2,'b',[2])
        old_a=dict(a); old_b=dict(b)
        select_recall_items([a],[b]); self.assertEqual(a,old_a); self.assertEqual(b,old_b)


if __name__=='__main__': unittest.main()
