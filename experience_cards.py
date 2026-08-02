"""Shared-experience card draft rules.

Phase 1 only provides the prompt and review-state validation. It deliberately
does not generate cards or expose them to chat retrieval.
"""

import re
from datetime import date

REVIEW_STATUSES = {"pending", "approved", "archived", "deleted", "superseded"}
GENERATION_OPERATIONS = {"manual_generate", "auto_generate", "regenerate", "split"}

SHARED_EXPERIENCE_CARD_PROMPT = """请根据下面带有 role、上海时间和 source_message_id 的对话，生成一张或多张“共同经历卡片”草稿。

卡片只记录过去发生了什么，不负责主题分类、重要性评分、人格分析、关系总结、教训或成长感悟。

1. 每张卡必须有清楚的事件脊柱：触发、行动、回应、发展或结果。普通临时状态如饿了、没洗澡、累了、随口想吃东西，如果没有推动后续互动、决策、冲突、纠正或约定，不要单独成卡。同一事件的起因、情绪、沟通和结果不要按阶段拆开。
2. title 使用具体对象和关键动作或变化，让人脱离上下文也能辨认事件；避免“某次讨论”“一次冲突”等泛标题。event_date_start/event_date_end 只填写能够从原文明示或消息时间可靠换算的 YYYY-MM-DD；单日事件两者相同，日期不明时都留空。不要保存“此前周末、昨天、最近”等相对日期，也不要精确到小时和分钟。event_summary 按原文顺序讲清谁先做了什么、为什么发生、怎样发展和这段对话结束时的结果。删除其他字段后仍应理解事件因果。不得把后来的担心、决定或结果改写成较早行为的原因。尚未完成或以后可能变化的状态必须写成“截至本次对话……”“当时尚未……”或等价的时间锚点，不得用无时间限定的“当前结果”。
3. Sasa 对现实经历、动机、感受和第三方关系的明确陈述可以作为事实。Rora 对自身当时反应的明确表达可以写入 interaction_trace；Rora 的推测、安慰性提议、角色化动作、虚构能力，以及 Sasa 没有承接的单方面内容，不能写成现实事实、约定或待办。双方共同承接的亲密互动、玩笑和想象可以记录为“对话中的互动”。
4. interaction_trace 只记录双方具体怎样回应及其带来的变化，不重复 event_summary。key_details 使用3至6条自然检索短语，优先覆盖 Sasa 以后可能采用的说法、独特物品、人物、地点、具体顾虑和有辨识度的互动；不要只写过于宽泛的名词，也不要写关系意义。explicit_corrections 使用 {"old_claim":"","new_claim":""} 对象写清完整旧说法和新说法。explicit_agreements 只记录双方明确确认的约定。open_threads 只保留原文明示且未来确实需要继续的事项。
5. source_message_ids 只能使用输入中真实存在、直接支持卡片内容的消息 ID，并按原始顺序排列。消息 ID 仅用于结构化溯源，title、event_summary、interaction_trace、key_details、explicit_corrections、explicit_agreements 和 open_threads 中不得出现“第4013条”“消息ID 4013”等内部编号；正文应直接说明对应的事情或说法。

只输出严格 JSON：
{"cards":[{"event_date_start":"","event_date_end":"","title":"","event_summary":"","interaction_trace":"","key_details":[],"explicit_corrections":[{"old_claim":"","new_claim":""}],"explicit_agreements":[],"open_threads":[],"source_message_ids":[]}]}
"""


def normalize_card_update(data: dict) -> dict:
    """Validate Dashboard edits without silently making drafts AI-visible."""
    allowed = {
        "event_date_start", "event_date_end", "title", "event_summary", "interaction_trace", "key_details",
        "explicit_corrections", "explicit_agreements", "open_threads",
        "review_status", "ai_visible", "revision_reason",
    }
    update = {key: data[key] for key in allowed if key in data}

    for key in ("event_date_start", "event_date_end"):
        if key in update:
            value = str(update[key] or "").strip()
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError(f"{key}_must_be_iso_date")
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError(f"{key}_must_be_iso_date") from exc
            update[key] = value
    start = update.get("event_date_start")
    end = update.get("event_date_end")
    if start and end and end < start:
        raise ValueError("event_date_end_before_start")

    status = update.get("review_status")
    if status is not None and status not in REVIEW_STATUSES:
        raise ValueError("invalid_review_status")

    for key in ("key_details", "explicit_corrections", "explicit_agreements", "open_threads"):
        if key in update and not isinstance(update[key], list):
            raise ValueError(f"{key}_must_be_list")

    if "explicit_corrections" in update:
        for correction in update["explicit_corrections"]:
            if not isinstance(correction, dict):
                raise ValueError("explicit_corrections_items_must_be_objects")
            if not isinstance(correction.get("old_claim"), str) or not isinstance(
                correction.get("new_claim"), str
            ):
                raise ValueError("explicit_correction_claims_must_be_strings")

    if "key_details" in update:
        update["key_details"] = update["key_details"][:6]

    if update.get("ai_visible") is True and status != "approved":
        raise ValueError("ai_visible_requires_approved_status")

    if status is not None and status != "approved":
        update["ai_visible"] = False

    return update


def apply_card_update(current: dict, update: dict) -> dict:
    """Apply a validated review transition while preserving immutable sources."""
    values = dict(current)
    values.update(update)
    status = values.get("review_status", "pending")
    values["review_status"] = status
    values["ai_visible"] = bool(values.get("ai_visible")) if status == "approved" else False
    return values


def soft_delete_card_update() -> dict:
    return {"review_status": "deleted", "ai_visible": False}


def restore_card_update() -> dict:
    return {"review_status": "pending", "ai_visible": False}


def should_auto_supersede(card: dict) -> bool:
    return card.get("review_status") in {"pending", "archived"}


def build_generation_prompt(messages: list[dict], operation: str) -> str:
    if operation not in GENERATION_OPERATIONS:
        raise ValueError("invalid_generation_operation")
    extra = ""
    if operation == "split":
        extra = """

本次任务是拆分：按互相独立的事件输出多张卡片。临时身体/生活状态，如没吃饭、没洗澡、累了、饿了，只能作为其他事件背景；除非它本身推动了明确决策、冲突、约定或后续行动，不得单独成卡，也不要放进 title、key_details 或 open_threads。"""
    evidence = "\n\n".join(
        f"[{item['created_at']}] role={item['role']} source_message_id={item['id']}\n{item['content']}"
        for item in messages
    )
    return SHARED_EXPERIENCE_CARD_PROMPT + extra + "\n\n待整理对话：\n---\n" + evidence + "\n---"


def is_basic_experience_candidate(messages: list[dict]) -> bool:
    """Narrowly reject trivial exchanges before an automatic model call.

    This intentionally does not attempt semantic importance scoring. It only
    filters empty/single-sided input and very short temporary-state exchanges.
    """
    useful = [
        item for item in messages
        if item.get("role") in {"user", "assistant"}
        and str(item.get("content") or "").strip()
    ]
    roles = {item["role"] for item in useful}
    if not {"user", "assistant"}.issubset(roles):
        return False
    total_chars = sum(len(str(item.get("content") or "").strip()) for item in useful)
    if len(useful) >= 4 or total_chars >= 160:
        return True
    temporary_only = re.compile(
        r"^(我)?(好)?(饿了|困了|累了|没吃饭|还没吃饭|没洗澡|想睡觉|去洗澡|吃饭了)[。！!~～…]*$"
    )
    user_texts = [
        str(item.get("content") or "").strip()
        for item in useful if item["role"] == "user"
    ]
    return not user_texts or not all(temporary_only.fullmatch(text) for text in user_texts)


def validate_generated_cards(payload: dict, allowed_message_ids: set[int]) -> list[dict]:
    cards = payload.get("cards") if isinstance(payload, dict) else None
    if not isinstance(cards, list) or not cards:
        raise ValueError("generated_cards_missing")
    validated = []
    for raw in cards:
        if not isinstance(raw, dict):
            raise ValueError("generated_card_must_be_object")
        ids = raw.get("source_message_ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("source_message_ids_required")
        try:
            ids = [int(value) for value in ids]
        except (TypeError, ValueError):
            raise ValueError("invalid_source_message_id")
        if not set(ids).issubset(allowed_message_ids):
            raise ValueError("source_message_ids_out_of_scope")
        update = normalize_card_update({
            key: raw.get(key, [] if key in {
                "key_details", "explicit_corrections", "explicit_agreements", "open_threads"
            } else "")
            for key in (
                "event_date_start", "event_date_end", "title", "event_summary", "interaction_trace", "key_details",
                "explicit_corrections", "explicit_agreements", "open_threads",
            )
        })
        update["source_message_ids"] = ids
        validated.append(update)
    return validated
