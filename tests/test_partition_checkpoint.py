"""Regression coverage: deletions must not shift summary checkpoints."""
import unittest

from partition_checkpoint import first_unsummarized_round, last_user_message_id


def round_for(message_id):
    return [
        {"role": "user", "_history_id": message_id, "content": "hello"},
        {"role": "assistant", "_history_id": message_id + 1, "content": "hi"},
    ]


class PartitionCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.rounds = [round_for(i) for i in (1, 5, 9, 15, 20, 27, 30, 39)]

    def test_checkpoint_starts_after_last_summarized_user(self):
        self.assertEqual(first_unsummarized_round(self.rounds, 15), 4)

    def test_deleting_older_rounds_does_not_change_boundary(self):
        remaining = [r for r in self.rounds if r[0]["_history_id"] not in (5, 9)]
        self.assertEqual(first_unsummarized_round(remaining, 15), 2)
        self.assertEqual(remaining[2][0]["_history_id"], 20)

    def test_deleted_checkpoint_row_still_works(self):
        remaining = [r for r in self.rounds if r[0]["_history_id"] != 15]
        self.assertEqual(remaining[first_unsummarized_round(remaining, 15)][0]["_history_id"], 20)

    def test_no_new_rounds_after_checkpoint(self):
        self.assertEqual(first_unsummarized_round(self.rounds, 39), len(self.rounds))

    def test_new_round_after_checkpoint(self):
        updated = self.rounds + [round_for(41)]
        self.assertEqual(first_unsummarized_round(updated, 39), 8)

    def test_checkpoint_records_last_user_not_last_assistant(self):
        self.assertEqual(last_user_message_id(self.rounds[2] + self.rounds[3]), 15)

    def test_no_persisted_user_id_cannot_advance(self):
        self.assertIsNone(last_user_message_id([{"role": "assistant", "content": "hi"}]))

    def test_checkpoint_in_mid_history_after_more_deletions(self):
        remaining = [r for r in self.rounds if r[0]["_history_id"] in (1, 20, 30, 39)]
        self.assertEqual(first_unsummarized_round(remaining, 27), 2)
        self.assertEqual(last_user_message_id(remaining[2]), 30)


if __name__ == "__main__":
    unittest.main()
