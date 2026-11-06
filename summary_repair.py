"""Explicit, preview-first recovery of a damaged partition summary. Never runs on startup."""
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
from database import get_pool, get_session_cache_state

_ARCHIVE_PART = re.compile(r"(?m)^【旧摘要(\d+)】\s*\n")
CONFIRM = "确认恢复摘要"
ROLLBACK_CONFIRM = "确认回滚摘要"


def _split_archive(content):
    marks = list(_ARCHIVE_PART.finditer(content or ""))
    if not marks or marks[0].start() != 0:
        raise HTTPException(400, "archive_format_not_auto_fold_original_parts")
    numbers = [int(m.group(1)) for m in marks]
    if numbers != list(range(1, len(marks) + 1)):
        raise HTTPException(400, "archive_part_numbers_invalid")
    parts = []
    for i, marker in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(content)
        part = content[marker.end():end].strip("\n")
        if not part.strip():
            raise HTTPException(400, "archive_contains_empty_part")
        parts.append(part)
    return parts


def _state_digest(summary_parts, cursor):
    return hashlib.sha256(json.dumps(
        [summary_parts, cursor], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


async def _source(session_id, archive_id, start_round, preserved_parts, x):
    if not session_id or x != 8:
        raise HTTPException(400, "active_session_or_x8_required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        archive = await conn.fetchrow(
            "SELECT id, session_id, content, period_label, source FROM historical_summaries WHERE id=$1",
            archive_id,
        )
        if not archive or archive["session_id"] != session_id or archive["source"] != "auto_fold":
            raise HTTPException(404, "matching_auto_fold_archive_not_found")
        rows = await conn.fetch(
            "SELECT role, content, metadata, created_at FROM conversations "
            "WHERE session_id=$1 ORDER BY created_at ASC", session_id
        )
    state = await get_session_cache_state(session_id)
    existing = state["summary_parts"]
    cursor = int(state["a_start_round"])
    if not existing or start_round < 0 or start_round > cursor:
        raise HTTPException(400, "invalid_rewind_start_round")
    if (cursor - start_round) % x:
        raise HTTPException(400, "rewind_must_align_to_8_rounds")
    if preserved_parts < 0 or preserved_parts > len(existing) - 1:
        raise HTTPException(400, "invalid_preserved_parts_count")
    archive_parts = _split_archive(archive["content"])
    # First current part is the damaged folded base. Preserve only explicitly selected tail.
    retained = existing[1:1 + preserved_parts]
    from database import db_row_to_message
    from main import group_by_rounds
    messages = []
    for row in rows:
        m = db_row_to_message(dict(row))
        m["created_at"] = row["created_at"]
        if m.get("role") == "tool":
            prior = messages[-1] if messages else None
            if not prior or not (prior.get("role") == "tool" or
                                (prior.get("role") == "assistant" and prior.get("tool_calls"))):
                continue
        messages.append(m)
    rounds = group_by_rounds(messages)
    if cursor > len(rounds):
        raise HTTPException(409, "cursor_exceeds_available_history")
    blocks = []
    for start in range(start_round, cursor, x):
        chunk = rounds[start:start + x]
        if len(chunk) != x:
            raise HTTPException(409, "incomplete_recovery_block")
        blocks.append([m for one in chunk for m in one])
    if len(blocks) > 32:
        raise HTTPException(400, "too_many_blocks_split_repair")
    # Digest raw source as well: a changed conversation invalidates a prior preview.
    history_hash = hashlib.sha256()
    for row in rows:
        for key in ("role", "content", "metadata", "created_at"):
            history_hash.update(str(row[key]).encode("utf-8"))
            history_hash.update(b"\x00")
    signature = hashlib.sha256(json.dumps(
        [session_id, archive_id, start_round, preserved_parts, x,
         _state_digest(existing, cursor), history_hash.hexdigest(),
         hashlib.sha256(archive["content"].encode("utf-8")).hexdigest()],
        ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return {
        "archive": dict(archive), "existing": existing, "cursor": cursor,
        "archive_parts": archive_parts, "retained": retained,
        "blocks": blocks, "rounds": len(rounds), "signature": signature,
    }


async def preview(session_id, archive_id, start_round, preserved_parts, x):
    s = await _source(session_id, archive_id, start_round, preserved_parts, x)
    return {
        "session_id": session_id, "archive_id": archive_id,
        "archive_label": s["archive"]["period_label"],
        "archive_source": s["archive"]["source"],
        "archive_chars": len(s["archive"]["content"]),
        "archive_parts": len(s["archive_parts"]),
        "current_chars": sum(map(len, s["existing"])),
        "current_parts": len(s["existing"]),
        "current_a_start_round": s["cursor"],
        "rewind_to_round": start_round,
        "keep_existing_parts": preserved_parts,
        "retained_preview": [
            {"index": i + 2, "chars": len(p), "text": p}
            for i, p in enumerate(s["retained"])
        ],
        # Boundary audit is READ-ONLY. Date mentions inside text can refer to older events,
        # so provide the actual paragraphs, not an unreliable automatic "safe" verdict.
        "archive_tail": [
            {"index": len(s["archive_parts"]) - min(3, len(s["archive_parts"])) + i + 1,
             "chars": len(part), "text": part}
            for i, part in enumerate(s["archive_parts"][-3:])
        ],
        "discarded_current_parts": [
            {"index": i + 2, "chars": len(part), "text": part}
            for i, part in enumerate(s["existing"][1 + preserved_parts:], start=preserved_parts)
        ],
        "boundary_check_note": (
            "逐段检查归档末尾、将舍弃的近期段及原始对话的时间范围。"
            "日期文字可能是在追述过去事件；预览不自动宣称无重叠或无遗漏。"
        ),
        "regenerate_blocks": len(s["blocks"]),
        "regenerate_round_ranges": [
            [start_round + i*x, start_round + (i+1)*x]
            for i in range(len(s["blocks"]))
        ],
        "block_date_ranges": [
            [
                block[0]["created_at"].astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
                if block and isinstance(block[0].get("created_at"), datetime) else "未知",
                block[-1]["created_at"].astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
                if block and isinstance(block[-1].get("created_at"), datetime) else "未知",
            ]
            for block in s["blocks"]
        ],
        "total_conversation_rounds": s["rounds"],
        "new_cursor_after_repair": s["cursor"],
        "preview_token": s["signature"],
        "requires_confirmation": CONFIRM,
        "changes_on_preview": False,
    }


async def execute(session_id, archive_id, start_round, preserved_parts, x, token, confirmation, generator):
    if confirmation != CONFIRM:
        raise HTTPException(400, "confirmation_phrase_required")
    s = await _source(session_id, archive_id, start_round, preserved_parts, x)
    if not token or s["signature"] != token:
        raise HTTPException(409, "preview_stale_refresh_before_execute")
    generated = []
    for block_index, block in enumerate(s["blocks"], start=1):
        summary = await generator(block, session_id)
        summary = summary.strip() if isinstance(summary, str) else ""
        if not summary:
            print(
                f"[summary-repair] invalid_output block={block_index}/{len(s['blocks'])} "
                f"reason=empty_result input_messages={len(block)}",
                flush=True,
            )
            raise HTTPException(
                502, f"summary_block_{block_index}_empty_result_no_state_changed"
            )
        if len(summary) > 600:
            print(
                f"[summary-repair] over_limit_retry block={block_index}/{len(s['blocks'])} "
                f"first_chars={len(summary)} input_messages={len(block)}",
                flush=True,
            )
            # One bounded fresh attempt with the existing, unchanged prompt.
            # Never cut the ending or store an over-limit draft.
            retry = await generator(block, session_id)
            summary = retry.strip() if isinstance(retry, str) else ""
            if len(summary) > 600 or not summary:
                failure = "empty_after_retry" if not summary else "over_600_after_retry"
                print(
                    f"[summary-repair] invalid_output block={block_index}/{len(s['blocks'])} "
                    f"reason={failure} chars={len(summary)}",
                    flush=True,
                )
                raise HTTPException(
                    502, f"summary_block_{block_index}_{failure}_"
                         f"actual_{len(summary)}_no_state_changed"
                )
        print(
            f"[summary-repair] validated block={block_index}/{len(s['blocks'])} "
            f"chars={len(summary)}",
            flush=True,
        )
        generated.append(summary)
    next_parts = s["archive_parts"] + s["retained"] + generated
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT summary, a_start_round FROM session_cache_state "
                "WHERE session_id=$1 FOR UPDATE", session_id
            )
            if not current:
                raise HTTPException(409, "state_missing")
            try:
                old_parts = json.loads(current["summary"] or "[]")
            except ValueError:
                old_parts = [current["summary"]]
            if _state_digest(old_parts, current["a_start_round"]) != _state_digest(s["existing"], s["cursor"]):
                raise HTTPException(409, "state_changed_during_generation_no_changes_applied")
            # Save full JSON snapshot (including cursor) transactionally before replacing state.
            snapshot = json.dumps({
                "format": "summary_repair_snapshot_v1",
                "summary_parts": old_parts,
                "a_start_round": s["cursor"],
                "source_archive_id": archive_id,
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False)
            snap = await conn.fetchrow(
                "INSERT INTO historical_summaries(session_id, period_label, content, source) "
                "VALUES($1,$2,$3,'repair_snapshot') RETURNING id",
                session_id, "摘要恢复前自动快照", snapshot
            )
            await conn.execute(
                "UPDATE session_cache_state SET summary=$2, a_start_round=$3, updated_at=NOW() "
                "WHERE session_id=$1",
                session_id, json.dumps(next_parts, ensure_ascii=False), s["cursor"]
            )
    return {
        "status": "recovered", "snapshot_id": snap["id"],
        "summary_parts": next_parts, "a_start_round": s["cursor"],
        "generated_blocks": len(generated),
        "note": "No startup migration. Snapshot is available for explicit rollback.",
    }


async def rollback(session_id, snapshot_id, confirmation):
    if confirmation != ROLLBACK_CONFIRM:
        raise HTTPException(400, "rollback_confirmation_required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            snap = await conn.fetchrow(
                "SELECT content FROM historical_summaries WHERE id=$1 "
                "AND session_id=$2 AND source='repair_snapshot'", snapshot_id, session_id
            )
            if not snap:
                raise HTTPException(404, "repair_snapshot_not_found")
            data = json.loads(snap["content"])
            if data.get("format") != "summary_repair_snapshot_v1":
                raise HTTPException(400, "snapshot_format_invalid")
            current = await conn.fetchrow(
                "SELECT summary, a_start_round FROM session_cache_state "
                "WHERE session_id=$1 FOR UPDATE", session_id
            )
            if not current:
                raise HTTPException(409, "state_missing")
            # Preserve current repaired state before rolling back as well.
            backup = json.dumps({
                "format": "summary_repair_snapshot_v1",
                "summary_parts": json.loads(current["summary"] or "[]"),
                "a_start_round": current["a_start_round"],
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }, ensure_ascii=False)
            reciprocal = await conn.fetchrow(
                "INSERT INTO historical_summaries(session_id, period_label, content, source) "
                "VALUES($1,$2,$3,'repair_snapshot') RETURNING id",
                session_id, "摘要回滚前自动快照", backup
            )
            await conn.execute(
                "UPDATE session_cache_state SET summary=$2,a_start_round=$3,updated_at=NOW() "
                "WHERE session_id=$1",
                session_id, json.dumps(data["summary_parts"], ensure_ascii=False),
                int(data["a_start_round"])
            )
    return {"status": "rolled_back", "saved_current_as_snapshot_id": reciprocal["id"],
            "summary_parts": data["summary_parts"], "a_start_round": data["a_start_round"]}
