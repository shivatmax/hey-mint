"""Build the guide: guide/src/*.html -> guide/*.html, plus assets/search-index.js.

Each source page starts with a JSON comment: <!--{"title": ..., "nav": ..., "lede": ..., "hello": ...}-->
Shortcuts inside pages:
  {{clip NAME | caption}}   a looping video from clips/NAME.mp4 (poster clips/NAME.jpg)
  {{img FILE | caption}}    a screenshot from shots/FILE
  {{orb}}                   the orb's face markup (eyes, mouth, cheeks, brows, shades, hands)
  {{voices}} {{tools}}      the 30 voices and the 88-tool table, from the code and GUIDE.md
"""
import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

SITE = "https://hey-mint.pages.dev/"          # where Cloudflare Pages serves the guide (clean URLs: /agents)
REPO = "https://github.com/shivatmax/hey-mint"
NAME = "Hey Mint"
PAGES = ["index", "talking", "orb", "doing", "showing", "personality", "agents", "learning", "voice",
         "settings", "reference"]

ORB = ('<span class="face"><span class="eyes"><span class="eye"></span><span class="eye"></span></span>'
       '<span class="mouth"></span><span class="cheek l"></span><span class="cheek r"></span>'
       '<span class="brows"></span><span class="shades"></span></span>'
       '<span class="hand l"></span><span class="hand r"></span>')


def voices():
    from mint.extra_tools import VOICES
    from mint.voices import GENDER
    return "".join(f"<span>{v} <i>{html.escape(GENDER.get(v, ''))} · {html.escape(d)}</i></span>" for v, d in VOICES.items())


def tools():
    rows = []
    for line in (ROOT / "GUIDE.md").read_text().splitlines():
        m = re.match(r"\| `(\w+)` \| ([^|]*) \| (.*) \|$", line)
        if m:
            name, args, what = m.groups()
            rows.append(f"<tr><td><code>{name}</code></td><td>{html.escape(args.strip())}</td>"
                        f"<td>{html.escape(what.strip()).replace('`', '')}</td></tr>")
    return "\n".join(rows)


def clip(m):
    name, cap = m.group(1).strip(), m.group(2).strip()
    assert (HERE / "clips" / f"{name}.mp4").exists(), f"missing clip {name}"
    return (f'<figure><video src="clips/{name}.mp4" poster="clips/{name}.jpg" preload="none" muted loop playsinline '
            f'aria-label="{html.escape(re.sub("<[^>]+>", "", cap))}"></video><figcaption>{cap}</figcaption></figure>')


def img(m):
    name, cap = m.group(1).strip(), m.group(2).strip()
    assert (HERE / "shots" / name).exists(), f"missing shot {name}"
    return (f'<figure><img src="shots/{name}" alt="{html.escape(re.sub("<[^>]+>", "", cap))}" loading="lazy">'
            f'<figcaption>{cap}</figcaption></figure>')


def expand(text):
    text = re.sub(r"\{\{clip ([^|}]+)\|(.*?)\}\}", clip, text, flags=re.S)
    text = re.sub(r"\{\{img ([^|}]+)\|(.*?)\}\}", img, text, flags=re.S)
    return text.replace("{{orb}}", ORB).replace("{{voices}}", voices()).replace("{{tools}}", tools())


HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{full_title}</title><meta name="description" content="{desc}">
<link rel="canonical" href="{url}"><meta name="theme-color" content="#2EC4B6">
<link rel="icon" href="assets/favicon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="assets/og.png">
<meta property="og:type" content="website"><meta property="og:site_name" content="Hey Mint">
<meta property="og:title" content="{full_title}"><meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}"><meta property="og:image" content="{site}assets/og.png">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{full_title}">
<meta name="twitter:description" content="{desc}"><meta name="twitter:image" content="{site}assets/og.png">
{jsonld}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="assets/guide.css"></head>
<body data-hello="{hello}">
<header class="top"><div class="top-in"><a class="brand" href="index.html"><span class="dot"></span>Hey Mint</a>
<nav class="pages" aria-label="Guide pages">{pages}</nav></div></header>
"""

TAIL = """<footer>Hey Mint · an open-source voice assistant for macOS · <a href="https://github.com/shivatmax/hey-mint">GitHub</a> · GPL-3.0 · every clip here is the real Mint, recorded on a blank desktop.</footer>
<div class="bubble" id="bubble" role="status" aria-live="polite"></div>
<div class="buddy" aria-hidden="true"><div class="body"><button class="orb" tabindex="-1" aria-label="The page's Mint. Poke it.">{orb}</button></div></div>
<dialog class="lb" id="lb"><img alt=""><p></p></dialog>
<script src="assets/search-index.js"></script><script src="assets/guide.js"></script>
</body></html>"""


def main():
    metas, bodies = {}, {}
    for name in PAGES:
        raw = (HERE / "src" / f"{name}.html").read_text()
        m = re.match(r"\s*<!--(\{.*?\})-->", raw, re.S)
        metas[name] = json.loads(m.group(1))
        bodies[name] = expand(raw[m.end():])
    index = []
    for i, name in enumerate(PAGES):
        meta, body = metas[name], bodies[name]
        nav = "".join(f'<a href="{p}.html"{" class=on" if p == name else ""}>{html.escape(metas[p]["nav"])}</a>'
                      for p in PAGES if p != "index")
        desc = re.sub("<[^>]+>", "", meta.get("description") or meta.get("lede", ""))
        full = meta.get("full_title") or f'{meta["title"]} · Hey Mint, the open-source voice assistant for macOS'
        url = SITE if name == "index" else f"{SITE}{name}"
        jsonld = ""
        if name == "index":
            jsonld = ('<script type="application/ld+json">' + json.dumps({
                "@context": "https://schema.org", "@type": "SoftwareApplication", "name": NAME,
                "alternateName": ["Mint", "Hey Mint assistant"], "operatingSystem": "macOS",
                "applicationCategory": "UtilitiesApplication", "description": desc, "url": SITE,
                "image": SITE + "assets/og.png", "license": "https://www.gnu.org/licenses/gpl-3.0.html",
                "codeRepository": REPO, "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
                "keywords": "voice assistant, macOS, Jarvis, AI agent, Gemini Live, open source, desktop automation"},
                ensure_ascii=False) + "</script>")
        page = HEAD.format(title=html.escape(meta["title"]), full_title=html.escape(full), desc=html.escape(desc),
                           url=url, site=SITE, jsonld=jsonld, hello=html.escape(meta.get("hello", "")), pages=nav)
        sections = re.findall(r'<section id="([\w-]+)"[^>]*>.*?<h2>(.*?)</h2>', body, re.S)
        if name == "index":
            page += body
        else:
            toc = "".join(f'<a href="#{sid}">{re.sub("<[^>]+>", "", title)}</a>' for sid, title in sections)
            prev_p, next_p = PAGES[i - 1], PAGES[i + 1] if i + 1 < len(PAGES) else None
            nxt = '<div class="next">'
            nxt += f'<a href="{prev_p}.html"><span>Previous</span><b>{html.escape(metas[prev_p]["nav"])}</b></a>'
            if next_p:
                nxt += f'<a href="{next_p}.html"><span>Next</span><b>{html.escape(metas[next_p]["nav"])}</b></a>'
            nxt += '</div>'
            page += (f'<div class="page-head"><div class="in"><div class="eyebrow">{html.escape(meta.get("eyebrow", "Mint guide"))}</div>'
                     f'<h1>{html.escape(meta["title"])}</h1><p class="lede">{meta.get("lede", "")}</p></div></div>'
                     f'<div class="wrap"><nav class="toc" aria-label="On this page"><h4>On this page</h4>{toc}</nav>'
                     f'<main>{body}{nxt}</main></div>')
        page += TAIL.format(orb=ORB)
        (HERE / f"{name}.html").write_text(page)
        # Every example on the page goes into the search index.
        for sid, sec in re.findall(r'<section id="([\w-]+)"[^>]*>(.*?)</section>', body, re.S):
            title = re.sub("<[^>]+>", "", (re.search(r"<h2>(.*?)</h2>", sec) or re.search(r"(.)", sec)).group(1))
            for q in re.findall(r"<q[^>]*>(.*?)</q>", sec, re.S):
                index.append({"t": html.unescape(re.sub("<[^>]+>", "", q)), "w": title, "h": f"{name}.html#{sid}"})
    today = __import__("datetime").date.today().isoformat()
    urls = "".join(f"<url><loc>{SITE if p == 'index' else SITE + p}</loc><lastmod>{today}</lastmod></url>" for p in PAGES)
    (HERE / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                      + urls + "</urlset>\n")
    (HERE / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}sitemap.xml\n")
    (HERE / "assets" / "search-index.js").write_text("window.MINT_INDEX = " + json.dumps(index, ensure_ascii=False) + ";\n")
    print(f"built {len(PAGES)} pages, {len(index)} examples indexed")


if __name__ == "__main__":
    main()
