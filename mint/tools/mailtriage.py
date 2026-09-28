"""Mail triage and replies in the user's own style - drafts only, never sent.

"what needs my attention in my inbox?", "triage my email", "draft a reply to Priya's email
saying I'll send it Friday", "reply to the second one, say yes".

Works through the Mail app (so any account added to it - Gmail, iCloud, Outlook - without
a Google sign-in here). triage reads the newest inbox messages (sender, subject, date,
unread, the first lines) and Gemini Flash Lite sorts them: needs a reply, needs action,
FYI, newsletters and promotions, with one line each on what it is about. draft_reply reads
the whole message, looks at a few messages the user sent to learn how they write
(greeting, length, sign-off, tone), writes the reply in that style, and opens it as a
draft in Mail for the user to read and send themselves.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("mint.tools.mailtriage")

_SEP = "␞"          # a record separator that will not appear in mail


def _run(script: str, timeout: float = 40) -> tuple[bool, str]:
    from mint.tools import everyday as skills
    return skills._osascript(script, timeout=timeout)


def inbox(count: int = 15) -> list[dict]:
    """The newest messages: sender, subject, unread. One bulk request per property - Mail
    answers per request, not per message (a 47,000-message inbox took ~1-2 s per request,
    so reading message by message timed out)."""
    count = max(1, min(int(count), 30))
    script = f'''
    tell application "Mail"
        set n to count of messages of inbox
        if n > {count} then set n to {count}
        if n is 0 then return ""
        set a to sender of messages 1 thru n of inbox
        set b to subject of messages 1 thru n of inbox
        set c to read status of messages 1 thru n of inbox
        set out to ""
        repeat with i from 1 to n
            set out to out & i & "{_SEP}" & (item i of a) & "{_SEP}" & (item i of b) & "{_SEP}" & (item i of c) & "{_SEP}{_SEP}"
        end repeat
        return out
    end tell'''
    ok, out = _run(script, timeout=120)
    if not ok:
        raise RuntimeError(out)
    rows = []
    for record in out.split(_SEP + _SEP):
        parts = record.strip().split(_SEP)
        if len(parts) >= 4:
            rows.append({"n": int(parts[0]), "from": parts[1], "subject": parts[2], "date": "",
                         "unread": parts[3].strip() == "false", "body": ""})
    return rows


def triage(count: int = 15) -> str:
    from mint.core import llm
    try:
        rows = inbox(count)
    except RuntimeError as error:
        from mint.tools import everyday as skills
        return "Could not read Mail. " + skills._automation_hint(str(error), "Mail")
    if not rows:
        return "The inbox is empty."
    listing = "\n".join(f"{r['n']}. {'[unread] ' if r['unread'] else ''}{r['from']} | {r['subject']}" for r in rows)
    answer = llm.ask_json(
        "Sort these emails (newest first) for a busy person. For each: its number, a category - reply (a person "
        "is waiting for their answer), action (something to do: pay, sign, book, review), fyi, newsletter, promo, "
        "automated (receipts, alerts, notifications) - urgent true/false, and one short line on what it is about. "
        'Return JSON {"emails": [{"n": 1, "category": "...", "urgent": false, "about": "..."}]}.\n\n' + listing)
    if not isinstance(answer, dict):
        return "Could not sort the inbox just now:\n" + listing[:1500]
    by_n = {r["n"]: r for r in rows}
    groups: dict[str, list[str]] = {}
    for e in answer.get("emails") or []:
        r = by_n.get(int(e.get("n", 0)) if str(e.get("n", "")).isdigit() else 0)
        if r is None:
            continue
        who = re.sub(r"\s*<[^>]+>", "", r["from"]).strip('" ')
        groups.setdefault(str(e.get("category", "fyi")), []).append(
            f"{'URGENT ' if e.get('urgent') else ''}#{r['n']} {who}: {e.get('about', r['subject'])}")
    order = ["reply", "action", "fyi", "automated", "newsletter", "promo"]
    out = [f"{k.upper()} ({len(v)}): " + "; ".join(v) for k in order for v in [groups.get(k, [])] if v]
    return ("Inbox, newest " + str(len(rows)) + " (numbers as in the inbox):\n" + "\n".join(out)
            + "\nTell the user what needs them first; offer to draft replies (draft_reply with the number).")


def _sent_samples(limit: int = 6) -> str:
    script = f'''
    tell application "Mail"
        set out to ""
        set n to count of messages of sent mailbox
        if n > {limit} then set n to {limit}
        if n is 0 then return ""
        set msgs to messages 1 thru n of sent mailbox
        repeat with i from 1 to n
            try
                set out to out & (text 1 thru 700 of (content of item i of msgs)) & "{_SEP}"
            on error
                try
                    set out to out & (content of item i of msgs) & "{_SEP}"
                end try
            end try
        end repeat
        return out
    end tell'''
    ok, out = _run(script, timeout=90)
    if not ok:
        return ""
    # Only what the user wrote: stop at the quoted message they replied to.
    samples = [re.split(r"\n\s*On .{5,80} wrote:|\n>", s)[0].strip() for s in out.split(_SEP)]
    return "\n---\n".join(s[:500] for s in samples if len(s) > 20)


def _message(number: int) -> dict:
    script = f'''
    tell application "Mail"
        set m to message {int(number)} of inbox
        return (sender of m) & "{_SEP}" & (subject of m) & "{_SEP}" & (content of m)
    end tell'''
    ok, out = _run(script, timeout=120)
    if not ok:
        raise RuntimeError(out)
    sender, subject, body = (out.split(_SEP, 2) + ["", ""])[:3]
    return {"from": sender, "subject": subject, "body": body[:8000]}


def draft_reply(number: int, instruction: str = "") -> str:
    from mint.core import llm
    from mint.core import prefs
    from mint.tools import everyday as skills
    from mint.knowledge.skills import has_secret
    try:
        msg = _message(number)
    except RuntimeError as error:
        return f"Could not read message {number}. " + skills._automation_hint(str(error), "Mail")
    style = _sent_samples()
    name = prefs.get("user_name") or ""
    prompt = (f"Write a reply email{(' from ' + name) if name else ''} to the message below. "
              + (f"What to say: {instruction}. " if instruction else "Reply sensibly to what it asks; keep promises "
                 "vague if you do not know the answer (e.g. 'I'll check and get back to you'). ")
              + "Match the user's own writing style from their sent emails below (greeting, length, tone, sign-off); "
              "if there are none, be brief and friendly. Never invent facts, dates or commitments beyond the "
              "instruction. Return ONLY the reply body.\n\n"
              f"MESSAGE from {msg['from']}, subject '{msg['subject']}':\n{msg['body'][:5000]}\n\n"
              f"THE USER'S SENT EMAILS (style only):\n{style or '(none available)'}")
    try:
        body, _ = llm.generate(prompt)
    except Exception as error:
        return f"FAILED: could not write the reply: {error}"
    body = body.strip()
    if has_secret(body):
        return "Refused: the draft would contain something that looks like a password or key."
    address = re.search(r"<([^>]+)>", msg["from"])
    to = address.group(1) if address else msg["from"].strip()
    subject = msg["subject"] if msg["subject"].lower().startswith("re:") else f"Re: {msg['subject']}"
    skills.compose_email(to, subject, body)
    return (f"Opened a draft reply to {to} ('{subject}') in the mail app - NOT sent; the user reads and sends it. "
            f"It says: {body[:600]}")


PROMPT = """Email: "what needs my attention in my inbox?", "triage my email" -> mail action=triage (sorted: \
needs a reply, to do, FYI, newsletters). "Draft a reply to #2 saying …", "reply to Priya's email" -> mail \
action=draft_reply with the number from triage/list_emails and what to say. Replies are drafts in the user's \
own style; Mint never sends email."""


def declarations():
    from google.genai import types
    S, I = types.Type.STRING, types.Type.INTEGER
    return [types.FunctionDeclaration(
        name="mail",
        description=("The Mail app's inbox, smarter: triage (sort the newest messages into needs a reply, to do, FYI, "
                     "newsletters, promos, with one line each) and draft_reply (write a reply to message `number` "
                     "in the user's own style, from what they say, and open it as a draft - never sent)."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "action": types.Schema(type=S, enum=["triage", "draft_reply"]),
            "number": types.Schema(type=I, description="draft_reply: the message number (1 = newest)"),
            "say": types.Schema(type=S, description="draft_reply: what the reply should say"),
            "count": types.Schema(type=I, description="triage: how many recent messages (default 15)")},
            required=["action"]))]


def tool(args: dict) -> str:
    """Mail can be slow with a big inbox: answer within 20 s, else finish in the background and report."""
    import threading
    if str(args.get("action") or "triage") == "draft_reply":
        if not args.get("number"):
            return "Which message? Give its number (from triage or list_emails)."
        job = lambda: draft_reply(int(args["number"]), str(args.get("say") or ""))  # noqa: E731
    else:
        job = lambda: triage(int(args.get("count") or 15))  # noqa: E731
    state: dict = {}

    def run():
        try:
            state["result"] = job()
        except Exception as error:
            state["result"] = f"FAILED: {error}"
        if state.get("late"):
            try:
                from mint.tools.work import _notify_mint
                _notify_mint("(Mint's mail helper, not the user.) " + state["result"])
            except Exception:
                pass
    worker = threading.Thread(target=run, daemon=True, name="mail")
    worker.start()
    worker.join(20)
    if worker.is_alive():
        state["late"] = True
        return ("The Mail app is slow with this inbox, so this continues in the background; a message comes when "
                "it is done. Tell the user in a few words. (For Gmail, reading the inbox in the browser is faster.)")
    return state["result"]


HANDLERS = {"mail": tool}
