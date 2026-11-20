"""Opt-in bound for already-formatted recall blocks. No I/O.

This layer keeps the existing retrieval order and does not claim source-aware
deduplication. Limits are character counts, not LLM tokens.
"""


def combine_recall_blocks(memory_block: str, experience_block: str, *,
                          max_chars: int = 6000, prefer_experience: bool = True) -> str:
    """Select complete blocks under a shared budget, without truncation.

    The caller must retrieve only approved experience cards. This function does
    not modify cooldown records or perform searches.
    """
    if type(max_chars) is not int or max_chars < 0:
        raise ValueError("invalid max_chars")
    memory = memory_block.strip() if isinstance(memory_block, str) else ""
    experience = experience_block.strip() if isinstance(experience_block, str) else ""
    ordered = ((experience, memory) if prefer_experience else (memory, experience))
    chosen, used = [], 0
    for block in ordered:
        if not block:
            continue
        extra = len(block) + (2 if chosen else 0)
        if used + extra > max_chars:
            continue
        if block in chosen:
            continue
        chosen.append(block)
        used += extra
    return "\n\n".join(chosen)
