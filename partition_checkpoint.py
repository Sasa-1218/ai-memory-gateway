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


def plan_legacy_resume(rounds, partition_size):
    """Anchor a bounded replay by message ID without trusting old position cursors.

    Leading assistant-only rounds can remain after the user message was deleted.
    Exclude those ghost rounds from the summary replay and record them for audit.
    """
    x = int(partition_size)
    if x < 1:
        raise ValueError("partition_size_must_be_positive")
    if not rounds:
        return None
    user_rounds = []
    for index, round_messages in enumerate(rounds):
        ids = [
            int(message["_history_id"])
            for message in round_messages
            if message.get("role") == "user" and message.get("_history_id") is not None
        ]
        if len(ids) == 0 and index == 0:
            # Orphaned initial assistant/tool messages after deletion.
            continue
        if len(ids) != 1:
            return None
        user_rounds.append((index, ids[0]))
    if not user_rounds:
        return None
    user_ids = [value for _, value in user_rounds]
    if any(left >= right for left, right in zip(user_ids, user_ids[1:])):
        return None
    first_replay = max(0, len(user_rounds) - x)
    first_replay_index = user_rounds[first_replay][0]
    checkpoint = user_ids[first_replay - 1] if first_replay else 0
    return {
        "checkpoint_user_id": checkpoint,
        "replayed_recent_rounds": len(user_rounds) - first_replay,
        "first_replay_round_index": first_replay_index,
        "first_replay_user_id": user_ids[first_replay],
        "latest_user_id_at_migration": user_ids[-1],
        "history_rounds_at_migration": len(rounds),
        "leading_orphan_rounds": user_rounds[0][0],
        "coverage_uncertain_before_user_id": user_ids[first_replay],
    }


def should_use_basic_cache(total_rounds, partition_size, summary_exists):
    """Never drop an existing summary just because history was deleted."""
    return total_rounds < partition_size and not summary_exists


def capped_rotation_count(configured_count):
    """Bound per-request summary model calls in every trigger mode."""
    return max(1, min(int(configured_count), 8))
