"""Opt-in shared budget for source memory records and formatted experience blocks.

Pure text processing; no I/O, DB writes, approvals or cooldown changes.
Character budget is not an LLM-token budget.
"""

MEMORY_OPEN = "<脑海中浮现的既往事实>"
MEMORY_CLOSE = "</脑海中浮现的既往事实>"


def fit_memory_entries(entries, max_chars: int) -> str:
    """Pack complete source memory records without parsing formatted text.

    Each entry is one whole preformatted fact including its '- ' prefix.
    Newlines and bullet markers inside an entry remain part of that fact.
    """
    if type(max_chars) is not int or max_chars < 0:
        raise ValueError("invalid max_chars")
    overhead = len(MEMORY_OPEN) + len(MEMORY_CLOSE) + 2
    selected = []
    used = overhead
    for entry in entries:
        if not isinstance(entry, str) or not entry.startswith("- ") or not entry.strip():
            continue
        extra = len(entry) + (1 if selected else 0)
        if used + extra > max_chars:
            continue
        selected.append(entry)
        used += extra
    if not selected:
        return ""
    return MEMORY_OPEN + "\n" + "\n".join(selected) + "\n" + MEMORY_CLOSE


def combine_recall_blocks(memory_block: str, experience_block: str, *,
                          max_chars: int = 6000, prefer_experience: bool = True) -> str:
    """Choose complete recall blocks and whole memories under one budget.

    The caller must retrieve only approved experience cards. This function
    never mutates the durable experience cooldown state.
    """
    if type(max_chars) is not int or max_chars < 0:
        raise ValueError("invalid max_chars")
    memory = memory_block.strip() if isinstance(memory_block, str) else ""
    experience = experience_block.strip() if isinstance(experience_block, str) else ""
    ordered = (("experience", experience), ("memory", memory))
    if not prefer_experience:
        ordered = tuple(reversed(ordered))
    chosen, used = [], 0
    for kind, block in ordered:
        if not block or block in chosen:
            continue
        remaining = max_chars - used - (2 if chosen else 0)
        if len(block) > remaining:
            continue
        chosen.append(block)
        used += len(block) + (2 if len(chosen) > 1 else 0)
    return "\n\n".join(chosen)
