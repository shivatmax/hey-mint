"""Documents: reading, writing, converting, translating, PDFs and TextEdit."""

from __future__ import annotations

import re
import subprocess

from . import sandbox as sb
from .registry import Ctx, check, cleanup, fail, ok, setup

MEETING = """Weekly sync - notes (typed quickly, sorry)

Attendees: Nina, Tom, Lena, Omar, and Grace (guest from finance).

1. Budget. Grace walked us through the Q4 numbers. We are 6% over on contractors.
   ACTION: Nina to send the revised budget to finance by Friday.
2. Offsite. Everyone prefers the lake venue over the city hotel.
   Tom will book the venue for the offsite in November.
3. Hiring. Two roles are open. Discussion about whether to use an agency (no decision).
   Lena to draft the hiring plan and share it next week.
4. Release. The login bug is still open and blocks the release.
   Omar should fix the login bug before the release on the 14th.
5. AOB: Grace mentioned the new expense tool launches next month. No action for us.
"""


@setup
def docs_action_items(ctx: Ctx):
    sb.write("Docs/meeting-notes.txt", MEETING)


@check
def docs_action_items_ok(ctx: Ctx):
    path = ctx.p("Docs", "action-items.md")
    if not path.exists():
        return fail("no Docs/action-items.md")
    bullets = [line for line in path.read_text().splitlines() if re.match(r"^\s*([-*+]|\d+[.)])\s+", line)]
    want = {"Nina": "budget", "Tom": "venue", "Lena": "hiring", "Omar": "login"}
    missing = [f"{who}/{what}" for who, what in want.items()
               if not any(who in b and what in b.lower() for b in bullets)]
    if missing:
        return fail(f"action items missing or not one bullet each: {missing}")
    if any("Grace" in b for b in bullets):
        return fail("listed Grace's non-action as an action item")
    return ok(f"{len(bullets)} bullets with all 4 owners")


PROPOSAL = """# Project Falcon

A proposal to move the billing service to the new platform.

## Goals

- Cut billing latency in half
- Retire the old batch jobs

## Timeline

| Phase | Weeks |
|---|---|
| Design | 2 |
| Build | 6 |
| Rollout | 3 |

## Risks

Data migration is the main risk; we will run both systems side by side for a month.
"""


@setup
def docs_convert(ctx: Ctx):
    sb.write("Docs/proposal.md", PROPOSAL)


@check
def docs_convert_ok(ctx: Ctx):
    found = [p for p in sb.new_files(ctx.started, (".docx",)) if "proposal" in p.name.lower()]
    if not found:
        return fail("no proposal .docx made")
    text = sb.docx_text(found[0])
    missing = [w for w in ("Project Falcon", "Goals", "Timeline", "Rollout", "Data migration") if w not in text]
    if missing:
        return fail(f"{found[0].name} lacks {missing}")
    where = "" if sb.ROOT in found[0].parents else " (saved outside ~/MintBench)"
    return ok(f"{found[0].name} has the headings and table{where}")


WELCOME = ("Welcome to the team! Your first day starts at nine in the morning. "
           "Please bring your ID card to reception. Lunch is provided every Friday.\n")


@setup
def docs_translate(ctx: Ctx):
    sb.write("Docs/welcome.txt", WELCOME)


@check
def docs_translate_ok(ctx: Ctx):
    found = sb.new_files(ctx.started, (".docx",))
    if not found:
        return fail("no Word document made")
    for path in found:
        text = sb.docx_text(path).lower()
        hits = [w for w in ("bienvenid", "equipo", "viernes", "almuerzo", "mañana", "identificación", "recepción")
                if w in text]
        if len(hits) >= 3 and "welcome to the team" not in text:
            return ok(f"{path.name} is Spanish ({', '.join(hits)})")
    return fail(f"no Spanish text in {[p.name for p in found]}")


STATUS = """Status for the week of 21 September

Wins: shipped the new onboarding flow; signed Contoso as a customer.
Risks: the payments vendor may raise prices; one engineer is out next week.
Next: start the mobile beta; hire a designer.
"""


@setup
def docs_pdf_report(ctx: Ctx):
    sb.write("Reports/status.txt", STATUS)


@check
def docs_pdf_report_ok(ctx: Ctx):
    pdfs = sb.new_files(ctx.started, (".pdf",))
    if not pdfs:
        return fail("no PDF made")
    inside = [p for p in pdfs if ctx.p("Reports") in p.parents]
    path = (inside or pdfs)[0]
    text = " ".join(sb.pdf_text(path).split()).lower()
    missing = [w for w in ("wins", "risks", "next steps") if w not in text]
    facts = [w for w in ("onboarding", "contoso", "payments", "mobile beta", "designer") if w in text]
    if missing:
        return fail(f"{path.name} lacks sections {missing}")
    if len(facts) < 3:
        return fail(f"{path.name} uses only {facts} from status.txt")
    if not inside:
        return fail(f"PDF made but saved at {path} instead of ~/MintBench/Reports")
    return ok(f"{path.name}: 3 sections, {len(facts)} facts")


LETTER = "Dear Sir,\n\nThank you for your quick reply about the lease.\n\nKind regards,\nAlex\n"


def _close_textedit_docs(ctx: Ctx) -> None:
    """Close (without saving) TextEdit windows on files in the sandbox. TextEdit keeps a window on a file even
    after the file is deleted and made again, still showing the old text, and "open letter.rtf" then only
    brings that old window forward; saving it then hits "changed by another application". A window left by
    an earlier task or run must not decide this one (run 20260929-215652 failed that way)."""
    if subprocess.run(["pgrep", "-xq", "TextEdit"], check=False).returncode != 0:
        return                                      # never start TextEdit for this
    for root in {str(ctx.root), str(ctx.root.resolve())}:
        script = f'tell application "TextEdit" to close (every document whose path starts with "{root}/") saving no'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15, check=False)


@cleanup
def close_textedit_docs(ctx: Ctx):
    _close_textedit_docs(ctx)


@setup
def docs_textedit(ctx: Ctx):
    _close_textedit_docs(ctx)
    txt = sb.write("Docs/letter.txt", LETTER)
    subprocess.run(["textutil", "-convert", "rtf", "-output", str(ctx.p("Docs", "letter.rtf")), str(txt)], check=True)
    txt.unlink()
    from .apple import app_running
    ctx.data["complication"] = "TextEdit was not open" if not app_running("TextEdit") else "TextEdit was already open"


@check
def docs_textedit_ok(ctx: Ctx):
    path = ctx.p("Docs", "letter.rtf")
    if not path.exists():
        return fail("letter.rtf is gone")
    text = sb.rich_text(path)
    if "Dear Ms. Rao" not in text and "Dear Ms Rao" not in text:
        return fail("letter.rtf does not say 'Dear Ms. Rao' (not saved?)")
    if "Dear Sir" in text or "Kind regards" not in text:
        return fail("letter.rtf content wrong: " + " / ".join(text.split("\n")[:3]))
    return ok("letter.rtf edited and saved (" + ctx.data.get("complication", "") + ")")
