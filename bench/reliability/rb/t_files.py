"""Files and folders."""

from __future__ import annotations

import datetime as dt
import io
import subprocess

from . import sandbox as sb
from .registry import Ctx, check, fail, ok, setup


def image_bytes(color: str, kind: str = "PNG", size=(64, 48)) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, kind)
    return buffer.getvalue()


def pdf_bytes(text: str) -> bytes:
    """A small real PDF (one page with a coloured block; `text` goes in the metadata)."""
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", (200, 120), "white").save(buffer, "PDF", title=text)
    return buffer.getvalue()


# --- organise an inbox by type ---------------------------------------------------------------

INBOX = {"report.pdf": ("Documents", lambda: pdf_bytes("report")),
         "budget.pdf": ("Documents", lambda: pdf_bytes("budget")),
         "photo1.jpg": ("Images", lambda: image_bytes("red", "JPEG")),
         "beach.png": ("Images", lambda: image_bytes("blue")),
         "todo.txt": ("Text", lambda: "buy milk\ncall mum\n"),
         "ideas.md": ("Text", lambda: "# Ideas\n- a podcast about maps\n")}


@setup
def files_organize(ctx: Ctx):
    ctx.data["hashes"] = {name: sb.sha(sb.write(f"Inbox/{name}", make())) for name, (_, make) in INBOX.items()}


@check
def files_organize_ok(ctx: Ctx):
    inbox = ctx.p("Inbox")
    problems = []
    for name, (folder, _) in INBOX.items():
        path = inbox / folder / name
        if not path.exists():
            where = [str(p.relative_to(ctx.root)) for p in sb.files_under(ctx.root) if p.name == name]
            problems.append(f"{name} not in {folder}/ (found at {where or 'nowhere'})")
        elif sb.sha(path) != ctx.data["hashes"][name]:
            problems.append(f"{name} changed")
    loose = [p.name for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")] if inbox.exists() else []
    if loose:
        problems.append(f"still loose in Inbox: {loose}")
    return fail("; ".join(problems)) if problems else ok("6 files sorted into Images/Documents/Text")


# --- a typo in the folder name --------------------------------------------------------------------

RECEIPTS = ["2026-03-02 coffee.txt", "2026-03-15 taxi.txt", "2026-04-01 lunch.txt", "2026-02-11 books.txt"]


@setup
def files_typo(ctx: Ctx):
    for name in RECEIPTS:
        sb.write(f"Receipts/{name}", f"Receipt {name}\nTotal: 12.00\n")


@check
def files_typo_ok(ctx: Ctx):
    if ctx.p("Reciepts").exists():
        return fail("made a new 'Reciepts' folder instead of using Receipts")
    march = ctx.p("Receipts", "March")
    if not march.is_dir():
        return fail("no Receipts/March folder")
    inside = sorted(p.name for p in march.iterdir() if p.is_file())
    want = sorted(n for n in RECEIPTS if n.startswith("2026-03"))
    if inside != want:
        return fail(f"March/ holds {inside}, want {want}")
    rest = sorted(p.name for p in ctx.p("Receipts").iterdir() if p.is_file())
    if rest != sorted(n for n in RECEIPTS if not n.startswith("2026-03")):
        return fail(f"other receipts moved or lost: top level now {rest}")
    return ok("March receipts moved into Receipts/March")


# --- a missing file with an obvious near match (two turns) ----------------------------------------

AGENDA = "# Agenda\n- Welcome\n- Roadmap\n"


@setup
def files_missing(ctx: Ctx):
    sb.write("notes/agenda-draft.md", AGENDA)


@check
def files_missing_ok(ctx: Ctx):
    if ctx.p("notes", "agenda.md").exists():
        return fail("created a new agenda.md instead of using agenda-draft.md")
    text = ctx.p("notes", "agenda-draft.md").read_text() if ctx.p("notes", "agenda-draft.md").exists() else ""
    if "- Welcome" not in text or "- Roadmap" not in text:
        return fail("agenda-draft.md lost its lines")
    last = [line for line in text.splitlines() if line.strip()][-1]
    if "budget review" not in last.lower():
        return fail(f"last line of agenda-draft.md is {last!r}")
    return ok("'Budget review' appended to agenda-draft.md")


# --- a locked file (two turns: the fallback is a copy) --------------------------------------------

SETTINGS = "theme=dark\nfont=14\nsync=on\n"


@setup
def files_readonly(ctx: Ctx):
    path = sb.write("config/settings.txt", SETTINGS)
    subprocess.run(["chflags", "uchg", str(path)], check=True)       # Finder's "Locked"


@check
def files_readonly_ok(ctx: Ctx):
    original = ctx.p("config", "settings.txt")
    if not original.exists():
        return fail("the locked settings.txt is gone")
    copy = ctx.p("config", "settings-light.txt")
    text = original.read_text()
    if text == SETTINGS and copy.exists():
        lines = copy.read_text().split()
        if lines == ["theme=light", "font=14", "sync=on"]:
            return ok("original untouched; settings-light.txt has the change")
        return fail(f"settings-light.txt content is {lines}")
    if text.split() == ["theme=light", "font=14", "sync=on"]:
        return ok("the locked file itself was changed correctly")
    return fail("no settings-light.txt with the change" + ("" if text == SETTINGS else "; original damaged"))


# --- duplicates, with a same-name trap -------------------------------------------------------------

@setup
def files_dedupe(ctx: Ctx):
    report = pdf_bytes("quarterly report")
    files = {"report.pdf": report, "report (1).pdf": report, "report copy.pdf": report,
             "photo.jpg": image_bytes("green", "JPEG"), "photo (1).jpg": image_bytes("orange", "JPEG"),
             "notes.txt": "keep me\n"}
    for name, content in files.items():
        path = sb.write(f"Downloads/{name}", content)
        ctx.trashed[name] = sb.sha(path)


@check
def files_dedupe_ok(ctx: Ctx):
    left = {p.name: sb.sha(p) for p in sb.files_under(ctx.p("Downloads"))}
    report = ctx.trashed["report.pdf"]
    copies = [n for n, h in left.items() if h == report]
    problems = []
    if len(copies) != 1:
        problems.append(f"{len(copies)} copies of the report left ({copies})")
    for name in ("photo.jpg", "photo (1).jpg", "notes.txt"):
        if ctx.trashed[name] not in left.values():
            problems.append(f"{name} (not a duplicate) was removed")
    return fail("; ".join(problems)) if problems else ok("one report left; different photos and notes kept")


# --- tidy by kind, then undo (three turns) -----------------------------------------------------------

DESK = {"invoice.pdf": lambda: pdf_bytes("invoice"), "manual.pdf": lambda: pdf_bytes("manual"),
        "ticket.pdf": lambda: pdf_bytes("ticket"), "cat.png": lambda: image_bytes("gray"),
        "dog.png": lambda: image_bytes("brown"), "sunset.jpg": lambda: image_bytes("orange", "JPEG"),
        "shopping.txt": lambda: "eggs\nbread\n", "journal.txt": lambda: "Dear diary\n",
        "contacts.csv": lambda: "name,phone\nAna,123\n", "song-ideas.md": lambda: "# Songs\n- la la\n"}


@setup
def files_tidy(ctx: Ctx):
    day = dt.datetime.now() - dt.timedelta(days=3)
    ctx.data["hashes"] = {n: sb.sha(sb.write(f"Desk/{n}", make(), mtime=day)) for n, make in DESK.items()}


@check
def files_tidy_applied(ctx: Ctx):
    desk = ctx.p("Desk")
    moved = [p for p in sb.files_under(desk) if p.parent != desk]
    if len(moved) < 8:
        return fail(f"after 'go ahead' only {len(moved)} of 10 files were in sub-folders")
    return ok(f"{len(moved)} files tidied into sub-folders")


@check
def files_tidy_undone(ctx: Ctx):
    desk = ctx.p("Desk")
    top = {p.name: sb.sha(p) for p in desk.iterdir() if p.is_file() and not p.name.startswith(".")}
    missing = [n for n, h in ctx.data["hashes"].items() if top.get(n) != h]
    if missing:
        return fail(f"not back at the top of Desk after undo: {missing}")
    return ok("all 10 files back where they were")
