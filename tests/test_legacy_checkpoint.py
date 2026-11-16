"""Regression tests for safe future-only legacy checkpoint migration."""
import unittest
from partition_checkpoint import (
    first_unsummarized_round, last_user_message_id, resolve_partition_boundary,
    plan_legacy_resume,
)


def round_for(i):
    return [
        {"role": "user", "_history_id": i},
        {"role": "assistant", "_history_id": i + 1},
    ]


class LegacyCheckpointTests(unittest.TestCase):

    def test_deleted_leading_user_keeps_last_eight_usable(self):
        orphan = [{"role": "assistant", "_history_id": 2}]
        rounds = [orphan] + [round_for(i) for i in range(5, 69, 4)]
        plan = plan_legacy_resume(rounds, 8)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["leading_orphan_rounds"], 1)
        self.assertEqual(plan["first_replay_round_index"], 9)
        self.assertEqual(first_unsummarized_round(rounds, plan["checkpoint_user_id"]), 9)

    def test_deleted_first_user_short_history(self):
        orphan = [{"role": "assistant", "_history_id": 2}]
        rounds = [orphan, round_for(5), round_for(9)]
        plan = plan_legacy_resume(rounds, 8)
        self.assertEqual(plan["checkpoint_user_id"], 0)
        self.assertEqual(plan["first_replay_round_index"], 1)

    def test_legacy_resume_replays_recent_eight_and_then_rotates(self):
        rows = [round_for(i) for i in range(1, 65, 4)]
        plan = plan_legacy_resume(rows, 8)
        self.assertIsNotNone(plan)
        self.assertEqual(plan["replayed_recent_rounds"], 8)
        self.assertEqual(plan["checkpoint_user_id"], 29)
        self.assertEqual(first_unsummarized_round(rows, plan["checkpoint_user_id"]), 8)
        rows.extend(round_for(i) for i in range(65, 97, 4))
        self.assertEqual(len(rows)-first_unsummarized_round(rows, 29), 16)

    def test_legacy_resume_rejects_out_of_order_ids(self):
        rows = [round_for(i) for i in [1, 10, 5, 20]]
        self.assertIsNone(plan_legacy_resume(rows, 8))

    def test_legacy_resume_rejects_missing_user_ids(self):
        rows = [round_for(1), [{"role": "assistant", "_history_id": 2}]]
        self.assertIsNone(plan_legacy_resume(rows, 8))

    def test_legacy_resume_rejects_ambiguous_multiple_users(self):
        rows = [[{"role":"user","_history_id":1},
                 {"role":"user","_history_id":3}]]
        self.assertIsNone(plan_legacy_resume(rows, 8))

    def test_legacy_resume_no_history_returns_none(self):
        self.assertIsNone(plan_legacy_resume([], 8))

    def test_legacy_resume_with_short_history_starts_at_zero(self):
        rows = [round_for(i) for i in [3, 7, 11]]
        plan = plan_legacy_resume(rows, 8)
        self.assertEqual(plan["checkpoint_user_id"], 0)
        self.assertEqual(plan["replayed_recent_rounds"], 3)

    def test_legacy_resume_invalid_partition_size(self):
        with self.assertRaises(ValueError):
            plan_legacy_resume([round_for(1)], 0)

    def test_legacy_summary_paused_even_if_numeric_cursor_is_in_range(self):
        rows = [round_for(i) for i in range(1, 65, 4)]
        start, blocked = resolve_partition_boundary(rows, True, 12, None, 8)
        self.assertTrue(blocked)
        self.assertEqual(start, 8)

    def test_legacy_summary_paused_if_numeric_cursor_is_out_of_range(self):
        rows = [round_for(i) for i in range(1, 33, 4)]
        start, blocked = resolve_partition_boundary(rows, True, 999, None, 8)
        self.assertTrue(blocked)
        self.assertEqual(start, 0)

    def test_verified_boundary_enables_rotation_without_numeric_cursor(self):
        rows = [round_for(i) for i in range(1, 65, 4)]
        start, blocked = resolve_partition_boundary(rows, True, 999, 29, 8)
        self.assertFalse(blocked)
        self.assertEqual(start, 8)

    def test_empty_summary_resets_to_zero(self):
        rows = [round_for(i) for i in range(1, 65, 4)]
        start, blocked = resolve_partition_boundary(rows, False, 999, None, 8)
        self.assertFalse(blocked)
        self.assertEqual(start, 0)

    def test_verified_checkpoint_at_last_user_keeps_future_only(self):
        rows = [round_for(i) for i in [1, 5, 9, 13, 17, 21, 25, 29]]
        anchor = last_user_message_id([m for r in rows for m in r])
        self.assertEqual(anchor, 29)
        self.assertEqual(first_unsummarized_round(rows, anchor), 8)
        rows += [round_for(i) for i in range(33, 65, 4)]
        self.assertEqual(len(rows) - first_unsummarized_round(rows, anchor), 8)

    def test_deletions_do_not_move_boundary(self):
        old = [round_for(i) for i in [1, 5, 9, 13, 17, 21, 25, 29]]
        new = [round_for(i) for i in range(33, 65, 4)]
        for keep in ([1, 5, 9, 13, 17, 21, 25, 29], [1, 9, 29], []):
            rows = [r for r in old if r[0]["_history_id"] in keep] + new
            self.assertEqual(len(rows) - first_unsummarized_round(rows, 29), 8)

    def test_missing_checkpoint_row_is_safe(self):
        rows = [round_for(i) for i in (1, 5, 13, 21)]
        self.assertEqual(rows[first_unsummarized_round(rows, 9)][0]["_history_id"], 13)

    def test_missing_user_id_must_not_anchor(self):
        self.assertIsNone(last_user_message_id([{"role": "assistant"}]))

    def test_rotation_advances_to_last_user(self):
        rows = [round_for(i) for i in range(1, 65, 4)]
        anchor = last_user_message_id([m for r in rows[:8] for m in r])
        self.assertEqual(anchor, 29)
        self.assertEqual(first_unsummarized_round(rows, anchor), 8)
