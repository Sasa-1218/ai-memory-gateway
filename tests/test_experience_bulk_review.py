"""Batch experience review: atomic approvals and safety guards (mock DB)."""
import unittest
from unittest.mock import AsyncMock, patch

from database import bulk_review_experience_cards


def card(cid, status="pending", visible=False, replace=None, text="发生过的一件共同经历", source_ids=None):
    return {
        "id":cid, "review_status":status, "ai_visible":visible,
        "supersedes_card_id":replace, "title":"猫耳簪", "event_summary":text,
        "source_message_ids": [101,102] if source_ids is None else source_ids,
    }


class Connection:
    def __init__(self, rows):
        self.rows = rows
        self.updates = []
    def transaction(self):
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return False
    async def fetch(self, sql, *args):
        if "FOR UPDATE" in sql:
            return [row for row in self.rows if row["id"] in args[0]]
        if "UPDATE shared_experience_cards" in sql:
            self.updates.append((sql, args[0]))
            return [{"id":cid} for cid in args[0]]
        raise AssertionError("Unexpected SQL: " + sql[:80])


class Pool:
    def __init__(self, conn):
        self.conn = conn
    def acquire(self):
        return self.conn


class BatchReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_selection_approved_atomically(self):
        conn = Connection([card(1),card(2),card(3)])
        with patch("database.get_pool", new=AsyncMock(return_value=Pool(conn))):
            result = await bulk_review_experience_cards([1,2],"approve")
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["ids"], [1,2])
        self.assertEqual(len(conn.updates), 1)
        self.assertNotIn(3,conn.updates[0][1])
        self.assertIn("ai_visible=TRUE",conn.updates[0][0])

    async def test_approved_card_prevents_bulk_write(self):
        conn = Connection([card(1),card(2,status="approved",visible=True)])
        with patch("database.get_pool", new=AsyncMock(return_value=Pool(conn))):
            with self.assertRaisesRegex(ValueError,"only_pending"):
                await bulk_review_experience_cards([1,2],"approve")
        self.assertEqual(conn.updates,[])

    async def test_replacement_candidate_requires_group_review(self):
        conn = Connection([card(1,replace=77)])
        with patch("database.get_pool", new=AsyncMock(return_value=Pool(conn))):
            with self.assertRaisesRegex(ValueError,"replacement_candidates"):
                await bulk_review_experience_cards([1],"approve")
        self.assertEqual(conn.updates,[])

    async def test_incomplete_card_cannot_be_approved(self):
        conn = Connection([card(1,source_ids=[])])
        with patch("database.get_pool", new=AsyncMock(return_value=Pool(conn))):
            with self.assertRaisesRegex(ValueError,"incomplete_card"):
                await bulk_review_experience_cards([1],"approve")
        self.assertEqual(conn.updates,[])

    async def test_stale_card_set_cannot_write_partial_batch(self):
        conn = Connection([card(1)])
        with patch("database.get_pool", new=AsyncMock(return_value=Pool(conn))):
            with self.assertRaisesRegex(ValueError,"cards_changed"):
                await bulk_review_experience_cards([1,404],"approve")
        self.assertEqual(conn.updates,[])

    async def test_no_duplicates_or_more_than_thirty(self):
        with self.assertRaisesRegex(ValueError,"duplicate"):
            await bulk_review_experience_cards([1,1],"approve")
        with self.assertRaisesRegex(ValueError,"limit"):
            await bulk_review_experience_cards(list(range(1,32)),"approve")


if __name__=="__main__":
    unittest.main()
