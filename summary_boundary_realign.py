"""Preview-first manual alignment of a legacy summary's first unsummarized user turn.

No historical rewriting, guessing, or automatic database changes. A successful
confirmation snapshots the exact old summary/cursor and writes one ID watermark.
"""
import hashlib
import json

from fastapi import HTTPException
from database import get_pool, db_row_to_message

CONFIRM_BOUNDARY = "确认摘要截止位置"


def _parts(raw):
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return [raw]
    return value if isinstance(value, list) else [raw]


def _token(session_id, state, rows):
    payload = [
        session_id, state["summary"], int(state["a_start_round"] or 0),
        state["summary_checkpoint_user_id"],
        state["updated_at"].isoformat() if state["updated_at"] else None,
        [(int(row["id"]), row["role"], str(row["created_at"]))
         for row in rows],
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _rounds(rows):
    from main import group_by_rounds
    msgs = []
    for row in rows:
        m = db_row_to_message(dict(row))
        m["created_at"] = row["created_at"]
        m["_history_id"] = row["id"]
        if m.get("role") == "tool":
            prior = msgs[-1] if msgs else None
            if not prior or not (
                prior.get("role") == "tool" or
                (prior.get("role") == "assistant" and prior.get("tool_calls"))
            ):
                continue
        msgs.append(m)
    return group_by_rounds(msgs)


def _find_start(rounds, first_id):
    user_ids = []
    selected_index = None
    for index, rnd in enumerate(rounds):
        users = [
            int(m["_history_id"]) for m in rnd
            if m.get("role") == "user" and m.get("_history_id") is not None
        ]
        if len(users) > 1:
            raise HTTPException(409, "multiple_user_messages_in_single_round")
        if users:
            user_ids.append(users[0])
            if users[0] == first_id:
                selected_index = index
    if not user_ids or selected_index is None:
        raise HTTPException(409, "selected_first_unsummarized_user_not_found")
    if any(a >= b for a, b in zip(user_ids, user_ids[1:])):
        raise HTTPException(409, "chronological_user_ids_not_monotonic")
    if any(
        any(m.get("role") == "user" and m.get("_history_id") is None for m in rnd)
        for rnd in rounds
    ):
        raise HTTPException(409, "history_user_id_missing")
    return selected_index


async def preview(session_id, before_id=None, limit=80):
    if not session_id:
        raise HTTPException(400, "session_id_required")
    if type(limit) is not int or not (1 <= limit <= 200):
        raise HTTPException(400, "limit_out_of_range")
    if before_id is not None and (type(before_id) is not int or before_id <= 0):
        raise HTTPException(400, "before_id_invalid")
    pool = await get_pool()
    async with pool.acquire() as conn:
        state = await conn.fetchrow(
            "SELECT summary,a_start_round,summary_checkpoint_user_id,updated_at "
            "FROM session_cache_state WHERE session_id=$1", session_id,
        )
        if state is None or not _parts(state["summary"]):
            raise HTTPException(409, "nonempty_existing_summary_required")
        rows = await conn.fetch(
            "SELECT id,role,content,metadata,created_at FROM conversations "
            "WHERE session_id=$1 ORDER BY created_at ASC,id ASC", session_id,
        )
    users = [r for r in rows if r["role"] == "user" and
             (before_id is None or r["id"] < before_id)]
    chosen = users[-limit:]
    candidates = []
    for row in reversed(chosen):
        m = db_row_to_message(dict(row))
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        candidates.append({
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "preview": str(content or "")[:180],
        })
    return {
        "session_id": session_id,
        "requires_alignment": state["summary_checkpoint_user_id"] is None,
        "summary_parts": len(_parts(state["summary"])),
        "stored_cursor": int(state["a_start_round"] or 0),
        "total_persisted_users": sum(r["role"] == "user" for r in rows),
        "state_token": _token(session_id, state, rows),
        "candidates": candidates,
        "next_before_id": candidates[-1]["id"] if len(candidates) == limit else None,
        "instruction": "Choose the FIRST user message NOT included in the existing summary; never choose an already-summarized message.",
    }


async def confirm(session_id, first_unsummarized_user_id, state_token, confirmation):
    if confirmation != CONFIRM_BOUNDARY:
        raise HTTPException(400, "boundary_confirmation_required")
    if not session_id or type(first_unsummarized_user_id) is not int or first_unsummarized_user_id <= 0:
        raise HTTPException(400, "invalid_selected_user_id")
    if not isinstance(state_token, str) or len(state_token) != 64:
        raise HTTPException(400, "invalid_state_token")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            state = await conn.fetchrow(
                "SELECT summary,a_start_round,summary_checkpoint_user_id,updated_at "
                "FROM session_cache_state WHERE session_id=$1 FOR UPDATE", session_id,
            )
            if state is None or not _parts(state["summary"]):
                raise HTTPException(409, "nonempty_existing_summary_required")
            if state["summary_checkpoint_user_id"] is not None:
                raise HTTPException(409, "summary_already_has_checkpoint")
            rows = await conn.fetch(
                "SELECT id,role,content,metadata,created_at FROM conversations "
                "WHERE session_id=$1 ORDER BY created_at ASC,id ASC", session_id,
            )
            if _token(session_id, state, rows) != state_token:
                raise HTTPException(409, "history_or_summary_changed_refresh_preview")
            rounds = _rounds(rows)
            cursor = _find_start(rounds, first_unsummarized_user_id)
            checkpoint = first_unsummarized_user_id - 1
            snapshot = json.dumps({
                "format": "summary_repair_snapshot_v1",
                "reason": "confirmed_legacy_summary_boundary",
                "summary_parts": _parts(state["summary"]),
                "a_start_round": int(state["a_start_round"] or 0),
                "summary_checkpoint_user_id": None,
                "first_unsummarized_user_id": first_unsummarized_user_id,
            }, ensure_ascii=False)
            archived = await conn.fetchrow(
                "INSERT INTO historical_summaries(session_id,period_label,content,source) "
                "VALUES($1,$2,$3,'repair_snapshot') RETURNING id",
                session_id, "确认摘要截止位置前的完整快照", snapshot,
            )
            await conn.execute(
                "UPDATE session_cache_state SET summary_checkpoint_user_id=$2,"
                "a_start_round=$3,updated_at=NOW() WHERE session_id=$1",
                session_id, checkpoint, cursor,
            )
    return {
        "status": "aligned",
        "session_id": session_id,
        "first_unsummarized_user_id": first_unsummarized_user_id,
        "checkpoint_user_id": checkpoint,
        "effective_a_start_round": cursor,
        "snapshot_id": archived["id"],
        "summary_content_changed": False,
        "history_content_changed": False,
    }
