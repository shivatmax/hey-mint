"""Compatibility layer over the memory bank (membank.py).

The first version kept memories as flat lines in memories.md; they now live as
blocks in memory/bank.json and are migrated on first use. These functions keep
the old names working, and prompt_text() keeps the old line format
('- [group] text (date)'), which vocab.py parses for "[vocabulary]" entries.
"""

from __future__ import annotations

from mint.knowledge import memory as membank


def remember(fact: str, topic: str = "", fixed: bool | None = None) -> str:
    return membank.add(fact, topic, pinned=fixed)


def forget(what: str) -> str:
    return membank.forget(what)


def recall(query: str = "") -> str:
    return membank.recall(query)


def prompt_text() -> str:
    """Every block, old format. For callers that want all of it (vocab.py);
    the session prompt itself uses pinned blocks plus on-demand recall."""
    return "\n".join(f"- [{b['group']}] {b['text']} ({b['updated'][:10]})" for b in membank.blocks())
