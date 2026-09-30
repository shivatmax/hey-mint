"""Build the guide: guide/src/{index,docs}.html -> guide/{index,docs}.html, plus the sitemap,
robots.txt and _redirects (the old one-page-per-topic addresses point into the docs).

Each source page starts with a JSON comment: <!--{"title": ..., "description": ..., "hello": ...}-->
Shortcuts inside pages:
  {{video NAME | caption}}     a looping clip from media/NAME.mp4 (poster media/NAME.jpg)
  {{video NAME 360 | caption}} the same, shown at most 360 px wide
  {{scene NAME}}               a clip for the landing page's player
  {{orb}}                      the orb's face (eyes, mouth, cheeks, brows, shades, hands)
  {{voices}} {{tools}}         the 30 voices and the tool table, from the code and docs/usage.md
  {{download}} {{repo}} {{agent_speed}}
Docs sections are <section id=... data-group="Group"> and become the side menu.
"""
import html
import json
import re
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

SITE = "https://hey-mint.pages.dev/"          # Cloudflare Pages (clean URLs: /docs)
REPO = "https://github.com/shivatmax/hey-mint"
DOWNLOAD = REPO + "/releases/latest"
NAME = "Hey Mint"
PAGES = ["index", "docs"]
# Old addresses (one page per topic) -> where that content lives now.
MOVED = {"talking": "talking", "orb": "orb", "doing": "doing", "showing": "showing", "personality": "personality",
         "agents": "agents", "learning": "memory", "voice": "voice", "settings": "settings", "reference": "help"}

ORB = ('<span class="face"><span class="eyes"><span class="eye"></span><span class="eye"></span></span>'
       '<span class="mouth"></span><span class="cheek l"></span><span class="cheek r"></span>'
       '<span class="brows"></span><span class="shades"></span></span>'
       '<span class="hand l"></span><span class="hand r"></span>')


def voices():
    from mint.tools.extra import VOICES
    from mint.voice.voices import GENDER
    return "".join(f"<span>{v} <i>{html.escape(GENDER.get(v, ''))} · {html.escape(d)}</i></span>" for v, d in VOICES.items())


def tools():
    rows = []
    for line in (ROOT / "docs" / "usage.md").read_text().splitlines():
        m = re.match(r"\| `(\w+)` \| ([^|]*) \| (.*) \|$", line)
        if m:
            name, _args, what = m.groups()
            rows.append(f"<tr><td><code>{name}</code></td><td>{html.escape(what.strip()).replace('`', '')}</td></tr>")
    return "\n".join(rows)


def media(name):
    poster = HERE / "media" / f"{name}.jpg"
    assert (HERE / "media" / f"{name}.mp4").exists() and poster.exists(), f"missing media/{name}"
    return Image.open(poster).size


def video(m):
    name, width, cap = m.group(1), m.group(2), (m.group(3) or "").strip()
    w, h = media(name)
    shown = int(width) if width else round(w * 0.75)          # about 1.5 pixels per point: sharp on Retina
    label = html.escape(re.sub("<[^>]+>", "", cap) or name)
    tag = (f'<div class="frame" style="--w:{shown}px"><video src="media/{name}.mp4" poster="media/{name}.jpg" '
           f'width="{w}" height="{h}" style="--ar:{w}/{h}" preload="none" muted loop playsinline aria-label="{label}"></video></div>')
    return f'<figure class="clip">{tag}<figcaption>{cap}</figcaption></figure>' if cap else tag


def scene(m):
    name = m.group(1)
    w, h = media(name)
    return (f'<video data-v="{name}" src="media/{name}.mp4" poster="media/{name}.jpg" width="{w}" height="{h}" '
            f'preload="metadata" muted playsinline aria-label="Mint: {name.replace("-", " ")}"></video>')


def expand(text):
    speed = json.loads((HERE / "media" / "agent.json").read_text()).get("speed", 2)
    for key, value in (("orb", ORB), ("download", DOWNLOAD), ("repo", REPO), ("agent_speed", str(speed))):
        text = text.replace("{{" + key + "}}", value)
    text = re.sub(r"\{\{video ([\w-]+)(?: (\d+))?\s*(?:\|(.*?))?\}\}", video, text, flags=re.S)
    text = re.sub(r"\{\{scene ([\w-]+)\}\}", scene, text)
    if "{{voices}}" in text:
        text = text.replace("{{voices}}", voices())
    if "{{tools}}" in text:
        text = text.replace("{{tools}}", tools())
    return text


HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{full_title}</title><meta name="description" content="{desc}">
<link rel="canonical" href="{url}"><meta name="theme-color" content="#F3F6FB">
<link rel="icon" href="assets/favicon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="assets/og.png">
<meta property="og:type" content="website"><meta property="og:site_name" content="Hey Mint">
<meta property="og:title" content="{full_title}"><meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}"><meta property="og:image" content="{site}assets/og.png">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{full_title}">
<meta name="twitter:description" content="{desc}"><meta name="twitter:image" content="{site}assets/og.png">
{jsonld}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="assets/guide.css"></head>
<body data-hello="{hello}" data-page="{page}">
<header class="top"><div class="top-in"><a class="brand" href="./"><span class="dot"></span>Hey Mint</a>
<nav aria-label="Site"><a href="docs"{docs_on}>Docs</a><a href="{repo}">GitHub</a><a class="btn primary small" href="{download}">Download</a></nav></div></header>
"""

TAIL = """<footer><span>Hey Mint · open-source voice assistant for macOS · GPL-3.0</span><span class="grow"></span>
<a href="docs">Docs</a><a href="{repo}">GitHub</a><a href="{repo}/releases">Releases</a><a href="{repo}/blob/main/docs/development.md">Contribute</a></footer>
<div class="bubble" id="bubble" role="status" aria-live="polite"></div>
<div class="buddy" aria-hidden="true"><div class="body"><button class="orb" tabindex="-1" aria-label="The page's Mint. Poke it.">{orb}</button></div></div>
{extra}<script src="assets/guide.js"></script><script src="assets/critters.js"></script>
</body></html>"""


def main():
    written = []
    index = []
    for name in PAGES:
        raw = (HERE / "src" / f"{name}.html").read_text()
        m = re.match(r"\s*<!--(\{.*?\})-->", raw, re.S)
        meta, body = json.loads(m.group(1)), expand(raw[m.end():])
        desc = meta["description"]
        full = meta.get("full_title") or f'{meta["title"]} · Hey Mint'
        url = SITE if name == "index" else f"{SITE}{name}"
        jsonld = ""
        if name == "index":
            jsonld = ('<script type="application/ld+json">' + json.dumps({
                "@context": "https://schema.org", "@type": "SoftwareApplication", "name": NAME,
                "alternateName": ["Mint", "Hey Mint assistant"], "operatingSystem": "macOS",
                "applicationCategory": "UtilitiesApplication", "description": desc, "url": SITE,
                "downloadUrl": DOWNLOAD, "image": SITE + "assets/og.png",
                "license": "https://www.gnu.org/licenses/gpl-3.0.html", "codeRepository": REPO,
                "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
                "keywords": "voice assistant, macOS, Jarvis, AI agent, Gemini Live, open source, desktop automation"},
                ensure_ascii=False) + "</script>")
        page = HEAD.format(full_title=html.escape(full), desc=html.escape(desc), url=url, site=SITE, jsonld=jsonld,
                           hello=html.escape(meta.get("hello", "")), page=name, repo=REPO, download=DOWNLOAD,
                           docs_on=' class="on"' if name == "docs" else "")
        extra = ""
        if name == "index":
            page += body
        else:
            sections = re.findall(r'<section id="([\w-]+)" data-group="([^"]+)">\s*<h2>(.*?)</h2>(.*?)</section>', body, re.S)
            groups, menu = [], ""
            for sid, group, title, inner in sections:
                if group not in groups:
                    groups.append(group)
                    menu += f"<h4>{html.escape(group)}</h4>"
                menu += f'<a href="#{sid}">{title}</a>'
                index.append({"t": re.sub("<[^>]+>", "", title), "h": sid, "k": "section"})
                for q in re.findall(r"<q[^>]*>(.*?)</q>", inner, re.S):
                    index.append({"t": html.unescape(re.sub("<[^>]+>", "", q)), "h": sid, "w": re.sub("<[^>]+>", "", title)})
            page += (f'<div class="docs"><aside class="side"><input type="search" id="find" placeholder="Search the docs" '
                     f'aria-label="Search the docs" autocomplete="off"><div class="hits" id="hits"></div>'
                     f'<nav aria-label="Sections">{menu}</nav></aside><article>{body}</article></div>')
            extra = "<script>window.MINT_INDEX=" + json.dumps(index, ensure_ascii=False) + ";</script>\n"
        page += TAIL.format(orb=ORB, repo=REPO, extra=extra)
        (HERE / f"{name}.html").write_text(page)
        written.append(name)
    # The one-line installer (curl -fsSL hey-mint.pages.dev/install.sh | bash) is served from the site.
    for candidate in (HERE.parent / "packaging" / "install.sh", HERE.parent.parent / "publish" / "overlay" / "packaging" / "install.sh"):
        if candidate.exists():
            (HERE / "install.sh").write_text(candidate.read_text())
            break
    today = __import__("datetime").date.today().isoformat()
    urls = "".join(f"<url><loc>{SITE if p == 'index' else SITE + p}</loc><lastmod>{today}</lastmod></url>" for p in PAGES)
    (HERE / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                      + urls + "</urlset>\n")
    (HERE / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}sitemap.xml\n")
    (HERE / "_redirects").write_text("".join(f"/{old} /docs#{new} 301\n/{old}.html /docs#{new} 301\n"
                                             for old, new in MOVED.items()))
    print(f"built {', '.join(written)}; {len(index)} docs entries indexed; {len(MOVED)} old addresses redirected")


if __name__ == "__main__":
    main()
