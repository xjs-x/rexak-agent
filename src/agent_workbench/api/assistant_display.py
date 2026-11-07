"""Conversation-only projection; never changes model history or saved artifacts."""

import re

_OPEN = re.compile(r"\s*<think>", re.IGNORECASE)
_CLOSE = re.compile(r"</think>", re.IGNORECASE)


def assistant_display_text(text: str) -> str:
    """Hide leading provider thinking, including an unfinished reasoning block.

    Only leading control blocks are recognized. Quoted tags and tags inside an
    ordinary answer or a fenced code example remain normal answer content.
    """
    remaining = text
    while match := _OPEN.match(remaining):
        closing = _CLOSE.search(remaining, match.end())
        if closing is None:
            return ""
        remaining = remaining[closing.end():].lstrip()
    partial = remaining.lstrip().lower()
    if partial and "<think>".startswith(partial):
        return ""
    return remaining
