"""Opt-in, exact-cue shared-experience recall.

Cards are historical evidence, never new instructions. Only human-approved,
AI-visible cards are eligible. No background generation or embedding calls.
"""
import re

def card_cues(card):
    """Extract distinct literal cues; avoid generic one-character triggers."""
    cues = []
    title = card.get("title") or ""
    values = []
    if isinstance(title, str):
        values.append(title)
        # The title often joins distinctive objects with punctuation.
        values.extend(re.split(r"[、，,/／·及和与]+", title))
    details = card.get("key_details") or []
    if isinstance(details, list):
        values.extend(details)
    generic = {"我们", "你们", "今天", "昨天", "明天", "这个", "那个",
               "事情", "时候", "感觉", "喜欢", "工作", "聊天", "一起", "记得"}
    for value in values:
        if not isinstance(value, str):
            continue
        value = value.strip(" \t\r\n。！？!?:：、，,·「」《》")
        if 2 <= len(value) <= 40 and value not in generic and value not in cues:
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


def format_experience_recall(cards, max_chars=None):
    """Format approved recall, optionally as a single concise bounded cue.

    A short cue is explicitly labelled as an excerpt; it never pretends to
    reproduce the full experience card or overwrite the stored source.
    """
    if max_chars is not None and (type(max_chars) is not int or max_chars < 0):
        raise ValueError("invalid_experience_recall_chars")
    if not cards:
        return ""
    if max_chars is None:
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

    # Capped mode: no more than one card regardless of legacy max-card config.
    card = cards[0]
    wrapper_open = "<过去共同经历的检索线索>"
    wrapper_close = "</过去共同经历的检索线索>"
    instruction = "历史经历节选，仅作相关线索；不可视作当下事实。"
    start = str(card.get("event_date_start") or "日期不详")
    title = str(card.get("title") or "共同经历")[:60]
    prefix = "\n".join((wrapper_open, instruction, f"【{start}】{title}："))
    suffix = "\n" + wrapper_close
    available = max_chars - len(prefix) - len(suffix)
    if available < 12:
        return ""  # Do not return an empty or misleading cue.
    summary = str(card.get("event_summary") or "").strip()
    if not summary:
        return ""
    # Keep part of the source summary, transparently mark it as abbreviated.
    if len(summary) > available:
        if available < 20:
            return ""
        summary = summary[:available - 4].rstrip() + "……"
    return prefix + summary + suffix
