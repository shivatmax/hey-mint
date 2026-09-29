"""Answers as cards on the island: when an answer is a count or a short list, Mint shows it too.

    "how many unread emails do I have?"       12  unread emails                (a big number)
    "what did I download today?"              a list of the files, with their icons; click one to open it
    "what automations do I have?"             each one, with when it runs next
    "who emailed me today?"                   senders and subjects

Mint calls show_card with what it found; the automations and trackers lists show theirs by
themselves. The card grows out of the orb (island.py), stays 20 seconds (longer while the
pointer is on it) and closes on a click.
"""

from __future__ import annotations

import logging

log = logging.getLogger("mint.tools.cards")

TINTS = ("mint", "blue", "orange", "green", "purple", "red", "teal")


def show(title: str, number=None, unit: str = "", subtitle: str = "", items: list | None = None, icon: str = "",
         tint: str = "mint", more: int = 0, seconds: float = 20.0) -> None:
    try:
        from mint.ui.island import island
        island.show_card({"title": title, "number": number, "unit": unit, "subtitle": subtitle,
                          "items": list(items or [])[:12], "icon": icon or "sparkles",
                          "tint": tint if tint in TINTS else "mint", "more": more}, seconds)
    except Exception as error:
        log.info("card: %s", error)


def tool(args: dict) -> str:
    items = []
    for item in args.get("items") or []:
        if isinstance(item, dict) and item.get("title"):
            items.append({k: str(item.get(k) or "") for k in ("title", "detail", "trailing", "path", "icon")})
    number = args.get("number")
    if isinstance(number, float) and number.is_integer():
        number = int(number)
    if not items and number in (None, ""):
        return "Nothing to show: give a number or some items."
    show(str(args.get("title") or ""), number, str(args.get("unit") or ""), str(args.get("subtitle") or ""), items,
         str(args.get("icon") or ""), str(args.get("tint") or "mint"), int(args.get("more") or 0))
    return "Shown on screen as a card. Say the answer in one short sentence; don't read the whole list out."


PROMPT = """Cards: when your answer is a COUNT or a SHORT LIST - how many unread emails, what's in Downloads, today's \
files, who emailed, which downloads finished, a list of results - ALSO call show_card (after you have the facts): \
number + unit for a count ("12", "unread emails"), or items (title, detail, trailing like a size or time, path for \
files so they can be opened), a fitting SF Symbol icon (envelope.fill, folder.fill, arrow.down.circle.fill, \
calendar, clock.fill, doc.fill, bolt.fill) and a tint. Then say it in one short sentence - the card carries the \
details."""


def declarations():
    from google.genai import types
    S, N = types.Type.STRING, types.Type.NUMBER
    item = types.Schema(type=types.Type.OBJECT, properties={
        "title": types.Schema(type=S), "detail": types.Schema(type=S, description="a second, smaller line"),
        "trailing": types.Schema(type=S, description="right side: a size, a time, a count"),
        "path": types.Schema(type=S, description="a file or folder path: shows its icon and opens on click"),
        "icon": types.Schema(type=S, description="an SF Symbol name when there is no path")})
    return [types.FunctionDeclaration(
        name="show_card",
        description=("Show an answer as a card that grows out of Mint's orb: a count as a big number, or a short "
                     "list (files, emails, downloads, events, results) with icons. Use it whenever the answer is a "
                     "number or a list of up to ~10 things."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "title": types.Schema(type=S, description="what it is, e.g. 'Unread emails', 'Downloaded today'"),
            "subtitle": types.Schema(type=S),
            "number": types.Schema(type=N, description="for a count"),
            "unit": types.Schema(type=S, description="beside the number, e.g. 'unread emails'"),
            "items": types.Schema(type=types.Type.ARRAY, items=item),
            "icon": types.Schema(type=S, description="SF Symbol for the card, e.g. envelope.fill"),
            "tint": types.Schema(type=S, enum=list(TINTS)),
            "more": types.Schema(type=N, description="how many more there are beyond the items")},
            required=["title"]))]


HANDLERS = {"show_card": tool}
