import unittest

from experience_cards import (
    SHARED_EXPERIENCE_CARD_PROMPT,
    apply_card_update,
    normalize_card_update,
    restore_card_update,
    soft_delete_card_update,
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


if __name__ == "__main__":
    unittest.main()
