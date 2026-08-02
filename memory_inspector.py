"""Pure formatting and ranking helpers for the read-only Memory Inspector."""

import math


def estimate_tokens(text: str) -> int:
    """Conservative display estimate for mixed Chinese/Latin text."""
    text = str(text or "")
    return math.ceil(len(text) / 2) if text else 0


def matched_terms(query: str, keywords: list[str], searchable: str) -> list[str]:
    haystack = str(searchable or "").casefold()
    candidates = [str(item).strip() for item in keywords if str(item).strip()]
    query = str(query or "").strip()
    if query and query not in candidates:
        candidates.append(query)
    found = []
    for term in candidates:
        if term.casefold() in haystack and term not in found:
            found.append(term)
    return found[:8]


def lexical_score(query: str, keywords: list[str], searchable: str) -> tuple[float, list[str]]:
    terms = matched_terms(query, keywords, searchable)
    if not terms:
        return 0.0, []
    query_exact = str(query or "").strip().casefold() in str(searchable or "").casefold()
    return float(len(terms) + (2 if query_exact else 0)), terms


def make_result(*, result_type: str, item_id, title: str, content: str,
                score: float, terms: list[str], source_session_id: str = "",
                source_message_ids=None, ai_visible: bool = False,
                review_status: str = "", key_details=None) -> dict:
    content = str(content or "").strip()
    return {
        "type": result_type,
        "id": item_id,
        "title": str(title or "").strip() or "未命名候选",
        "snippet": content[:600],
        "preview_content": content[:1200],
        "matched_terms": list(terms or []),
        "key_details": list(key_details or []),
        "source_session_id": str(source_session_id or ""),
        "source_message_ids": [int(value) for value in (source_message_ids or [])],
        "estimated_tokens": estimate_tokens(content[:1200]),
        "ai_visible": bool(ai_visible),
        "review_status": str(review_status or ""),
        "score": round(float(score or 0), 4),
    }


def build_injection_preview(results: list[dict], max_chars: int = 6000) -> dict:
    blocks = []
    used = 0
    for item in results:
        label = item.get("title") or item.get("type") or "候选"
        content = str(item.get("preview_content") or "").strip()
        if not content:
            continue
        block = f"[{item.get('type', 'candidate')}] {label}\n{content}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(block) > remaining:
            block = block[:remaining]
        blocks.append(block)
        used += len(block) + 2
    text = "\n\n".join(blocks)
    return {"content": text, "chars": len(text), "estimated_tokens": estimate_tokens(text)}
