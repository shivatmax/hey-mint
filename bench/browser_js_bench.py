"""Checks the page JavaScript the `browser` tool sends, in a real (headless) Chrome.

The `browser` tool runs these scripts in the user's browser through AppleScript
when "Allow JavaScript from Apple Events" is on. This bench runs the very same
wrapped scripts in a separate, windowless Chrome with a throwaway profile, over
the DevTools protocol - no window, no focus change, the user's Chrome untouched.

    .venv/bin/python bench/browser_js_bench.py
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from mint import harness_tools as h  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGE = """<!doctype html><html><head><title>JS bench form</title></head><body>
<main><h1>JS bench</h1><p>Some article text that is long enough to count as the main content of the page.
</p>""" + "<p>filler paragraph for the main area.</p>" * 20 + """
<a href="https://example.com/a">Example A</a> <a href="/rel">Relative link</a>
<label for="n">Your name</label> <input id="n" placeholder="Full name">
<label for="p">Password</label> <input id="p" type="password">
<label for="c">Favourite colour</label> <select id="c"><option>Red</option><option>Green</option><option>Blue</option></select>
<div contenteditable="true" aria-label="Notes box"></div>
<button onclick="document.getElementById('out').textContent='Previewed: '+document.getElementById('n').value+' / '+document.getElementById('c').value">Preview</button>
<button onclick="document.getElementById('out').textContent='Sent!'">Send message</button>
<p id="out">Nothing yet</p></main></body></html>"""

results: list[bool] = []


def case(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail[:160]}" if not ok else ""))


async def main() -> int:
    folder = pathlib.Path(tempfile.mkdtemp(prefix="mint-js-"))
    (folder / "form.html").write_text(PAGE)
    port = 9333
    chrome = subprocess.Popen([CHROME, "--headless=new", f"--remote-debugging-port={port}",
                               f"--user-data-dir={folder / 'profile'}", "--no-first-run",
                               "--no-default-browser-check", f"file://{folder / 'form.html'}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = next(p for p in pages if p.get("type") == "page")
                break
            except Exception:
                time.sleep(0.2)
        else:
            print("headless Chrome did not start")
            return 1
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=None) as ws:
            counter = 0

            async def run(code: str) -> str:
                nonlocal counter
                counter += 1
                await ws.send(json.dumps({"id": counter, "method": "Runtime.evaluate",
                                          "params": {"expression": h._wrap_js(code), "returnByValue": True,
                                                     "userGesture": True}}))
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get("id") == counter:
                        return str(reply["result"]["result"].get("value"))

            await asyncio.sleep(0.5)
            text = await run(h._READ_JS.replace("MAX", "4000"))
            case("read: title, address and main text", text.startswith("JS bench form") and "article text" in text, text)
            links = await run(h._LINKS_JS)
            case("links: text -> absolute URL", "Example A -> https://example.com/a" in links
                 and "Relative link -> file://" in links, links)
            def fill(want, value, kind="fill"):
                return run(h._FILL_JS.replace("WANT", json.dumps(want)).replace("VALUE", json.dumps(value))
                           .replace("KIND", json.dumps(kind)))

            out = await fill("your name", "Mint Tester")
            case("fill a text field by its label", out.startswith("FILLED"), out)
            out = await fill("password", "x")
            case("fill refuses a password field", out == "PASSWORD", out)
            out = await fill("color", "Blue", "select")
            case("select by a differently spelt label (color/colour)", out.startswith("FILLED"), out)
            out = await fill("favourite colour", "Purple", "select")
            case("missing option lists the options", out.startswith("NOOPTION") and "Green" in out, out)
            out = await fill("notes box", "hello")
            case("fill a contenteditable box", out.startswith("FILLED"), out)
            await run("document.getElementById('c').focus(); return 'ok'")
            out = await fill("", "x")
            case("empty target with a dropdown focused asks which field", out.startswith("WHICH"), out)
            out = await fill("colour", "x")
            case("fill never picks a dropdown", not out.startswith("FILLED") or "colour" not in out.lower(), out)
            click = h._CLICK_JS.replace("WANT", json.dumps("Preview"))
            out = await run(click.replace("RISKY_CHECK", "true"))
            case("click a button by its text", out.startswith("CLICKED button"), out)
            shown = await run("return document.getElementById('out').textContent")
            case("the click and fills took effect", shown == "Previewed: Mint Tester / Blue", shown)
            out = await run(h._CLICK_JS.replace("WANT", json.dumps("Send message")).replace("RISKY_CHECK", "true"))
            case("a Send button is reported as risky, not clicked", out.startswith("RISKY"), out)
            shown = await run("return document.getElementById('out').textContent")
            case("...and nothing was sent", shown != "Sent!", shown)
            out = await run(h._CLICK_JS.replace("WANT", json.dumps("nonexistent thing")).replace("RISKY_CHECK", "true"))
            case("click on something missing", out == "NOTFOUND", out)
            out = await run("return (document.title);")
            case("plain js expression", out == "JS bench form", out)
            out = await run("return undefinedThing.x;")
            case("js errors come back as text", out.startswith("JS error"), out)
            case("js guard blocks cookies and fetch", bool(h._JS_BLOCK.search("document.cookie"))
                 and bool(h._JS_BLOCK.search("fetch('https://x')")) and not h._JS_BLOCK.search("document.title"))
    finally:
        chrome.terminate()
        try:
            chrome.wait(5)
        except subprocess.TimeoutExpired:
            chrome.kill()
        shutil.rmtree(folder, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
