"""Shared-experience card draft rules.

Phase 1 only provides the prompt and review-state validation. It deliberately
does not generate cards or expose them to chat retrieval.
"""

REVIEW_STATUSES = {"pending", "approved", "archived", "deleted", "superseded"}

SHARED_EXPERIENCE_CARD_PROMPT = """请根据下面带有 role、上海时间和 source_message_id 的对话，生成一张或多张“共同经历卡片”草稿。

卡片只记录过去发生了什么，不负责主题分类、重要性评分、人格分析、关系总结、教训或成长感悟。

1. 每张卡必须有清楚的事件脊柱：触发、行动、回应、发展或结果。普通临时状态如饿了、没洗澡、累了、随口想吃东西，如果没有推动后续互动、决策、冲突、纠正或约定，不要单独成卡。同一事件的起因、情绪、沟通和结果不要按阶段拆开。
2. event_summary 按原文顺序讲清谁先做了什么、为什么发生、怎样发展和当前结果。删除其他字段后仍应理解事件因果。不得把后来的担心、决定或结果改写成较早行为的原因。
3. Sasa 对现实经历、动机、感受和第三方关系的明确陈述可以作为事实。Rora 对自身当时反应的明确表达可以写入 interaction_trace；Rora 的推测、安慰性提议、角色化动作、虚构能力，以及 Sasa 没有承接的单方面内容，不能写成现实事实、约定或待办。双方共同承接的亲密互动、玩笑和想象可以记录为“对话中的互动”。
4. interaction_trace 只记录双方具体怎样回应及其带来的变化，不重复 event_summary。key_details 最多3项，只放搜索线索。explicit_corrections 使用 {"old_claim":"","new_claim":""} 对象写清完整旧说法和新说法。explicit_agreements 只记录双方明确确认的约定。open_threads 只保留原文明示且未来确实需要继续的事项。
5. source_message_ids 只能使用输入中真实存在、直接支持卡片内容的消息 ID，并按原始顺序排列。

只输出严格 JSON：
{"cards":[{"title":"","event_summary":"","interaction_trace":"","key_details":[],"explicit_corrections":[{"old_claim":"","new_claim":""}],"explicit_agreements":[],"open_threads":[],"source_message_ids":[]}]}
"""


def normalize_card_update(data: dict) -> dict:
    """Validate Dashboard edits without silently making drafts AI-visible."""
    allowed = {
        "title", "event_summary", "interaction_trace", "key_details",
        "explicit_corrections", "explicit_agreements", "open_threads",
        "review_status", "ai_visible", "revision_reason",
    }
    update = {key: data[key] for key in allowed if key in data}

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
        update["key_details"] = update["key_details"][:3]

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
