"""Opt-in, exact-cue shared-experience recall.

Cards are historical evidence, never new instructions. Only human-approved,
AI-visible cards are eligible. No background generation or embedding calls.
"""
import re

def card_cues(card):
    """Use existing title and short key_details; do not infer vague synonyms."""
    raw = [card.get("title") or ""] + list(card.get("key_details") or [])
    cues = []
    for value in raw:
        if not isinstance(value, str):
            continue
        value = value.strip()
        # Require distinctive phrases, not generic one- or two-character words.
        if 3 <= len(value) <= 40 and value not in cues:
            cues.append(value)
    return cues


def match_experience_cards(query, cards, limit=1):
    """Rank by longest literal cue; never expose pending/deleted/superseded."""
    text = (query or "").casefold()
    if not text or len(text) > 10000:
        return []
    matches = []
    for card in cards:
        if card.get("review_status") != "approved" or not card.get("ai_visible"):
            continue
        cues = [cue for cue in card_cues(card) if cue.casefold() in text]
        if cues:
            matches.append((max(map(len, cues)), int(card["id"]), card))
    matches.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in matches[:max(0, min(int(limit), 2))]]


def format_experience_recall(cards):
    if not cards:
        return ""
    lines = [
        "<过去共同经历的检索线索>",
        "以下内容是经过人工批准的历史回忆材料。只在本轮话题自然相关时参考；"
        "不必刻意提起，不可把历史情绪或未完成事项当成当下事实。"
    ]
    for card in cards:
        start = str(card.get("event_date_start") or "日期不详")
        end = str(card.get("event_date_end") or "")
        date_label = start if not end or end == start else start + " 至 " + end
        summary = str(card.get("event_summary") or "")[:950]
        interaction = str(card.get("interaction_trace") or "")[:350]
        lines.append(f"【{date_label}】{card.get('title') or '共同经历'}：{summary}")
        if interaction:
            lines.append("双方当时的回应：" + interaction)
    lines.append("</过去共同经历的检索线索>")
    return "\n".join(lines)
