"""Translating what is on screen: "translate this page", "what does this say?" (a Japanese
site, a Hindi WhatsApp message, a German PDF), "translate this window into Hindi".

The front window is captured and Gemini reads it as a picture - any script, printed or
handwritten - and returns each block of foreign text with where it is and its
translation. Each translation is pinned beside its text on Mint's marks overlay (click-
through, never in screenshots) for a minute, and the whole translation comes back so Mint
can say or summarise it. Selected text is translated in place by edit_selection instead.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("mint.tools.translate")

MODELS = ["gemini-3.5-flash-lite", "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite"]


def _capture():
    """(image of the front window or the whole main display, its Quartz rect)."""
    from mint.screen import ocr
    image, area = ocr._screen()
    scale = image.width / area["width"]
    window = ocr._front_window()
    if window is None:
        return image, (area["left"], area["top"], area["width"], area["height"])
    left, top = window["x"] - area["left"], window["y"] - area["top"]
    box = tuple(int(v * scale) for v in (left, top, left + window["w"], top + window["h"]))
    box = (max(0, box[0]), max(0, box[1]), min(image.width, box[2]), min(image.height, box[3]))
    return image.crop(box), (area["left"] + box[0] / scale, area["top"] + box[1] / scale,
                             (box[2] - box[0]) / scale, (box[3] - box[1]) / scale)


def translate_screen(target: str = "English", show: bool = True) -> str:
    from google.genai import types
    from mint.core import llm
    try:
        image, (x0, y0, w, h) = _capture()
    except Exception as error:
        return f"FAILED: could not see the screen ({error}). Mint needs Screen Recording permission."
    image.thumbnail((1600, 1600))
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=85)
    prompt = (f"Find the blocks of text in this window that are NOT in {target} (skip menus, buttons and UI labels "
              f"unless they are all there is). Translate each into natural {target}. Return JSON: "
              '{"language": "<the source language>", "blocks": [{"box": [ymin, xmin, ymax, xmax], "text": "<original, '
              'short>", "translation": "..."}]} with boxes on a 0-1000 scale, in reading order, at most 14 blocks '
              '(merge lines of one paragraph). Nothing foreign: {"language": "", "blocks": []}.')
    try:
        text, _ = llm.generate([types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"), prompt],
                               MODELS, json_mode=True)
        data = llm.parse_json(text)
    except Exception as error:
        return f"FAILED: could not read and translate the screen: {error}"
    blocks = [b for b in (data.get("blocks") or []) if isinstance(b, dict) and b.get("translation")]
    if not blocks:
        return f"Nothing on screen needs translating into {target}."
    if show:
        from mint.ui.marks import marks
        for n, block in enumerate(blocks):
            try:
                ymin, xmin, ymax, xmax = [float(v) for v in block["box"]]
            except (KeyError, TypeError, ValueError):
                continue
            rect = (x0 + xmin / 1000 * w, y0 + ymin / 1000 * h, (xmax - xmin) / 1000 * w, (ymax - ymin) / 1000 * h)
            marks.show([rect], style="highlight", note=str(block["translation"])[:160], seconds=60.0, clear=n == 0)
    lines = "\n".join(f"- {b.get('text', '')[:80]} → {b['translation']}" for b in blocks)
    return (f"Translated {len(blocks)} block(s) from {data.get('language') or 'the page'} into {target}"
            + (" and pinned each translation beside its text for a minute" if show else "") + f":\n{lines}\n"
            "Tell the user the gist (or read it out if they asked what it says).")


PROMPT = """Translation: "translate this page / window", "what does this say?" about foreign text on screen -> \
translate_screen (it shows each translation beside the text). Selected text to replace in place -> \
edit_selection 'translate to …'."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="translate_screen",
        description=("Translate the foreign text in the front window (any language or script, even in images) and "
                     "pin each translation beside its text on screen for a minute."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "target": types.Schema(type=S, description="the language to translate into (default English)"),
            "show": types.Schema(type=types.Type.BOOLEAN, description="pin translations on screen (default true)")}))]


HANDLERS = {"translate_screen": lambda a: translate_screen(str(a.get("target") or "English"), a.get("show") is not False)}
