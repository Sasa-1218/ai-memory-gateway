"""Regression tests for safe future-only legacy checkpoint migration."""
import unittest
from partition_checkpoint import (
    first_unsummarized_round, last_user_message_id, resolve_partition_boundary,
)


def round_for(i):
    return [
        {"role": "user", "_history_id": i},
        {"role": "assistant", "_history_id": i + 1},
    ]


class LegacyCheckpointTests(unittest.TestCase):

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
