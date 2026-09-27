"""Which finds the right thing to click: Gemini vision, or on-device OCR + Jev?

Nothing is clicked. One screenshot of the front window is taken; macOS text
recognition gives the true box of every label. For each target, phrased the way
a person would say it:

  vision   - Gemini is shown the screenshot and asked to point (0-1000 coords).
             Hit if the point lands inside the target's true box (+margin).
  ocr+jev  - the recognised labels are the options; exact match or Jev picks.
             Hit if the chosen label is the target.

Usage: python bench/click_bench.py "spoken request=Expected Label" ...
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai              # noqa: E402
from google.genai import types        # noqa: E402

from mint import config, jev, ocr   # noqa: E402,F401  (config loads .env)

VISION_MODEL = os.environ.get("BENCH_VISION_MODEL", "gemini-3-flash-preview")
MARGIN = 12  # points of slack around a label's box


def main(pairs: list[tuple[str, str]]) -> None:
    items, area = ocr.read_screen()
    image, _ = ocr._screen()
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    jpeg = buffer.getvalue()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def truth(label):
        want = ocr._words(label)
        return [i for i in items if ocr._words(i["text"]) == want]

    rows = []
    for spoken, label in pairs:
        boxes = truth(label)
        if not boxes:
            print(f"  skip {label!r}: not on screen")
            continue

        # --- Gemini vision ---------------------------------------------------
        t = time.monotonic()
        try:
            reply = client.models.generate_content(
                model=VISION_MODEL,
                contents=[types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
                          f"Point to: {spoken}. Reply with JSON only: "
                          '{"x": <0-1000 from left>, "y": <0-1000 from top>} for its centre.'])
            found = re.search(r"\{.*?\}", reply.text or "", re.S)
            point = json.loads(found.group(0)) if found else {}
            px = area["left"] + float(point.get("x", -1)) / 1000 * area["width"]
            py = area["top"] + float(point.get("y", -1)) / 1000 * area["height"]
            v_hit = any(b["x"] - MARGIN <= px <= b["x"] + b["w"] + MARGIN
                        and b["y"] - MARGIN <= py <= b["y"] + b["h"] + MARGIN for b in boxes)
            v_note = f"({px:.0f},{py:.0f})"
        except Exception as error:
            v_hit, v_note = False, f"error {str(error)[:40]}"
        v_time = time.monotonic() - t

        # --- OCR + Jev -------------------------------------------------------
        t = time.monotonic()
        options = {str(i): f"'{it['text']}' ({ocr._where(it, area)} of the screen)"
                   for i, it in enumerate(items[:250])}
        chosen, why = jev.resolve(spoken, options, what="on-screen text")
        j_time = time.monotonic() - t
        j_hit = chosen is not None and ocr._words(items[int(chosen)]["text"]) == ocr._words(label)
        j_note = repr(items[int(chosen)]["text"]) if chosen is not None else "refused"

        rows.append((spoken, v_hit, v_time, v_note, j_hit, j_time, j_note))
        print(f"  {spoken[:34]:34} vision {'HIT ' if v_hit else 'miss'} {v_time:4.1f}s {v_note:14} "
              f"| ocr+jev {'HIT ' if j_hit else 'miss'} {j_time:4.1f}s {j_note}")

    if rows:
        n = len(rows)
        print(f"\n  vision : {sum(r[1] for r in rows)}/{n} hits, mean {sum(r[2] for r in rows)/n:.1f}s")
        print(f"  ocr+jev: {sum(r[4] for r in rows)}/{n} hits, mean {sum(r[5] for r in rows)/n:.1f}s "
              f"(+ ~0.1s to read the screen once)")


if __name__ == "__main__":
    main([tuple(arg.split("=", 1)) for arg in sys.argv[1:]])
