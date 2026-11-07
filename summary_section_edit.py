"""Safe date-section editing within existing partition summary parts.

The historical auto-fold archive can be one 25k-character DB part containing
dozens of dated headings. Edit only an identified section without rewriting
the surrounding source or moving a_start_round.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from database import get_pool, get_session_cache_state

# Match dated diary headings only at start of line. Normal prose and other
# bracketed titles within paragraphs must not become replacement boundaries.
DATE_HEADING = re.compile(
    r"(?m)^【(?:20\d{2}[-年]\d{1,2}(?:[-月]\d{1,2}(?:日)?)?)"
    r"[^】\n]{0,70}】[^\n]*"
)
SNAPSHOT_FORMAT = "summary_repair_snapshot_v1"
DATE_PREFIX = re.compile(r"^【(20\d{2}[-年]\d{1,2}(?:[-月]\d{1,2}(?:日)?)?)")


def _digest(parts, cursor):
    payload = json.dumps([parts, int(cursor)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def split_sections(parts):
    result = []
    for part_no, part in enumerate(parts):
        headings = list(DATE_HEADING.finditer(part))
        if not headings:
            if part.strip():
                result.append(dict(part_index=part_no, section_index=0,
                                   start=0, end=len(part), heading="整段（无日期标题）",
                                   text=part))
            continue
        # The prefix remains untouched and isn't itself an editable section.
        for section_no, marker in enumerate(headings):
            end = headings[section_no + 1].start() if section_no + 1 < len(headings) else len(part)
            result.append(dict(
                part_index=part_no, section_index=section_no,
                start=marker.start(), end=end, heading=marker.group(0),
                text=part[marker.start():end],
            ))
    return result


def _validate_selection(parts, part_index, section_index):
    if not isinstance(part_index, int) or isinstance(part_index, bool):
        raise HTTPException(400, "invalid_part_index")
    if not isinstance(section_index, int) or isinstance(section_index, bool):
        raise HTTPException(400, "invalid_section_index")
    for section in split_sections(parts):
        if section["part_index"] == part_index and section["section_index"] == section_index:
            return section
    raise HTTPException(404, "summary_section_not_found_refresh_editor")


async def read(session_id):
    state = await get_session_cache_state(session_id)
    parts = state["summary_parts"]
    cursor = int(state["a_start_round"])
    return {
        "session_id": session_id, "part_count": len(parts),
        "a_start_round": cursor, "state_token": _digest(parts, cursor),
        "sections": [
            {
                "part_index": section["part_index"],
                "section_index": section["section_index"],
                "heading": section["heading"],
                "chars": len(section["text"]),
                "text": section["text"],
            }
            for section in split_sections(parts)
        ],
    }


async def replace(session_id, part_index, section_index, expected_token, original_text, replacement, merge_next=False, next_original_text=None):
    if not isinstance(replacement, str) or not replacement.strip():
        raise HTTPException(400, "replacement_cannot_be_empty")
    if not isinstance(original_text, str) or not isinstance(expected_token, str):
        raise HTTPException(400, "invalid_original_or_token")
    if replacement == original_text:
        raise HTTPException(400, "no_changes_to_save")
    if not isinstance(merge_next, bool):
        raise HTTPException(400, "invalid_merge_flag")
    if len(replacement) > 100000:
        raise HTTPException(400, "replacement_too_long")
    # The dated header remains byte-for-byte the same: this safeguards
    # neighboring section boundaries and date-based navigation.
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT summary, a_start_round FROM session_cache_state "
                "WHERE session_id=$1 FOR UPDATE", session_id,
            )
            if not current:
                raise HTTPException(404, "session_summary_not_found")
            try:
                parts = json.loads(current["summary"] or "[]")
            except (ValueError, TypeError):
                parts = [current["summary"] or ""]
            if not isinstance(parts, list) or any(not isinstance(p, str) for p in parts):
                raise HTTPException(409, "invalid_current_summary_structure")
            cursor = int(current["a_start_round"] or 0)
            if _digest(parts, cursor) != expected_token:
                raise HTTPException(409, "summary_changed_since_open_refresh_editor")
            section = _validate_selection(parts, part_index, section_index)
            if section["text"] != original_text:
                raise HTTPException(409, "selected_section_changed_refresh_editor")
            original_heading = section["heading"]
            end_offset = section["end"]
            if original_heading != "整段（无日期标题）":
                old_date = DATE_PREFIX.match(original_heading)
                new_date = DATE_PREFIX.match(replacement)
                if not old_date or not new_date or old_date.group(1) != new_date.group(1):
                    raise HTTPException(400, "keep_original_date_unchanged")
                matches = list(DATE_HEADING.finditer(replacement))
                if len(matches) != 1 or matches[0].start() != 0:
                    raise HTTPException(400, "replacement_must_contain_one_date_heading")
            elif merge_next:
                raise HTTPException(400, "cannot_merge_undated_section")
            if merge_next:
                next_section = next((
                    item for item in split_sections(parts)
                    if item["part_index"] == part_index and
                       item["section_index"] == section_index + 1
                ), None)
                if next_section is None or next_section["start"] != section["end"]:
                    raise HTTPException(400, "next_section_not_in_same_summary_part")
                next_date = DATE_PREFIX.match(next_section["heading"])
                current_date = DATE_PREFIX.match(original_heading)
                if not next_date or not current_date or next_date.group(1) != current_date.group(1):
                    raise HTTPException(400, "merge_requires_same_date")
                if next_original_text != next_section["text"]:
                    raise HTTPException(409, "next_section_changed_refresh_editor")
                end_offset = next_section["end"]
            next_part = (
                parts[part_index][:section["start"]] + replacement +
                parts[part_index][end_offset:]
            )
            updated_parts = list(parts)
            updated_parts[part_index] = next_part
            snapshot = json.dumps({
                "format": SNAPSHOT_FORMAT,
                "summary_parts": parts,
                "a_start_round": cursor,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "reason": "manual_date_section_edit",
                "part_index": part_index,
                "section_index": section_index,
                "merge_next": merge_next,
            }, ensure_ascii=False)
            snap = await conn.fetchrow(
                "INSERT INTO historical_summaries(session_id, period_label, content, source) "
                "VALUES($1,$2,$3,'repair_snapshot') RETURNING id",
                session_id, "单节摘要修改前自动快照", snapshot,
            )
            await conn.execute(
                "UPDATE session_cache_state SET summary=$2, a_start_round=$3, updated_at=NOW() "
                "WHERE session_id=$1",
                session_id, json.dumps(updated_parts, ensure_ascii=False), cursor,
            )
    return {
        "status": "ok",
        "snapshot_id": snap["id"],
        "part_count": len(updated_parts),
        "a_start_round": cursor,
        "summary_length": sum(map(len, updated_parts)),
        "old_section_chars": len(original_text),
        "new_section_chars": len(replacement),
        "merged_next": merge_next,
    }
