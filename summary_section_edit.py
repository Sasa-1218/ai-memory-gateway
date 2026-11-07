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
# Diagnostic only: a date-shaped title embedded in prose may be a former
# section boundary accidentally joined by an older save. Never auto-change it.
INLINE_DATE = re.compile(r"【20\d{2}[-年]\d{1,2}(?:[-月]\d{1,2}(?:日)?)?[^】\n]{0,70}】")


def _digest(parts, cursor):
    payload = json.dumps([parts, int(cursor)], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def splice_section(part, start, end, replacement):
    """Replace a section without swallowing the following date heading.

    A textarea commonly has no final newline. If the old section is followed
    immediately by the next dated heading, add a separator so it remains a
    stand-alone line and a distinct section on the next read.
    """
    prefix, suffix = part[:start], part[end:]
    if suffix and DATE_HEADING.match(suffix) and not replacement.endswith("\n"):
        replacement += "\n\n"
    return prefix + replacement + suffix


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
                "suspected_joined_dates": [
                    match.group(0) for match in INLINE_DATE.finditer(section["text"])
                    if match.start() > 0 and section["text"][match.start() - 1] != "\n"
                ],
            }
            for section in split_sections(parts)
        ],
    }


def _range_edit(parts, start_part, start_section, end_part, end_section, original_text, replacement):
    """Pure range edit. Replaces inclusive dated sections, across DB parts if needed."""
    sections = split_sections(parts)
    first = next((x for x in sections if
                  (x["part_index"], x["section_index"]) == (start_part, start_section)), None)
    last = next((x for x in sections if
                 (x["part_index"], x["section_index"]) == (end_part, end_section)), None)
    if first is None or last is None or sections.index(first) > sections.index(last):
        raise HTTPException(400, "invalid_date_range")
    if first["heading"] == "整段（无日期标题）" or last["heading"] == "整段（无日期标题）":
        raise HTTPException(400, "date_range_requires_dated_sections")
    if not isinstance(replacement, str) or not replacement.strip() or len(replacement) > 150000:
        raise HTTPException(400, "invalid_replacement_length")
    markers = list(DATE_HEADING.finditer(replacement))
    if not markers or markers[0].start() != 0:
        raise HTTPException(400, "replacement_must_start_with_date_heading")
    if any(m.start() > 0 and replacement[m.start() - 1] != "\n"
           for m in INLINE_DATE.finditer(replacement)):
        raise HTTPException(400, "new_draft_contains_joined_date_heading")

    # Every replacement date must be inside the original selected inclusive span.
    # Do not infer dates from prose; only date-heading starts determine boundaries.
    def parse_date(heading):
        m = re.match(r"^【(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", heading)
        if not m:
            raise HTTPException(400, "range_requires_full_ymd_headings")
        from datetime import date
        try:
            return date(*map(int, m.groups()))
        except ValueError:
            raise HTTPException(400, "invalid_date_in_heading")
    from_date, to_date = parse_date(first["heading"]), parse_date(last["heading"])
    dates = [parse_date(m.group(0)) for m in markers]
    if (from_date > to_date or dates[0] != from_date or
            any(d < from_date or d > to_date for d in dates) or
            any(a > b for a, b in zip(dates, dates[1:]))):
        raise HTTPException(400, "replacement_dates_must_stay_in_selected_range")

    pi, pj = first["part_index"], last["part_index"]
    if pi == pj:
        actual_original = parts[pi][first["start"]:last["end"]]
        next_part = splice_section(parts[pi], first["start"], last["end"], replacement)
        updated = list(parts)
        updated[pi] = next_part
    else:
        # Reject unexpected non-section prefixes inside the span. They must
        # never be silently removed or moved without being shown to the user.
        for part_index in range(pi + 1, pj + 1):
            first_in_part = next((x for x in sections if x["part_index"] == part_index), None)
            if first_in_part is None or first_in_part["start"] != 0:
                raise HTTPException(409, "unrecognized_part_prefix_in_range")
        actual_original = (
            parts[pi][first["start"]:] +
            "".join(parts[pi + 1:pj]) +
            parts[pj][:last["end"]]
        )
        tail = parts[pj][last["end"]:]
        candidate = parts[pi][:first["start"]] + replacement
        if tail and DATE_HEADING.match(tail) and not candidate.endswith("\n"):
            candidate += "\n\n"
        updated = list(parts[:pi]) + [candidate + tail] + list(parts[pj + 1:])
    if actual_original != original_text:
        raise HTTPException(409, "selected_range_changed_reload_preview")
    if actual_original == replacement:
        raise HTTPException(400, "no_changes_to_save")

    # Verify the outside of the requested range is byte-identical and all
    # replacement headings are still recognized as standalone date headings.
    old_prefix = parts[pi][:first["start"]]
    old_suffix = parts[pj][last["end"]:]
    if not updated[pi].startswith(old_prefix) or (
        old_suffix and not updated[pi].endswith(old_suffix)
    ):
        raise HTTPException(409, "outside_range_modified")
    new_dated = DATE_HEADING.findall(updated[pi])
    if len(new_dated) != len(DATE_HEADING.findall(old_prefix)) + len(markers) + len(DATE_HEADING.findall(old_suffix)):
        raise HTTPException(409, "date_boundary_integrity_check_failed")
    if updated[:pi] != parts[:pi] or updated[pi + 1:] != parts[pj + 1:]:
        raise HTTPException(409, "unselected_parts_modified")
    return updated, {"old_chars": len(actual_original), "new_chars": len(replacement),
                     "selected_sections": sections.index(last) - sections.index(first) + 1,
                     "first_heading": first["heading"], "last_heading": last["heading"]}


async def replace_range(session_id, start_part, start_section, end_part, end_section,
                        expected_token, original_text, replacement):
    if not isinstance(expected_token, str) or not isinstance(original_text, str):
        raise HTTPException(400, "invalid_original_or_token")
    for value in (start_part, start_section, end_part, end_section):
        if not isinstance(value, int) or isinstance(value, bool):
            raise HTTPException(400, "invalid_range_index")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT summary, a_start_round FROM session_cache_state "
                "WHERE session_id=$1 FOR UPDATE", session_id)
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
                raise HTTPException(409, "summary_changed_since_open_reload_preview")
            updated_parts, audit = _range_edit(
                parts, start_part, start_section, end_part, end_section, original_text, replacement)
            snapshot = json.dumps({
                "format": SNAPSHOT_FORMAT, "summary_parts": parts,
                "a_start_round": cursor, "saved_at": datetime.now(timezone.utc).isoformat(),
                "reason": "manual_date_range_replace",
                "first_heading": audit["first_heading"],
                "last_heading": audit["last_heading"],
            }, ensure_ascii=False)
            snap = await conn.fetchrow(
                "INSERT INTO historical_summaries(session_id, period_label, content, source) "
                "VALUES($1,$2,$3,'repair_snapshot') RETURNING id",
                session_id, "日期范围替换前自动快照", snapshot)
            await conn.execute(
                "UPDATE session_cache_state SET summary=$2, a_start_round=$3, updated_at=NOW() "
                "WHERE session_id=$1",
                session_id, json.dumps(updated_parts, ensure_ascii=False), cursor)
    return {
        "status": "ok", "snapshot_id": snap["id"],
        "a_start_round": cursor, "part_count": len(updated_parts),
        "previous_summary_length": sum(map(len, parts)),
        "summary_length": sum(map(len, updated_parts)),
        "saved_state_token": _digest(updated_parts, cursor), **audit,
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
            next_part = splice_section(
                parts[part_index], section["start"], end_offset, replacement,
            )
            # Validate the complete dated-section structure before writing.
            trailing = parts[part_index][end_offset:]
            if original_heading != "整段（无日期标题）":
                expected_count = len(DATE_HEADING.findall(parts[part_index])) - (1 if merge_next else 0)
                if len(DATE_HEADING.findall(next_part)) != expected_count:
                    raise HTTPException(409, "date_boundary_integrity_check_failed")
                expected_text = replacement + (
                    "\n\n" if trailing and DATE_HEADING.match(trailing)
                    and not replacement.endswith("\n") else ""
                )
                after_sections = split_sections([next_part])
                actual = next((item for item in after_sections
                               if item["section_index"] == section_index), None)
                if actual is None or actual["text"] != expected_text:
                    raise HTTPException(409, "saved_section_integrity_check_failed")
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
        "previous_summary_length": sum(map(len, parts)),
        "old_section_chars": len(original_text),
        "new_section_chars": len(replacement),
        "merged_next": merge_next,
    }
