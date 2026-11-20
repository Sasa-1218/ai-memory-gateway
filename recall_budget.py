"""Opt-in shared budget for already-formatted memory and experience blocks.

Pure text processing; no I/O, DB writes, approvals or cooldown changes.
Character budget is not an LLM-token budget.
"""

MEMORY_OPEN = "<脑海中浮现的既往事实>"
MEMORY_CLOSE = "</脑海中浮现的既往事实>"


def fit_memory_fact_block(text: str, max_chars: int) -> str:
    """Keep complete ranked memory facts under a character budget.

    Only process the gateway's known memory-block format. A record starts
    with '- '; any following lines belong to that record. Unknown formats
    are never partially rewritten.
    """
    if type(max_chars) is not int or max_chars < 0:
        raise ValueError("invalid max_chars")
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != MEMORY_OPEN or lines[-1] != MEMORY_CLOSE:
        return ""
    records = []
    current = None
    for line in lines[1:-1]:
        if line.startswith("- "):
            if current is not None:
                records.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
        else:
            # Unexpected free text: cannot tell which memory it belongs to.
            return ""
    if current is not None:
        records.append(current)
    overhead = len(MEMORY_OPEN) + len(MEMORY_CLOSE) + 2
    chosen = []
    used = overhead
    for record in records:
        body = "\n".join(record)
        extra = len(body) + 1
        if used + extra <= max_chars:
            chosen.append(body)
            used += extra
    if not chosen:
        return ""
    result = MEMORY_OPEN + "\n" + "\n".join(chosen) + "\n" + MEMORY_CLOSE
    assert len(result) <= max_chars
    return result


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
        if kind == "memory" and len(block) > remaining:
            block = fit_memory_fact_block(block, max(0, remaining))
        if not block or len(block) > remaining:
            continue
        chosen.append(block)
        used += len(block) + (2 if len(chosen) > 1 else 0)
    return "\n\n".join(chosen)
