import asyncio
import os
import unittest
import uuid
from urllib.parse import urlsplit, urlunsplit

from experience_cards import (
    SHARED_EXPERIENCE_CARD_PROMPT,
    apply_card_update,
    normalize_card_update,
    restore_card_update,
    soft_delete_card_update,
    build_generation_prompt,
    validate_generated_cards,
    should_auto_supersede,
)


def card_state(status="pending", visible=False):
    return {
        "title": "保留的标题",
        "event_summary": "保留的正文",
        "interaction_trace": "保留的互动",
        "key_details": ["线索"],
        "explicit_corrections": [
            {"old_claim": "旧说法", "new_claim": "新说法"}
        ],
        "explicit_agreements": ["约定"],
        "open_threads": ["待跟进"],
        "source_session_id": "main-session",
        "source_message_ids": [101, 102],
        "review_status": status,
        "ai_visible": visible,
    }


def transition(current, payload):
    return apply_card_update(current, normalize_card_update(payload))


class ExperienceCardReviewTests(unittest.TestCase):
    def test_approved_is_required_for_ai_visibility(self):
        with self.assertRaisesRegex(ValueError, "ai_visible_requires_approved_status"):
            normalize_card_update({"review_status": "pending", "ai_visible": True})

    def test_non_approved_status_forces_visibility_off(self):
        update = normalize_card_update({"review_status": "archived"})
        self.assertFalse(update["ai_visible"])

    def test_approved_card_can_be_visible(self):
        update = normalize_card_update({"review_status": "approved", "ai_visible": True})
        self.assertTrue(update["ai_visible"])

    def test_invalid_status_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_review_status"):
            normalize_card_update({"review_status": "visible"})

    def test_key_details_are_limited_to_three(self):
        update = normalize_card_update({"key_details": ["a", "b", "c", "d"]})
        self.assertEqual(update["key_details"], ["a", "b", "c"])

    def test_corrections_require_object_items(self):
        with self.assertRaisesRegex(
            ValueError, "explicit_corrections_items_must_be_objects"
        ):
            normalize_card_update({"explicit_corrections": ['{"old_claim":"x"}']})

    def test_correction_claims_require_strings(self):
        with self.assertRaisesRegex(
            ValueError, "explicit_correction_claims_must_be_strings"
        ):
            normalize_card_update({
                "explicit_corrections": [{"old_claim": "旧", "new_claim": 1}]
            })

    def test_structured_corrections_are_preserved(self):
        corrections = [{"old_claim": "旧说法", "new_claim": "新说法"}]
        update = normalize_card_update({"explicit_corrections": corrections})
        result = apply_card_update(card_state(), update)
        self.assertEqual(result["explicit_corrections"], corrections)

    def test_pending_to_approved(self):
        result = transition(
            card_state(), {"review_status": "approved", "ai_visible": True}
        )
        self.assertEqual(result["review_status"], "approved")
        self.assertTrue(result["ai_visible"])

    def test_approved_to_archived(self):
        result = transition(card_state("approved", True), {"review_status": "archived"})
        self.assertEqual(result["review_status"], "archived")
        self.assertFalse(result["ai_visible"])

    def test_approved_to_deleted_preserves_content_and_sources(self):
        before = card_state("approved", True)
        result = transition(before, {"review_status": "deleted", "ai_visible": False})
        self.assertEqual(result["review_status"], "deleted")
        self.assertFalse(result["ai_visible"])
        self.assertEqual(result["event_summary"], before["event_summary"])
        self.assertEqual(result["source_message_ids"], before["source_message_ids"])
        self.assertEqual(result["explicit_corrections"], before["explicit_corrections"])

    def test_archived_restore_is_pending_and_hidden(self):
        result = transition(
            card_state("archived", False),
            {"review_status": "pending", "ai_visible": False},
        )
        self.assertEqual(result["review_status"], "pending")
        self.assertFalse(result["ai_visible"])

    def test_deleted_restore_is_pending_and_hidden(self):
        result = transition(
            card_state("deleted", False),
            {"review_status": "pending", "ai_visible": False},
        )
        self.assertEqual(result["review_status"], "pending")
        self.assertFalse(result["ai_visible"])

    def test_superseded_is_forced_hidden(self):
        result = transition(card_state("approved", True), {"review_status": "superseded"})
        self.assertEqual(result["review_status"], "superseded")
        self.assertFalse(result["ai_visible"])

    def test_non_approved_cannot_remain_visible(self):
        for status in ("pending", "archived", "deleted", "superseded"):
            with self.subTest(status=status):
                result = transition(card_state("approved", True), {"review_status": status})
                self.assertFalse(result["ai_visible"])

    def test_source_message_ids_cannot_be_edited(self):
        update = normalize_card_update({"source_message_ids": [999]})
        self.assertNotIn("source_message_ids", update)

    def test_soft_delete_api_transition_preserves_content_and_sources(self):
        before = card_state("approved", True)
        result = apply_card_update(before, soft_delete_card_update())
        self.assertEqual(result["review_status"], "deleted")
        self.assertFalse(result["ai_visible"])
        self.assertEqual(result["event_summary"], before["event_summary"])
        self.assertEqual(result["source_message_ids"], before["source_message_ids"])

    def test_restore_api_transition_is_pending_hidden(self):
        for status in ("archived", "deleted"):
            with self.subTest(status=status):
                result = apply_card_update(card_state(status, False), restore_card_update())
                self.assertEqual(result["review_status"], "pending")
                self.assertFalse(result["ai_visible"])

    def test_prompt_keeps_single_stage_and_fact_boundaries(self):
        self.assertIn("事件脊柱", SHARED_EXPERIENCE_CARD_PROMPT)
        self.assertIn("不得把后来的担心", SHARED_EXPERIENCE_CARD_PROMPT)
        self.assertIn("虚构能力", SHARED_EXPERIENCE_CARD_PROMPT)
        self.assertNotIn("topic_tags", SHARED_EXPERIENCE_CARD_PROMPT)
        self.assertNotIn("事件重要性", SHARED_EXPERIENCE_CARD_PROMPT)

    def test_generated_cards_are_pending_compatible_and_source_scoped(self):
        payload = {"cards": [{"title":"t","event_summary":"e","interaction_trace":"i",
            "key_details":[],"explicit_corrections":[],"explicit_agreements":[],
            "open_threads":[],"source_message_ids":[1,2]}]}
        cards = validate_generated_cards(payload, {1,2,3})
        self.assertEqual(cards[0]["source_message_ids"], [1,2])

    def test_generated_source_ids_out_of_scope_are_rejected(self):
        payload = {"cards": [{"title":"t","event_summary":"e","interaction_trace":"i",
            "key_details":[],"explicit_corrections":[],"explicit_agreements":[],
            "open_threads":[],"source_message_ids":[99]}]}
        with self.assertRaisesRegex(ValueError, "source_message_ids_out_of_scope"):
            validate_generated_cards(payload, {1,2})

    def test_multiple_cards_may_share_source_ids(self):
        base = {"title":"t","event_summary":"e","interaction_trace":"i",
            "key_details":[],"explicit_corrections":[],"explicit_agreements":[],"open_threads":[]}
        payload = {"cards": [dict(base, source_message_ids=[1,2]), dict(base, source_message_ids=[2,3])]}
        self.assertEqual(len(validate_generated_cards(payload, {1,2,3})), 2)

    def test_split_prompt_has_only_split_specific_constraint(self):
        messages = [{"id":1,"role":"user","content":"x","created_at":"now"}]
        regular = build_generation_prompt(messages, "regenerate")
        split = build_generation_prompt(messages, "split")
        self.assertNotIn("不得单独成卡，也不要放进 title", regular)
        self.assertIn("不得单独成卡，也不要放进 title", split)

    def test_only_pending_or_archived_auto_supersede(self):
        self.assertTrue(should_auto_supersede({"review_status":"pending","ai_visible":False}))
        self.assertTrue(should_auto_supersede({"review_status":"archived","ai_visible":False}))
        self.assertFalse(should_auto_supersede({"review_status":"approved","ai_visible":True}))
        self.assertFalse(should_auto_supersede({"review_status":"deleted","ai_visible":False}))


@unittest.skipUnless(
    os.getenv("EXPERIENCE_CARD_TEST_DATABASE_URL"),
    "EXPERIENCE_CARD_TEST_DATABASE_URL is required for PostgreSQL transaction tests",
)
class ExperienceCardDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        import database

        self.asyncpg = asyncpg
        self.db = database
        await database.close_pool()
        self.admin_url = os.environ["EXPERIENCE_CARD_TEST_DATABASE_URL"]
        self.database_name = f"experience_card_{uuid.uuid4().hex}"
        admin = await asyncpg.connect(self.admin_url)
        try:
            await admin.execute(f'CREATE DATABASE "{self.database_name}"')
        finally:
            await admin.close()
        parsed = urlsplit(self.admin_url)
        database.DATABASE_URL = urlunsplit(
            (parsed.scheme, parsed.netloc, f"/{self.database_name}", parsed.query, parsed.fragment)
        )
        pool = await database.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS shared_experience_cards (
                    id BIGSERIAL PRIMARY KEY,
                    source_session_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '', event_summary TEXT NOT NULL DEFAULT '',
                    interaction_trace TEXT NOT NULL DEFAULT '',
                    key_details JSONB NOT NULL DEFAULT '[]',
                    explicit_corrections JSONB NOT NULL DEFAULT '[]',
                    explicit_agreements JSONB NOT NULL DEFAULT '[]',
                    open_threads JSONB NOT NULL DEFAULT '[]',
                    source_message_ids BIGINT[] NOT NULL DEFAULT '{}',
                    review_status TEXT NOT NULL DEFAULT 'pending',
                    ai_visible BOOLEAN NOT NULL DEFAULT FALSE,
                    supersedes_card_id BIGINT REFERENCES shared_experience_cards(id),
                    revision_reason TEXT NOT NULL DEFAULT '', generator_model TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '', approved_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT shared_experience_cards_status_check CHECK
                        (review_status IN ('pending','approved','archived','deleted','superseded')),
                    CONSTRAINT shared_experience_cards_visibility_check CHECK
                        (NOT ai_visible OR review_status='approved')
                );
                CREATE TABLE IF NOT EXISTS experience_card_generation_jobs (
                    job_id TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL CHECK
                        (operation_type IN ('manual_generate','regenerate','split')),
                    status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed')),
                    source_session_id TEXT NOT NULL, source_message_ids BIGINT[] NOT NULL,
                    source_card_id BIGINT REFERENCES shared_experience_cards(id),
                    model TEXT NOT NULL DEFAULT '', error_message TEXT NOT NULL DEFAULT '',
                    result_card_ids BIGINT[] NOT NULL DEFAULT '{}',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_card_jobs_active_card
                ON experience_card_generation_jobs(source_card_id)
                WHERE source_card_id IS NOT NULL AND status='running';
                TRUNCATE experience_card_generation_jobs, shared_experience_cards RESTART IDENTITY CASCADE;
            """)

    async def asyncTearDown(self):
        await self.db.close_pool()
        admin = await self.asyncpg.connect(self.admin_url)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{self.database_name}" WITH (FORCE)')
        finally:
            await admin.close()

    async def _card(self, status="pending", visible=False):
        pool = await self.db.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("""
                INSERT INTO shared_experience_cards
                (source_session_id,title,event_summary,source_message_ids,review_status,ai_visible)
                VALUES ('main-session','old','old summary',ARRAY[101,102]::bigint[],$1,$2)
                RETURNING id
            """, status, visible)

    @staticmethod
    def _generated(title="new"):
        return [{"title":title,"event_summary":"summary","interaction_trace":"interaction",
            "key_details":[],"explicit_corrections":[],"explicit_agreements":[],
            "open_threads":[],"source_message_ids":[101,102]}]

    async def test_concurrent_approve_and_split_rechecks_locked_source(self):
        old_id = await self._card()
        await self.db.begin_experience_generation_job(
            "race-job", "split", "main-session", [101, 102], old_id
        )
        blocker = await self.asyncpg.connect(self.db.DATABASE_URL)
        tx = blocker.transaction()
        await tx.start()
        await blocker.execute("""
            UPDATE shared_experience_cards SET review_status='approved',ai_visible=TRUE
            WHERE id=$1
        """, old_id)
        completion = asyncio.create_task(
            self.db.complete_experience_generation_job(
                "race-job", self._generated(), "test-model"
            )
        )
        await asyncio.sleep(0.1)
        self.assertFalse(completion.done())
        await tx.commit()
        await blocker.close()
        new_ids = await completion
        pool = await self.db.get_pool()
        async with pool.acquire() as conn:
            old = await conn.fetchrow(
                "SELECT review_status,ai_visible FROM shared_experience_cards WHERE id=$1", old_id
            )
            candidate = await conn.fetchrow(
                "SELECT review_status,ai_visible FROM shared_experience_cards WHERE id=$1", new_ids[0]
            )
        self.assertEqual((old["review_status"], old["ai_visible"]), ("approved", True))
        self.assertEqual((candidate["review_status"], candidate["ai_visible"]), ("pending", False))

    async def test_approved_split_replaces_all_candidates_atomically(self):
        old_id = await self._card("approved", True)
        await self.db.begin_experience_generation_job(
            "group-job", "split", "main-session", [101, 102], old_id
        )
        cards = self._generated("first") + self._generated("second")
        new_ids = await self.db.complete_experience_generation_job(
            "group-job", cards, "test-model"
        )
        approved = await self.db.approve_experience_replacement(new_ids[0])
        self.assertEqual({item["id"] for item in approved}, set(new_ids))
        pool = await self.db.get_pool()
        async with pool.acquire() as conn:
            old = await conn.fetchrow(
                "SELECT review_status,ai_visible FROM shared_experience_cards WHERE id=$1", old_id
            )
            rows = await conn.fetch("""
                SELECT review_status,ai_visible FROM shared_experience_cards
                WHERE id=ANY($1::bigint[])
            """, new_ids)
        self.assertEqual((old["review_status"], old["ai_visible"]), ("superseded", False))
        self.assertTrue(all(row["review_status"] == "approved" and row["ai_visible"] for row in rows))

    async def test_duplicate_group_approval_is_serialized(self):
        old_id = await self._card("approved", True)
        await self.db.begin_experience_generation_job(
            "approval-race", "split", "main-session", [101, 102], old_id
        )
        new_ids = await self.db.complete_experience_generation_job(
            "approval-race", self._generated("first") + self._generated("second"), "test-model"
        )
        results = await asyncio.gather(
            self.db.approve_experience_replacement(new_ids[0]),
            self.db.approve_experience_replacement(new_ids[1]),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(item, list) for item in results), 1)
        self.assertEqual(sum(isinstance(item, ValueError) for item in results), 1)

    async def test_model_failure_keeps_source_unchanged(self):
        old_id = await self._card()
        await self.db.begin_experience_generation_job(
            "failed-job", "regenerate", "main-session", [101, 102], old_id
        )
        await self.db.fail_experience_generation_job("failed-job", "TimeoutError")
        pool = await self.db.get_pool()
        async with pool.acquire() as conn:
            old = await conn.fetchrow(
                "SELECT review_status,ai_visible FROM shared_experience_cards WHERE id=$1", old_id
            )
            count = await conn.fetchval("SELECT COUNT(*) FROM shared_experience_cards")
        self.assertEqual((old["review_status"], old["ai_visible"]), ("pending", False))
        self.assertEqual(count, 1)

    async def test_job_idempotency_and_payload_binding(self):
        old_id = await self._card()
        first, created = await self.db.begin_experience_generation_job(
            "same-job", "split", "main-session", [101, 102], old_id
        )
        second, created_again = await self.db.begin_experience_generation_job(
            "same-job", "split", "main-session", [101, 102], old_id
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaisesRegex(ValueError, "job_id_payload_mismatch"):
            await self.db.begin_experience_generation_job(
                "same-job", "regenerate", "main-session", [101, 102], old_id
            )

    async def test_concurrent_same_job_id_creates_one_job(self):
        old_id = await self._card()
        results = await asyncio.gather(
            self.db.begin_experience_generation_job(
                "concurrent-job", "split", "main-session", [101, 102], old_id
            ),
            self.db.begin_experience_generation_job(
                "concurrent-job", "split", "main-session", [101, 102], old_id
            ),
        )
        self.assertEqual(sorted(created for _, created in results), [False, True])
        pool = await self.db.get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM experience_card_generation_jobs
                WHERE job_id='concurrent-job'
            """)
        self.assertEqual(count, 1)

    async def test_duplicate_click_on_same_card_is_rejected(self):
        old_id = await self._card()
        await self.db.begin_experience_generation_job(
            "click-one", "split", "main-session", [101, 102], old_id
        )
        with self.assertRaisesRegex(ValueError, "job_already_running"):
            await self.db.begin_experience_generation_job(
                "click-two", "split", "main-session", [101, 102], old_id
            )


if __name__ == "__main__":
    unittest.main()
