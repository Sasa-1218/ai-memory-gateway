"""Read-only provenance resolution for staged memory lineage work.

No database operations. Do not infer historical message IDs from memory text.
An incomplete lineage must not be shown as verified source evidence.
"""
from collections.abc import Mapping


def _ids(row):
    ids = row.get('source_message_ids') or []
    if not isinstance(ids, (list, tuple)):
        return set()
    return {i for i in ids if isinstance(i, int) and not isinstance(i, bool) and i > 0}


def _parents(row):
    parents = row.get('merged_from') or []
    if not isinstance(parents, (list, tuple)):
        return None
    if any(not isinstance(p, int) or isinstance(p, bool) or p <= 0 for p in parents):
        return None
    return set(parents)


def trace_memory_provenance(memory, rows_by_id, *, max_nodes=100):
    """Return verified, session-scoped IDs plus a completeness flag.

    Parent edges reflect memory merges, NOT conversation messages.
    A mixed-session lineage returns no message IDs to avoid false linkage.
    """
    if not isinstance(memory, Mapping) or not isinstance(rows_by_id, Mapping):
        raise TypeError('expected mapping records')
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or max_nodes < 1:
        raise ValueError('invalid max_nodes')
    stack = [memory]
    seen = set()
    sessions = set()
    message_ids = set()
    incomplete = False
    while stack:
        row = stack.pop()
        if not isinstance(row, Mapping):
            incomplete = True
            continue
        row_id = row.get('id')
        if isinstance(row_id, int) and not isinstance(row_id, bool):
            if row_id in seen:
                incomplete = True  # Cycle or repeated lineage branch, conservatively flag it.
                continue
            seen.add(row_id)
        if len(seen) > max_nodes:
            incomplete = True
            break
        session = row.get('source_session') or row.get('source_session_id') or ''
        if session:
            sessions.add(str(session))
        ids = _ids(row)
        if ids and not session:
            incomplete = True  # No verified session context for these message IDs.
        elif ids:
            message_ids.update(ids)
        parents = _parents(row)
        if parents is None:
            incomplete = True
            continue
        for parent_id in parents:
            if parent_id in seen:
                incomplete = True
                continue
            parent = rows_by_id.get(parent_id)
            if parent is None:
                incomplete = True
            else:
                stack.append(parent)
    if len(sessions) != 1:
        # Never combine message IDs from different sessions or unscoped rows.
        return {'source_session': '', 'source_message_ids': [], 'complete': False}
    return {'source_session': next(iter(sessions)),
            'source_message_ids': sorted(message_ids),
            'complete': not incomplete and bool(message_ids)}
