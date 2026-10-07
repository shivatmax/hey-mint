"""Build the guide: guide/src/index.html -> guide/index.html, and the docs:
guide/src/docs.html + guide/src/docs/*.html -> guide/docs.html (the overview) and one page per
docs group, guide/docs/<slug>.html. Also the search index (assets/docs-index.js), the sitemap,
robots.txt and _redirects (the old one-page-per-topic addresses point into the docs).

Each top-level source page starts with a JSON comment: <!--{"title": ..., "description": ..., "hello": ...}-->
Shortcuts inside pages:
  {{video NAME | caption}}     a looping clip from media/NAME.mp4 (poster media/NAME.jpg)
  {{video NAME 360 | caption}} the same, shown at most 360 px wide
  {{scene NAME}}               a clip for the landing page's player
  {{orb}}                      the orb's face (eyes, mouth, cheeks, brows, shades, hands)
  {{voices}} {{tools}}         the 30 voices and the tool table, from the code and docs/usage.md
  {{setting KEY | what it does}}   a settings.json row; the default comes from mint/core/prefs.py DEFAULTS
  {{env NAME | what it does}}      an environment-variable row; the default comes from the code when it is a literal
  {{download}} {{repo}} {{agent_speed}}
Docs sections are <section id=... data-group="Group">. Each group is one page (DOC_PAGES below says its
address, title and blurb, in reading order); a new group gets a page of its own automatically. Links
between sections are written as href="#id" (or "docs#id" on the landing page) and point at the right
page in the built site.
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

SITE = "https://hey-mint.pages.dev/"          # Cloudflare Pages (clean URLs: /docs, /docs/start)
REPO = "https://github.com/shivatmax/hey-mint"
DOWNLOAD = REPO + "/releases/latest"
NAME = "Hey Mint"
OG_ALT = ("Hey Mint: a Jarvis-style voice assistant for your Mac. A teal orb with eyes, a row of little critters, "
          "and the request “Hey Mint, do a trick”.")
# The docs pages, in reading order: data-group -> (address under docs/, title, blurb, sections shown first).
DOC_PAGES = [
    ("Start", "start", "Get started",
     "Install Hey Mint, talk to it, read the orb, try notch mode, and learn the shortcuts and dictation.", ()),
    ("Using Mint", "using", "Using Mint",
     "Apps, clicking and typing, files, the browser, the clipboard, meeting notes, videos, documents, pictures, "
     "music, the Apple apps, automations and more.", ()),
    ("From anywhere", "remote", "From anywhere",
     "Use your Mac from anywhere: your own Telegram bot, a Google Meet that shares your screen, or plain email.", ()),
    ("Agents", "agents", "Agents",
     "Background agents for big jobs, the critters that show them, several jobs at once, Claude Code and Codex in "
     "the notch, and the ChatGPT and Claude apps.", ("agents", "critters", "parallel", "claude-mode", "agent-checks", "agent-apps")),
    ("Learning", "learning", "Memory and learning",
     "What Mint remembers about you, the journal of what happened when, the skills it learns, and teaching it a task "
     "by showing it once.", ("memory", "history", "skills", "teach")),
    ("Personality", "personality", "Personality and voices",
     "Give Mint a personality in one line, see its expressions and tricks, and pick one of 30 voices.", ()),
    ("Your voice", "voice", "Your voice",
     "The wake word, training Mint on your voice, and the voice lock that keeps other voices out.", ()),
    ("Safety", "safety", "Safety and privacy",
     "The guard that asks before anything is deleted or changed, why pages and emails can't give Mint orders, undo, "
     "and what stays on your Mac.", ()),
    ("Settings", "settings", "Settings and updates",
     "Every page of Settings, and how Hey Mint updates itself safely.", ()),
    ("Configuration", "configuration", "Configuration",
     "Every setting with its default and what it does, the files Mint keeps them in, and the environment variables "
     "for keys and models.", ()),
    ("Troubleshooting", "troubleshooting", "Troubleshooting",
     "Fixes for common problems, where the log and your files are, and how to report a bug.", ("help", "logs", "report")),
    ("Architecture", "architecture", "Architecture",
     "How Hey Mint is built: the big picture, the packages, how a request flows, the helper apps, safety by "
     "construction, and every tool.", ("arch-overview", "arch-packages", "arch-flow", "arch-helpers", "arch-safety", "tools")),
]
# Old addresses (one page per topic) -> the section that content lives in now.
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


# ---------- the configuration page: settings.json and the environment, checked against the code ----------

DOCUMENTED_SETTINGS = set()


def pref_defaults() -> dict:
    from mint.core.prefs import DEFAULTS
    return DEFAULTS


def shown(value) -> str:
    """A default as the configuration table shows it."""
    if value is True:
        return "on"
    if value is False:
        return "off"
    if value is None or value == "" or value == []:
        return "<i>empty</i>"
    if isinstance(value, list):
        items = ", ".join(f"<code>{html.escape(str(v))}</code>" for v in value[:3])
        return items + (f" and {len(value) - 3} more" if len(value) > 3 else "")
    return f"<code>{html.escape(str(value))}</code>"


def setting(m):
    key, what = m.group(1), m.group(2).strip()
    value, found = pref_defaults(), True
    for part in key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            found = False
    if not found:
        print(f"  warning: {{{{setting {key}}}}} is not in mint/core/prefs.py DEFAULTS (default shown as empty)")
        value = None
    DOCUMENTED_SETTINGS.add(key.split(".")[0])
    return (f'<tr id="set-{key.replace(".", "-").replace("_", "-")}"><td><code>{key}</code></td>'
            f'<td>{shown(value)}</td><td>{what}</td></tr>')


_SOURCE = []


def code_text() -> str:
    if not _SOURCE:
        _SOURCE.append("\n".join(p.read_text(errors="ignore") for p in sorted((ROOT / "mint").rglob("*.py"))))
    return _SOURCE[0]


def env(m):
    name, what = m.group(1), m.group(2).strip()
    text = code_text()
    if name not in text:
        print(f"  warning: {{{{env {name}}}}}: no file in mint/ mentions it any more")
    d = re.search(r"""(?:environ\.get|getenv)\(\s*["']%s["']\s*,\s*["']([^"']+)["']""" % name, text)
    default = f"<code>{html.escape(d.group(1))}</code>" if d else "<i>not set</i>"
    return f'<tr id="env-{name.lower().replace("_", "-")}"><td><code>{name}</code></td><td>{default}</td><td>{what}</td></tr>'


def media(name):
    poster = HERE / "media" / f"{name}.jpg"
    assert (HERE / "media" / f"{name}.mp4").exists() and poster.exists(), f"missing media/{name}"
    return Image.open(poster).size


def video(m):
    name, width, cap = m.group(1), m.group(2), (m.group(3) or "").strip()
    w, h = media(name)
    shown_w = int(width) if width else round(w * 0.75)          # about 1.5 pixels per point: sharp on Retina
    label = html.escape(re.sub("<[^>]+>", "", cap) or name)
    tag = (f'<div class="frame" style="--w:{shown_w}px"><video src="media/{name}.mp4" poster="media/{name}.jpg" '
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
    text = re.sub(r"\{\{setting ([\w.]+)\s*\|(.*?)\}\}", setting, text, flags=re.S)
    text = re.sub(r"\{\{env ([A-Z0-9_]+)\s*\|(.*?)\}\}", env, text, flags=re.S)
    if "{{voices}}" in text:
        text = text.replace("{{voices}}", voices())
    if "{{tools}}" in text:
        text = text.replace("{{tools}}", tools())
    return text


HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{full_title}</title><meta name="description" content="{desc}">
<link rel="canonical" href="{url}"><meta name="theme-color" content="#F3F6FB">
<link rel="icon" href="{root}assets/favicon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="{root}assets/og.png">
<meta property="og:type" content="website"><meta property="og:site_name" content="Hey Mint">
<meta property="og:title" content="{full_title}"><meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}"><meta property="og:image" content="{site}assets/og.png">
<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta property="og:image:alt" content="{og_alt}">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{full_title}">
<meta name="twitter:description" content="{desc}"><meta name="twitter:image" content="{site}assets/og.png">
<meta name="twitter:image:alt" content="{og_alt}">
{jsonld}
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&family=Atkinson+Hyperlegible:ital,wght@0,400;0,700;1,400&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{root}assets/guide.css"></head>
<body data-hello="{hello}" data-page="{page}" data-doc="{doc}" data-root="{root}">
<header class="top"><div class="top-in"><a class="brand" href="{home}"><span class="dot"></span>Hey Mint</a>
<nav aria-label="Site"><a href="{root}docs"{docs_on}>Docs</a><a href="{repo}">GitHub</a><a class="btn primary small" href="{download}">Download</a></nav></div></header>
"""

TAIL = """<footer><span>Hey Mint · open-source voice assistant for macOS · GPL-3.0</span><span class="grow"></span>
<a href="{root}docs">Docs</a><a href="{repo}">GitHub</a><a href="{repo}/releases">Releases</a><a href="{repo}/blob/main/docs/development.md">Contribute</a></footer>
<div class="bubble" id="bubble" role="status" aria-live="polite"></div>
<div class="buddy" aria-hidden="true"><div class="body"><button class="orb" tabindex="-1" aria-label="The page's Mint. Poke it.">{orb}</button></div></div>
{extra}<script src="{root}assets/guide.js"></script><script src="{root}assets/critters.js"></script>
</body></html>"""

SECTION = re.compile(r'<section id="([\w-]+)" data-group="([^"]+)">\s*<h2>(.*?)</h2>(.*?)</section>', re.S)


def plain(fragment: str) -> str:
    return html.unescape(re.sub("<[^>]+>", "", fragment)).strip()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def ld(data: dict) -> str:
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False) + "</script>"


def crumbs(*items) -> str:
    return ld({"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": i + 1, "name": name, "item": url} for i, (name, url) in enumerate(items)]})


def relink(text: str, depth: int, here: str | None, where: dict, hashes: bool = False) -> str:
    """Make a page's links work from where it is built: guide/ (depth 0) or guide/docs/ (depth 1).
    href="docs#id" (and, on docs pages or with `hashes`, href="#id") go to the page that has that section now."""
    up = "../" * depth

    def nest(m):
        attr, url = m.group(1), m.group(2)
        if not depth or re.match(r"[a-zA-Z][\w+.-]*:|#|/|\{", url):
            return m.group(0)
        return f'{attr}="{up}{url[2:] if url.startswith("./") else url}"'

    def anchor(m):
        target = m.group(2)
        page = where.get(target)
        if page is None:
            print(f"  warning: a link points at #{target}, which no docs section has")
            return m.group(0)
        if depth:
            return f'href="{"" if page == here else page}#{target}"'
        return f'href="docs/{page}#{target}"'

    text = re.sub(r'\b(href|src|poster)="([^"]*)"', nest, text)
    text = re.sub(r'href="(%sdocs)#([\w-]+)"' % re.escape(up), anchor, text)
    if depth or hashes:
        text = re.sub(r'href="(#)([\w-]+)"', lambda m: m.group(0) if where.get(m.group(2)) == here else anchor(m), text)
    return text


def side(pages, here: str | None, root: str, sections=()) -> str:
    """The docs menu: search, every page (this one marked), and this page's sections."""
    cur = ' class="cur" aria-current="page"'
    links = [f'<a href="{root}docs"{cur if here is None else ""}>Overview</a>']
    for p in pages:
        links.append(f'<a href="{"" if root else "docs/"}{p["slug"]}"{cur if p["slug"] == here else ""}>'
                     f'{html.escape(p["title"])}</a>')
    out = (f'<aside class="side"><input type="search" id="find" placeholder="Search the docs" aria-label="Search the docs" '
           f'autocomplete="off"><div class="hits" id="hits"></div><nav class="pages" aria-label="Docs pages"><h4>Docs</h4>'
           + "".join(links) + "</nav>")
    if sections:
        out += ('<details class="onpage" open><summary>On this page</summary><nav aria-label="Sections on this page">'
                + "".join(f'<a href="#{sid}">{title}</a>' for sid, title, _ in sections) + "</nav></details>")
    return out + "</aside>"


def pager(prev, nxt) -> str:
    def one(cls, label, p):
        if not p:
            return "<span></span>"
        return f'<a class="{cls}" href="{p[0]}"><small>{label}</small><span>{html.escape(p[1])}</span></a>'
    return f'<nav class="pager" aria-label="Previous and next page">{one("prev", "Previous", prev)}{one("next", "Next", nxt)}</nav>'


def read_docs():
    """The overview's head (everything before the first section) and every docs page with its sections."""
    sources = [HERE / "src" / "docs.html"] + sorted((HERE / "src" / "docs").glob("*.html"))
    raw = sources[0].read_text()
    m = re.match(r"\s*<!--(\{.*?\})-->", raw, re.S)
    meta = json.loads(m.group(1))
    text = expand(raw[m.end():])
    head = text[:text.index("<section")] if "<section" in text else text
    for extra in sources[1:]:
        text += "\n" + expand(extra.read_text())
    known = {group: i for i, (group, *_rest) in enumerate(DOC_PAGES)}
    pages = [{"group": g, "slug": s, "title": t, "blurb": b, "first": f, "sections": []} for g, s, t, b, f in DOC_PAGES]
    for sm in SECTION.finditer(text):
        sid, group, title = sm.group(1), sm.group(2), sm.group(3).strip()
        if group not in known:                                   # a new group: a page of its own, at the end
            print(f"  note: docs group {group!r} is not in DOC_PAGES; it gets docs/{slugify(group)}")
            known[group] = len(pages)
            pages.append({"group": group, "slug": slugify(group), "title": group, "blurb": "", "first": (), "sections": []})
        pages[known[group]]["sections"].append((sid, title, sm.group(0)))
    pages = [p for p in pages if p["sections"]]
    for p in pages:
        first = [s for f in p["first"] for s in p["sections"] if s[0] == f]
        p["sections"] = first + [s for s in p["sections"] if s not in first]
    return meta, head, pages


def main():
    written = []
    docs_meta, docs_head, pages = read_docs()
    where = {}                                                   # every id inside a docs section -> its page
    for p in pages:
        for sid, _title, block in p["sections"]:
            for i in re.findall(r'\bid="([\w-]+)"', block):
                if i in where and where[i] != p["slug"]:
                    print(f"  warning: id {i!r} is on two docs pages ({where[i]}, {p['slug']})")
                where.setdefault(i, p["slug"])

    def write(rel, *, full_title, desc, url, jsonld, hello, page, doc, depth, body, extra=""):
        root = "../" * depth
        out = HEAD.format(full_title=html.escape(full_title), desc=html.escape(desc), url=url, site=SITE, jsonld=jsonld,
                          og_alt=html.escape(OG_ALT), hello=html.escape(hello), page=page, doc=doc, root=root,
                          home=root or "./", repo=REPO, download=DOWNLOAD, docs_on=' class="on"' if page == "docs" else "")
        out += body + TAIL.format(orb=ORB, repo=REPO, extra=extra, root=root)
        path = HERE / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(out)
        written.append(rel)

    # ---- the landing page ----
    raw = (HERE / "src" / "index.html").read_text()
    m = re.match(r"\s*<!--(\{.*?\})-->", raw, re.S)
    meta, body = json.loads(m.group(1)), expand(raw[m.end():])
    desc = meta["description"]
    jsonld = ld({"@context": "https://schema.org", "@type": "SoftwareApplication", "name": NAME,
                 "alternateName": ["Mint", "Hey Mint assistant"], "operatingSystem": "macOS",
                 "applicationCategory": "UtilitiesApplication", "description": desc, "url": SITE,
                 "downloadUrl": DOWNLOAD, "image": SITE + "assets/og.png",
                 "license": "https://www.gnu.org/licenses/gpl-3.0.html", "codeRepository": REPO,
                 "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
                 "keywords": "voice assistant, macOS, Jarvis, AI agent, Gemini Live, open source, desktop automation"})
    write("index.html", full_title=meta.get("full_title") or f'{meta["title"]} · Hey Mint', desc=desc, url=SITE,
          jsonld=jsonld, hello=meta.get("hello", ""), page="index", doc="", depth=0, body=relink(body, 0, None, where))

    # ---- the search index: every page, section, example, and named row (settings, variables, tools) ----
    index = []
    for p in pages:
        index.append({"t": p["title"], "p": p["slug"], "k": "page"})
        for sid, title, block in p["sections"]:
            name = plain(title)
            index.append({"t": name, "h": sid, "p": p["slug"], "k": "section", "w": p["title"]})
            for h3 in re.findall(r"<h3[^>]*>(.*?)</h3>", block, re.S):
                index.append({"t": plain(h3), "h": sid, "p": p["slug"], "w": name, "k": "part"})
            for q in re.findall(r"<q[^>]*>(.*?)</q>", block, re.S):
                index.append({"t": plain(q), "h": sid, "p": p["slug"], "w": name})
            for rid, key in re.findall(r'<tr(?: id="([\w-]+)")?><td><code>([^<]+)</code>', block):
                index.append({"t": plain(key), "h": rid or sid, "p": p["slug"], "w": name, "k": "key"})
    (HERE / "assets" / "docs-index.js").write_text(
        "/* Built by guide/build.py: everything the docs search can find. */\nwindow.MINT_INDEX="
        + json.dumps(index, ensure_ascii=False, separators=(",", ":")) + ";\n")
    search = '<script src="{root}assets/docs-index.js"></script>\n'

    # ---- the overview: docs.html (old #anchors are sent on to their page) ----
    cards = "".join(
        f'<div class="doc-card"><h3><a href="docs/{p["slug"]}">{html.escape(p["title"])}</a></h3>'
        f'<p>{html.escape(p["blurb"])}</p><p class="secs">'
        + " · ".join(f'<a href="docs/{p["slug"]}#{sid}">{title}</a>' for sid, title, _ in p["sections"])
        + "</p></div>" for p in pages)
    overview = (f'<div class="docs">{side(pages, None, "")}<article>{relink(docs_head, 0, None, where, hashes=True)}'
                f'<div class="doc-cards">{cards}</div>'
                f'{pager(None, ("docs/" + pages[0]["slug"], pages[0]["title"]))}</article></div>')
    moved = ("<script>(function(){var m=" + json.dumps(where, separators=(",", ":")) + ";function go(){var h="
             "decodeURIComponent(location.hash.slice(1));if(m[h])location.replace('docs/'+m[h]+'#'+h)}go();"
             "addEventListener('hashchange',go)})()</script>")
    write("docs.html", full_title=docs_meta.get("full_title") or "Docs · Hey Mint", desc=docs_meta["description"],
          url=SITE + "docs", jsonld=crumbs((NAME, SITE), ("Docs", SITE + "docs")) + "\n" + moved,
          hello=docs_meta.get("hello", ""), page="docs", doc="", depth=0, body=overview, extra=search.format(root=""))

    # ---- one page per group ----
    keep = set()
    for i, p in enumerate(pages):
        prev = ("../docs", "Overview") if i == 0 else (pages[i - 1]["slug"], pages[i - 1]["title"])
        nxt = (pages[i + 1]["slug"], pages[i + 1]["title"]) if i + 1 < len(pages) else None
        head = (f'<div class="doc-head"><div class="eyebrow">Docs · {i + 1} of {len(pages)}</div>'
                f'<h1>{html.escape(p["title"])}</h1>' + (f'<p>{html.escape(p["blurb"])}</p>' if p["blurb"] else "") + "</div>")
        sections = "\n\n".join(block for _sid, _title, block in p["sections"])
        body = (f'<div class="docs">{side(pages, p["slug"], "../", p["sections"])}<article>{head}'
                f'{relink(sections, 1, p["slug"], where)}{pager(prev, nxt)}</article></div>')
        url = f'{SITE}docs/{p["slug"]}'
        desc = p["blurb"] or f'{p["title"]}: part of the Hey Mint docs.'
        write(f'docs/{p["slug"]}.html', full_title=f'{p["title"]} · Hey Mint docs', desc=desc, url=url,
              jsonld=crumbs((NAME, SITE), ("Docs", SITE + "docs"), (p["title"], url)),
              hello=docs_meta.get("hello", ""), page="docs", doc=p["slug"], depth=1, body=body,
              extra=search.format(root="../"))
        keep.add(f'{p["slug"]}.html')
    for old in (HERE / "docs").glob("*.html"):                    # pages of groups that no longer exist
        if old.name not in keep:
            old.unlink()

    unlisted = sorted(set(pref_defaults()) - DOCUMENTED_SETTINGS)
    if unlisted:
        print(f"  note: settings not on the Configuration page yet: {', '.join(unlisted)}")

    # The one-line installer (curl -fsSL hey-mint.pages.dev/install.sh | bash) is served from the site.
    for candidate in (HERE.parent / "packaging" / "install.sh", HERE.parent.parent / "publish" / "overlay" / "packaging" / "install.sh"):
        if candidate.exists():
            (HERE / "install.sh").write_text(candidate.read_text())
            break
    today = __import__("datetime").date.today().isoformat()
    addresses = [SITE, SITE + "docs"] + [f'{SITE}docs/{p["slug"]}' for p in pages]
    urls = "".join(f"<url><loc>{u}</loc><lastmod>{today}</lastmod></url>" for u in addresses)
    (HERE / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                      + urls + "</urlset>\n")
    (HERE / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}sitemap.xml\n")
    (HERE / "_redirects").write_text("".join(f"/{old} /docs/{where[new]}#{new} 301\n/{old}.html /docs/{where[new]}#{new} 301\n"
                                             for old, new in MOVED.items()))
    print(f"built {len(written)} pages ({', '.join(written)}); {len(index)} docs entries indexed; "
          f"{len(MOVED)} old addresses redirected")


if __name__ == "__main__":
    main()
