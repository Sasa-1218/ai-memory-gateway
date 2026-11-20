"""Unit checks for the experience-card database recall path (no real database)."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from database import retrieve_experience_recall


class FakeConnection:
    def __init__(self, cards, user_count=10):
        self.cards = cards
        self.user_count = user_count
        self.saved = {}
        self.calls = []

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def fetchval(self, sql, *args):
        if "COUNT(*)" in sql:
            return self.user_count
        if "SELECT value" in sql:
            return self.saved.get(args[0])
        raise AssertionError("Unexpected fetchval: " + sql)

    async def fetch(self, sql, *args):
        assert "review_status='approved' AND ai_visible=TRUE" in sql
        return self.cards

    async def execute(self, sql, *args):
        self.calls.append(sql)
        if "INSERT INTO gateway_config" in sql:
            self.saved[args[0]] = args[1]
        elif "pg_advisory_xact_lock" not in sql:
            raise AssertionError("Unexpected execute: " + sql)
        return "OK"


class FakePool:
    def __init__(self, connection):
        self.conn = connection

    def acquire(self):
        return self.conn


def card(id=1, title="烟花日的手链", status="approved", visible=True):
    return {
        "id": id, "title": title, "key_details": json.dumps(["烟花日", "星星手链"]),
        "review_status": status, "ai_visible": visible,
        "event_summary": "我们看过烟花",
        "interaction_trace": "她告诉我很开心",
        "event_date_start": "2026-08-08", "event_date_end": "2026-08-09",
    }


class DatabaseExperienceRecallTests(unittest.IsolatedAsyncioTestCase):
    async def test_approved_card_recall_has_durable_interval(self):
        conn = FakeConnection([card()])
        with patch("database.get_pool", new=AsyncMock(return_value=FakePool(conn))):
            text = await retrieve_experience_recall("6.14", "你记得烟花日吗？", 4, 1)
            self.assertIn("烟花日的手链", text)
            state = json.loads(conn.saved["experience_recall_last_round:6.14"])
            self.assertEqual(state["card_id"], 1)
            self.assertEqual(state["round"], 11)
            self.assertEqual(await retrieve_experience_recall("6.14", "烟花日", 4, 1), "")
            conn.user_count = 12
            self.assertEqual(await retrieve_experience_recall("6.14", "烟花日", 4, 1), "")

    async def test_same_card_does_not_repeat_after_only_four_rounds(self):
        conn = FakeConnection([card()], user_count=14)
        conn.saved["experience_recall_last_round:6.14"] = json.dumps({"round": 11, "card_id": 1})
        with patch("database.get_pool", new=AsyncMock(return_value=FakePool(conn))):
            self.assertEqual(await retrieve_experience_recall("6.14", "烟花日", 4, 1), "")
            self.assertEqual(json.loads(conn.saved["experience_recall_last_round:6.14"])["round"], 11)

    async def test_budget_rejection_does_not_consume_cooldown(self):
        conn = FakeConnection([card()])
        with patch("database.get_pool", new=AsyncMock(return_value=FakePool(conn))):
            self.assertEqual(
                await retrieve_experience_recall("6.14", "烟花日", 4, 1, max_chars=5), ""
            )
            self.assertNotIn("experience_recall_last_round:6.14", conn.saved)
            text = await retrieve_experience_recall("6.14", "烟花日", 4, 1, max_chars=6000)
            self.assertIn("烟花日的手链", text)
            self.assertIn("experience_recall_last_round:6.14", conn.saved)

    async def test_invalid_budget_rejected(self):
        conn = FakeConnection([card()])
        with patch("database.get_pool", new=AsyncMock(return_value=FakePool(conn))):
            with self.assertRaises(ValueError):
                await retrieve_experience_recall("6.14", "烟花日", max_chars=-1)

    async def test_pending_and_hidden_cards_are_never_injected(self):
        conn = FakeConnection([card(1, status="pending"), card(2, visible=False)])
        with patch("database.get_pool", new=AsyncMock(return_value=FakePool(conn))):
            result = await retrieve_experience_recall("6.14", "烟花日", 4, 1)
            self.assertEqual(result, "")
            self.assertEqual(conn.saved, {})

    async def test_when_relevant_next_card_exists_use_that(self):
        conn = FakeConnection([card(), card(2, "樱花照、汉服与猫耳簪")], user_count=14)
        conn.saved["experience_recall_last_round:6.14"] = json.dumps({"round": 11, "card_id": 1})
        with patch("database.get_pool", new=AsyncMock(return_value=FakePool(conn))):
            result = await retrieve_experience_recall("6.14", "烟花日和猫耳簪的事情", 4, 1)
            self.assertIn("猫耳簪", result)
            state = json.loads(conn.saved["experience_recall_last_round:6.14"])
            self.assertEqual(state["card_id"], 2)


if __name__ == "__main__":
    unittest.main()
