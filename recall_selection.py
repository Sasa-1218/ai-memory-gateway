"""Pure, staged recall selection. No I/O, injection, or database mutation.

Cards are only eligible after human approval. Source-based deduplication
is conservative: close source overlap OR identical normalized text.
"""

import re
from typing import Iterable


def _source_ids(item):
    value = item.get('source_message_ids') or []
    if not isinstance(value, (list, tuple)):
        return frozenset()
    return frozenset(x for x in value if isinstance(x, int) and not isinstance(x, bool) and x > 0)


def _normal_text(value):
    return re.sub(r'\s+', '', value).casefold()


def _duplicate(a, b):
    if _normal_text(a['text']) == _normal_text(b['text']):
        return True
    if a['session_id'] and a['session_id'] == b['session_id']:
        left, right = a['source_ids'], b['source_ids']
        if left and right and len(left & right) / len(left | right) >= 0.7:
            return True
    return False


def select_recall_items(memories: Iterable[dict], cards: Iterable[dict], *,
                        max_items: int = 8, max_chars: int = 6000):
    """Return selected candidates within one shared budget, cards first.

    This function does not fetch or inject anything; callers remain responsible
    for guarding memory retrieval and for formatting the selected candidates.
    """
    if not isinstance(max_items, int) or isinstance(max_items, bool) or max_items < 0:
        raise ValueError('invalid max_items')
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 0:
        raise ValueError('invalid max_chars')
    selected = []
    used = 0
    for kind, records in (('experience', cards), ('memory', memories)):
        for item in records:
            if not isinstance(item, dict):
                continue
            if kind == 'experience' and (item.get('review_status') != 'approved' or item.get('ai_visible') is not True):
                continue
            if kind == 'memory' and item.get('is_active', True) is not True:
                continue
            content = item.get('event_summary') if kind == 'experience' else item.get('content')
            if not isinstance(content, str) or not content.strip():
                continue
            normalized = {
                'kind': kind, 'text': content.strip(),
                'id': item.get('id'),
                'session_id': str(item.get('source_session_id') or item.get('source_session') or ''),
                'source_ids': _source_ids(item),
            }
            if any(_duplicate(normalized, old) for old in selected):
                continue
            length = len(normalized['text'])
            if length > max_chars - used:
                continue  # Never truncate an event and accidentally change meaning.
            if len(selected) >= max_items:
                return selected
            used += length
            selected.append(normalized)
    return selected
