"""Email remote control: email Mint a request from anywhere, get the answer back by email.

For when Telegram isn't at hand. Two ways in (Settings ▸ Accounts & connections ▸ Email control ▸ "Read mail with";
"auto", the default, takes IMAP whenever the address has an app password in the Keychain):
  - IMAP + SMTP with an app password (in the macOS Keychain, never shown again - the user pastes it, Mint never
    types one). The fast way: a second connection sits in IMAP IDLE (re-issued every IDLE_RENEW, before Gmail's
    ~29 min limit; reconnects with backoff), so the server tells Mint about a new message within a second or
    two; without IDLE it checks every POLL seconds. Replies are multipart (plain text + an HTML page).
  - Apple Mail (the fallback when there is no app password): the account the user already added in System
    Settings ▸ Internet Accounts (Gmail, Google Workspace, iCloud, …). Mint asks the Mail app over AppleScript
    every MAIL_POLL seconds for unread inbox messages with the prefix, reads their raw source, and answers with
    Mail's own reply (plain text). Mail is opened in the background when it isn't running. Nothing to create:
    works where Google won't make an app password (Workspace policy, no 2-Step Verification).
Either way there is no server in between, no API, nothing paid: only this Mac talks to the mail server. Replies
go out on a thread of their own (email-send): sending never holds up reading.

One email = one request = one answer. Requests run one at a time: a second email waits until the first is
finished - Mint's turn ended with no tool still running and nothing new for REPLY_SETTLE seconds (or the
session said "done") - and its answer holds Mint's own words and the steps it took. The same request twice
(another Message-ID, the same words from the same sender within DUP_WINDOW) runs once.

A message is acted on only when ALL of these hold - otherwise it is not touched (left unread, logged):
  - its subject starts with the prefix (default "Mint:"; "Re: Mint: …" is a reply in the same thread), so
    newsletters, ads and everything else in the inbox are never even read past their headers;
  - it came in after the channel was switched on and is at most MAX_AGE old (the Mac was asleep: not run);
  - it is not one Mint sent, not an automatic or mailing-list message, and its Message-ID was not seen before;
  - it has exactly one From address, and that address is allowed (the list, or "all");
  - the receiving server says the From address is genuine: in the topmost Authentication-Results header from
    the trusted server (mx.google.com for Gmail) DKIM passed for the From domain (header.d / header.i aligned),
    or SPF passed for an aligned envelope sender AND DMARC passed. A From header alone is trivially forged.
    Mail the account sent to itself never crossed an MX, so it counts instead when the account really sent
    it: over IMAP, Gmail's \\Sent label; in Apple Mail, the same Message-ID in the account's Sent mailbox
    with the same subject and text, addressed to the account itself (see AppleMailMailbox._sent_by_me);
  - the secret word is in the subject, when one is set (required when anyone may send requests);
  - at most MAX_PER_HOUR requests an hour.
Then it is marked read, quoted text and signatures are cut from the body, and the rest runs exactly like a
Telegram message: into the running session as if typed (telegram.SessionHost), every confirmation rule applies.
Mint's words, the steps it took, files it made, screenshots and a Google Meet link come back as ONE reply, only
to the verified sender, threaded (In-Reply-To / References, "Re: …"). Reply-To is ignored.

Commands, in the subject or as the first line: /status /screenshot /stop /meet (/meet end, /meet status)
/briefing /missed /help. When the guard asks before deleting or changing something during an emailed request,
the question comes by email too ("Reply YES or NO"), and a reply to it answers.

The way in is a Mailbox (ImapMailbox, AppleMailMailbox). Every request, command and refusal goes to
~/Library/Application Support/Mint/remote.log (never a password).
"""

from __future__ import annotations

import email
import hashlib
import html as _html
import imaplib
import json
import logging
import mimetypes
import os
import queue
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid
from html.parser import HTMLParser
from pathlib import Path

from mint.app import telegram

log = logging.getLogger("mint.app.email_remote")

SUPPORT = Path.home() / "Library" / "Application Support" / "Mint"
STATE = SUPPORT / "email_remote.json"
AUDIT = SUPPORT / "remote.log"            # the same log as Telegram's remote requests
KEYCHAIN = "Mint email control"           # Keychain service; the account is the address
SECRET_ACCOUNT = "secret word"            # ... and this account holds the secret word (no "@": never an address)

POLL = 15                   # seconds between checks of the inbox (IMAP without IDLE)
IDLE_POLL = 120             # ... while IDLE is watching: only a safety net
IDLE_RENEW = 25 * 60        # IDLE is ended and started again this often (Gmail drops it at ~29 min)
FAST_POLL = 5               # ... while a guard question waits for an emailed answer
MAIL_POLL = 30              # the same through Apple Mail: each AppleScript call takes 1-2 s on a big inbox
MAIL_FAST_POLL = 8
MAIL_WINDOW = 20 * 60       # Apple Mail: read messages this recent are listed too (mail sent to oneself may
                            # arrive read, or the user opened it on the Mac); the id cursor and dedupe keep it once
MAX_AGE = 15 * 60           # a request older than this (the Mac was asleep) is not run
MAX_PER_HOUR = 20           # requests and commands an hour, from everyone together
MAX_SCAN = 50               # new messages looked at per check (their headers only)
MAX_RAW = 30 * 1024 * 1024  # a message bigger than this is not fetched
MAX_TEXT = 4000             # the longest request taken from a message
MAX_ATTACH = 20 * 1024 * 1024   # all attachments of one reply together (Gmail takes 25 MB)
MAX_FILES = 5
KEEP_IDS = 500              # Message-IDs remembered (seen, and sent by Mint)
DUP_WINDOW = 120            # the same words from the same sender within this long: one request
MAX_QUEUE = 5               # emailed requests waiting for the one running
# When is a request finished (Channel._over)? Mint's words were the last thing, no tool is running and the
# session is quiet (no turn open, not speaking) for REPLY_SETTLE - or PLAN_SETTLE while plan steps are open
# (the autopilot carries on after ~2 s). A tool still running: up to WORK_CAP, then "still working".
REPLY_SETTLE = 3.0
PLAN_SETTLE = 20.0
AFTER_TOOL = 60.0           # a tool's result was the last thing and no words follow
SILENT_WAIT = 120.0         # nothing at all happened
WORK_CAP = 5 * 60

PREFS = {"email_enabled": False, "email_address": "", "email_allow": "list", "email_allowed": "",
         "email_prefix": "Mint:", "email_read_only": False, "email_imap_host": "", "email_smtp_host": "",
         "email_auth_server": "", "email_backend": "auto", "email_mail_account": ""}

# Well-known providers: (IMAP host, SMTP host, SMTP port). 465 is SMTP over TLS, 587 STARTTLS.
SERVERS = {"gmail.com": ("imap.gmail.com", "smtp.gmail.com", 465),
           "googlemail.com": ("imap.gmail.com", "smtp.gmail.com", 465),
           "icloud.com": ("imap.mail.me.com", "smtp.mail.me.com", 587),
           "me.com": ("imap.mail.me.com", "smtp.mail.me.com", 587),
           "mac.com": ("imap.mail.me.com", "smtp.mail.me.com", 587),
           "fastmail.com": ("imap.fastmail.com", "smtp.fastmail.com", 465),
           "yahoo.com": ("imap.mail.yahoo.com", "smtp.mail.yahoo.com", 465)}
# Which server's Authentication-Results to trust, by IMAP host (others: the topmost header).
AUTH_SERVERS = {"imap.gmail.com": "mx.google.com"}

HELP = ("Mint by email\n\n"
        "Write your request after the prefix in the subject (\"Mint: what's on my calendar today?\") or in the "
        "message, and I'll do it on the Mac and answer here. Reply in this thread to carry on.\n\n"
        "Commands (in the subject or as the first line):\n"
        "/status - what Mint is doing, and whether the screen is locked\n"
        "/screenshot - a picture of the screen\n"
        "/stop - stop whatever Mint is doing\n"
        "/meet - start a Google Meet with Mint (it shares the screen); /meet end, /meet status\n"
        "/briefing - my day\n"
        "/missed - what did I miss? (notifications)\n"
        "/help - this list\n\n"
        "When Mint wants to delete or change something, it asks here first: reply YES or NO.")

_COMMANDS = {"help", "status", "screenshot", "screen", "shot", "stop", "meet", "briefing", "missed"}
_MEET_LINK = re.compile(r"https://meet\.google\.com/[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}\b")
_RE = re.compile(r"^\s*(?:re|aw|sv|antw|odp)\s*(?:\[\d+\])?\s*:\s*", re.I)


# --- secrets (the macOS Keychain) -----------------------------------------------------------------

def keychain_get(account: str) -> str:
    """The stored value, '' when there is none. `security` made the item, so reading it asks nobody."""
    if not account:
        return ""
    try:
        done = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN, "-a", account, "-w"],
                              capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    return done.stdout.rstrip("\n") if done.returncode == 0 else ""


def keychain_has(account: str) -> bool:
    if not account:
        return False
    try:
        return subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN, "-a", account],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def keychain_set(account: str, value: str) -> bool:
    """Save (or, empty, remove) a secret. The value goes in on stdin, never on the command line."""
    if not account:
        return False
    if not value:
        subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN, "-a", account],
                       capture_output=True, timeout=10)
        return True
    done = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN, "-a", account, "-w"],
                          input=f"{value}\n{value}\n", capture_output=True, text=True, timeout=10)
    return done.returncode == 0


def save_password(address: str, value: str) -> bool:
    """Settings: the app password the user pasted (spaces Google shows between the groups dropped)."""
    ok = keychain_set(address.strip().lower(), "".join(value.split()))
    refresh()
    return ok


def save_secret(value: str) -> bool:
    ok = keychain_set(SECRET_ACCOUNT, value.strip())
    refresh()
    return ok


# --- reading a message ----------------------------------------------------------------------------

def _one_line(value) -> str:
    return " ".join(str(value or "").split())


def sender(msg) -> str:
    """The one From address, lower-cased; '' when there are several From headers or addresses (a trick to
    have one address checked and another shown)."""
    headers = msg.get_all("From") or []
    if len(headers) != 1:
        return ""
    pairs = [a for _, a in getaddresses([str(headers[0])]) if a]
    if len(pairs) != 1:
        return ""
    address = pairs[0].strip().lower()
    return address if re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address) else ""


def _domain(value: str) -> str:
    return str(value or "").strip().strip("<>").rsplit("@", 1)[-1].strip().strip(".").lower()


def aligned(signer: str, author: str) -> bool:
    """Relaxed alignment: the same domain, or one a subdomain of the other (both need DNS control there)."""
    signer, author = _domain(signer), _domain(author)
    if not signer or not author or "." not in signer or "." not in author:
        return False
    return signer == author or signer.endswith("." + author) or author.endswith("." + signer)


_PROP = re.compile(r"([a-z]+\.[a-z-]+)\s*=\s*(\"[^\"]*\"|[^\s;]+)", re.I)
_RESULT = re.compile(r"^\s*([a-z-]+)\s*=\s*([a-z]+)", re.I)


def auth_results(msg, trusted: str = "") -> tuple[str, list[dict]] | None:
    """(server, [{method, result, props}]) from the receiving server's Authentication-Results header: the
    topmost one, or with `trusted` the first one that server wrote (it adds its own above anything the sender
    put in the message). None when there is none."""
    for value in msg.get_all("Authentication-Results") or []:
        text = re.sub(r"\([^()]*\)", " ", _one_line(value))        # comments: (google.com: domain of ...)
        head, _, rest = text.partition(";")
        server = (head.split() or [""])[0].lower()
        if trusted and server != trusted.lower():
            continue
        results = []
        for part in rest.split(";"):
            found = _RESULT.match(part)
            if not found:
                continue
            props = {k.lower(): v.strip('"') for k, v in _PROP.findall(part[found.end():])}
            results.append({"method": found.group(1).lower(), "result": found.group(2).lower(), "props": props})
        return server, results
    return None


def authenticated(msg, author: str, trusted: str = "", account: str = "", labels=()) -> tuple[bool, str]:
    """(genuine?, how or why not) for the From address `author`."""
    if account and author == account and "\\Sent" in set(labels or ()):
        return True, "sent from this account"
    found = auth_results(msg, trusted)
    if found is None:
        return False, f"no Authentication-Results from {trusted or 'the receiving server'}"
    _, results = found
    for r in results:
        if r["method"] == "dkim" and r["result"] == "pass":
            signer = r["props"].get("header.d") or r["props"].get("header.i", "")
            if aligned(signer, author):
                return True, f"dkim=pass for {_domain(signer)}"
    spf = any(r["method"] == "spf" and r["result"] == "pass"
              and aligned(r["props"].get("smtp.mailfrom", ""), author) for r in results)
    dmarc = any(r["method"] == "dmarc" and r["result"] == "pass"
                and aligned(r["props"].get("header.from", author), author) for r in results)
    if spf and dmarc:
        return True, "spf=pass and dmarc=pass"
    seen = " ".join(f"{r['method']}={r['result']}" for r in results) or "no results"
    return False, f"not verified ({seen})"


def automatic(msg) -> bool:
    """Auto-replies, bounces and mailing lists: never requests (and never answered: no mail loops)."""
    auto = _one_line(msg.get("Auto-Submitted", "")).lower()
    if auto and auto != "no":
        return True
    if _one_line(msg.get("Precedence", "")).lower() in ("bulk", "list", "junk", "auto_reply"):
        return True
    return bool(msg.get("List-Id") or msg.get("List-Unsubscribe") or msg.get("X-Autoreply")
                or msg.get("X-Autorespond"))


def subject_parts(subject: str, prefix: str, secret: str = "") -> tuple[str, str, bool]:
    """('ok' | 'no prefix' | 'no secret', the rest of the subject, is a reply). The prefix is matched case-
    insensitively after any "Re:"; the secret word, when set, must be in the rest and is taken out of it."""
    text, reply = _one_line(subject), False
    while True:
        found = _RE.match(text)
        if not found:
            break
        text, reply = text[found.end():], True
    prefix = _one_line(prefix) or PREFS["email_prefix"]
    if not text.lower().startswith(prefix.lower()):
        return "no prefix", "", reply
    rest = text[len(prefix):]
    if prefix[-1].isalnum() and rest[:1].isalnum():
        return "no prefix", "", reply                  # "Mint" must not match "Minty deals"
    rest = rest.lstrip(" :-–—").strip()
    if secret:
        at = rest.lower().find(secret.lower())
        if at < 0:
            return "no secret", rest, reply
        rest = _one_line(rest[:at] + " " + rest[at + len(secret):]).strip(" :-–—")
    return "ok", rest, reply


class _HtmlText(HTMLParser):
    """An HTML-only message as plain text: no scripts or styles, nothing quoted (blockquote, Gmail's quote)."""

    _BREAKS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip: list[str] = []          # open tags whose content is left out

    def handle_starttag(self, tag, attrs):
        classes = " ".join(str(v or "") for k, v in attrs if k == "class")
        if self.skip or tag in ("script", "style", "head", "blockquote") or "gmail_quote" in classes \
                or "gmail_signature" in classes:
            if tag not in ("br", "img", "hr", "meta", "link", "input"):
                self.skip.append(tag)
            return
        if tag in self._BREAKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in self._BREAKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(markup: str) -> str:
    parser = _HtmlText()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", markup)
    return re.sub(r"[ \t\xa0]+", " ", "".join(parser.out))


def body_text(msg) -> str:
    """The message's own words: its text/plain part, else its HTML as text. Attachments are not read."""
    plain = rich = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        kind = part.get_content_type()
        if kind not in ("text/plain", "text/html"):
            continue
        try:
            text = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode("utf-8", "replace")
        if kind == "text/plain" and not plain:
            plain = str(text)
        elif kind == "text/html" and not rich:
            rich = str(text)
    return plain if plain.strip() else html_text(rich) if rich else ""


# Where the quoted message or the signature starts: everything from there on is cut.
_CUTS = [re.compile(p, re.I | re.M) for p in (
    r"^\s*On\b[^\n]{0,300}(?:\n[^\n]{0,300})?\bwrote:\s*$",              # Gmail, Apple Mail (wrapped too)
    r"^\s*Le\b[^\n]{0,300}a écrit\s*:\s*$", r"^\s*Am\b[^\n]{0,300}schrieb[^\n]{0,80}:\s*$",
    r"^\s*-{2,}\s*(?:Original|Forwarded) Message\s*-{2,}", r"^\s*Begin forwarded message:",
    r"^\s*_{8,}\s*$",                                                     # Outlook's line
    r"^\s*From:\s[^\n]+\n\s*(?:Sent|Date):\s",                            # Outlook's header block
    r"^-- ?$",                                                            # the signature delimiter
    r"^\s*Sent from my (?:iPhone|iPad|Android|phone|mobile)\b", r"^\s*Get Outlook for\b",
    r"^\s*Sent from (?:Mail|Yahoo Mail|Outlook) for\b")]


def _unquoted(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    cut = min([m.start() for m in (p.search(text) for p in _CUTS) if m] or [len(text)])
    lines = [line.rstrip() for line in text[:cut].split("\n") if not line.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def strip_quoted(text: str) -> str:
    """Only what the sender wrote now: no quoted reply, no "On … wrote:", no signature."""
    return _unquoted(text)[:MAX_TEXT]


# Apple Mail sends its own MIME: no X-Mint-Remote header and its own Message-ID. So each message Mint sends
# through it ends with "Mint ref: <12 hex>" (from the Message-ID make_reply chose, which Mint remembers): in a
# message's own words it marks Mint's own mail; quoted in a reply it says which guard question is answered.
_MARK = re.compile(r"\bMint ref:? ?([0-9a-f]{12})\b")


def mark_of(message_id: str) -> str:
    return hashlib.sha256(str(message_id or "").encode()).hexdigest()[:12]


def _all_text(msg) -> str:
    """Every text part as it is (quotes included, HTML not rendered): where a quoted mark is looked for."""
    out = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart() or part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            out.append(str(part.get_content()))
        except Exception:
            out.append((part.get_payload(decode=True) or b"").decode("utf-8", "replace"))
    return "\n".join(out)


def command(text: str) -> tuple[str, str] | None:
    """('meet', 'end') for '/meet end' at the start of `text`; None when it isn't one of the commands."""
    found = re.match(r"^\s*/([a-z]+)\b[ \t]*([^\n]*)", text or "", re.I)
    if not found or found.group(1).lower() not in _COMMANDS:
        return None
    return found.group(1).lower(), found.group(2).strip()


def _norm(text: str) -> str:
    """For comparing requests: lower case, words only."""
    return " ".join(re.sub(r"[^\w\s]", " ", str(text or "").lower()).split())


def _unaddressed(text: str, prefix: str) -> str:
    """`text` without the prefix or a "Mint," / "Hey Mint" it starts with ("Mint: Mint what's on" -> "what's on")."""
    prefix = _one_line(prefix) or PREFS["email_prefix"]
    word = re.sub(r"[^\w]", "", prefix)
    lead = [re.escape(prefix) + (r"(?!\w)" if prefix[-1].isalnum() else "")]
    if word:
        lead.append(r"(?:(?:hey|hi|ok|okay)\s+)?" + re.escape(word) + r"\b")
    pattern = re.compile(r"^\s*(?:" + "|".join(lead) + r")[\s,:;.!–—-]*", re.I)
    text = str(text or "").strip()
    while True:
        found = pattern.match(text)
        if not found or not found.group(0).strip():
            return text.strip()
        text = text[found.end():]


def _without_secret(text: str, secret: str) -> str:
    """The secret word never goes to the model: it is taken out of the message too (it must be in the subject)."""
    if not secret:
        return text
    return re.sub(r"[ \t]{2,}", " ", re.sub(re.escape(secret), " ", str(text or ""), flags=re.I)).strip()


def request_text(rest: str, body: str, reply: bool = False, prefix: str = "", secret: str = "") -> str:
    """What Mint is asked, from the subject after the prefix (`rest`) and the message's own words (`body`,
    quotes already cut): each without the prefix, a leading "Mint," or the secret word; in a reply only the
    body (the subject is the old request); a command in the subject alone; the body once when it only
    repeats the subject (or carries on from it)."""
    rest = _unaddressed(_without_secret(rest, secret), prefix)
    body = _unaddressed(_without_secret(body, secret), prefix)
    if reply:
        return body
    if command(rest) or not body:
        return rest
    if not rest:
        return body
    subject, words = _norm(rest), _norm(body)
    if words == subject or words.startswith(subject + " "):
        return body
    if subject.startswith(words + " "):
        return rest
    return f"{rest}\n\n{body}"


# --- the HTML reply -------------------------------------------------------------------------------
# Email clients drop <style>: every style is inline. Nothing from Mint's words or an email goes in unescaped.

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
_LINK = "color:#1a73e8;text-decoration:underline;"
_SAFE_URL = re.compile(r"(?:https?://|mailto:)[^\s<>\"'`]+\Z", re.I)
_ENTITY_STOP = re.compile(r"&(?:quot|#x27|#39|lt|gt);")


def _anchor(href: str, label: str) -> str:
    """`href` and `label` are escaped already; only http(s) and mailto links are made."""
    return f'<a href="{href}" style="{_LINK}">{label}</a>'


def _inline(line: str) -> str:
    """One line of Mint's words as HTML: escaped first, then `code`, [links](https://…), bare links, **bold**
    and *italic*. Made markup is held aside so nothing later rewrites it."""
    held: list[str] = []

    def hold(markup: str) -> str:
        held.append(markup)
        return f"\x00{len(held) - 1}\x00"

    def bare(found) -> str:
        url = _ENTITY_STOP.split(found.group(0))[0]
        while url and url[-1] in ".,;:!?)]}*'" and not url.endswith("&amp;"):
            url = url[:-1]
        if not _SAFE_URL.match(url) or len(url) < 10:
            return found.group(0)
        return hold(_anchor(url, url)) + found.group(0)[len(url):]

    def link(found) -> str:
        label, url = found.group(1), found.group(2)
        if _ENTITY_STOP.search(url) or not _SAFE_URL.match(url):
            return found.group(0)
        return hold(_anchor(url, label))

    text = _html.escape(str(line).replace("\x00", ""), quote=True)
    text = re.sub(r"`([^`\n]+)`", lambda m: hold(
        f'<code style="font-family:{_MONO};font-size:13px;background:#f1f3f2;padding:1px 4px;'
        f'border-radius:4px;">{m.group(1)}</code>'), text)
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, text)
    text = re.sub(r"https?://[^\s<>\x00]+", bare, text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])", r"<em>\1</em>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], text)


_BULLET = re.compile(r"^\s*(?:([-*•])|(\d{1,3})[.)])\s+(.*)$")


def md_html(text: str) -> str:
    """Mint's words (markdown-ish) as safe HTML: paragraphs, line breaks, bullet and numbered lists, headings
    (as bold lines), ``` blocks, `code`, **bold**, *italic*, links. Raw HTML in the words stays visible text."""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    kind = ""

    def flush_para() -> None:
        if para:
            out.append('<p style="margin:0 0 12px 0;">' + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal kind
        if items:
            out.append(f'<{kind} style="margin:0 0 12px 0;padding-left:22px;">'
                       + "".join(f'<li style="margin:0 0 4px 0;">{_inline(x)}</li>' for x in items) + f"</{kind}>")
            items.clear()
        kind = ""

    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.strip().startswith("```"):
            flush_para()
            flush_list()
            block = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            out.append(f'<pre style="margin:0 0 12px 0;padding:10px 12px;background:#f5f7f6;border-radius:8px;'
                       f'font-family:{_MONO};font-size:13px;line-height:1.45;white-space:pre-wrap;'
                       f'word-break:break-word;">{_html.escape(chr(10).join(block), quote=True)}</pre>')
            continue
        if not line.strip():
            flush_para()
            flush_list()
            continue
        bullet = _BULLET.match(line)
        if bullet:
            flush_para()
            want = "ul" if bullet.group(1) else "ol"
            if kind and kind != want:
                flush_list()
            kind = want
            items.append(bullet.group(3).strip())
            continue
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            flush_para()
            flush_list()
            out.append(f'<p style="margin:0 0 8px 0;"><strong>{_inline(heading.group(1).strip())}</strong></p>')
            continue
        if items and line[:1] in (" ", "\t"):
            items[-1] += " " + line.strip()             # a list item carried onto the next line
            continue
        flush_list()
        para.append(line.strip())
    flush_para()
    flush_list()
    return "".join(out)


def email_html(words: str, head: str = "", notes=(), links=(), steps=(), total: int = 0, files=(), too_big=(),
               footer: str = "") -> str:
    """The whole HTML reply: a small "Mint" header, `head` (how it ended), the words, a Google Meet button, the
    steps, what is attached, and the footer that marks Mint's own reply. At most 600 px wide, system fonts."""
    esc = _html.escape
    cell = f"font-family:{_FONT};"
    rows = [f'<tr><td style="padding:18px 24px 0 24px;{cell}font-size:13px;font-weight:600;color:#2e8b62;'
            f'letter-spacing:0.2px;"><span style="display:inline-block;width:9px;height:9px;border-radius:5px;'
            f'background:#3cb878;margin-right:6px;"></span>Mint</td></tr>']
    if head:
        rows.append(f'<tr><td style="padding:10px 24px 0 24px;{cell}font-size:13px;color:#5f6b66;">'
                    f"{esc(head)}</td></tr>")
    rows.append(f'<tr><td style="padding:12px 24px 2px 24px;{cell}font-size:15px;line-height:1.55;color:#1d2321;">'
                f"{md_html(words)}</td></tr>")
    for link in [x for x in links if _MEET_LINK.fullmatch(x)][:2]:
        rows.append(f'<tr><td style="padding:2px 24px 14px 24px;{cell}"><a href="{esc(link)}" style="display:'
                    f'inline-block;background:#1a73e8;color:#ffffff;text-decoration:none;font-weight:600;'
                    f'font-size:14px;padding:10px 18px;border-radius:8px;">Join the Google Meet</a>'
                    f'<div style="font-size:12px;color:#8a948f;padding-top:6px;">{esc(link)}</div></td></tr>')
    for note in notes:
        rows.append(f'<tr><td style="padding:0 24px 12px 24px;{cell}font-size:13px;color:#5f6b66;">'
                    f"{esc(note)}</td></tr>")
    steps = list(steps)
    if steps:
        total = max(total, len(steps))
        shown = steps[-12:]
        more = (f'<li style="margin:0 0 3px 0;list-style:none;">… {total - len(shown)} earlier</li>'
                if total > len(shown) else "")
        rows.append(f'<tr><td style="padding:4px 24px 12px 24px;{cell}font-size:13px;color:#5f6b66;">'
                    f'<div style="font-weight:600;color:#7a857f;padding-bottom:4px;">Steps ({total})</div>'
                    f'<ul style="margin:0;padding-left:0;list-style:none;">{more}'
                    + "".join(f'<li style="margin:0 0 3px 0;">{esc(s)}</li>' for s in shown) + "</ul></td></tr>")
    if files:
        rows.append(f'<tr><td style="padding:0 24px 12px 24px;{cell}font-size:13px;color:#5f6b66;">'
                    f"📎 Attached: {esc(', '.join(Path(p).name for p in files))}</td></tr>")
    if too_big:
        rows.append(f'<tr><td style="padding:0 24px 12px 24px;{cell}font-size:13px;color:#5f6b66;">'
                    f"Too big to attach: {esc(', '.join(telegram._home(Path(p)) for p in list(too_big)[:3]))}"
                    "</td></tr>")
    if footer:
        rows.append(f'<tr><td style="padding:12px 24px 18px 24px;border-top:1px solid #eef1ef;{cell}font-size:12px;'
                    f'color:#8a948f;">{esc(footer)}</td></tr>')
    else:
        rows.append('<tr><td style="padding:0 0 8px 0;"></td></tr>')
    return ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="color-scheme" content="light only"></head>'
            '<body style="margin:0;padding:0;background:#f4f6f5;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="background:#f4f6f5;"><tr><td align="center" style="padding:20px 10px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="max-width:600px;background:#ffffff;border:1px solid #e3e8e5;border-radius:12px;">'
            + "".join(rows) + "</table></td></tr></table></body></html>")


def _message_ids(value) -> list[str]:
    return re.findall(r"<[^<>\s]+>", str(value or ""))


def make_reply(original, from_addr: str, to: str, text: str, attachments=(), html: str = "") -> EmailMessage:
    """A reply in the same thread: "Re: <subject>", In-Reply-To and References, marked as automatic (so other
    auto-responders, and Mint itself, never answer it). With `html`: multipart/alternative, the plain text
    first (attachments make it multipart/mixed around that)."""
    reply = EmailMessage()
    subject = _one_line(original.get("Subject", "")) or "Mint"
    reply["Subject"] = subject if _RE.match(subject) else f"Re: {subject}"
    reply["From"] = from_addr
    reply["To"] = to
    reply["Date"] = formatdate(localtime=True)
    reply["Message-ID"] = make_msgid(domain=_domain(from_addr) or "localhost")
    first = (_message_ids(original.get("Message-ID", "")) or [""])[0]
    if first:
        reply["In-Reply-To"] = first
        refs = _message_ids(original.get("References", "")) + [first]
        reply["References"] = " ".join(refs[-20:])
    reply["Auto-Submitted"] = "auto-replied"
    reply["X-Mint-Remote"] = "reply"
    reply.set_content(text)
    if html:
        reply.add_alternative(html, subtype="html")
    for path in attachments:
        path = Path(path)
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        main, _, sub = kind.partition("/")
        reply.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
    return reply


# --- the mailbox ----------------------------------------------------------------------------------

class MailError(Exception):
    pass


@dataclass
class Incoming:
    uid: int
    raw: bytes
    received: float                     # when the server got it (INTERNALDATE): the sender can't set it
    labels: frozenset = field(default_factory=frozenset)


class Mailbox:
    """Where requests come from and replies go: ImapMailbox (IMAP + SMTP) or AppleMailMailbox (the Mail app).
    A Mailbox may set `poll` / `fast_poll` (seconds between checks), and `keeps_headers = False` when what it
    sends loses Mint's own headers (X-Mint-Remote, the Message-ID): Mint then knows its own mail another way.
    `rich`: it sends the MIME it is given, so replies get an HTML part too."""

    keeps_headers = True
    rich = False

    def watch(self, on_new) -> None:
        """Call on_new() (any thread) as soon as new mail arrives, where the server can say so (IMAP IDLE)."""

    def fetch_new(self, cursor: dict, want) -> list[Incoming]:
        """Messages that arrived since the last call and pass want(headers). The first call only notes where
        the inbox ends: what was there before is never looked at."""
        raise NotImplementedError

    def mark_read(self, uid: int) -> None:
        raise NotImplementedError

    def send(self, message: EmailMessage, to: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


def servers(address: str, imap_host: str = "", smtp_host: str = "") -> tuple[str, str, int]:
    """(IMAP host, SMTP host, SMTP port) for an address: settings first, then the provider, else imap.<domain>."""
    known = SERVERS.get(_domain(address), (f"imap.{_domain(address)}", f"smtp.{_domain(address)}", 465))
    smtp, port = known[1], known[2]
    if smtp_host:
        smtp, _, given = smtp_host.partition(":")
        port = int(given) if given.isdigit() else 465
    return (imap_host or known[0]), smtp, port


class ImapMailbox(Mailbox):
    """IMAP + SMTP. One connection reads (fetch_new, mark_read); a second one, on its own thread, only sits in
    IDLE and calls on_new() when the server says the inbox grew (watch). SMTP connects per reply."""

    rich = True
    fast_poll = FAST_POLL

    def __init__(self, address: str, password: str, imap_host: str, smtp_host: str, smtp_port: int = 465,
                 imap_factory=None, smtp_factory=None, timeout: float = 30):
        self.address, self.password = address, password
        self.imap_host, self.smtp_host, self.smtp_port = imap_host, smtp_host, smtp_port
        self.imap_factory, self.smtp_factory, self.timeout = imap_factory, smtp_factory, timeout
        self.imap = None
        self.gmail = False              # X-GM-EXT-1: labels (\Sent) can be read
        self.idling = False             # the IDLE connection is up: new mail is told at once
        self._watcher: threading.Thread | None = None
        self._watch_imap = None
        self._closed = threading.Event()

    @property
    def poll(self) -> float:
        return IDLE_POLL if self.idling else POLL

    def _login(self):
        try:
            if self.imap_factory is not None:
                imap = self.imap_factory(self.imap_host, 993)
            else:
                imap = imaplib.IMAP4_SSL(self.imap_host, 993, ssl_context=ssl.create_default_context(),
                                         timeout=self.timeout)
            imap.login(self.address, self.password)
        except imaplib.IMAP4.error as error:
            raise MailError(f"login refused: {_clean(error)}") from error
        except OSError as error:
            raise MailError(f"can't reach {self.imap_host}: {_clean(error)}") from error
        return imap

    def _connect(self):
        if self.imap is not None:
            return self.imap
        imap = self._login()
        self.gmail = "X-GM-EXT-1" in getattr(imap, "capabilities", ())
        self.imap = imap
        return imap

    # IDLE ----------------------------------------------------------------------------------------

    def watch(self, on_new) -> None:
        """Start the IDLE thread once (a server without IDLE: nothing, the channel checks every POLL)."""
        if self._watcher is not None or self._closed.is_set():
            return
        self._watcher = threading.Thread(target=self._watch_loop, args=(on_new,), name="email-idle", daemon=True)
        self._watcher.start()

    def _watch_loop(self, on_new) -> None:
        delay = 1.0
        while not self._closed.is_set():
            imap = None
            try:
                imap = self._login()
                if "IDLE" not in getattr(imap, "capabilities", ()) or not hasattr(imap, "idle"):
                    log.info("email: %s has no IMAP IDLE - checking every %s s", self.imap_host, POLL)
                    return
                self._ok(imap.select("INBOX", True), "select")
                self._watch_imap = imap
                if self._closed.is_set():
                    return
                self.idling = True
                on_new()                        # whatever came while nobody was watching
                delay = 1.0
                while not self._closed.is_set():
                    with imap.idle(duration=IDLE_RENEW) as idler:
                        for kind, _ in idler:
                            if kind in ("EXISTS", "RECENT"):
                                on_new()
                            if self._closed.is_set():
                                break
            except Exception as error:
                if self._closed.is_set():
                    return
                log.info("email IDLE: %s - again in %.0f s", _clean(error), delay)
            finally:
                self.idling = False
                self._watch_imap = None
                if imap is not None:
                    try:
                        imap.logout()
                    except Exception:
                        pass
            self._closed.wait(delay)
            delay = min(delay * 2, 60.0)

    def _ok(self, answer, what: str):
        typ, data = answer
        if typ != "OK":
            raise MailError(f"{what}: {_clean(data)}")
        return data

    def fetch_new(self, cursor: dict, want) -> list[Incoming]:
        reused, found = self.imap is not None, []
        try:
            self._fetch_new(cursor, want, found)
        except MailError as error:
            if found:
                log.info("email: %s - the rest next time", error)
                return found                # the cursor is past these: they must be handled now
            if not reused or "login" in str(error).lower():
                raise
            # A connection left alone a while may have been dropped by the server: once more, on a new one.
            log.info("email: reconnecting (%s)", error)
            self._fetch_new(cursor, want, found)
        return found

    def _fetch_new(self, cursor: dict, want, found: list) -> None:
        imap = self._connect()
        try:
            self._ok(imap.select("INBOX"), "select")
            data = self._ok(imap.status("INBOX", "(UIDVALIDITY UIDNEXT)"), "status")
            facts = dict(re.findall(r"(UIDVALIDITY|UIDNEXT) (\d+)", _text(data)))
            validity, end = int(facts.get("UIDVALIDITY", 0)), int(facts.get("UIDNEXT", 1)) - 1
            if cursor.get("validity") != validity or "last" not in cursor:
                cursor.update(validity=validity, last=end)      # a new start: only what comes from now on
                return
            last = int(cursor["last"])
            if end <= last:
                return
            data = self._ok(imap.uid("SEARCH", "UID", f"{last + 1}:*"), "search")
            uids = sorted(u for u in (int(x) for x in _text(data).split() if x.isdigit()) if u > last)[:MAX_SCAN]
            items = "(INTERNALDATE FLAGS RFC822.SIZE" + (" X-GM-LABELS" if self.gmail else "") + " BODY.PEEK[HEADER])"
            for uid in uids:
                meta, head = _fetched(self._ok(imap.uid("FETCH", str(uid), items), "fetch"))
                headers = email.message_from_bytes(head, policy=policy.default)
                size = re.search(rb"RFC822\.SIZE (\d+)", meta)
                if not want(headers):
                    pass                    # not for Mint: not read, not touched
                elif size and int(size.group(1)) > MAX_RAW:
                    log.info("email: message %s too big to fetch", uid)
                else:
                    _, raw = _fetched(self._ok(imap.uid("FETCH", str(uid), "(BODY.PEEK[])"), "fetch"))
                    stamp = imaplib.Internaldate2tuple(meta)
                    found.append(Incoming(uid, raw, time.mktime(stamp) if stamp else time.time(), _labels(meta)))
                cursor["last"] = uid        # only once it was looked at: a dropped connection retries it
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as error:
            self._drop()
            raise MailError(_clean(error)) from error

    def mark_read(self, uid: int) -> None:
        imap = self._connect()
        try:
            self._ok(imap.uid("STORE", str(uid), "+FLAGS", "(\\Seen)"), "mark read")
        except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as error:
            self._drop()
            raise MailError(_clean(error)) from error

    def send(self, message: EmailMessage, to: str) -> None:
        """Only ever to `to`: the envelope names that one address, whatever the headers say."""
        try:
            if self.smtp_factory is not None:
                smtp = self.smtp_factory(self.smtp_host, self.smtp_port)
            elif self.smtp_port == 465:
                smtp = smtplib.SMTP_SSL(self.smtp_host, 465, context=ssl.create_default_context(),
                                        timeout=self.timeout)
            else:
                smtp = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout)
                smtp.starttls(context=ssl.create_default_context())
            try:
                smtp.login(self.address, self.password)
                smtp.send_message(message, from_addr=self.address, to_addrs=[to])
            finally:
                try:
                    smtp.quit()
                except Exception:
                    pass
        except (smtplib.SMTPException, OSError) as error:
            raise MailError(f"sending failed: {_clean(error)}") from error

    def _drop(self) -> None:
        """The reading connection only (IDLE carries on)."""
        imap, self.imap = self.imap, None
        if imap is not None:
            try:
                imap.logout()
            except Exception:
                pass

    def close(self) -> None:
        """Both connections: the IDLE thread's socket is shut so its blocking read ends now."""
        self._closed.set()
        watching = self._watch_imap
        if watching is not None:
            try:
                watching.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        self._drop()


def _clean(error) -> str:
    text = error.decode("utf-8", "replace") if isinstance(error, bytes) else str(error)
    return _one_line(text)[:200]


def _text(data) -> str:
    return " ".join(x.decode("utf-8", "replace") if isinstance(x, bytes) else str(x) for x in data or [] if x)


def _fetched(data) -> tuple[bytes, bytes]:
    """imaplib's FETCH answer -> (the items line, the literal): [(b'1 (UID 5 … BODY[HEADER] {120}', b'…'), b')']."""
    meta, body = b"", b""
    for item in data or []:
        if isinstance(item, tuple):
            meta += item[0] + b" "
            body = body or item[1]
        elif isinstance(item, bytes):
            meta += item + b" "
    return meta, body


def _labels(meta: bytes) -> frozenset:
    """Gmail's X-GM-LABELS ("\\\\Inbox" "\\\\Sent" Work) -> {'\\Inbox', '\\Sent', 'Work'}."""
    found = re.search(rb"X-GM-LABELS \(([^)]*)\)", meta)
    if not found:
        return frozenset()
    out = set()
    for quoted, bare in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', found.group(1).decode("utf-8", "replace")):
        out.add(re.sub(r"\\(.)", r"\1", quoted) if quoted else bare)
    return frozenset(out)


# --- Apple Mail (no password) ---------------------------------------------------------------------------

def as_applescript(text) -> str:
    """`text` as ONE AppleScript string literal - the only way anything from an email (or a path, an address) goes
    into a script: backslashes and quotes escaped, line breaks and tabs as \\n and \\t, other control characters
    dropped. So `" & do shell script "…` stays text inside the string."""
    out = []
    for ch in str(text or "").replace("\r\n", "\n").replace("\r", "\n"):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) >= 32 and ord(ch) != 127:
            out.append(ch)
    return '"' + "".join(out) + '"'


def osascript(script: str, timeout: float = 60) -> tuple[bool, str]:
    """(worked, output). The script goes in on stdin, not on the command line like skills._osascript: `ps` shows
    arguments to everyone, and a long reply would not fit there."""
    try:
        done = subprocess.run(["osascript"], input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    except OSError as error:
        return False, str(error)
    if done.returncode != 0:
        return False, (done.stderr or done.stdout or "").strip()
    return True, (done.stdout or "").removesuffix("\n")


def mail_running() -> bool:
    """Asks the system, never Mail (an Apple Event would open it in front)."""
    try:
        return subprocess.run(["pgrep", "-x", "Mail"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch_mail() -> None:
    """Mail, opened in the background and hidden (-g: not in front, -j: hidden)."""
    try:
        subprocess.Popen(["open", "-g", "-j", "-a", "Mail"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        log.info("opening Mail: %s", error)


OUTBOX = SUPPORT / "mail-outbox"          # attachments handed to Mail (it reads them while sending)


class AppleMailMailbox(Mailbox):
    """The Mail app, over AppleScript: the account the user added in System Settings ▸ Internet Accounts (or
    every inbox when `account` is ''). No password: Mail already signed in.

    Listing asks for unread messages whose subject contains the prefix (and read ones from the last MAIL_WINDOW:
    a message the account sent itself may arrive read) - one `whose` call, a few properties per candidate; the
    ids looked at are kept in the cursor, so each message's source is read once. That raw source (with the
    receiving server's Authentication-Results) goes through the same checks as IMAP mail.

    Self-sent mail has no "\\Sent" label here. A message whose From is the account's own address counts as
    sent by the account only when the account's Sent mailbox holds a message with the same Message-ID, the same
    subject and the same text, addressed (To/Cc) to the account itself (_sent_by_me) - a forger can't put mail in
    Sent, and copying the Message-ID of something the user once sent them doesn't match all of that. Otherwise
    it needs the trusted server's Authentication-Results like everyone else (aligned DKIM, or SPF + DMARC).

    Replies: Mail can't send MIME made elsewhere, so it is Mail's own `reply` (threaded, from the receiving
    account) with the content set, every recipient replaced by the verified sender alone (Reply-To and Cc
    dropped), the attachments added, then `send`. Mail's message has no X-Mint-Remote header and its own
    Message-ID, hence the "Mint ref:" mark (mark_of) and `keeps_headers = False`.

    Every string put into a script goes through as_applescript; numbers through int()."""

    keeps_headers = False
    poll, fast_poll = MAIL_POLL, MAIL_FAST_POLL

    def __init__(self, address: str, account: str = "", prefix: str = "", runner=None, running=None, launch=None,
                 outbox: Path | None = None, clock=time.time):
        self.address, self.account = address.strip().lower(), _one_line(account)
        self.prefix = _one_line(prefix) or PREFS["email_prefix"]
        self.runner = runner or osascript
        self.running = running or mail_running
        self.launch = launch or launch_mail
        self.outbox, self.clock = Path(outbox or OUTBOX), clock
        self.ids: dict[str, int] = {}       # a request's Message-ID -> Mail's id (to reply to that message)
        self.server: str | None = None      # the account's incoming server, once asked

    # talking to Mail ---------------------------------------------------------------------------

    def _run(self, script: str, timeout: float = 60) -> str:
        if not self.running():
            self.launch()
            raise MailError("Mail wasn't open, so Mint is opening it in the background - checking again shortly.")
        ok, out = self.runner(script, timeout)
        if not ok:
            raise MailError(self._problem(out))
        return out

    def _problem(self, out: str) -> str:
        if "-1743" in out or "Not authorized" in out or "not allowed" in out.lower():
            return ("macOS hasn't allowed Mint to control Mail - approve the prompt, or turn it on in System "
                    "Settings ▸ Privacy & Security ▸ Automation.")
        if out == "timed out":
            return "Mail took too long to answer (a big inbox, or it is still starting) - trying again shortly."
        if self.account and ("-1728" in out or "No inbox" in out) and "account" in out.lower():
            return f"Mail has no account “{self.account}” with an inbox - pick one in Settings ▸ Email control."
        return f"Mail: {_clean(out)}"

    def _box(self) -> str:
        """AppleScript lines that set `box` to the inbox watched (inside `tell application "Mail"`)."""
        if not self.account:
            return "set box to inbox"
        return (f"set acct to account {as_applescript(self.account)}\n"
                "    set names to name of every mailbox of acct\n"
                "    set box to missing value\n"
                "    repeat with i from 1 to count of names\n"
                '        if item i of names is "INBOX" then\n'
                "            set box to mailbox i of acct\n"
                "            exit repeat\n"
                "        end if\n"
                "    end repeat\n"
                '    if box is missing value then error "No inbox in that Mail account"')

    def _message(self, mail_id: int) -> str:
        return f"set m to first message of box whose id is {int(mail_id)}"

    # the Mailbox ---------------------------------------------------------------------------------

    def list_script(self) -> str:
        return f"""tell application "Mail"
    {self._box()}
    set cutoff to (current date) - {int(MAIL_WINDOW)}
    set found to (messages of box whose subject contains {as_applescript(self.prefix)} and (read status is false or date received > cutoff))
    set out to ""
    repeat with i from 1 to count of found
        if i > {int(MAX_SCAN)} then exit repeat
        try
            set m to item i of found
            set out to out & (id of m) & " " & ((current date) - (date received of m)) & " " & (message size of m) & linefeed
        end try
    end repeat
    return out
end tell"""

    @staticmethod
    def parse_list(out: str) -> list[tuple[int, float, int]]:
        """'id age size' lines -> [(Mail's id, seconds since it arrived, bytes)]; anything else is ignored."""
        rows = []
        for line in str(out or "").splitlines():
            found = re.fullmatch(r"\s*(\d+) (-?\d+(?:[.,]\d+)?) (\d+)\s*", line)
            if found:
                rows.append((int(found.group(1)), float(found.group(2).replace(",", ".")), int(found.group(3))))
        return rows

    def fetch_new(self, cursor: dict, want) -> list[Incoming]:
        rows = self.parse_list(self._run(self.list_script()))
        if not cursor.get("mail"):
            # A new start: what is there now is never looked at, only what comes from now on.
            cursor.update(mail=True, done=[mail_id for mail_id, _, _ in rows][-KEEP_IDS:], tries={})
            return []
        done, tries = cursor.setdefault("done", []), cursor.setdefault("tries", {})
        found = []
        for mail_id, age, size in rows:
            if mail_id in done:
                continue
            if size > MAX_RAW:
                log.info("email: Mail message %s too big to read", mail_id)
                done.append(mail_id)
                continue
            raw = self._source(mail_id)
            if not raw.strip():                 # not downloaded yet: again next time, three times at most
                tries[str(mail_id)] = tries.get(str(mail_id), 0) + 1
                if tries[str(mail_id)] < 3:
                    continue
            tries.pop(str(mail_id), None)
            done.append(mail_id)
            del done[:-KEEP_IDS]
            msg = email.message_from_bytes(raw, policy=policy.default)
            if not raw.strip() or not want(msg):
                continue                        # not for Mint: not marked, not touched
            mids = _message_ids(msg.get("Message-ID", ""))
            if mids:
                self.ids[mids[0]] = mail_id
                while len(self.ids) > KEEP_IDS:
                    self.ids.pop(next(iter(self.ids)))
            labels = frozenset()
            if self.address and sender(msg) == self.address and self._sent_by_me(mail_id, msg):
                labels = frozenset({"\\Sent"})
            found.append(Incoming(mail_id, raw, self.clock() - max(0.0, age), labels))
        return found

    def _source(self, mail_id: int) -> bytes:
        out = self._run(f"""tell application "Mail"
    {self._box()}
    {self._message(mail_id)}
    return source of m
end tell""")
        return out.encode("utf-8", "replace")

    def sent_script(self, mail_id: int, message_id: str) -> str:
        bare = message_id.strip().strip("<>")
        mine = (f"if name of account of mailbox of x is not {as_applescript(self.account)} then set mine to false"
                if self.account else "")
        return f"""tell application "Mail"
    {self._box()}
    {self._message(mail_id)}
    set c to content of m
    set s to subject of m
    set hits to 0
    set copies to (messages of sent mailbox whose message id is {as_applescript(bare)} or message id is {as_applescript("<" + bare + ">")})
    repeat with i from 1 to count of copies
        set x to item i of copies
        set mine to true
        {mine}
        if mine then
            set people to (address of every to recipient of x) & (address of every cc recipient of x)
            if people contains {as_applescript(self.address)} then
                considering case
                    if (subject of x) is s and (content of x) is c then set hits to hits + 1
                end considering
            end if
        end if
    end repeat
    return hits
end tell"""

    def _sent_by_me(self, mail_id: int, msg) -> bool:
        mids = _message_ids(msg.get("Message-ID", ""))
        if not mids:
            return False
        try:
            out = self._run(self.sent_script(mail_id, mids[0]))
        except MailError as error:
            log.info("email: looking in Sent: %s", error)
            return False
        return out.strip().isdigit() and int(out.strip()) > 0

    def mark_read(self, uid: int) -> None:
        self._run(f"""tell application "Mail"
    {self._box()}
    {self._message(uid)}
    set read status of m to true
end tell""", timeout=30)

    def auth_server(self) -> str:
        """Whose Authentication-Results to trust, from the Mail account's incoming server (imap.gmail.com ->
        mx.google.com); '' when unknown (every inbox, or Mail didn't say)."""
        if self.server is None and self.account:
            try:
                self.server = _one_line(self._run(f'tell application "Mail" to get server name of account '
                                                  f'{as_applescript(self.account)}', timeout=20)).lower()
            except MailError:
                return ""
        return AUTH_SERVERS.get(self.server or "", "")

    def send_script(self, message: EmailMessage, to: str, files=()) -> str:
        """Mail's reply to the request (or, when Mail no longer has it, a new message with the same subject),
        sent to `to` alone, with the text and files of `message`."""
        if not re.fullmatch(r"[^@\s<>\"\\]+@[^@\s<>\"\\]+\.[^@\s<>\"\\]+", to or ""):
            raise MailError(f"not an address: {to!r}")
        try:
            text = message.get_body(preferencelist=("plain",)).get_content()
        except Exception:
            text = ""
        text = text.rstrip() + f"\n\nMint ref: {mark_of(message.get('Message-ID', ''))}"
        subject = _one_line(message.get("Subject", "")) or "Mint"
        first = (_message_ids(message.get("In-Reply-To", "")) or [""])[0]
        new = (f"set r to make new outgoing message with properties {{visible:false, subject:{as_applescript(subject)}"
               + (f", sender:{as_applescript(self.address)}" if self.address else "") + "}")
        mail_id = self.ids.get(first)
        if mail_id is not None:
            start = (f"""try
        {self._box()}
        {self._message(mail_id)}
        set r to reply m without opening window
    on error
        {new}
    end try""")
        else:
            start = new
        attach = "".join(f"""
        tell content of r to make new attachment with properties {{file name:(POSIX file {as_applescript(str(path))})}} at after the last paragraph"""
                         for path in files)
        return f"""tell application "Mail"
    {start}
    tell r
        set content to {as_applescript(text)}
        delete every to recipient
        delete every cc recipient
        delete every bcc recipient
        make new to recipient at end of to recipients with properties {{address:{as_applescript(to)}}}
    end tell{attach}
    {"delay 1" if files else ""}
    set done to send r
    return done
end tell"""

    def send(self, message: EmailMessage, to: str) -> None:
        files = self._spill(message)
        out = self._run(self.send_script(message, to, files), timeout=120)
        if out.strip().lower() == "false":
            raise MailError("sending failed: Mail didn't send it")

    def _spill(self, message: EmailMessage) -> list[Path]:
        """The reply's attachments as files Mail can read (a folder per reply; old ones cleaned up)."""
        parts = list(message.iter_attachments()) if message.is_multipart() else []
        if not parts:
            return []
        self.outbox.mkdir(parents=True, exist_ok=True)
        for old in self.outbox.iterdir():
            try:
                if old.is_dir() and time.time() - old.stat().st_mtime > 3600:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
        folder = Path(tempfile.mkdtemp(prefix="reply-", dir=self.outbox))
        files = []
        for n, part in enumerate(parts):
            name = re.sub(r"[^\w .()+-]", "_", Path(str(part.get_filename() or "")).name).strip(" .")
            path = folder / (name[:120] or f"attachment-{n + 1}")
            if path.exists():
                path = folder / f"{n + 1}-{path.name}"
            path.write_bytes(part.get_payload(decode=True) or b"")
            files.append(path)
        return files


# --- one request by email -------------------------------------------------------------------------

class EmailRequest(telegram.Request):
    """A Telegram Request (its steps, plan and results are collected the same way) that answers by email."""

    def __init__(self, text: str, asked: str, msg, author: str, locked: bool = False, now: float | None = None):
        super().__init__(text, "email", None, locked=locked, asked=asked)
        self.msg, self.author = msg, author
        if now is not None:
            self.started = self.last_event = now
        self.replies: list[str] = []
        self.silent = False             # ended by /stop: that email's answer says so
        self.running = 0                # tools started and not yet ended
        self.last_kind = ""             # the last thing that happened: "reply", "tool_end", ...
        self.busy_at = 0.0              # when the session was last seen busy (a turn open, speaking, a tool)

    def start_tool(self, name: str, args: dict) -> None:
        self.running += 1
        super().start_tool(name, args)

    def end_tool(self, name: str, args: dict, result: str) -> None:
        self.running = max(0, self.running - 1)     # a refused tool ends without having started
        super().end_tool(name, args, result)

    def open_steps(self) -> int:
        return sum(1 for _, mark in self.plan if mark == "·")


@dataclass
class Answer:
    """What one email back says, for its plain text and its HTML."""
    head: str = ""
    words: str = ""
    notes: list = field(default_factory=list)
    links: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    total: int = 0
    files: list = field(default_factory=list)
    too_big: list = field(default_factory=list)
    footer: str = ""

    def text(self) -> str:
        parts = [p for p in (self.head, self.words) if p]
        if self.links and not any(link in self.words for link in self.links):
            parts.append("Google Meet: " + " ".join(self.links[:2]))
        parts += self.notes
        if self.steps:
            parts.append(f"What I did ({self.total} step{'s' if self.total != 1 else ''}):\n"
                         + ("…\n" if self.total > len(self.steps) else "") + "\n".join(self.steps))
        if self.files:
            parts.append("Attached: " + ", ".join(p.name for p in self.files))
        if self.too_big:
            parts.append("Too big to attach: " + ", ".join(telegram._home(p) for p in self.too_big[:3]))
        parts.append(self.footer)
        return "\n\n".join(p for p in parts if p)

    def html(self) -> str:
        return email_html(self.words, self.head, self.notes, self.links, self.steps, self.total, self.files,
                          self.too_big, self.footer)


@dataclass
class Verdict:
    what: str                           # run | skip | reject | stale | limited | no secret | guard | duplicate
    why: str = ""
    author: str = ""
    text: str = ""
    answer: bool = False                # the sender is verified and allowed: a short answer may go back


# --- the channel ----------------------------------------------------------------------------------

class Channel:
    """The poller, the mirror and the bookkeeping. One per app (`channel` below); tests make their own."""

    def __init__(self, host=None, state_path: Path = STATE, audit_path: Path = AUDIT, setting=None,
                 password=None, secret=None, mailbox=None, poll: float = POLL, clock=time.time,
                 mono=time.monotonic):
        self.host = host
        self.state_path, self.audit_path = Path(state_path), Path(audit_path)
        self._setting = setting
        self._password = password or keychain_get
        self._secret = secret or (lambda: keychain_get(SECRET_ACCOUNT))
        self._make_mailbox = mailbox or self._default_mailbox
        self.poll, self.clock, self.mono = poll, clock, mono
        self.mailbox: Mailbox | None = None
        self.box_key = ("", "")
        self.events: queue.Queue = queue.Queue()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.quit = threading.Event()
        self.request: EmailRequest | None = None
        self.pending: list[tuple] = []          # (text, msg, author, kind) waiting for the request running
        self.outgoing: queue.Queue = queue.Queue()  # (mailbox, message, to) for the email-send thread
        self.sending = False                    # that thread runs (else replies go out on the caller's thread)
        self.failures = 0                       # mail-server errors in a row: the next try waits longer
        self.questions: dict[str, tuple[str, EmailRequest]] = {}   # guard question Message-ID -> (ident, request)
        self.state_now = ("", "")
        self.running = False
        self.error = ""
        self.checked = 0.0
        self.later = _later
        self.capture = capture_screen
        self._cache: dict = {}
        self._threads: list[threading.Thread] = []
        self._state = self._load()

    # settings and state -----------------------------------------------------------------------

    def setting(self, key: str):
        if self._setting is not None:
            value = self._setting(key)
        else:
            from mint.core import prefs
            value = prefs.get(key)
        return PREFS.get(key) if value is None else value

    def address(self) -> str:
        return _one_line(self.setting("email_address")).lower()

    def chosen(self) -> str:
        """The setting: 'auto' (the default), 'imap' or 'mail'."""
        value = _one_line(self.setting("email_backend")).lower()
        return value if value in ("imap", "mail") else "auto"

    def backend(self) -> str:
        """'imap' (an app password: IDLE, seconds) or 'mail' (Apple Mail, no password). 'auto' takes IMAP whenever
        the address has an app password in the Keychain; Apple Mail is only the fallback without one."""
        chosen = self.chosen()
        if chosen != "auto":
            return chosen
        return "imap" if self._stored_password() else "mail"

    def mail_account(self) -> str:
        """The Mail account watched; '' = every inbox in Mail."""
        return _one_line(self.setting("email_mail_account"))

    def _stored_password(self) -> str:
        """The address's app password from the Keychain (read once; forget() reads it again)."""
        address = self.address()
        if self._cache.get("password_for") != address:
            self._cache.update(password_for=address, password=self._password(address) if address else "")
        return self._cache["password"]

    def password(self) -> str:
        if self.backend() != "imap":
            return ""                           # Apple Mail is signed in already: no password needed
        return self._stored_password()

    def secret(self) -> str:
        if "secret" not in self._cache:
            self._cache["secret"] = _one_line(self._secret())
        return self._cache["secret"]

    def allowed(self) -> set[str]:
        listed = {a.strip().lower() for a in re.split(r"[,;\s]+", str(self.setting("email_allowed") or ""))
                  if "@" in a}
        return listed or {self.address()}

    def trusted(self) -> str:
        given = _one_line(self.setting("email_auth_server")).lower()
        if given:
            return given
        box = self.mailbox
        if box is not None and hasattr(box, "auth_server"):
            found = box.auth_server()           # Apple Mail: from the account's incoming server
            if found:
                return found
        imap, _, _ = servers(self.address(), _one_line(self.setting("email_imap_host")))
        return AUTH_SERVERS.get(imap, "")

    def forget(self) -> None:
        """A password, address or secret word changed: read them again, reconnect."""
        self._cache.clear()
        self.wake.set()

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        with self.lock:
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.state_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._state, indent=1))
                os.chmod(tmp, 0o600)
                tmp.replace(self.state_path)
            except OSError as error:
                log.warning("saving email state: %s", error)

    def _remember(self, key: str, value: str) -> None:
        with self.lock:
            kept = self._state.setdefault(key, [])
            if value not in kept:
                kept.append(value)
                del kept[:-KEEP_IDS]

    def audit(self, kind: str, text: str = "", outcome: str = "") -> None:
        """One line per remote event in remote.log (mode 600, rolled over at 1 MB), as Telegram's."""
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}  email {kind:<10} "
                + (json.dumps(text, ensure_ascii=False) if text else "")
                + (f"  -> {outcome}" if outcome else "") + "\n")
        with self.lock:
            try:
                self.audit_path.parent.mkdir(parents=True, exist_ok=True)
                if self.audit_path.exists() and self.audit_path.stat().st_size > 1_000_000:
                    self.audit_path.replace(self.audit_path.with_suffix(".log.1"))
                with open(self.audit_path, "a") as out:
                    out.write(line)
                os.chmod(self.audit_path, 0o600)
            except OSError as error:
                log.warning("remote.log: %s", error)

    def problem(self) -> str:
        """Why the channel can't run now ('' when it can)."""
        if not self.setting("email_enabled"):
            return ""
        if not self.address():
            return ("Add your email address (the one in Mail)." if self.backend() == "mail"
                    else "Add the email address Mint should watch.")
        if self.backend() == "imap" and not self.password():
            return "Add the app password for that address."
        if str(self.setting("email_allow")) == "all" and not self.secret():
            return "Anyone may send requests only with a secret word - add one, or allow only listed addresses."
        return ""

    def status(self) -> dict:
        """For Settings: on, the address, whether the password and secret word are set, the last problem."""
        address, backend, chosen = self.address(), self.backend(), self.chosen()
        password = (False if chosen == "mail" else keychain_has(address) if self._password is keychain_get
                    else bool(self._stored_password()))
        account = self.mail_account()
        where = ((f"the “{account}” inbox in Mail" if account else "every inbox in Mail") if backend == "mail"
                 else address)
        return {"enabled": bool(self.setting("email_enabled")), "address": address, "backend": backend,
                "chosen": chosen, "idle": bool(getattr(self.mailbox, "idling", False)), "mail_account": account, "where": where, "password_set": password,
                "secret_set": bool(self.secret()), "running": self.running, "error": self.error or self.problem(),
                "checked": self.checked, "allow": str(self.setting("email_allow"))}

    # threads -------------------------------------------------------------------------------

    def start(self) -> None:
        if self._threads:
            self.wake.set()
            return
        self.sending = True
        for target, name in ((self._poll_loop, "email-poll"), (self._mirror_loop, "email-mirror"),
                             (self._send_loop, "email-send")):
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop_threads(self) -> None:
        self.quit.set()
        self.wake.set()
        for thread in self._threads:
            thread.join(timeout=10)
        self._threads = []
        self.sending = False
        self._close()

    def _close(self) -> None:
        box, self.mailbox = self.mailbox, None
        if box is not None:
            box.close()

    def _poll_loop(self) -> None:
        while not self.quit.is_set():
            self.wake.clear()                           # before looking: a wake-up meanwhile is not lost
            try:
                wait = self.poll_once()
            except Exception:
                log.exception("email poller")          # never die: try again shortly
                wait = 30
            self.wake.wait(wait)

    def _key(self) -> tuple:
        """What the mailbox is made from: another value, another mailbox."""
        return (self.backend(), self.address(), self.password(), self.mail_account(),
                _one_line(self.setting("email_prefix")))

    def _box(self) -> Mailbox | None:
        """The mailbox to send through (made again after a connection problem closed it)."""
        if self.mailbox is None and not self.problem() and self.address():
            self.mailbox, self.box_key = self._make_mailbox(self.address(), self.password()), self._key()
        return self.mailbox

    def _default_mailbox(self, address: str, password: str) -> Mailbox:
        if self.backend() == "mail":
            return AppleMailMailbox(address, self.mail_account(), _one_line(self.setting("email_prefix")))
        return self._imap_mailbox(address, password)

    def _imap_mailbox(self, address: str, password: str) -> Mailbox:
        imap, smtp, port = servers(address, _one_line(self.setting("email_imap_host")),
                                   _one_line(self.setting("email_smtp_host")))
        return ImapMailbox(address, password, imap, smtp, port)

    def poll_once(self) -> float:
        """One look at the inbox; -> seconds until the next."""
        address = self.address()
        if not self.setting("email_enabled") or self.problem():
            self._close()
            if [self._state.pop(k, None) for k in ("enabled_at", "cursor")] != [None, None]:
                self._save()
            self.running, self.error = False, self.problem()
            return 5
        where = (self.backend(), self.mail_account())
        if (self._state.get("address") != address or not self._state.get("enabled_at")
                or tuple(self._state.get("source") or ()) != where):
            # Switched on, or another address, way in or Mail account: only mail from now on counts.
            self._state.update(address=address, source=list(where), enabled_at=self.clock(), cursor={})
            self._save()
        key = self._key()
        if self.mailbox is None or self.box_key != key:
            self._close()
            self.mailbox, self.box_key = self._make_mailbox(address, self.password()), key
        if self.sending:                                # the threads run (start()): not in a one-off check
            self.mailbox.watch(self.wake.set)           # IMAP IDLE: new mail wakes the poller at once
        try:
            cursor = self._state.setdefault("cursor", {})
            items = self.mailbox.fetch_new(cursor, self.wanted)
            self._save()
        except MailError as error:
            self._trouble(str(error))
            self.failures += 1
            text = str(error).lower()
            return 120 if "login" in text else 10 if "opening it" in text else min(60, 2 ** self.failures)
        if not self.running:
            print(f"  [email: watching {self.status()['where'] if where[0] == 'mail' else address}"
                  + (" - IMAP" if where[0] == "imap" else "") + "]", flush=True)
        self.running, self.error, self.checked, self.failures = True, "", self.clock(), 0
        for item in items:
            try:
                self.handle(item)
            except Exception:
                log.exception("email message failed")
        if self.questions:
            return getattr(self.mailbox, "fast_poll", FAST_POLL)
        return getattr(self.mailbox, "poll", self.poll)

    def _trouble(self, message: str) -> None:
        if "login" in message.lower():
            message = ("The mail server refused the address or app password - check them in Settings ▸ Accounts "
                       f"& keys ▸ Email control ({message}).")
        if message != self.error:
            print(f"  [email: {message}]", flush=True)
        self.running, self.error = False, message
        self._close()

    # incoming mail ---------------------------------------------------------------------------

    def wanted(self, headers) -> bool:
        """From the headers alone: could this be for Mint? Everything else is never fetched or touched."""
        what, _, _ = subject_parts(str(headers.get("Subject", "")), str(self.setting("email_prefix")))
        return what == "ok" or bool(self._question_of(headers))

    def _question_of(self, msg) -> str:
        """The guard question `msg` answers ('' none): by In-Reply-To / References, or - for mail sent through
        Apple Mail, whose Message-ID Mint never learns - by the question's "Mint ref:" mark quoted in it."""
        if not self.questions:
            return ""
        refs = _message_ids(msg.get("In-Reply-To", "")) + _message_ids(msg.get("References", ""))
        found = next((m for m in refs if m in self.questions), "")
        if found:
            return found
        marks = {mark_of(m): m for m in self.questions}
        return next((marks[k] for k in _MARK.findall(_all_text(msg)) if k in marks), "")

    def _own(self, msg, mid: str) -> bool:
        """One Mint sent (it lands in the inbox when the user emails themselves)."""
        if msg.get("X-Mint-Remote") or mid in self._state.get("sent", []):
            return True
        if getattr(self.mailbox, "keeps_headers", True):
            return False
        # Sent through Apple Mail: its own words carry the mark of a message Mint sent, or it answers a message
        # Mint already answered (Mail's reply is In-Reply-To that request; the user's reply is to Mint's reply).
        sent = {mark_of(m) for m in self._state.get("sent", [])}
        if set(_MARK.findall(_unquoted(body_text(msg)))) & sent:
            return True
        first = (_message_ids(msg.get("In-Reply-To", "")) or [""])[0]
        return bool(first) and first in self._state.get("replied", [])

    def inspect(self, msg, item: Incoming) -> Verdict:
        """Every check, in order; the first that fails decides."""
        mid, where = self._ids(msg, item)
        with self.lock:
            if self._own(msg, mid):
                return Verdict("skip", "Mint's own reply")
            if mid in self._state.get("seen", []) or where in self._state.get("seen_at", []):
                return Verdict("skip", "seen before")
        if automatic(msg):
            return Verdict("skip", "automatic or mailing-list message")
        guard_reply = bool(self._question_of(msg))
        subject = str(msg.get("Subject", ""))
        what, rest, reply = subject_parts(subject, str(self.setting("email_prefix")), self.secret())
        if guard_reply:
            # An answer to the guard: the prefix may have been edited away, never the secret word.
            what = "no secret" if self.secret() and self.secret().lower() not in subject.lower() else "ok"
        if what == "no prefix":
            return Verdict("skip", "no prefix")
        author = sender(msg)
        if not author:
            return Verdict("reject", "not exactly one From address")
        if str(self.setting("email_allow")) != "all" and author not in self.allowed():
            return Verdict("reject", "sender not allowed", author)
        ok, how = authenticated(msg, author, self.trusted(), self.address(), item.labels)
        if not ok:
            return Verdict("reject", f"From not verified: {how}", author)
        listed = str(self.setting("email_allow")) != "all"
        if what == "no secret":
            return Verdict("no secret", "no secret word in the subject", author, answer=listed)
        if self.clock() - item.received > MAX_AGE or item.received < float(self._state.get("enabled_at", 0)) - 60:
            return Verdict("stale", f"{int(self.clock() - item.received)} s old", author, answer=True)
        body = strip_quoted(body_text(msg))
        if guard_reply:
            return Verdict("guard", how, author, body, answer=True)
        text = request_text(rest, body, reply, str(self.setting("email_prefix")), self.secret())
        key = _norm(text)
        with self.lock:
            now = self.clock()
            recent = [r for r in self._state.get("recent", []) if 0 <= now - float(r[2]) < DUP_WINDOW]
            self._state["recent"] = recent
            twin = next((r for r in recent if key and r[0] == author and r[1] == key), None)
            if twin is not None:
                return Verdict("duplicate", f"the same request {int(now - float(twin[2]))} s earlier", author, text)
            hour = [t for t in self._state.get("times", []) if now - t < 3600]
            self._state["times"] = hour
            if len(hour) >= MAX_PER_HOUR:
                return Verdict("limited", f"{len(hour)} in the last hour", author, text, answer=True)
            hour.append(now)
            if key:
                recent.append([author, key, now])
        return Verdict("run", how, author, text, answer=True)

    def _ids(self, msg, item: Incoming) -> tuple[str, str]:
        """(its Message-ID - or where it is when it has none, where it is: the IMAP UID or Mail's id)."""
        where = f"{self.backend()}:{self._state.get('cursor', {}).get('validity', '')}:{item.uid}"
        ids = _message_ids(msg.get("Message-ID", ""))
        return (ids[0] if ids else f"uid:{where}"), where

    def handle(self, item: Incoming) -> None:
        msg = email.message_from_bytes(item.raw, policy=policy.default)
        verdict = self.inspect(msg, item)
        mid, where = self._ids(msg, item)
        subject = _one_line(msg.get("Subject", ""))[:80]
        if verdict.what == "skip":
            if verdict.why not in ("Mint's own reply", "seen before", "no prefix"):
                self.audit("skipped", subject, verdict.why)
            return
        self._remember("seen", mid)
        self._remember("seen_at", where)
        self._save()
        if verdict.what == "reject":
            # Left unread, never answered: an answer would tell a forger the address is watched.
            self.audit("rejected", f"{verdict.author or '?'}: {subject}", verdict.why)
            print(f"  [email: ignored a message ({verdict.why})]", flush=True)
            return
        try:
            self.mailbox.mark_read(item.uid)
        except Exception as error:
            log.info("email mark read: %s", error)
        if verdict.what == "duplicate":
            # One request, one answer: the first copy's answer covers it.
            self.audit("duplicate", verdict.text, f"not run again ({verdict.why})")
            return
        if verdict.what == "no secret":
            self.audit("rejected", f"{verdict.author}: {subject}", verdict.why)
            if verdict.answer:
                self.reply(msg, verdict.author, "Not run: the subject needs your secret word (Settings ▸ Accounts "
                                                "& keys ▸ Email control).")
            return
        if verdict.what == "stale":
            self.audit("stale", subject, f"not run ({verdict.why})")
            self.reply(msg, verdict.author, "This came while the Mac was asleep or offline "
                                            f"({telegram._took(self.clock() - item.received)} ago), so I didn't run "
                                            "it. Send it again if you still want it.")
            return
        if verdict.what == "limited":
            self.audit("limited", subject, verdict.why)
            with self.lock:
                told = float(self._state.get("limited_told", 0))
                if self.clock() - told > 3600:
                    self._state["limited_told"] = self.clock()
                    self._save()
                    told = 0
            if not told:
                self.reply(msg, verdict.author, f"Not run: that's more than {MAX_PER_HOUR} requests in an hour. "
                                                "Try again a little later.")
            return
        if verdict.what == "guard":
            self._guard_reply(msg, verdict)
            return
        self.audit("verified", verdict.author, verdict.why)
        found = command(verdict.text)
        if found:
            self._command(found[0], found[1], msg, verdict.author)
        elif verdict.text.strip():
            self.run(verdict.text.strip(), msg, verdict.author)
        else:
            self.reply(msg, verdict.author, "Write what you'd like after the prefix in the subject, or in the "
                                            "message. /help lists the commands.")

    def _command(self, word: str, rest: str, msg, author: str) -> None:
        self.audit("command", word)
        if word == "help":
            self.reply(msg, author, HELP)
        elif word == "status":
            self.reply(msg, author, self.describe())
        elif word == "stop":
            self.stop(msg, author)
        elif word in ("screenshot", "screen", "shot"):
            self.later(self.screenshot, msg, author)
        elif word == "meet":
            self.later(self.meet, rest, msg, author)
        elif word == "briefing":
            self.run(telegram.BRIEF, msg, author, kind="command")
        elif word == "missed":
            self.run(telegram.MISSED, msg, author, kind="command")

    def _guard_reply(self, msg, verdict: Verdict) -> None:
        """A reply to a guard question: YES or NO on its first line answers it."""
        from mint.core import guard
        mid = self._question_of(msg)
        ident, request = self.questions.get(mid, ("", None))
        first = (verdict.text.strip().splitlines() or [""])[0]
        yes = None if not first else False if guard.NO.search(first) else True if guard.YES.search(first) else None
        if yes is None:
            self.reply(msg, verdict.author, "Reply YES to allow it or NO to stop it.")
            return
        done = guard.answer(ident, yes, "email")
        self.audit("guard", f"{'yes' if yes else 'no'}", "answered" if done else "too late (question closed)")
        if not done:
            self.reply(msg, verdict.author, "That question was already closed, so nothing changed.")

    # what the commands do ----------------------------------------------------------------------

    def _host(self):
        if self.host is None:
            self.host = telegram.SessionHost()
        return self.host

    def run(self, text: str, msg, author: str, kind: str = "text") -> None:
        """Into the running session as if typed; the mirror collects what happens until it ends. One at a time:
        while another emailed request runs, this one waits its turn (never cuts the other's answer short)."""
        host = self._host()
        if not host.ready():
            self.reply(msg, author, "Mint isn't running right now, so I couldn't do that.")
            self.audit(kind, text, "Mint not running")
            return
        locked = bool(telegram.screen_locked())
        with self.lock:
            running = self.request
            if running is None:
                request = self.request = EmailRequest(text, text, msg, author, locked=locked, now=self.mono())
            elif len(self.pending) < MAX_QUEUE:
                self.pending.append((text, msg, author, kind))
                waiting = len(self.pending)
            else:
                waiting = -1
        if running is None:
            self._inject(request, kind)
        elif waiting < 0:
            self.audit(kind, text, "not run: too many waiting")
            self.reply(msg, author, f"Not run: {MAX_QUEUE} emailed requests are already waiting. Send it again "
                                    "once I've answered those.")
        else:
            self.audit(kind, text, f"waiting for “{telegram._short(running.asked, 60)}” ({waiting} in line)")
            print(f"  [email: request from {author} waits for the one running]", flush=True)

    def _inject(self, request: EmailRequest, kind: str = "text") -> None:
        self.audit(kind, request.text, "sent to Mint" + (" (screen locked)" if request.locked else ""))
        print(f"  [email: request from {request.author}]", flush=True)
        try:
            self._host().inject(request.text, asked=request.asked)
        except Exception as error:
            log.info("email inject: %s", error)
            self.events.put(("_end", "failed", request))
            self.audit(kind, request.text, f"failed: {str(error)[:120]}")

    def _next(self) -> None:
        """The request before is answered: the next emailed one in line goes to Mint."""
        while True:
            with self.lock:
                if self.request is not None or not self.pending:
                    return
                text, msg, author, kind = self.pending.pop(0)
            if not self._host().ready():
                self.reply(msg, author, "Mint isn't running right now, so I couldn't do that.")
                self.audit(kind, text, "Mint not running")
                continue
            locked = bool(telegram.screen_locked())
            with self.lock:
                if self.request is not None:             # one came in meanwhile (can't, but never two at once)
                    self.pending.insert(0, (text, msg, author, kind))
                    return
                request = self.request = EmailRequest(text, text, msg, author, locked=locked, now=self.mono())
            self._inject(request, kind)
            return

    def stop(self, msg, author: str) -> None:
        host = self._host()
        request = self.request
        if host is None or not host.ready():
            self.reply(msg, author, "Mint isn't running right now.")
            return
        try:
            host.stop("email")
        except Exception as error:
            self.reply(msg, author, f"Could not stop Mint: {str(error)[:120]}")
            return
        with self.lock:
            dropped, self.pending = self.pending, []
        if request is not None:
            request.silent = True
            self.events.put(("_end", "stopped", request))
        for _, old, who, _ in dropped:
            self.reply(old, who, "⏹ Not run: /stop came before I got to it.")
        self.reply(msg, author, "⏹ Stopped." + (f" (It was working on “{telegram._short(request.asked, 80)}”.)"
                                                if request is not None else "")
                   + (f" {len(dropped)} waiting request{'s' if len(dropped) != 1 else ''} dropped."
                      if dropped else ""))

    def describe(self) -> str:
        host = self._host()
        facts = host.doing() if host is not None else {"running": False}
        if not facts.get("running"):
            lines = ["Mint isn't running."]
        else:
            mood = ("paused (microphone off)" if facts.get("paused") else "asleep" if facts.get("asleep")
                    else "awake")
            lines = [f"Mint is {mood}" + ("" if facts.get("connected") else ", reconnecting") + "."]
            request = self.request
            state, note = self.state_now
            if request is not None:
                doing = request.current or (request.steps[-1][2:] if request.steps else "thinking")
                lines.append(f"Working on your email “{telegram._short(request.asked, 80)}” for "
                             f"{telegram._took(self.mono() - request.started)} - {len(request.steps)} steps, "
                             f"now: {doing}")
                if self.pending:
                    lines.append(f"{len(self.pending)} more emailed request{'s' if len(self.pending) != 1 else ''} "
                                 "waiting.")
            elif facts.get("busy") or state in ("working", "thinking"):
                lines.append("Busy: " + (note or state))
            else:
                lines.append("Not doing anything right now.")
            if facts.get("task"):
                lines.append(f"Task: {facts['task']}")
        locked = telegram.screen_locked()
        lines.append(telegram.LOCKED_NOTE if locked else "Screen: unlocked." if locked is False else "Screen: unknown.")
        lines.append("Email control: on" + (" · read-only" if self.setting("email_read_only") else ""))
        lines.append(f"(as of {time.strftime('%H:%M:%S')})")
        return "\n".join(lines)

    def screenshot(self, msg, author: str) -> None:
        folder = Path(tempfile.mkdtemp(prefix="mint-email-"))
        path = folder / f"Screen {time.strftime('%Y-%m-%d at %H.%M.%S')}.jpg"
        try:
            why = self.capture(path)
            if why:
                self.reply(msg, author, f"Could not take a screenshot: {why}")
                self.audit("screenshot", "", "failed")
                return
            app = telegram._frontmost_app()
            words = (f"Your screen at {time.strftime('%H:%M')}" + (f", {app} in front" if app else "") + "."
                     + ("\n" + telegram.LOCKED_NOTE if telegram.screen_locked() else ""))
            self.reply(msg, author, words, [path])
            self.audit("screenshot", "", "sent")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def meet(self, rest: str, msg, author: str) -> None:
        """/meet, /meet end, /meet status: the call's link comes back by email."""
        from mint.app import meet_call
        word = rest.strip().lower()
        if word in ("end", "stop", "leave", "off"):
            self.reply(msg, author, meet_call.end())
            return
        call = meet_call.current()
        if word in ("status", "?") or (call is not None and not call.over.is_set()):
            if call is None:
                self.reply(msg, author, "No call going on. /meet starts one.")
            else:
                self.reply(msg, author, call.describe() + (f"\nJoin: {call.link}" if call.link else ""))
            return
        said = meet_call.start(asked_from="email")
        call = meet_call.current()
        deadline = time.monotonic() + 60
        while call is not None and not call.link and not call.over.is_set() and time.monotonic() < deadline:
            time.sleep(1)                       # still joining: wait a little for the link
        link = call.link if call is not None else ""
        if link and link not in said:
            said += f"\nJoin: {link}"
        self.audit("meet", "", "link sent" if link else "no link")
        self.reply(msg, author, said)

    def gate(self, name: str, args: dict) -> str:
        """'' to go ahead; a refusal for a send, delete or purchase while a read-only emailed request runs."""
        if self.request is None or not self.setting("email_read_only"):
            return ""
        why = telegram._read_only_block(name, args or {})
        if not why:
            return ""
        self.audit("blocked", f"{name}: {why}", "read-only")
        return (f"NOT DONE (read-only by email): {why}. This request came by email and email control is read-only, "
                "so nothing is sent, deleted or bought. Tell the user in one sentence; they can do it at the Mac, or "
                "turn read-only off in Settings ▸ Accounts & connections ▸ Email control.")

    # guard questions by email ---------------------------------------------------------------------

    def guard_ask(self, p) -> None:
        """The guard's question about an emailed request, in its thread; a reply with YES or NO answers it."""
        request = self.request
        if request is None:
            return
        lines = "\n".join(f"    {line}" for line in p.danger.lines[:8])
        text = (f"{p.who} wants to {p.danger.title}" + (f":\n{lines}" if lines else ".") + "\n\n"
                f"Reply YES to allow it or NO to stop it. Nothing happens unless you say yes (within "
                f"{int(p.timeout // 60)} min).")
        mid = self.reply(request.msg, request.author, text)
        if mid:
            self.questions[mid] = (p.ident, request)
            self.wake.set()                     # look for the answer more often
            self.audit("guard ask", p.danger.title)

    def guard_done(self, p) -> None:
        for mid, (ident, _) in list(self.questions.items()):
            if ident == p.ident:
                self.questions.pop(mid, None)

    # events from the session -----------------------------------------------------------------

    def event(self, kind: str, data: dict) -> None:
        """Any thread; cheap. Collected on the mirror thread, in order."""
        if kind == "state":
            self.state_now = (str(data.get("name", "")), str(data.get("note", "")))
            return
        request = self.request
        if request is not None:
            self.events.put((kind, data, request))

    def _mirror_loop(self) -> None:
        while not self.quit.is_set():
            self.drain(0.25)

    def drain(self, wait: float = 0.0) -> None:
        """Apply what has come in (waiting up to `wait` for the first), then check the request's clocks."""
        while True:
            try:
                kind, data, request = self.events.get(timeout=wait) if wait else self.events.get_nowait()
            except queue.Empty:
                break
            wait = 0.0
            try:
                self._apply(kind, data, request)
            except Exception:
                log.exception("email mirror: %s", kind)
        try:
            self._tick()
        except Exception:
            log.exception("email mirror tick")

    def _apply(self, kind: str, data, request: EmailRequest | None) -> None:
        if kind == "_end":
            self.finish(request, data)
            return
        if request is None or request.finished:
            return
        now = self.mono()
        request.last_event = now
        request.last_kind = kind
        if kind == "request":
            text = " ".join(str(data.get("text", "")).split())
            if not request.confirmed and text == " ".join(request.text.split()):
                request.confirmed = True
            else:
                self.finish(request, "local")        # typed at the Mac (or sent from Telegram) meanwhile
        elif kind == "tool_start":
            request.start_tool(str(data.get("name", "")), dict(data.get("args") or {}))
        elif kind == "tool_end":
            request.end_tool(str(data.get("name", "")), dict(data.get("args") or {}), str(data.get("result", "")))
        elif kind == "reply":
            text = str(data.get("text", "")).strip()
            if text:
                request.last_reply = now
                request.replies.append(text)
        elif kind == "done":
            # The session's own "finished": its turn ended, no tool running, nothing new for 2 s.
            if not request.running:
                self.finish(request, "done")
        elif kind == "stop":
            self.finish(request, "stopped")

    def _tick(self) -> None:
        request = self.request
        if request is None or request.finished:
            return
        now = self.mono()
        if any(asked is request for _, asked in list(self.questions.values())):
            request.last_event = now            # waiting for the user's YES or NO: not over
            return
        how = self._over(request, now)
        if how:
            self.finish(request, how)

    def _over(self, request: EmailRequest, now: float) -> str:
        """How the request ended ('' = not yet). Never before Mint's last words: while a tool runs or the session
        is busy (a turn open, its answer still being spoken, a tool batch) it waits, up to WORK_CAP."""
        if now - request.started > telegram.LONGEST:
            return "working"
        busy = _session_busy(self.host, speech=bool(request.open_steps()))
        if busy:
            request.busy_at = now
        if request.running or busy:
            return "working" if now - request.last_event > WORK_CAP else ""
        quiet = now - max(request.last_event, request.busy_at)
        if request.last_kind == "reply":
            return "done" if quiet >= (PLAN_SETTLE if request.open_steps() else REPLY_SETTLE) else ""
        if request.last_kind in ("tool_start", "tool_end"):
            return "quiet" if quiet >= AFTER_TOOL else ""     # its answer to the tool's result is coming
        return "quiet" if quiet >= SILENT_WAIT else ""

    def finish(self, request: EmailRequest | None, how: str) -> None:
        """The request is over: one email back with what Mint said, did and made."""
        if request is None or request.finished:
            return
        with self.lock:
            if self.request is request:
                self.request = None
        request.finished = how
        for mid, (_, asked) in list(self.questions.items()):
            if asked is request:
                self.questions.pop(mid, None)
        try:
            if not request.silent:
                answer = self.answer(request, how)
                self.reply(request.msg, request.author, answer.text(), answer.files, html=answer.html())
                self.audit("answered", request.asked, f"{how}, {telegram._took(self.mono() - request.started)}, "
                           f"{len(request.steps)} steps, {len(answer.files)} files")
        finally:
            self._next()

    def compose(self, request: EmailRequest, how: str) -> tuple[str, list[Path]]:
        answer = self.answer(request, how)
        return answer.text(), answer.files

    def answer(self, request: EmailRequest, how: str) -> Answer:
        """The email back: Mint's own words (never nothing), how it ended, the steps, files and Meet link."""
        took = telegram._took(self.mono() - request.started)
        head = {"stopped": "⏹ Stopped.", "local": "↪ Another request (typed at the Mac, or from Telegram) took over, "
                "so I stopped following this one.", "next": "↪ Replaced by your next email.",
                "failed": "✗ Could not reach Mint, so nothing was done.",
                "working": f"⏳ Still working on it after {took} - here is what I have so far."}.get(how, "")
        said: list[str] = []
        for text in request.replies:
            if not said or said[-1] != text:
                said.append(text)
        words = "\n\n".join(said)
        if not words and how in ("done", "quiet", "working", "stopped", "local"):
            if request.steps:
                words = (f"Mint didn't say anything about it, but took {len(request.steps)} "
                         f"step{'s' if len(request.steps) != 1 else ''} (below).")
            elif how in ("done", "quiet"):
                words = ("Mint finished without an answer or a step - it may not have understood. Try saying it "
                         "another way, or send /status.")
        if not words and not head:
            words = "Nothing was done."
        links: list[str] = []
        for text in request.results + request.replies:
            links += [link for link in _MEET_LINK.findall(text) if link not in links]
        notes = []
        open_steps = request.open_steps()
        if open_steps and how in ("done", "quiet", "working"):
            notes.append(f"{open_steps} of {len(request.plan)} plan steps not done yet.")
        if request.locked:
            notes.append(telegram.LOCKED_NOTE)
        files, too_big = (self._files(request) if how in ("done", "quiet", "stopped", "working") else ([], []))
        return Answer(head=head, words=words, notes=notes, links=links, steps=request.steps[-25:],
                      total=len(request.steps), files=files, too_big=too_big,
                      footer=f"— Mint, on your Mac · “{telegram._short(request.asked, 80)}” · {took}")

    def _files(self, request: EmailRequest) -> tuple[list[Path], list[Path]]:
        """Files Mint made (or screenshots it took) during this request, under home and not secret, within
        the size limit of one email."""
        found: list[Path] = []
        try:
            from mint.tools.harness import MADE
            found += [Path(p) for p in json.loads(MADE.read_text())]
        except Exception:
            pass
        for result in request.results:
            found += telegram._paths(result)
        chosen, too_big, seen, total = [], [], set(), 0
        for path in found:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                if not path.is_file() or path.stat().st_mtime < request.wall - 2 or not _may_attach(path):
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            if total + size > MAX_ATTACH or len(chosen) >= MAX_FILES:
                too_big.append(path)
                continue
            chosen.append(path)
            total += size
        return chosen, too_big

    # sending -------------------------------------------------------------------------------

    def reply(self, original, to: str, text: str, attachments=(), html: str = "") -> str:
        """A threaded reply to `to` - always the verified sender of `original`. -> its Message-ID ('' if not made).
        Through IMAP/SMTP it carries an HTML part too (`html`, else `text` laid out). It is sent on the email-send
        thread when the channel runs (never holding up reading or the mirror), else here."""
        box = self._box()
        if box is None or not to or not str(text or "").strip():
            return ""
        try:
            rich = (html or email_html(text)) if getattr(box, "rich", False) else ""
            message = make_reply(original, self.address(), to, text, attachments, html=rich)
        except Exception as error:
            log.info("email reply: %s", error)
            self.audit("send failed", to, _clean(error))
            return ""
        mid = str(message["Message-ID"])
        self._remember("sent", mid)             # before it goes: it may land in the inbox at once
        first = (_message_ids(original.get("Message-ID", "")) or [""])[0]
        if first:
            self._remember("replied", first)    # Apple Mail: its reply to this is Mint's own (see _own)
        self._save()
        if self.sending:
            self.outgoing.put((box, message, to))
        else:
            self._deliver(box, message, to)
        return mid

    def _send_loop(self) -> None:
        while not self.quit.is_set():
            try:
                box, message, to = self.outgoing.get(timeout=0.5)
            except queue.Empty:
                continue
            self._deliver(box, message, to)

    def _deliver(self, box: Mailbox, message: EmailMessage, to: str) -> bool:
        """Send, and once more a few seconds later when the server didn't take it."""
        for attempt in (1, 2):
            try:
                box.send(message, to)
                return True
            except Exception as error:
                log.info("email send (try %d): %s", attempt, error)
                if attempt == 2 or "not an address" in str(error) or self.quit.wait(3):
                    self.audit("send failed", to, _clean(error))
                    return False
        return False


def _session_busy(host, speech: bool = False) -> bool:
    """The running session is still at it: a turn open (the model answering), a tool batch - and with `speech`
    (plan steps are open, so the autopilot may carry on once Mint stops talking) its answer still being spoken.
    Otherwise the email doesn't wait for the voice: the words are all there. False when it can't be asked."""
    mint = getattr(host, "mint", None) if host is not None else None
    if mint is None:
        return False
    try:
        task = getattr(mint, "_tool_task", None)
        audio = getattr(mint, "audio", None)
        return bool(getattr(mint, "_turn_open", False) or getattr(mint, "_busy", False)
                    or (task is not None and not task.done())
                    or (speech and audio is not None and getattr(audio, "playing", False)))
    except Exception:
        return False


def _may_attach(path: Path) -> bool:
    """Only the user's own files under home, never secrets or app data (as Telegram's _may_send)."""
    try:
        real = path.expanduser().resolve()
        if Path.home() not in real.parents:
            return False
        from mint.tools.harness import _blocked
        return not _blocked(real, write=True)
    except Exception:
        return False


def capture_screen(path: Path) -> str:
    """The whole screen as a JPEG at `path`; '' when it worked, else why not."""
    try:
        done = subprocess.run(["screencapture", "-x", "-m", "-t", "jpg", str(path)], capture_output=True, text=True,
                              timeout=20)
    except Exception as error:
        return str(error)[:120]
    if done.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        return (done.stderr.strip()[:120] or "nothing was captured") + " - Mint may need Screen Recording permission."
    return ""


def _later(fn, *args) -> None:
    threading.Thread(target=fn, args=args, name="email-work", daemon=True).start()


# --- what the rest of Mint calls ------------------------------------------------------------------

channel = Channel()


def start(mint=None) -> None:
    """Once, when the session starts (from telegram.start). Idle until it is switched on with an address (and,
    reading with an app password instead of Apple Mail, that password); changes in Settings take effect within
    seconds."""
    if mint is not None:
        channel.host = telegram.SessionHost(mint)
    channel.start()
    try:
        from mint.core import prefs
        prefs.on_change(lambda key, _value: refresh() if key.startswith("email_") else None)
    except Exception:
        pass


def refresh() -> None:
    channel.forget()


def on_event(kind: str, data: dict | None = None) -> None:
    """The session's events (through telegram.on_event). Never raises and never blocks."""
    try:
        channel.event(kind, data or {})
    except Exception:
        log.debug("email event %s", kind, exc_info=True)


def gate(name: str, args: dict) -> str:
    try:
        return channel.gate(name, args)
    except Exception:
        log.debug("email gate", exc_info=True)
        return ""


def from_email() -> bool:
    """An emailed request is running (the guard asks by email too)."""
    return channel.request is not None


def guard_ask(p) -> None:
    try:
        channel.guard_ask(p)
    except Exception:
        log.exception("email guard ask")


def guard_done(p) -> None:
    try:
        channel.guard_done(p)
    except Exception:
        log.debug("email guard done", exc_info=True)


def status() -> dict:
    return channel.status()
