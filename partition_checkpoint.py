"""Stable message-ID checkpoints for the partition summary pipeline.

A checkpoint records the last user message successfully summarized.
Deleting earlier conversation rows cannot change that message ID.
"""


def first_unsummarized_round(rounds, checkpoint_user_id):
    """Return first round starting after checkpoint; empty tail => len(rounds)."""
    if checkpoint_user_id is None:
        raise ValueError("checkpoint_user_id_required")
    boundary = int(checkpoint_user_id)
    for index, round_messages in enumerate(rounds):
        for message in round_messages:
            message_id = message.get("_history_id")
            if (
                message.get("role") == "user"
                and message_id is not None
                and int(message_id) > boundary
            ):
                return index
    return len(rounds)


def last_user_message_id(round_messages):
    """Return last persisted user ID in completed summary block, or None."""
    for message in reversed(round_messages):
        message_id = message.get("_history_id")
        if message.get("role") == "user" and message_id is not None:
            return int(message_id)
    return None


def resolve_partition_boundary(rounds, summary_exists, numeric_start, checkpoint_user_id, partition_size):
    """Resolve a safe boundary without inventing provenance for a legacy summary.

    Unverified old summaries are read-only until an explicit archive-based
    recovery supplies a trustworthy checkpoint. Their recent raw conversation
    remains visible to the model; the original summary is never overwritten.
    """
    if checkpoint_user_id is not None:
        return first_unsummarized_round(rounds, checkpoint_user_id), False
    if summary_exists:
        return max(0, len(rounds) - max(1, int(partition_size))), True
    return 0, False
