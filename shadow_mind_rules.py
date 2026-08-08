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

CHAT_BURST_MINUTES = 30
DRIVE_SOFT_CEIL = {
    "longing": 80,
    "curiosity": 70,
    "share": 65,
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


def _toward(current: int, target: int, step: int) -> int:
    if current == target or step <= 0:
        return 0
    return max(-step, min(step, target - current))


def _fatigue_load(recent_turns: int) -> int:
    """Small bounded load from sustained interaction, not from every message."""
    recent_turns = max(0, int(recent_turns))
    return min(6, max(0, (recent_turns - 1) // 4) * 2)


def _apply(state: dict, changes: dict) -> tuple[dict, dict]:
    previous = normalize_state(state)
    current = dict(previous)
    for field, delta in changes.items():
        if field in STATE_FIELDS:
            current[field] = clamp(field, current[field] + delta)
    deltas = {field: current[field] - previous[field] for field in STATE_FIELDS if current[field] != previous[field]}
    return current, deltas


def settle_normal_chat(
    state: dict | None,
    now: datetime,
    recent_turns: int = 1,
    new_burst: bool = True,
) -> tuple[dict, dict, str, float]:
    """Settle only facts known from timing and interaction density."""
    current = normalize_state(state)
    recent_turns = max(1, int(recent_turns or 1))
    arousal_target = BASE_STATE["arousal"] + min(24, max(0, recent_turns - 1) * 2)
    changes = {"arousal": _toward(current["arousal"], arousal_target, 2)}

    if new_burst:
        changes["longing"] = -min(8, max(2, round(current["longing"] * 0.20)))
        reason = "normal_chat_burst_started"
    else:
        # Add load only when the rolling hour crosses a density band (5/9/13).
        fatigue_target = _fatigue_target(now) + _fatigue_load(recent_turns)
        changes["fatigue"] = (
            _toward(current["fatigue"], fatigue_target, 2)
            if recent_turns in {5, 9, 13}
            else 0
        )
        reason = "normal_chat_density_updated"

    result, deltas = _apply(current, changes)
    return result, deltas, reason, 1.0


def settle_elapsed(
    state: dict | None,
    last_computed_at: datetime,
    now: datetime,
    recent_turns: int = 0,
) -> tuple[dict, dict, str, float]:
    """Lazily settle elapsed time; time never implies conflict resolution."""
    current = normalize_state(state)
    elapsed_minutes = max(0, int((now - last_computed_at).total_seconds() // 60))
    if elapsed_minutes < 45:
        return current, {}, "elapsed_below_change_threshold", 1.0

    elapsed_hours = elapsed_minutes // 60
    changes = {
        "longing": _toward(current["longing"], DRIVE_SOFT_CEIL["longing"], min(8, elapsed_hours)),
        "curiosity": _toward(
            current["curiosity"],
            min(DRIVE_SOFT_CEIL["curiosity"], BASE_STATE["curiosity"] + elapsed_hours // 3),
            min(6, max(1, elapsed_hours)),
        ),
        "share": _toward(
            current["share"],
            min(DRIVE_SOFT_CEIL["share"], BASE_STATE["share"] + elapsed_hours // 4),
            min(6, max(1, elapsed_hours)),
        ),
        "warmth": _toward(current["warmth"], BASE_STATE["warmth"], min(4, max(1, elapsed_hours))),
        "valence": _toward(current["valence"], BASE_STATE["valence"], min(6, max(1, elapsed_hours))),
        "arousal": _toward(current["arousal"], BASE_STATE["arousal"], min(8, elapsed_minutes // 45)),
        # Silence can ease elevated tension/hurt, but never creates or resolves them.
        "tension": _toward(current["tension"], min(current["tension"], BASE_STATE["tension"]), min(6, elapsed_minutes // 90)),
        "hurt": _toward(current["hurt"], min(current["hurt"], BASE_STATE["hurt"]), min(3, elapsed_minutes // 240)),
    }
    # Connection and concern intentionally do not change from silence alone.
    fatigue_step = min(6, max(1, elapsed_minutes // 120))
    target = _fatigue_target(now) + _fatigue_load(recent_turns)
    changes["fatigue"] = max(-fatigue_step, min(fatigue_step, target - current["fatigue"]))
    result, deltas = _apply(current, changes)
    return result, deltas, "elapsed_time_settlement", 1.0
