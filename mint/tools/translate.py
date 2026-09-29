"""Translating what is on screen: "translate this page", "what does this say?" (a Japanese
site, a Hindi WhatsApp message, a German PDF), "translate this window into Hindi".

In place, like a camera translator: each foreign paragraph is covered with its own
background colour and the translation is written over it, in a colour and size that fit
- so a dark app gets light text on dark, a white page dark text on white.

1. The front window is captured, and the Mac's own text recognition (Vision, any script,
   the language detected by itself) finds every line and its exact box.
2. Gemini sees the picture and those numbered lines, groups the foreign ones into
   paragraphs and translates them. Text inside pictures that recognition missed comes
   back as a box of its own.
3. For each paragraph the background is sampled just around it and the text colour inside
   it; the overlay (click-through, never in screenshots) paints the cover and the
   translation, fitted to the box, for a minute. "clear the marks" takes it away at once.

Selected text is translated in place in the document itself by edit_selection instead.
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


def _lines(image) -> list[dict]:
    """Every line of text in the picture: {"text", "box": (x0, y0, x1, y1) in its pixels}."""
    import Quartz
    import Vision
    from Foundation import NSData

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    data = NSData.dataWithBytes_length_(buffer.getvalue(), len(buffer.getvalue()))
    cg = Quartz.CGImageSourceCreateImageAtIndex(Quartz.CGImageSourceCreateWithData(data, None), 0, None)
    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)
    if hasattr(request, "setAutomaticallyDetectsLanguage_"):
        request.setAutomaticallyDetectsLanguage_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
    ok, _error = handler.performRequests_error_([request], None)
    if not ok:
        return []
    w, h = image.size
    out = []
    for observation in request.results() or []:
        top = observation.topCandidates_(1)
        if not top or not str(top[0].string()).strip():
            continue
        b = observation.boundingBox()
        out.append({"text": str(top[0].string()),
                    "box": (b.origin.x * w, (1 - b.origin.y - b.size.height) * h,
                            (b.origin.x + b.size.width) * w, (1 - b.origin.y) * h)})
    out.sort(key=lambda l: (round(l["box"][1] / 8), l["box"][0]))
    return out


def _colours(image, box) -> tuple[tuple, tuple]:
    """(background, text colour) of a block of text, as 0-1 RGB: the background from a thin ring just
    outside it, the text from the pixels inside that differ most from it."""
    import numpy as np
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = image.size
    pad = 4
    outer = np.asarray(image.crop((max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad))),
                       dtype=np.float32).reshape(-1, 3)
    inner = np.asarray(image.crop((max(0, x0), max(0, y0), min(w, x1), min(h, y1))), dtype=np.float32).reshape(-1, 3)
    if outer.size == 0 or inner.size == 0:
        return (1.0, 1.0, 1.0), (0.1, 0.1, 0.1)
    bg = np.median(outer, axis=0)
    distance = np.linalg.norm(inner - bg, axis=1)
    far = inner[distance > 70]
    if len(far) >= 12:                       # the strongest ink, not the anti-aliased grey edges
        far = far[np.argsort(-distance[distance > 70])[: max(12, len(far) // 3)]]
    luminance = (0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]) / 255
    if len(far) >= 12:
        fg = np.median(far, axis=0)
        # Too little contrast to read (anti-aliased thin text): push it towards black or white.
        if abs((0.299 * fg[0] + 0.587 * fg[1] + 0.114 * fg[2]) / 255 - luminance) < 0.35:
            fg = np.array([20, 20, 24]) if luminance > 0.5 else np.array([240, 240, 244])
    else:
        fg = np.array([20, 20, 24]) if luminance > 0.5 else np.array([240, 240, 244])
    return tuple(float(v) / 255 for v in bg), tuple(float(v) / 255 for v in fg)


LANGUAGES = {"ja": "Japanese", "zh": "Chinese", "ko": "Korean", "hi": "Hindi", "de": "German", "fr": "French",
             "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian", "ar": "Arabic", "tr": "Turkish",
             "nl": "Dutch", "sv": "Swedish", "pl": "Polish", "th": "Thai", "vi": "Vietnamese", "id": "Indonesian",
             "he": "Hebrew", "uk": "Ukrainian", "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "mr": "Marathi",
             "ur": "Urdu", "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "pa": "Punjabi", "en": "English"}


def _language(name: str) -> str:
    key = (name or "").strip().lower().split("-")[0]
    return LANGUAGES.get(key, name.strip().title() if name else "the page")


def translate_screen(target: str = "English", show: bool = True) -> str:
    from google.genai import types
    from mint.core import llm
    try:
        image, (x0, y0, w, h) = _capture()
    except Exception as error:
        return f"FAILED: could not see the screen ({error}). Mint needs Screen Recording permission."
    try:
        lines = _lines(image)
    except Exception as error:
        log.info("text recognition: %s", error)
        lines = []
    shot = image.copy()
    shot.thumbnail((1600, 1600))
    buf = io.BytesIO()
    shot.convert("RGB").save(buf, "JPEG", quality=85)
    listing = "\n".join(f"{i}: {line['text'][:160]}" for i, line in enumerate(lines[:220]))
    prompt = (f"Translate the text on this window that is NOT in {target} into natural {target}. The window's lines, "
              f"as the Mac read them, numbered:\n{listing or '(none read)'}\n\n"
              "Make one block per paragraph, heading or message: a block is only lines that follow each other with "
              "no gap (a wrapped sentence) - never merge separate paragraphs or a heading with the text under it. "
              "Skip menus, buttons and labels unless "
              "they are all there is, and skip anything already in the target language. Return JSON: "
              '{"language": "<the source language, its name in English, e.g. Japanese>", "blocks": [{"lines": [<line numbers>], "translation": "..."}]}. '
              'For foreign text the list does not have (inside a picture), give "box": [ymin, xmin, ymax, xmax] '
              '(0-1000 of the image) instead of "lines". At most 16 blocks, in reading order. Nothing foreign: '
              '{"language": "", "blocks": []}.')
    try:
        text, _ = llm.generate([types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"), prompt],
                               MODELS, json_mode=True)
        data = llm.parse_json(text)
    except Exception as error:
        return f"FAILED: could not read and translate the screen: {error}"
    blocks = [b for b in (data.get("blocks") or []) if isinstance(b, dict) and b.get("translation")]
    if not blocks:
        return f"Nothing on screen needs translating into {target}."
    iw, ih = image.size
    covers = []
    for block in blocks:
        ids = [int(i) for i in block.get("lines") or [] if str(i).isdigit() and int(i) < len(lines)]
        if ids:
            boxes = [lines[i]["box"] for i in ids]
            box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                   max(b[3] for b in boxes))
            heights = sorted(b[3] - b[1] for b in boxes)
            line_px = heights[len(heights) // 2]
        else:
            try:
                ymin, xmin, ymax, xmax = [float(v) for v in block["box"]]
            except (KeyError, TypeError, ValueError):
                continue
            box = (xmin / 1000 * iw, ymin / 1000 * ih, xmax / 1000 * iw, ymax / 1000 * ih)
            line_px = box[3] - box[1]
        bg, fg = _colours(image, box)
        sx, sy = w / iw, h / ih                       # image pixels -> screen points
        pad = max(4.0, line_px * sy * 0.28)           # the recognizer's boxes hug the glyphs
        rect = (x0 + box[0] * sx - pad, y0 + box[1] * sy - pad, (box[2] - box[0]) * sx + 2 * pad,
                (box[3] - box[1]) * sy + 2 * pad)
        covers.append({"rect": rect, "text": str(block["translation"]), "bg": bg, "fg": fg, "line": line_px * sy})
    if show and covers:
        from mint.ui.marks import marks
        marks.cover(covers, note=f"Translated from {_language(data.get('language', ''))}", seconds=60.0)
    found = "\n".join(f"- {b['translation']}" for b in blocks)
    return (f"Translated {len(blocks)} block(s) from {_language(data.get('language', ''))} into {target}"
            + (", written over the original text on screen for a minute" if show else "") + f":\n{found}\n"
            "Tell the user the gist (or read it out if they asked what it says). 'clear the marks' removes the overlay.")


PROMPT = """Translation: "translate this page / window", "what does this say?" about foreign text on screen -> \
translate_screen (the translation is written over the original, in place, for a minute). Selected text to \
replace in the document itself -> edit_selection 'translate to …'."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="translate_screen",
        description=("Translate the foreign text in the front window (any language or script, even in images) and "
                     "show it in place: each paragraph covered in its own colours with the translation written over "
                     "it, for a minute."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "target": types.Schema(type=S, description="the language to translate into (default English)"),
            "show": types.Schema(type=types.Type.BOOLEAN, description="write it over the screen (default true)")}))]


HANDLERS = {"translate_screen": lambda a: translate_screen(str(a.get("target") or "English"), a.get("show") is not False)}
