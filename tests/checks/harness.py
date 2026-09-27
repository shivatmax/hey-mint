"""Offline checks for the harness tools (mint/tools/harness.py) that need no screen.

Files work in a temporary folder under ~/Documents/Mint; web search and fetch use
the network; AppleScript runs one harmless script. Screen tools (browser, menu,
scroll_to, wait_for_text) are checked through the app: see README "Harness".

    .venv/bin/python tests/checks/harness.py
"""

from __future__ import annotations

import pathlib
import os
import shutil
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from mint.tools import harness as h
from mint.app import live  # noqa: E402

results: list[tuple[bool, str]] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail[:140]}" if detail and not ok else ""))


def main() -> int:
    work = h.MINT_FILES / f"bench-{int(time.time())}"
    work.mkdir(parents=True, exist_ok=True)
    h.BACKUPS = work / "backups"              # keep the real backups and made-files list untouched
    h.MADE = work / "made_files.json"
    try:
        print("paths")
        for path, bad in (("~/.ssh/id_rsa", True), ("~/Library/Keychains/login.keychain-db", True),
                          ("/etc/passwd", True), ("~/project/.env", True), ("~/Documents/notes.md", False),
                          ("~/Library/Application Support/Google/Chrome/Default/Cookies", True)):
            why = h._blocked(pathlib.Path(pathlib.Path(path).expanduser()))
            case(f"blocked({path}) = {bad}", bool(why) == bad, why)
        case("no writing into hidden folders", bool(h._blocked(pathlib.Path.home() / ".zshrc", write=True)))
        case("no writing into Library", bool(h._blocked(pathlib.Path.home() / "Library/LaunchAgents/x.plist", write=True)))
        case("writing into iCloud Drive is fine",
             not h._blocked(pathlib.Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/x.md", write=True))

        print("files")
        target = str(work / "plan.md")
        r = h.write_file({"path": target, "content": "# Plan\n- one\n- two\n"})
        case("create", r.startswith("Created"), r)
        r = h.write_file({"path": target, "content": "again"})
        case("create refuses an existing file", r.startswith("FAILED") and "already exists" in r, r)
        r = h.write_file({"path": target, "content": "- three\n", "mode": "append"})
        case("append", r.startswith("Appended"), r)
        r = h.write_file({"path": target, "content": "- TWO", "mode": "replace", "find": "- two"})
        case("replace once, with a backup", r.startswith("Edited") and "previous version" in r, r)
        r = h.write_file({"path": target, "content": "x", "mode": "replace", "find": "- "})
        case("replace refuses an ambiguous find", "occurs" in r, r)
        r = h.read_file({"path": target})
        case("read back", "- TWO" in r and "- three" in r and "4 lines" in r, r)
        r = h.write_file({"path": str(work / "keys.txt"), "content": "api key: sk-abcdefghijklmnopqrstuvwx"})
        case("refuses secrets", r.startswith("REFUSED"), r)
        big = "\n".join(f"line {i}" for i in range(1, 3001))
        h.write_file({"path": str(work / "big.txt"), "content": big})
        r = h.read_file({"path": str(work / "big.txt"), "max_chars": 1000})
        case("long file comes in parts", "more lines: read_file start_line=" in r, r[-120:])
        r2 = h.read_file({"path": str(work / "big.txt"), "start_line": 2990})
        case("continue from start_line", "line 3000" in r2 and "more lines" not in r2, r2[-80:])
        r = h.read_file({"path": str(pathlib.Path.home() / ".ssh/config")})
        case("read refuses ~/.ssh", r.startswith("FAILED") and "private" in r, r)
        html = work / "page.html"
        html.write_text("<html><body><h1>Hello</h1><p>World of Mint</p></body></html>")
        r = h.read_file({"path": str(html)})
        case("html through textutil", "World of Mint" in r, r)
        r = h.read_file({"path": str(work)})
        case("a folder lists it", "plan.md" in r and "big.txt" in r, r)

        print("find and act")
        r = h.find_files({"folder": str(work), "query": ""})
        case("list folder", "plan.md" in r, r)
        r = h.find_files({"folder": str(work), "query": "plan", "content": False})
        case("find by name in a folder", "plan.md" in r, r)
        r = h.find_files({})
        case("recent files: Mint-made first", r.startswith("Made by Mint recently") and "plan.md" in r, r[:160])
        r = h.file_action({"action": "copy", "path": target, "to": str(work / "copy")})
        case("copy into a new folder", r.startswith("Copied") and (work / "copy" / "plan.md").exists(), r)
        r = h.file_action({"action": "rename", "path": str(work / "copy" / "plan.md"), "to": "plan-v2.md"})
        case("rename", (work / "copy" / "plan-v2.md").exists(), r)
        r = h.file_action({"action": "copy", "path": target, "to": str(work / "copy" / "plan-v2.md")})
        case("never overwrites", "already exists" in r, r)
        r = h.file_action({"action": "info", "path": target})
        case("info", "modified" in r, r)
        live.typed("open my plan")
        r = h.file_action({"action": "trash", "path": str(work / "copy" / "plan-v2.md")})
        case("trash refused when not asked", r.startswith("REFUSED"), r)
        live.typed("delete the plan copy")
        r = h.file_action({"action": "trash", "path": str(work / "copy" / "plan-v2.md")})
        case("trash when asked (recoverable)", "Trash" in r and not (work / "copy" / "plan-v2.md").exists(), r)

        print("guards")
        for text, request, want in (("Send", "type hi, do not send it", "send"), ("Send", "reply and send it", ""),
                                    ("Delete", "delete the draft", ""), ("Quit Safari", "open safari", "quit"),
                                    ("Submit", "fill it in without submitting", "submit"),
                                    ("Export as PDF…", "export as pdf", "")):
            got = h._risky(text, request)
            case(f"risky({text!r} | {request!r}) = {want!r}", got == want, got)
        h._calls.clear()
        h._outcomes.clear()
        warn = [h.check("ui_act", {"action": "click", "target": "Save"}, "Clicked Save, nothing changed")
                for _ in range(3)]
        case("loop warning on the 3rd identical call", not warn[0] and not warn[1] and "[Loop warning]" in warn[2])
        case("scrolling repeatedly is not a loop",
             not any(h.check("scroll", {"direction": "down"}, "Scrolled down 3 times") for _ in range(4)))
        h._outcomes.clear()
        fails = [h.check(f"tool{i}", {}, "FAILED: nope") for i in range(4)]
        case("warning after 4 failures in a row", "[Loop warning]" in fails[-1])
        h._names.clear()
        flood = [h.check("find_files", {"query": f"try {i}"}, f"{i} found") for i in range(6)]
        case("warning after 6 calls in a row to one tool", not any(flood[:5]) and "in a row" in flood[5])
        h._names.clear()
        tricks = [h.check("move_orb", {"trick": t}, f"Doing a {t} trick") for t in
                  ("loop", "hops", "figure8", "bounce", "spin", "bee", "chase", "peek")]
        case("doing a list of things (8 orb tricks) is not a loop", not any(tricks), str(tricks))

        print("browser helpers")
        case("url: bare host gets https", h._url("example.com") == "https://example.com")
        case("url: localhost gets http", h._url("localhost:8000/x") == "http://localhost:8000/x")
        tabs = [{"window": 1, "tab": 1, "active": True, "title": "Inbox (3) - Gmail", "url": "https://mail.google.com/"},
                {"window": 1, "tab": 2, "active": False, "title": "Example Domain", "url": "https://example.com/"},
                {"window": 2, "tab": 1, "active": True, "title": "Pull requests", "url": "https://github.com/pulls"}]
        case("tab by title", h._match_tab(tabs, "gmail")["tab"] == 1)
        case("tab by address", h._match_tab(tabs, "github.com")["window"] == 2)
        case("tab by number", h._match_tab(tabs, "2")["title"] == "Example Domain")
        case("no tab matches", h._match_tab(tabs, "jira") is None)
        links = h._links_from_html('<a href="/a">One</a> <a class=x href="https://b.org/">Two <b>2</b></a>'
                                   '<a href="javascript:void(0)">no</a>', "https://site.com/page")
        case("links from Safari source", "One -> https://site.com/a" in links and "Two 2 -> https://b.org/" in links
             and "javascript" not in links, links)
        case("close tab needs asking", h._risky("close", "switch to the gmail tab") == "close"
             and h._risky("close", "close the example tab") == "")

        print("clipboard (the user's clipboard is put back afterwards)")
        from mint.tools import clipboard as c
        from mint.tools.everyday import BOARD_LOCK
        BOARD_LOCK.acquire()        # this thread reads the pasteboard directly too: keep the watcher out
        board = c._board()
        saved = [(str(t), board.dataForType_(t)) for t in (board.types() or [])]
        try:
            r = c.clipboard({"action": "copy", "text": "hello from the bench"})
            case("copy text", r.startswith("Copied 20"), r)
            r = c.clipboard({"action": "get"})
            case("get text", "hello from the bench" in r, r)
            r = c.clipboard({"action": "copy", "text": "my api key is sk-abcdefghijklmnopqrstuvwxyz123"})
            case("a secret is copied but never kept in history",
                 not any("sk-abc" in (x.get("text") or "") for x in c.HISTORY), str(c.HISTORY[:2]))
            r = c.clipboard({"action": "copy_path", "path": target})
            case("copy a file's path", r.endswith(str(pathlib.Path(target))) and board.stringForType_("public.utf8-plain-text") == str(pathlib.Path(target)), r)
            r = c.clipboard({"action": "copy_file", "path": target})
            case("copy a file (Finder can paste it)", "as a file" in r and c._files(board) == [str(pathlib.Path(target))], r)
            r = c.clipboard({"action": "get"})
            case("get shows the file", "1 file" in r and "plan.md" in r, r)
            from PIL import Image
            pic = work / "pic.png"
            Image.new("RGB", (300, 200), (30, 140, 90)).save(pic)
            r = c.clipboard({"action": "copy_image", "path": str(pic)})
            case("copy an image file as a picture", r.startswith("Copied the picture"), r)
            r = c.clipboard({"action": "get"})
            case("get shows the image and its size", "an image (300x200" in r, r)
            r = c.clipboard({"action": "history"})
            case("history lists recent copies", "hello from the bench" in r and "[image]" in r and "sk-abc" not in r, r)
            number = next(i for i, x in enumerate(c.HISTORY, 1) if x.get("text") == "hello from the bench")
            r = c.clipboard({"action": "restore", "index": number})
            case("restore an earlier copy", board.stringForType_("public.utf8-plain-text") == "hello from the bench", r)
            r = c.clipboard({"action": "clear"})
            case("clear", not (board.types() or []), r)
            big = work / "shot.png"
            Image.new("RGB", (1000, 400), (200, 50, 50)).save(big)
            c._resize(big, "1280x720")
            case("resize to an exact size (cover + crop)", Image.open(big).size == (1280, 720))
            c._resize(big, "640")
            case("resize to a width keeps the shape", Image.open(big).size == (640, 360))
            case("bad size is explained", "not understood" in c._resize(big, "huge"))
        finally:
            board.clearContents()
            for kind, data in saved:
                if data is not None:
                    board.setData_forType_(data, kind)
            BOARD_LOCK.release()

        print("quit")
        for request, want in (("quit mint", True), ("turn yourself off", True), ("exit", True),
                              ("go to sleep", False), ("quit spotify", False), ("shut down the mac", False)):
            case(f"quit guard: {request!r} -> {want}", h._asks_to_quit_mint(request) == want)
        from mint.app import power
        case("start at login reflects the login item", power.starts_at_login() == power.AGENT.exists())

        print("applescript")
        live.typed("what's two plus two")
        r = h.run_applescript({"script": "return 2 + 2"})
        case("runs a script", r.strip() == "4", r)
        r = h.run_applescript({"script": 'set t to 0\nrepeat with i from 1 to 10\n  set t to t + i\nend repeat\nreturn "sum " & t'})
        case("multi-line script", r == "sum 55", r)
        r = h.run_applescript({"script": "return (1 +"})
        case("syntax error comes back", r.startswith("FAILED") and "syntax error" in r, r)
        r = h.run_applescript({"script": 'tell application "System Events" to return name of first application process whose frontmost is true'})
        case("System Events: front app", bool(r) and not r.startswith(("FAILED", "REFUSED")), r)
        for bad in ('set x to "do shell " & "script"\nrun script x', 'use framework "Foundation"\nreturn 1',
                    'tell application "Finder" to close front window'):
            r = h.run_applescript({"script": bad})
            case(f"refuses {bad.splitlines()[0][:34]!r}", r.startswith("REFUSED"), r)
        r = h.run_applescript({"script": 'do shell script "ls"'})
        case("refuses do shell script", r.startswith("REFUSED"), r)
        r = h.run_applescript({"script": 'tell application "Mail" to send (make new outgoing message)'})
        case("refuses send when not asked", r.startswith("REFUSED"), r)

        print("browser choice")
        from mint.tools import browser_choice as bc
        bc.RULES = work / "browser_rules.json"
        memory_rule = bc.rule_from_text("The user's Brave browser is the default for entertainment and specific "
                                        "sites like YouTube, anime streaming, and music services unless an app is "
                                        "specified.")
        case("a remembered preference becomes a rule", bool(memory_rule) and memory_rule["browser"] == "Brave Browser"
             and {"entertainment", "youtube", "anime"} <= set(memory_rule["topics"]), str(memory_rule))
        case("a fact without a browser is not a rule",
             bc.rule_from_text("Always open applications in full screen when asked to open something.") is None)
        case("one service named covers only it",
             bc.rule_from_text("Always use Brave for Netflix")["words"] == ["netflix"])
        case("'instead of' picks the wanted browser",
             bc.rule_from_text("Use Brave instead of Chrome for netflix.com")["browser"] == "Brave Browser")
        saved_memory = bc._memory_rules
        bc._memory_rules = lambda: [dict(memory_rule, **{"from": "memory m12"})]
        try:
            for request, want in (("open anime website on this Chrome only", "com.google.Chrome"),
                                  ("open Chrome, open YouTube and anime", "com.google.Chrome"),
                                  ("open my brief browser", "com.brave.Browser"),
                                  ("in brief, open github", "")):
                case(f"named browser: {request!r}", bc.named_in(request) == want, bc.named_in(request))
            case("YouTube follows the rule", bc.choose("https://www.youtube.com", "", "open YouTube")[0] == "com.brave.Browser")
            case("an anime site by its address", bc.choose("https://hianimez.org/home", "Google Chrome", "watch something")[0]
                 == "com.brave.Browser")
            case("the user's words beat the rule",
                 bc.choose("https://www.youtube.com", "", "open youtube in chrome")[0] == "com.google.Chrome")
            case("no rule for github: not the rule's pick",
                 not bc.choose("https://github.com", "", "open github")[1].startswith("your rule"))
            case("opening Chrome for YouTube is stopped", bc.conflicts("Google Chrome", "open YouTube") == "Brave Browser")
            case("...unless the user named Chrome", bc.conflicts("Google Chrome", "open Chrome and YouTube") == "")
            case("opening Brave for YouTube is fine", bc.conflicts("Brave Browser", "open YouTube") == "")
            r = bc.set_rule("replit.com, work", "Chrome")
            case("set a rule", r.startswith("Rule saved") and bc.rule_for("https://replit.com/x", "")["browser"] == "Google Chrome", r)
            case("rules are listed", "replit.com" in bc.describe_rules() and "memory m12" in bc.describe_rules())
            r = bc.remove_rule("replit.com")
            case("remove a rule", r.startswith("Removed 1") and bc.rule_for("https://replit.com/x", "") is None, r)
            case("unknown browser refused", bc.set_rule("news", "Netscape").startswith("FAILED"))
        finally:
            bc._memory_rules = saved_memory

        print("web")
        started = time.monotonic()
        r = h.web_search({"query": "python 3.14 release date"})
        case(f"web_search ({time.monotonic() - started:.1f}s)", "python.org" in r.lower(), r[:200])
        started = time.monotonic()
        r = h.read_url({"url": "example.com"})
        case(f"read_url ({time.monotonic() - started:.1f}s)", "Example Domain" in r, r[:200])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    passed = sum(ok for ok, _ in results)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    # The clipboard watcher thread touches NSPasteboard; letting the interpreter tear it down
    # crashed Python at exit (a macOS crash dialog). Leave at once instead.
    os._exit(code)
