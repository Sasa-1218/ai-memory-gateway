"""Deterministic Shadow Mind Phase A2 state transitions.

This module has no database, network, model, or production prompt dependencies.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


DRIVE_FIELDS = ("longing", "curiosity", "share", "warmth", "concern")
EMOTION_FIELDS = ("valence", "arousal", "connection", "tension", "hurt", "fatigue")
STATE_FIELDS = DRIVE_FIELDS + EMOTION_FIELDS
EVENT_TYPES = {
    "normal_chat",
    "user_replied",
    "warm_exchange",
    "topic_continued",
    "correction_detected",
    "boundary_mentioned",
    "conflict_possible",
    "repair_possible",
    "silence_elapsed",
}

BASE_STATE = {
    "longing": 24,
    "curiosity": 28,
    "share": 26,
    "warmth": 62,
    "concern": 8,
    "valence": 20,
    "arousal": 24,
    "connection": 72,
    "tension": 10,
    "hurt": 4,
    "fatigue": 20,
}


def clamp(field: str, value: int | float) -> int:
    value = int(round(value))
    if field == "valence":
        return max(-100, min(100, value))
    return max(0, min(100, value))


def normalize_state(state: dict | None) -> dict:
    state = state or {}
    return {field: clamp(field, state.get(field, BASE_STATE[field])) for field in STATE_FIELDS}


def _fatigue_target(now: datetime) -> int:
    hour = now.astimezone(ZoneInfo("Asia/Shanghai")).hour
    if hour >= 23 or hour < 6:
        return 58
    if 6 <= hour < 10:
        return 30
    if 13 <= hour < 15:
        return 38
    return 20


def _apply(state: dict, changes: dict) -> tuple[dict, dict]:
    previous = normalize_state(state)
    current = dict(previous)
    for field, delta in changes.items():
        if field in STATE_FIELDS:
            current[field] = clamp(field, current[field] + delta)
    deltas = {field: current[field] - previous[field] for field in STATE_FIELDS if current[field] != previous[field]}
    return current, deltas


def settle_normal_chat(state: dict | None, now: datetime) -> tuple[dict, dict, str, float]:
    """Apply a bounded transition for the verified fact that a normal turn completed."""
    current = normalize_state(state)
    fatigue_delta = max(-2, min(2, _fatigue_target(now) - current["fatigue"]))
    changes = {
        "longing": -4,
        "curiosity": 2,
        "share": 1,
        "warmth": 2,
        "valence": 2,
        "arousal": 4,
        "connection": 1,
        "tension": -1,
        "fatigue": fatigue_delta,
    }
    result, deltas = _apply(current, changes)
    return result, deltas, "normal_chat_completed", 1.0


def settle_elapsed(state: dict | None, last_computed_at: datetime, now: datetime) -> tuple[dict, dict, str, float]:
    """Lazily settle elapsed time; time never implies conflict resolution."""
    current = normalize_state(state)
    elapsed_minutes = max(0, int((now - last_computed_at).total_seconds() // 60))
    if elapsed_minutes < 45:
        return current, {}, "elapsed_below_change_threshold", 1.0

    changes = {
        "longing": min(8, elapsed_minutes // 60),
        "arousal": -min(8, elapsed_minutes // 45),
        "tension": -min(6, elapsed_minutes // 90),
        "hurt": -min(3, elapsed_minutes // 240),
    }
    # Connection and concern intentionally do not change from silence alone.
    fatigue_step = min(6, max(1, elapsed_minutes // 120))
    target = _fatigue_target(now)
    changes["fatigue"] = max(-fatigue_step, min(fatigue_step, target - current["fatigue"]))
    result, deltas = _apply(current, changes)
    return result, deltas, "elapsed_time_settlement", 1.0
