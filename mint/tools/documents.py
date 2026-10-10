"""Reading what is on screen as text, and producing documents.

These make multi-step work possible: read a Gmail inbox or any web page as
text (not a picture), let Gemini reason over it, then write the result
somewhere - a Notion page, an email draft, a PDF.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from mint.tools import fastinput

def _output() -> Path:
    """Where PDFs go: the Documents folder of Mint's storage (Settings > Storage)."""
    from mint.core import config
    return config.storage("Documents")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Roles whose text is content rather than chrome around it.
_TEXT_ROLES = {"AXStaticText", "AXHeading", "AXLink", "AXTextField", "AXTextArea",
               "AXCell", "AXButton", "AXMenuButton", "AXListItem"}


def _ax(element, attribute):
    import ApplicationServices as AX
    err, value = AX.AXUIElementCopyAttributeValue(element, attribute, None)
    return value if err == 0 else None


def _ocr_front_window() -> str:
    """The front window's visible text, top to bottom, via on-device recognition."""
    try:
        import AppKit

        from mint.screen import ocr
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        try:
            # A picture of that window alone: no other window's words, and not Mint's own notch or cursor tag.
            inside, _, _ = ocr.read_window(app)
        except Exception:
            items, _ = ocr.read_screen()
            front = ocr._front_window()
            inside = [i for i in items if front is None or ocr._inside(i, front)]
        inside.sort(key=lambda i: (round(i["y"] / 8), i["x"]))
        return "\n".join(i["text"] for i in inside)
    except Exception:
        return ""


def _find_web_area(window, limit: int = 400):
    """The page inside a browser window (AXWebArea), if there is one."""
    stack, seen = [window], 0
    while stack and seen < limit:
        node = stack.pop()
        seen += 1
        if _ax(node, "AXRole") == "AXWebArea":
            return node
        stack.extend(_ax(node, "AXChildren") or [])
    return None


TERMINALS = {"com.apple.Terminal", "com.googlecode.iterm2", "com.mitchellh.ghostty", "dev.warp.Warp-Stable",
             "com.github.wez.wezterm", "net.kovidgoyal.kitty", "org.alacritty", "co.zeit.hyper", "com.raphaelamorim.rio"}


def terminal_tail(text: str, max_chars: int = 12000) -> str:
    """The newest part of a terminal's text: its last lines (line breaks kept, blank runs squeezed), cut at a line
    start. A terminal's text is its whole scrollback - on 5 Oct 1.1 million characters, read from the top, so the
    output of the command Mint had just run never reached it."""
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    out, size = [], 0
    for line in reversed(lines):
        if size + len(line) + 1 > max_chars:
            break
        if line or (out and out[-1]):
            out.append(line)
            size += len(line) + 1
    return "\n".join(reversed(out))


def _terminal_text(window) -> str:
    """A terminal window's text area value (the scrollback), or ''."""
    stack, seen = [window], 0
    while stack and seen < 200:
        node = stack.pop()
        seen += 1
        if _ax(node, "AXRole") == "AXTextArea":
            value = _ax(node, "AXValue")
            if isinstance(value, str) and value.strip():
                return value
        stack.extend(_ax(node, "AXChildren") or [])
    return ""


def read_window(max_chars: int = 12000, time_limit: float = 4.0, with_links: bool = False) -> str:
    """All the text in the front window, in reading order, through Accessibility.

    Unlike a screenshot this includes text scrolled out of view, and it is
    exact - an email subject is read, not recognised from pixels.
    """
    import AppKit
    import ApplicationServices as AX

    if not fastinput.has_accessibility():
        return "FAILED: Mint lacks Accessibility permission, so it cannot read windows."
    front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return "FAILED: no app is in front."
    app = AX.AXUIElementCreateApplication(front.processIdentifier())
    # Chromium and Electron build their accessibility tree only when asked.
    AX.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    AX.AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", True)
    window = _ax(app, "AXFocusedWindow") or _ax(app, "AXMainWindow")
    if window is None:
        return f"FAILED: {front.localizedName()} has no window open."
    # A just-opened web app is often still loading: in testing Notion read as an
    # "Untitled" page and the model filled the gap with a guess. Wait for the
    # page to say it has loaded, and for a real title.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        title = _ax(window, "AXTitle") or ""
        area = _find_web_area(window)
        loaded = area is None or _ax(area, "AXLoaded") is not False
        if loaded and title and not title.startswith(("Untitled", "New Tab", "Loading")):
            break
        time.sleep(0.4)
        window = _ax(app, "AXFocusedWindow") or window
    title = _ax(window, "AXTitle") or ""

    if (front.bundleIdentifier() or "") in TERMINALS:
        scrollback = _terminal_text(window)
        if scrollback:
            tail = terminal_tail(scrollback, max_chars)
            print(f"  [read_window: terminal, last {tail.count(chr(10)) + 1} lines of {len(scrollback)} chars "
                  f"from '{title[:60]}']", flush=True)
            more = "the newest part; older output is above it" if len(tail) < len(scrollback.strip()) else "all of it"
            return f"{front.localizedName()} - {title}\n(terminal - {more}; the last line is the prompt)\n\n{tail}"

    lines, seen, total = [], set(), 0
    stack = [window]
    deadline = time.monotonic() + time_limit
    visited = 0
    while stack and total < max_chars and time.monotonic() < deadline:
        node = stack.pop()
        visited += 1
        role = _ax(node, "AXRole") or ""
        if role in _TEXT_ROLES:
            text = ""
            for attribute in ("AXValue", "AXTitle", "AXDescription"):
                value = _ax(node, attribute)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
            text = " ".join(text.split())
            if with_links and role == "AXLink" and text:
                # Where a link goes, e.g. ChatGPT's "NASA Science" source pills,
                # so work handed on keeps its sources.
                url = _ax(node, "AXURL")
                url = str(url.absoluteString()) if hasattr(url, "absoluteString") else str(url or "")
                if url.startswith("http") and url not in text:
                    text = f"{text} <{url[:160]}>"
            # Skip repeats (a link's text is often also its child's text).
            if len(text) > 1 and text not in seen:
                seen.add(text)
                lines.append(text)
                total += len(text) + 1
        children = _ax(node, "AXChildren") or []
        stack.extend(reversed(list(children)))

    body = "\n".join(lines)
    truncated = total >= max_chars or time.monotonic() >= deadline
    print(f"  [read_window: {len(lines)} lines, {total} chars from '{title[:60]}']", flush=True)
    if total < 300:
        # Apps like Slack expose almost nothing to Accessibility (19 controls,
        # no messages). Fall back to reading the pixels on the Mac itself.
        visible = _ocr_front_window()
        if visible and len(visible) > total:
            print(f"  [read_window: fell back to on-screen text, {len(visible)} chars]", flush=True)
            return (f"{front.localizedName()} - {title}\n(read from the screen: only the visible part)\n\n"
                    f"{visible[:max_chars]}")
    if not body.strip():
        return (f"FAILED: {front.localizedName()} shows no readable text through Accessibility. "
                "Try look instead.")
    return (f"{front.localizedName()} - {title}\n\n{body[:max_chars]}"
            + ("\n\n(truncated)" if truncated else ""))


def export_doc_pdf(open_after: bool = True) -> str:
    """Download the open Google Doc as a PDF - the document itself, as Docs renders it.

    Uses Docs' own export address (…/export?format=pdf) in the same Chrome
    profile, so it is exact and needs no menu clicking. The file lands in
    Downloads; it is moved to Mint's Documents folder and opened.
    """
    from mint.tools import workspace

    url = workspace.front_tab_url()
    if "docs.google.com/document/d/" not in url:
        found = workspace.find_tab("docs.google.com")
        if found is None:
            return "FAILED: no Google Doc is open to export."
        workspace.focus(*found)
        url = workspace.front_tab_url()
    match = re.search(r"/document/d/([A-Za-z0-9_-]+)", url)
    if not match:
        return f"FAILED: the front tab is not a Google Doc ({url[:80]})."
    doc_id = match.group(1)

    downloads = Path.home() / "Downloads"
    started = time.time()
    subprocess.run(["open", "-a", "Google Chrome",
                    f"https://docs.google.com/document/d/{doc_id}/export?format=pdf"], check=False)
    # Wait for the finished file (Chrome writes *.crdownload until it is done).
    found_file, deadline = None, time.monotonic() + 30
    while time.monotonic() < deadline and found_file is None:
        time.sleep(0.5)
        fresh = [p for p in downloads.glob("*.pdf") if p.stat().st_mtime >= started - 1]
        if fresh:
            found_file = max(fresh, key=lambda p: p.stat().st_mtime)
    if found_file is None:
        return ("FAILED: the export started but no PDF arrived in Downloads within 30 seconds. "
                "Chrome may be asking where to save it.")
    output = _output()
    target = output / found_file.name
    if target.exists():
        target = output / f"{found_file.stem}-{time.strftime('%H%M%S')}.pdf"
    shutil.move(str(found_file), target)
    if open_after:
        subprocess.run(["open", str(target)], check=False)
    return f"Exported the Google Doc as a PDF: {target}" + (" (opened)." if open_after else ".")


def _to_html(title: str, content: str) -> str:
    """Light Markdown (#..######, -, **bold**, blank-line paragraphs) to a styled page."""
    out, in_list = [], False
    for raw in content.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith(("- ", "* ", "• ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line.lstrip()[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            level = "h2" if len(heading.group(1)) <= 2 else "h3"
            out.append(f"<{level}>{_inline(heading.group(2))}</{level}>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    stamp = time.strftime("%-d %B %Y, %-I:%M %p")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
    @page {{ margin: 22mm 20mm; }}
    body {{ font: 11pt/1.55 -apple-system, "Helvetica Neue", sans-serif; color: #1d1d1f; }}
    h1 {{ font-size: 22pt; margin: 0 0 4pt; letter-spacing: -0.3pt; }}
    .meta {{ color: #86868b; font-size: 9.5pt; margin-bottom: 18pt; }}
    h2 {{ font-size: 13.5pt; margin: 18pt 0 6pt; border-bottom: 1px solid #e5e5ea; padding-bottom: 3pt; }}
    h3 {{ font-size: 11.5pt; margin: 12pt 0 3pt; }}
    ul {{ padding-left: 16pt; margin: 4pt 0; }} li {{ margin: 2pt 0; }} p {{ margin: 5pt 0; }}
    </style></head><body><h1>{html.escape(title)}</h1>
    <div class="meta">Prepared by Mint · {stamp}</div>{''.join(out)}</body></html>"""


def _inline(text: str) -> str:
    text = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def create_pdf(title: str, content: str, open_after: bool = True, save_to: str = "") -> str:
    """Write a formatted PDF where the user asked (else Mint's Documents folder) and optionally open it."""
    if not Path(CHROME).exists():
        return "FAILED: Google Chrome is needed to render PDFs and was not found."
    from mint.tools import saveto
    name = re.sub(r"\.pdf$", "", re.sub(r"[^\w .()-]", "", title).strip(), flags=re.I)[:60] or "document"
    target, moved = saveto.destination(_output() / f"{name}.pdf", ".pdf", save_to)
    if target.parent == _output():
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")[:60] or "document"
        target = _output() / f"{slug}-{time.strftime('%Y%m%d-%H%M')}.pdf"

    work = Path(tempfile.mkdtemp(prefix="mint-pdf-"))
    try:
        page = work / "page.html"
        page.write_text(_to_html(title, content))
        # A separate profile so this never touches the user's running Chrome.
        process = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--user-data-dir={work / 'profile'}", f"--print-to-pdf={target}", page.as_uri()],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Headless Chrome writes the file and then often does not exit (seen in
        # testing), so wait for the file to settle rather than for the process.
        deadline, last = time.monotonic() + 30, -1
        while time.monotonic() < deadline:
            time.sleep(0.3)
            size = target.stat().st_size if target.exists() else -1
            if size > 0 and size == last:
                break
            last = size
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not target.exists() or target.stat().st_size == 0:
        return "FAILED: the PDF was not produced."
    if open_after:
        subprocess.run(["open", str(target)], check=False)
    return f"Saved the PDF to {target}" + (" and opened it." if open_after else ".") + (f" {moved}" if moved else "")
