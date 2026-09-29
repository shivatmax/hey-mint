"""Clipboard, video and memory tasks."""

from __future__ import annotations

import re
import time

from . import sandbox as sb
from .registry import Ctx, check, cleanup, fail, ok, setup
from .t_files import pdf_bytes

# --- clipboard -----------------------------------------------------------------------------------


def _shown(text: str) -> str:
    """Clipboard text for a result file: only the benchmark's own, never the user's."""
    if "mintbench" in text.lower() or str(sb.ROOT) in text:
        return repr(text[:100])
    return "something else (not the benchmark's text; not shown)" if text else "nothing"


@setup
def clip_path(ctx: Ctx):
    sb.write("Docs/contract.pdf", pdf_bytes("contract"))
    sb.write("Docs/contract-notes.txt", "not this one\n")


@check
def clip_path_ok(ctx: Ctx):
    want = str(ctx.p("Docs", "contract.pdf"))
    text = sb.clipboard_get().strip().strip("'\"")
    if text in (want, want.replace(str(sb.HOME), "~")):
        return ok("the clipboard holds the contract's path")
    if want in sb.clipboard_files():
        return fail("copied the FILE, not its path")
    return fail(f"clipboard holds {_shown(text)}")


PIN_TEXT = "MintBench invoice template: Invoice #___ - due in 14 days - bank ref MB-2291"


@setup
def clip_pin(ctx: Ctx):
    sb.clipboard_set(PIN_TEXT)
    time.sleep(2.5)                    # let Mint's clipboard history see it


@check
def clip_pin_ok(ctx: Ctx):
    for label, value in sb.pins().items():
        if "mintbench" in label.lower().replace(" ", "") and "MB-2291" in str(value):
            return ok(f"pinned as {label!r}")
    labels = list(sb.pins())
    return fail(f"no 'MintBench template' pin holding the text (pins: {labels[:8]})")


CODES = ["MintBench code ALPHA-1111", "MintBench code BRAVO-2222", "MintBench code CHARLIE-3333"]


@setup
def clip_history(ctx: Ctx):
    for code in CODES:
        sb.clipboard_set(code)
        time.sleep(2.5)                # Mint's clipboard watcher must see each copy


@check
def clip_history_ok(ctx: Ctx):
    text = sb.clipboard_get()
    return ok("ALPHA-1111 is back") if "ALPHA-1111" in text else fail(f"clipboard holds {_shown(text)}")


SCORES = "name,score\nAna,91\nBo,78\nCy,85\n"


@setup
def clip_to_file(ctx: Ctx):
    sb.clipboard_set(SCORES)
    time.sleep(1.5)


@check
def clip_to_file_ok(ctx: Ctx):
    path = ctx.p("scores.csv")
    if not path.exists():
        return fail("no scores.csv")
    got = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    want = [line for line in SCORES.splitlines() if line]
    return ok("scores.csv matches the clipboard") if got == want else fail(f"scores.csv is {got}")


# --- video -------------------------------------------------------------------------------------------


@setup
def video_demo(ctx: Ctx):
    sb.make_video(ctx.p("Video", "demo.mp4"), 20)
    ctx.data["demo"] = sb.sha(ctx.p("Video", "demo.mp4"))


def _outputs(ctx: Ctx, suffixes: tuple[str, ...]) -> list:
    demo = ctx.p("Video", "demo.mp4")
    return [p for p in sb.new_files(ctx.started, suffixes) if p != demo]


def _original_kept(ctx: Ctx) -> str:
    demo = ctx.p("Video", "demo.mp4")
    return "" if demo.exists() and sb.sha(demo) == ctx.data["demo"] else "; the original demo.mp4 was changed"


@check
def video_trim_ok(ctx: Ctx):
    outs = _outputs(ctx, (".mp4", ".mov", ".m4v"))
    for path in outs:
        info = sb.media(path)
        if info and abs(info["duration"] - 15) <= 1.0 and info["has_video"]:
            kept = _original_kept(ctx)
            return fail("trimmed, but" + kept) if kept else ok(f"{path.name}: {info['duration']:.1f}s")
    got = {p.name: round(sb.media(p).get("duration", 0), 1) for p in outs}
    return fail(f"no 15 s video (new videos: {got or 'none'})")


@check
def video_audio_ok(ctx: Ctx):
    outs = _outputs(ctx, (".mp3", ".m4a", ".wav", ".aac"))
    for path in outs:
        info = sb.media(path)
        if info and info["has_audio"] and abs(info["duration"] - 20) <= 1.0:
            wrong = "" if path.suffix.lower() == ".mp3" else f" (as {path.suffix}, not mp3)"
            return fail("audio extracted" + wrong) if wrong else ok(f"{path.name}: {info['duration']:.1f}s audio")
    return fail(f"no 20 s audio file (new: {[p.name for p in outs] or 'none'})")


@check
def video_vertical_ok(ctx: Ctx):
    outs = _outputs(ctx, (".mp4", ".mov", ".m4v"))
    for path in outs:
        info = sb.media(path)
        if info and info["height"] and abs(info["width"] / info["height"] - 9 / 16) < 0.02:
            return ok(f"{path.name}: {info['width']}x{info['height']}{_original_kept(ctx)}")
    got = {p.name: f"{sb.media(p).get('width')}x{sb.media(p).get('height')}" for p in outs}
    return fail(f"no 9:16 video (new videos: {got or 'none'})")


@setup
def video_info(ctx: Ctx):
    sb.make_video(ctx.p("Video", "clip.mov"), 12, tone=660)


@check
def video_info_ok(ctx: Ctx):
    path = ctx.p("Video", "video-info.txt")
    if not path.exists():
        return fail("no Video/video-info.txt")
    text = path.read_text()
    if not re.search(r"1280\s*(x|×|by|\*)\s*720|720p", text, re.I):
        return fail(f"no 1280x720 in {text.strip()[:100]!r}")
    if not re.search(r"\b12(\.\d+)?\s*(s\b|sec|second)|0?0:12\b", text, re.I):
        return fail(f"no 12 s length in {text.strip()[:100]!r}")
    return ok("resolution and length written")


# --- memory (only facts that carry the MintBench marker; removed in cleanup) --------------------------


@check
def mem_recall_ok(ctx: Ctx):
    saved = sb.bank_matching(("kestrel-7", "kestrel 7"))
    path = ctx.p("server.txt")
    if not saved:
        return fail("kestrel-7 was not saved to memory")
    if not path.exists() or "kestrel" not in path.read_text().lower():
        return fail("server.txt missing or without kestrel-7")
    return ok("saved to memory and written back")


@setup
def mem_update(ctx: Ctx):
    ctx.data["id"] = sb.bank_add("The user's MintBench team standup is at 9:30 every weekday.")


@check
def mem_update_ok(ctx: Ctx):
    blocks = sb.bank_matching(("standup",))
    blocks = [b for b in blocks if "mintbench" in b["text"].lower().replace(" ", "")]
    if any("9:30" in b["text"] for b in blocks):
        return fail("the old 9:30 standup is still remembered")
    if not any("10:15" in b["text"] for b in blocks):
        return fail("no memory of the 10:15 standup")
    return ok("standup memory now says 10:15")


@setup
def mem_forget(ctx: Ctx):
    ctx.data["id"] = sb.bank_add("The user's favourite MintBench test fruit is durian.")


@check
def mem_forget_ok(ctx: Ctx):
    left = sb.bank_matching(("durian",))
    return fail(f"still remembered: {left[0]['text']!r}") if left else ok("durian forgotten")


@check
def mem_preference_ok(ctx: Ctx):
    if not sb.bank_matching(("markdown",)):
        return fail("the Markdown preference was not saved to memory")
    for path in sb.files_under(ctx.p("Notes")):
        text = path.read_text(errors="replace").lower()
        if all(c in text for c in ("teal", "amber", "plum")):
            if path.suffix.lower() != ".md":
                return fail(f"colours saved as {path.name}, not Markdown")
            return ok(f"Notes/{path.name}, as the remembered preference says")
    elsewhere = [p.name for p in sb.new_files(ctx.started) if "teal" in sb.any_text(p).lower()]
    return fail("no colours file in ~/MintBench/Notes" + (f" (found {elsewhere})" if elsewhere else ""))


@cleanup
def forget_bench_memories(ctx: Ctx):
    """Every memory with the task's markers (always also "mintbench") goes; see the standard cleanup."""
    ctx.data["memories_removed"] = sb.bank_remove(("mintbench", "mint bench", *ctx.markers))
