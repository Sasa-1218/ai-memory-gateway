"""Bounded, read-only lineage inspection for existing merged memory rows.

The existing memories table does not yet have verified source_message_ids.
This service reports *memory* ancestry only and never guesses conversation IDs.
"""
from collections.abc import Mapping


async def inspect_memory_lineage(memory_id, fetch_rows, *, max_nodes=100, max_depth=16):
    if not isinstance(memory_id, int) or isinstance(memory_id, bool) or memory_id <= 0:
        raise ValueError("invalid memory id")
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or max_nodes < 1:
        raise ValueError("invalid max_nodes")
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise ValueError("invalid max_depth")
    pending = [(memory_id, 0)]
    queued = {memory_id}
    found = {}
    missing = set()
    truncated = False
    malformed = False
    while pending:
        if len(found) + len(missing) >= max_nodes:
            truncated = True
            break
        depth = pending[0][1]
        batch = []
        while pending and pending[0][1] == depth and len(batch) + len(found) + len(missing) < max_nodes:
            mid, _ = pending.pop(0)
            batch.append(mid)
        rows = await fetch_rows(batch)
        if not isinstance(rows, (list, tuple)):
            raise TypeError("fetch_rows must return records")
        mapped = {row["id"]: dict(row) for row in rows
                  if isinstance(row, Mapping) and type(row.get("id")) is int and row["id"] in batch}
        for mid in batch:
            if mid not in mapped:
                missing.add(mid)
                continue
            row = mapped[mid]
            found[mid] = row
            parents = row.get("merged_from") or []
            if not isinstance(parents, (list, tuple)) or any(type(p) is not int or p <= 0 for p in parents):
                malformed = True
                continue
            if parents and depth >= max_depth:
                truncated = True
                continue
            for parent_id in parents:
                if parent_id in queued:
                    # Repeated parents are valid in a DAG; no infinite loop.
                    continue
                queued.add(parent_id)
                pending.append((parent_id, depth + 1))
    root = found.get(memory_id)
    if root is None:
        return {"exists": False, "memory_id": memory_id, "source_memory_ids": [],
                "source_session": "", "source_message_ids": [], "message_sources_verified": False,
                "lineage_complete": False, "missing_memory_ids": sorted(missing), "truncated": truncated}
    # A repeated parent may be a legitimate DAG join. Detect actual cycles
    # after fetching, without recursion or unbounded traversal.
    visiting, visited = set(), set()
    cycle = False
    for start in found:
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                visiting.discard(node)
                visited.add(node)
                continue
            if node in visiting:
                cycle = True
                break
            if node in visited:
                continue
            visiting.add(node)
            stack.append((node, True))
            for parent in found[node].get("merged_from") or []:
                if parent in found:
                    stack.append((parent, False))
        if cycle:
            break
    sessions = {str(row["source_session"]) for row in found.values() if row.get("source_session")}
    source_session = next(iter(sessions)) if len(sessions) == 1 else ""
    return {
        "exists": True, "memory_id": memory_id,
        "source_memory_ids": sorted(found),
        "source_session": source_session,
        "source_message_ids": [],  # No such verified column in current memories schema.
        "message_sources_verified": False,
        "lineage_complete": not (truncated or malformed or missing or pending or cycle or len(sessions) > 1),
        "missing_memory_ids": sorted(missing),
        "truncated": truncated,
    }
