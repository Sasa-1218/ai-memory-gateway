"""Optional memory retrieval helpers. Pure, bounded and fail-closed.

Aliases are literal user-defined equivalents (JSON mapping phrase->list[str]);
no model requests, no arbitrary regex, no instructions taken from memory text.
"""
import json


def expand_query_terms(query, raw_dictionary, *, max_terms=4, max_length=500):
    if not isinstance(query, str):
        return ""
    query = query.strip()
    if not query or len(query) > max_length:
        return query
    try:
        dictionary = json.loads(raw_dictionary) if isinstance(raw_dictionary, str) else raw_dictionary
    except (ValueError, TypeError):
        return query
    if not isinstance(dictionary, dict):
        return query
    matches = []
    lowered = query.casefold()
    for key, values in dictionary.items():
        if not isinstance(key, str) or not 2 <= len(key) <= 40:
            continue
        if key.casefold() not in lowered:
            continue
        if not isinstance(values, list):
            continue
        for term in values[:8]:
            if not isinstance(term, str):
                continue
            term = term.strip()
            if not 2 <= len(term) <= 40 or term.casefold() in lowered:
                continue
            if term in matches:
                continue
            matches.append(term)
            if len(matches) >= max_terms:
                break
        if len(matches) >= max_terms:
            break
    return query + (" " + " ".join(matches) if matches else "")


def pinned_lines(rows, *, max_items=4, max_chars=900, return_ids=False):
    """Strictly cap stable context. Return whole facts only."""
    if type(max_items) is not int or not 0 <= max_items <= 10:
        raise ValueError("bad max_items")
    if type(max_chars) is not int or not 0 <= max_chars <= 3000:
        raise ValueError("bad max_chars")
    opening = "<固定记忆（已人工标记）>"
    closing = "</固定记忆（已人工标记）>"
    chosen = []
    selected_ids = []
    used = len(opening) + len(closing) + 2
    for row in rows:
        if len(chosen) >= max_items:
            break
        if not isinstance(row, dict) or row.get("is_pinned") is not True or row.get("is_active") is not True:
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        line = "- " + content.strip()
        extra = len(line) + (1 if chosen else 0)
        if used + extra > max_chars:
            continue
        chosen.append(line)
        selected_ids.append(row.get("id"))
        used += extra
    text = opening + "\n" + "\n".join(chosen) + "\n" + closing if chosen else ""
    return (text, selected_ids) if return_ids else text
