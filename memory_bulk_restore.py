"""Safe preview-first, atomic bulk restoration for archived layer-1 memory fragments.

Never recreate deleted IDs or remove merged event memories. Confirmation uses a
one-time state fingerprint; concurrent archive/edit changes invalidate preview.
"""
import hashlib
import json
from fastapi import HTTPException
from database import get_pool


def _ids(values):
    if not isinstance(values, list) or not values or len(values) > 500:
        raise HTTPException(status_code=400, detail="select_1_to_500_memory_ids")
    if any(type(v) is not int or v <= 0 for v in values):
        raise HTTPException(status_code=400, detail="invalid_memory_id")
    if len(set(values)) != len(values):
        raise HTTPException(status_code=400, detail="duplicate_memory_ids")
    return sorted(values)


def _fingerprint(selected, rows, parent_rows):
    details = [
        [int(row["id"]), int(row["layer"] or 1), bool(row["is_active"]),
         row["content"], str(row["created_at"])]
        for row in rows
    ]
    parents = sorted(
        [int(row["id"]), list(row["merged_from"] or []),
         bool(row["is_active"])] for row in parent_rows
    )
    payload = json.dumps([selected, details, parents],
                         ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _result(ids, rows, parent_rows):
    found = {int(r["id"]): r for r in rows}
    missing = [id for id in ids if id not in found]
    archived = [r for r in rows if int(r["layer"] or 1) == 1 and r["is_active"] is False]
    active = [int(r["id"]) for r in rows if r["is_active"] is True]
    wrong_layer = [int(r["id"]) for r in rows if int(r["layer"] or 1) != 1]
    parent_counts = {}
    for parent in parent_rows:
        for fragment_id in parent["merged_from"] or []:
            if int(fragment_id) in ids:
                parent_counts.setdefault(int(fragment_id), []).append(int(parent["id"]))
    return {
        "selected": len(ids),
        "restorable": len(archived),
        "missing_ids": missing,
        "already_active_ids": active,
        "other_layer_ids": wrong_layer,
        "associated_events": [
            {"memory_id": id, "event_ids": parent_counts.get(id, [])}
            for id in ids if parent_counts.get(id)
        ],
        "preview_memories": [
            {"id": int(r["id"]), "content": str(r["content"]),
             "created_at": str(r["created_at"])}
            for r in archived
        ],
        "state_token": _fingerprint(ids, rows, parent_rows),
    }


async def _read(conn, ids, *, lock=False):
    clause = " FOR UPDATE" if lock else ""
    rows = await conn.fetch(
        "SELECT id,content,layer,is_active,created_at FROM memories "
        "WHERE id = ANY($1::int[]) ORDER BY id" + clause, ids,
    )
    # Lock parent events in a deterministic order during confirmation.
    parents = await conn.fetch(
        "SELECT id,merged_from,is_active FROM memories "
        "WHERE merged_from && $1::int[] ORDER BY id" + clause, ids,
    )
    return rows, parents


async def preview(ids):
    selected = _ids(ids)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows, parents = await _read(conn, selected)
    return _result(selected, rows, parents)


async def restore(ids, token, confirmation):
    selected = _ids(ids)
    if confirmation != "确认批量恢复" or not isinstance(token, str) or len(token) != 64:
        raise HTTPException(status_code=400, detail="explicit_restore_confirmation_required")
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows, parents = await _read(conn, selected, lock=True)
            result = _result(selected, rows, parents)
            if result["state_token"] != token:
                raise HTTPException(status_code=409, detail="memory_state_changed_refresh_preview")
            if (result["missing_ids"] or result["already_active_ids"]
                    or result["other_layer_ids"]):
                raise HTTPException(status_code=409, detail="selection_contains_unrestorable_memory")
            if result["restorable"] != len(selected):
                raise HTTPException(status_code=409, detail="restore_count_mismatch")
            restored = await conn.fetch(
                "UPDATE memories SET is_active=TRUE WHERE id = ANY($1::int[]) "
                "AND layer=1 AND is_active=FALSE RETURNING id", selected,
            )
            if len(restored) != len(selected):
                raise HTTPException(status_code=409, detail="restore_race_detected")
            # Entire transaction rolls back on mismatch; merged events are untouched.
            return {"status": "ok", "restored": len(restored),
                    "restored_ids": sorted(int(r["id"]) for r in restored),
                    "merged_events_unchanged": True}
