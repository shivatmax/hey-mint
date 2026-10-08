"""The app library window: every app Mint can work with, with its logo, what Mint does with it, and one button.

    ┌──────────────────────────────────────────────────────────────┐
    │ App library                                 [ Search apps  ] │
    │ Connect the apps you use and Mint can work in them for you.  │
    │ (All) (On your Mac) (Built in) (Chat & calls) (Notes & docs)… │
    ├──────────────────────────────────────────────────────────────┤
    │ Connected 24                                                 │
    │ ┌ [logo] Slack ───────────────┐ ┌ [logo] Spotify ──────────┐ │
    │ │ Opens channels and DMs…     │ │ Plays, pauses and picks…  │ │
    │ │ ✓ Connected        [ Open ] │ │ ✓ Connected      [ Open ] │ │
    │ On your Mac 12 · Get more apps · In your browser · Accounts · More on your Mac
    └──────────────────────────────────────────────────────────────┘

Buttons: Connect (app_library.connect: asks macOS once, or plans a connector and shows it in a sheet before saving),
Get it (the App Store or the maker's page), Use in browser, Set up (accounts), Open. Every action runs on a
background thread; the card says what is happening and what happened. The rows come from app_library (read off the
main thread; the window opens at once with what is cached, then refreshes).

open_library(category=None) opens it from any thread: category "mac" (On your Mac), one of
app_library.CATEGORIES, or a search ("notion").
"""

from __future__ import annotations

import logging
import threading

import AppKit
import objc
from PyObjCTools import AppHelper

from mint.tools import app_library

log = logging.getLogger("mint.ui.library_window")

W, H = 820, 680
PAD = 24
GAP = 12
ICON = 40
CARD_PAD = 14
BUTTON_H = 28
CHIP_H = 26
ALL, MAC = "all", "mac"


# --- small helpers -------------------------------------------------------------------------------------------

def _font(size: float, weight=None):
    return AppKit.NSFont.systemFontOfSize_weight_(size, AppKit.NSFontWeightRegular if weight is None else weight)


def _text_height(text: str, font, width: float) -> float:
    """The height `text` wraps to in a label `width` wide (a label lays text out ~2 pt in from each side)."""
    if not text:
        return 0.0
    rect = AppKit.NSString.stringWithString_(text).boundingRectWithSize_options_attributes_(
        AppKit.NSMakeSize(max(1.0, width - 5), 10_000), AppKit.NSStringDrawingUsesLineFragmentOrigin,
        {AppKit.NSFontAttributeName: font})
    return float(rect.size.height) + 2


def _text_width(text: str, font) -> float:
    return float(AppKit.NSString.stringWithString_(text).sizeWithAttributes_({AppKit.NSFontAttributeName: font})
                 .width)


def _label(view, text: str, x: float, y: float, w: float, font, color=None, h: float | None = None):
    """A wrapping label sized to its text (never cut off)."""
    field = AppKit.NSTextField.labelWithString_(text)
    field.setFont_(font)
    field.setTextColor_(color or AppKit.NSColor.labelColor())
    field.setMaximumNumberOfLines_(0)
    field.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
    field.setFrame_(AppKit.NSMakeRect(x, y, w, h if h is not None else _text_height(text, font, w)))
    view.addSubview_(field)
    return field


def _mark(name: str) -> str:
    """The letter for a drawn logo: "Microsoft Word" -> W, "Google Docs" -> D."""
    words = [w for w in name.split() if w.lower() not in ("microsoft", "google", "adobe", "apple")] or name.split()
    return (words[0][:1] if words else "?").upper()


def _rgb(rgb, alpha: float = 1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


# --- views ---------------------------------------------------------------------------------------------------

class MintLibraryFlipped(AppKit.NSView):
    def isFlipped(self):
        return True


def _short(said: str, name: str) -> str:
    """A Test result for a small tile: without the app's name, which the tile shows already."""
    for prefix in (f"✓ {name} works: ", f"✗ {name}: "):
        if said.startswith(prefix):
            return said[0] + " " + said[len(prefix):]
    return said


class MintLibraryCard(AppKit.NSView):
    """A rounded card, in the system's colours (redrawn for light and dark)."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        bounds = AppKit.NSInsetRect(self.bounds(), 0.5, 0.5)
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 12, 12)
        dark = "Dark" in str(self.effectiveAppearance().name())
        (AppKit.NSColor.whiteColor().colorWithAlphaComponent_(0.055) if dark
         else AppKit.NSColor.whiteColor()).setFill()
        path.fill()
        AppKit.NSColor.separatorColor().setStroke()
        path.setLineWidth_(0.5 if dark else 0.75)
        path.stroke()


class MintLibraryChip(AppKit.NSView):
    """A category chip: a pill; the chosen one is filled in the accent colour."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        bounds = self.bounds()
        h = bounds.size.height
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, h / 2, h / 2)
        on = bool(getattr(self, "on", False))
        (AppKit.NSColor.controlAccentColor() if on else AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.07)
         ).setFill()
        path.fill()
        font = _font(12, AppKit.NSFontWeightMedium if not on else AppKit.NSFontWeightSemibold)
        color = AppKit.NSColor.whiteColor() if on else AppKit.NSColor.labelColor()
        attrs = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: color}
        text = AppKit.NSString.stringWithString_(getattr(self, "title", ""))
        size = text.sizeWithAttributes_(attrs)
        text.drawAtPoint_withAttributes_(AppKit.NSMakePoint((bounds.size.width - size.width) / 2,
                                                            (h - size.height) / 2), attrs)

    def mouseDown_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.choose(self.key)

    def acceptsFirstMouse_(self, event):
        return True


class MintLibraryTile(AppKit.NSView):
    """A drawn logo for an app with no logo file and no app on this Mac: its first letter on its brand colour."""

    def drawRect_(self, rect):
        s = float(self.bounds().size.width)
        rgb = getattr(self, "rgb", (0.45, 0.48, 0.55))
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), s * 0.225, s * 0.225)
        top = tuple(min(1.0, c + 0.12) for c in rgb)
        AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(_rgb(top), _rgb(rgb)).drawInBezierPath_angle_(
            path, -90)
        letter = (getattr(self, "letter", "") or "?")[:1].upper()
        font = AppKit.NSFont.systemFontOfSize_weight_(s * 0.5, AppKit.NSFontWeightBold)
        attrs = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor()}
        text = AppKit.NSString.stringWithString_(letter)
        size = text.sizeWithAttributes_(attrs)
        text.drawAtPoint_withAttributes_(AppKit.NSMakePoint((s - size.width) / 2, (s - size.height) / 2), attrs)


class MintLibraryTarget(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(MintLibraryTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def act_(self, sender):
        handler = self.owner.handlers.get(int(sender.tag()))
        if handler is not None:
            handler()

    def controlTextDidChange_(self, note):
        self.owner.searched(str(note.object().stringValue()))

    def windowWillClose_(self, note):
        self.owner.closed()


# --- the window ----------------------------------------------------------------------------------------------

class LibraryWindow:
    def __init__(self) -> None:
        self.window = None
        self.category = ALL
        self.query = ""
        self.busy: dict[str, str] = {}        # app id -> what it is doing ("Connecting…")
        self.msgs: dict[str, str] = {}        # app id -> what happened
        self.handlers: dict[int, object] = {}
        self._tag = 0
        self._loading = False
        self.search = None

    # --- opening ---------------------------------------------------------------------------------------------

    def show(self, category: str | None = None) -> None:
        """Main thread."""
        if category:
            key = str(category)
            if key.lower() in (ALL, MAC):
                self.category, self.query = key.lower(), ""
            elif key in app_library.CATEGORIES or key == app_library.MORE:
                self.category, self.query = key, ""
            else:
                self.category, self.query = ALL, key
        if self.window is None:
            self._build()
        if self.search is not None and str(self.search.stringValue()) != self.query:
            self.search.setStringValue_(self.query)
        self.render()
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.reload(deep=True)

    def reload(self, deep: bool = False) -> None:
        """Read the apps again off the main thread, then redraw."""
        if self._loading:
            return
        self._loading = True

        def done() -> None:
            self._loading = False
            AppHelper.callAfter(self.render)
        app_library.warm(deep=deep, then=done)

    def closed(self) -> None:
        self.window = None

    def _build(self) -> None:
        self.target = MintLibraryTarget.alloc().initWithOwner_(self)
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskFullSizeContentView)
        window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H), style, AppKit.NSBackingStoreBuffered, False)
        window.setTitle_("App library")
        window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        window.setTitlebarAppearsTransparent_(True)
        window.setReleasedWhenClosed_(False)
        window.setDelegate_(self.target)
        window.center()
        root = MintLibraryFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        window.setContentView_(root)
        self.window, self.root = window, root

        title = _label(root, "App library", PAD, 34, 300, _font(22, AppKit.NSFontWeightBold), h=28)
        title.setMaximumNumberOfLines_(1)
        field = AppKit.NSSearchField.alloc().initWithFrame_(AppKit.NSMakeRect(W - PAD - 260, 36, 260, 26))
        field.setPlaceholderString_("Search apps")
        field.setDelegate_(self.target)
        field.setStringValue_(self.query)
        root.addSubview_(field)
        self.search = field
        try:
            from mint.core import prefs
            name = prefs.name()
        except Exception:
            name = "Mint"
        self.subtitle = _label(root, f"Connect the apps you use and {name} can work in them for you. {name} asks "
                               "macOS before it controls an app, and never sends, pays or deletes unless you ask.",
                               PAD, 68, W - 2 * PAD, _font(12.5), AppKit.NSColor.secondaryLabelColor())
        self.chips = MintLibraryFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(PAD - 2, 0, W - 2 * PAD + 4, 10))
        root.addSubview_(self.chips)
        self.divider = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, 1))
        self.divider.setBoxType_(AppKit.NSBoxSeparator)
        root.addSubview_(self.divider)
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, 10))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setDrawsBackground_(False)
        self.doc = MintLibraryFlipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, 10))
        scroll.setDocumentView_(self.doc)
        root.addSubview_(scroll)
        self.scroll = scroll

    # --- drawing ---------------------------------------------------------------------------------------------

    def _handler(self, fn) -> int:
        self._tag += 1
        self.handlers[self._tag] = fn
        return self._tag

    def _chip_list(self, rows: list[dict]) -> list[tuple[str, str]]:
        present = {r["category"] for r in rows}
        chips = [(ALL, "All"), (MAC, "On your Mac")]
        chips += [(c, c) for c in app_library.CATEGORIES if c in present]
        if app_library.MORE in present:
            chips.append((app_library.MORE, app_library.MORE))
        return chips

    def _layout_chips(self, rows: list[dict]) -> float:
        """The chips, wrapped onto as many lines as they need; -> where the list starts."""
        top = float(self.subtitle.frame().origin.y + self.subtitle.frame().size.height) + 14
        chips = self._chip_list(rows)
        for view in list(self.chips.subviews()):
            view.removeFromSuperview()
        width = W - 2 * PAD + 4
        x, y, font = 2.0, 0.0, _font(12, AppKit.NSFontWeightSemibold)
        for key, title in chips:
            w = _text_width(title, font) + 26
            if x + w > width - 2 and x > 2:
                x, y = 2.0, y + CHIP_H + 8
            chip = MintLibraryChip.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, CHIP_H))
            chip.owner, chip.key, chip.title, chip.on = self, key, title, key == self.category and not self.query
            self.chips.addSubview_(chip)
            x += w + 8
        height = y + CHIP_H
        self.chips.setFrame_(AppKit.NSMakeRect(PAD - 2, top, width, height))
        list_top = top + height + 14
        self.divider.setFrame_(AppKit.NSMakeRect(0, list_top - 1, W, 1))
        self.scroll.setFrame_(AppKit.NSMakeRect(0, list_top, W, H - list_top))
        return list_top

    def _sections(self, rows: list[dict]) -> list[tuple[str, str, list[dict]]]:
        if self.query:
            found = app_library.search(self.query, rows)
            return [(f"Results for “{self.query}”", "" if found else
                     "No app by that name here. Mint can still try: say “connect” and the app's name.", found)]
        if self.category == MAC:
            rows = [r for r in rows if r["installed"]]
        elif self.category != ALL:
            rows = [r for r in rows if r["category"] == self.category]
        g = app_library.sort_groups(rows, max_suggested=10_000)
        name = self._name()
        return [
            ("Connected", f"Working now: just ask {name}.", g["connected"]),
            ("On your Mac", f"Installed apps {name} can work with. Connect asks once; then {name} can use them when "
                            "you ask.", g["on_mac"]),
            ("Accounts", "Set up once in System Settings; your Mac keeps them in sync.", g["accounts"]),
            ("Get more apps", f"Popular apps {name} works with. Get it opens the App Store or the maker's download "
                              "page; many also work in your browser.", g["get"]),
            ("In your browser", f"No app needed: {name} uses them in your browser, where you're signed in.",
             g["browser"]),
            (app_library.MORE, f"Other apps here that can be told what to do. Connect reads what each can do and "
                               "shows you before anything is saved.", g["more_on_mac"]),
        ]

    def _name(self) -> str:
        try:
            from mint.core import prefs
            return prefs.name()
        except Exception:
            return "Mint"

    def render(self) -> None:
        """Main thread: rebuild the list from the cached rows (keeps the scroll position)."""
        if self.window is None:
            return
        rows = app_library.cached()
        self.handlers.clear()
        self._layout_chips(rows or [])
        clip = self.scroll.contentView()
        keep = float(clip.bounds().origin.y)
        for view in list(self.doc.subviews()):
            view.removeFromSuperview()
        y = 18.0
        if rows is None:
            _label(self.doc, "Looking at the apps on this Mac…", PAD, y, W - 2 * PAD, _font(13),
                   AppKit.NSColor.secondaryLabelColor())
            y += 40
        else:
            col_w = (W - 2 * PAD - GAP) / 2
            for title, hint, items in self._sections(rows):
                if not items and not self.query:
                    continue
                y = self._section_title(title, len(items), hint, y)
                if title == "Connected" and not self.query:      # what already works: small tiles, 4 to a line
                    y = self._tiles(items, y)
                    continue
                for i in range(0, len(items), 2):
                    pair = items[i:i + 2]
                    h = max(self._card_height(r, col_w) for r in pair)
                    for k, r in enumerate(pair):
                        self._card(r, PAD + k * (col_w + GAP), y, col_w, h)
                    y += h + GAP
                y += 14
        doc_h = max(y + 8, float(self.scroll.contentSize().height))
        self.doc.setFrame_(AppKit.NSMakeRect(0, 0, W, doc_h))
        clip.scrollToPoint_(AppKit.NSMakePoint(0, min(keep, max(0.0, doc_h - clip.bounds().size.height))))
        self.scroll.reflectScrolledClipView_(clip)

    def _tiles(self, items: list[dict], y: float) -> float:
        """Connected apps as small tiles: logo, name, and what is set up."""
        cols, gap, icon, test_w = 4, 10, 28, 46
        w = (W - 2 * PAD - gap * (cols - 1)) / cols
        tx = 10 + icon + 9
        tw = w - tx - 10 - test_w - 6
        name_font, note_font = _font(12.5, AppKit.NSFontWeightSemibold), _font(11)
        for i in range(0, len(items), cols):
            line = items[i:i + cols]
            notes = [self.busy.get(r["id"]) or _short(self.msgs.get(r["id"], ""), r["name"])
                     or ("✓ " + (r["detail"] if r.get("detail") and r["detail"] != "Ready" else "Connected"))
                     for r in line]
            h = max(max(_text_height(r["name"], name_font, tw) + 1 + _text_height(n, note_font, tw), icon) + 20
                    for r, n in zip(line, notes))
            for k, (r, note) in enumerate(zip(line, notes)):
                card = MintLibraryCard.alloc().initWithFrame_(AppKit.NSMakeRect(PAD + k * (w + gap), y, w, h))
                self.doc.addSubview_(card)
                self._icon(card, r, 10, (h - icon) / 2, icon)
                name_h, note_h = _text_height(r["name"], name_font, tw), _text_height(note, note_font, tw)
                ty = (h - (name_h + 1 + note_h)) / 2
                _label(card, r["name"], tx, ty, tw, name_font, h=name_h)
                tested = r["id"] in self.msgs or r["id"] in self.busy
                color = (AppKit.NSColor.systemRedColor() if note.startswith("✗") else
                         AppKit.NSColor.controlAccentColor() if tested else AppKit.NSColor.systemGreenColor())
                _label(card, note, tx, ty + name_h + 1, tw, note_font, color, h=note_h)
                button = AppKit.NSButton.buttonWithTitle_target_action_("Test", self.target, "act:")
                button.setBezelStyle_(AppKit.NSBezelStyleRounded)
                button.setControlSize_(AppKit.NSControlSizeSmall)
                button.setFont_(_font(11))
                button.setFrame_(AppKit.NSMakeRect(w - 10 - test_w, (h - 22) / 2, test_w, 22))
                if r["id"] in self.busy:
                    button.setEnabled_(False)
                else:
                    button.setTag_(self._handler(lambda row=r: self._run(row, "Testing…", app_library.check)))
                button.setToolTip_(f"Check that {r['name']} really works with Mint")
                card.addSubview_(button)
                card.setToolTip_(f"{r['name']}: {r['what']}")
            y += h + gap
        return y + 14

    def _section_title(self, title: str, count: int, hint: str, y: float) -> float:
        font = _font(15, AppKit.NSFontWeightSemibold)
        tw = min(_text_width(title, font) + 6, W - 2 * PAD - 60)
        _label(self.doc, title, PAD, y, tw, font, h=_text_height(title, font, tw))
        if count:
            _label(self.doc, str(count), PAD + tw + 4, y + 2, 50, _font(12.5, AppKit.NSFontWeightMedium),
                   AppKit.NSColor.tertiaryLabelColor(), h=18)
        y += _text_height(title, font, tw) + 2
        if hint:
            hint_label = _label(self.doc, hint, PAD, y, W - 2 * PAD, _font(12), AppKit.NSColor.secondaryLabelColor())
            y += float(hint_label.frame().size.height)
        return y + 10

    # --- one card --------------------------------------------------------------------------------------------

    def _buttons(self, row: dict) -> list[tuple[str, object, bool]]:
        busy = self.busy.get(row["id"])
        if busy:
            return [(busy, None, False)]
        state = row["state"]
        if state == "connected":
            out = [("Test", lambda r=row: self._run(r, "Testing…", app_library.check), False)]
            if row.get("path") or row.get("web_url"):
                out.append(("Open", lambda r=row: self._open(r), False))
            return out
        if state == "ready":
            return [("Connect", lambda r=row: self._run(r, "Connecting…", app_library.connect), True)]
        if state == "setup":
            return [("Set up…", lambda r=row: self._run(r, "Opening…", app_library.connect), True)]
        if state == "get":
            out = [("Get it", lambda r=row: self._run(r, "Opening…", app_library.connect), True)]
            if row.get("web_url"):
                out.insert(0, ("Use in browser", lambda r=row: self._run(r, "Opening…", app_library.use_in_browser),
                               False))
            return out
        if state == "web":
            return [("Use in browser", lambda r=row: self._run(r, "Opening…", app_library.use_in_browser), True)]
        return []

    def _tag_line(self, row: dict) -> tuple[str, object]:
        state, detail = row["state"], row.get("detail") or ""
        if state == "connected":
            return ("✓ Connected" + (f" · {detail}" if detail and detail != "Ready" else ""),
                    AppKit.NSColor.systemGreenColor())
        if state == "ready":
            return detail or "On your Mac", AppKit.NSColor.secondaryLabelColor()
        if state == "setup":
            return detail or "Not set up yet", AppKit.NSColor.systemOrangeColor()
        if state == "get":
            return ("Not on this Mac" + (" · works in your browser" if row.get("browser_ok") else ""),
                    AppKit.NSColor.secondaryLabelColor())
        return "Works in your browser", AppKit.NSColor.secondaryLabelColor()

    def _measure(self, row: dict, w: float) -> dict:
        tx = CARD_PAD + ICON + 12
        tw = w - tx - CARD_PAD
        name_font, what_font, tag_font = _font(13.5, AppKit.NSFontWeightSemibold), _font(12), _font(11.5)
        buttons = self._buttons(row)
        bfont = _font(13)
        widths = [max(72.0, _text_width(t, bfont) + 30) for t, _, _ in buttons]
        bw = sum(widths) + 8 * max(0, len(widths) - 1)
        tag, color = self._tag_line(row)
        msg = self.msgs.get(row["id"], "")
        # The tag shares the bottom line with the buttons when it fits beside them; else it goes above them.
        side_w = w - CARD_PAD - bw - 10 - tx if buttons else tw
        tag_inline = bool(tag) and side_w >= 90 and _text_height(tag, tag_font, side_w) <= 34
        m = {"tx": tx, "tw": tw, "buttons": buttons, "widths": widths, "bw": bw, "tag": tag, "tag_color": color,
             "tag_inline": tag_inline, "side_w": side_w if tag_inline else tw, "msg": msg,
             "name_h": _text_height(row["name"], name_font, tw), "what_h": _text_height(row["what"], what_font, tw),
             "msg_h": _text_height(msg, _font(11.5, AppKit.NSFontWeightMedium), tw) if msg else 0.0,
             "fonts": (name_font, what_font, tag_font)}
        m["tag_h"] = _text_height(tag, tag_font, m["side_w"]) if tag else 0.0
        text_h = m["name_h"] + 2 + m["what_h"] + (m["msg_h"] + 4 if msg else 0)
        if not tag_inline and tag:
            text_h += m["tag_h"] + 4
        bottom = max(BUTTON_H if buttons else 0.0, m["tag_h"] if tag_inline else 0.0)
        m["text_h"] = text_h
        m["height"] = CARD_PAD + max(ICON, text_h) + (10 + bottom if bottom else 0) + CARD_PAD
        return m

    def _card_height(self, row: dict, w: float) -> float:
        return self._measure(row, w)["height"]

    def _card(self, row: dict, x: float, y: float, w: float, h: float) -> None:
        m = self._measure(row, w)
        card = MintLibraryCard.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        self.doc.addSubview_(card)
        self._icon(card, row, CARD_PAD, CARD_PAD)
        name_font, what_font, tag_font = m["fonts"]
        tx, tw = m["tx"], m["tw"]
        ty = float(CARD_PAD) - 1
        _label(card, row["name"], tx, ty, tw, name_font, h=m["name_h"])
        ty += m["name_h"] + 2
        _label(card, row["what"], tx, ty, tw, what_font, AppKit.NSColor.secondaryLabelColor(), h=m["what_h"])
        ty += m["what_h"]
        if m["msg"]:
            ty += 4
            _label(card, m["msg"], tx, ty, tw, _font(11.5, AppKit.NSFontWeightMedium),
                   AppKit.NSColor.controlAccentColor(), h=m["msg_h"])
            ty += m["msg_h"]
        if m["tag"] and not m["tag_inline"]:
            ty += 4
            _label(card, m["tag"], tx, ty, tw, tag_font, m["tag_color"], h=m["tag_h"])
        # bottom line: the tag (when it fits there) and the buttons, right-aligned
        by = h - CARD_PAD - BUTTON_H
        if m["tag"] and m["tag_inline"]:
            mid = by + BUTTON_H / 2 if m["buttons"] else h - CARD_PAD - m["tag_h"] / 2
            _label(card, m["tag"], tx, mid - m["tag_h"] / 2, m["side_w"], tag_font, m["tag_color"], h=m["tag_h"])
        bx = w - CARD_PAD - m["bw"]
        for (title, handler, primary), bw in zip(m["buttons"], m["widths"]):
            button = AppKit.NSButton.buttonWithTitle_target_action_(title, self.target, "act:")
            button.setBezelStyle_(AppKit.NSBezelStyleRounded)
            button.setFrame_(AppKit.NSMakeRect(bx, by, bw, BUTTON_H))
            if handler is None:
                button.setEnabled_(False)
            else:
                button.setTag_(self._handler(handler))
            if primary:
                button.setBezelColor_(AppKit.NSColor.controlAccentColor())
                if hasattr(button, "setTintProminence_"):
                    button.setTintProminence_(getattr(AppKit, "NSTintProminencePrimary", 2))
            card.addSubview_(button)
            bx += bw + 8
        card.setToolTip_(f"{row['name']}: {row.get('how_words') or ''}")

    def _icon(self, card, row: dict, x: float, y: float, size: float = ICON) -> None:
        """The app's own icon when it is on this Mac; else its brand logo (brands.py); else a drawn tile."""
        if row.get("path"):
            image = AppKit.NSWorkspace.sharedWorkspace().iconForFile_(row["path"])
            pad = round(size * 0.08)
            view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(x - pad, y - pad, size + 2 * pad,
                                                                               size + 2 * pad))
            view.setImage_(image)
            view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            card.addSubview_(view)
            return
        slug = row.get("slug") or ""
        if slug:
            try:
                from mint.ui import brands
                known = getattr(brands, "BRANDS", {})
                if (getattr(brands, "logo_file", lambda k: None)(slug) or slug in known) and hasattr(brands,
                                                                                                    "icon_tile"):
                    brands.icon_tile(card, x, y, slug, size)
                    return
            except Exception:
                log.debug("brand %s", slug, exc_info=True)
        tile = MintLibraryTile.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, size, size))
        tile.rgb, tile.letter = tuple(row.get("color") or (0.45, 0.48, 0.55)), _mark(row["name"])
        card.addSubview_(tile)

    # --- actions ---------------------------------------------------------------------------------------------

    def choose(self, key: str) -> None:
        self.category, self.query = key, ""
        if self.search is not None:
            self.search.setStringValue_("")
        self.scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0, 0))
        self.render()

    def searched(self, text: str) -> None:
        self.query = text.strip()
        self.scroll.contentView().scrollToPoint_(AppKit.NSMakePoint(0, 0))
        self.render()

    def _open(self, row: dict) -> None:
        if row.get("path"):
            AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.fileURLWithPath_(row["path"]))
        elif row.get("web_url"):
            AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(row["web_url"]))

    def _say(self, app_id: str, words: str) -> None:
        def show() -> None:
            self.msgs[app_id] = " ".join(str(words).split())[:240]
            self.render()
        AppHelper.callAfter(show)

    def _run(self, row: dict, busy: str, fn) -> None:
        """An action on a background thread; the card shows `busy`, then what happened."""
        app_id = row["id"]
        if app_id in self.busy:
            return
        self.busy[app_id] = busy
        self.msgs.pop(app_id, None)
        self.render()

        def run() -> None:
            try:
                if fn in (app_library.connect, app_library.use_in_browser):
                    words = fn(app_id, confirm=self._confirm, say=lambda w: self._say(app_id, w))
                else:
                    words = fn(app_id)
            except Exception as error:
                log.exception("library action")
                words = f"Couldn't: {error}"
            try:
                app_library.all_rows(fresh=True)
            except Exception:
                log.debug("re-reading the apps", exc_info=True)

            def done() -> None:
                self.busy.pop(app_id, None)
                self.msgs[app_id] = " ".join(str(words).split())[:240]
                self.render()
            AppHelper.callAfter(done)
        threading.Thread(target=run, daemon=True, name="library-action").start()

    def _confirm(self, plan: dict) -> bool:
        """From the action's thread: the connector plan in a sheet; True when the user says Connect."""
        title, body = app_library.plan_summary(plan)
        answer = {"ok": False}
        event = threading.Event()

        def ask() -> None:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(body)
            alert.addButtonWithTitle_("Connect")
            alert.addButtonWithTitle_("Cancel")

            def finished(code) -> None:
                answer["ok"] = code == AppKit.NSAlertFirstButtonReturn
                event.set()
            if self.window is not None and self.window.isVisible():
                alert.beginSheetModalForWindow_completionHandler_(self.window, finished)
            else:
                finished(alert.runModal())
        AppHelper.callAfter(ask)
        event.wait(600)
        return answer["ok"]


_window: list = [None]


def window() -> LibraryWindow:
    if _window[0] is None:
        _window[0] = LibraryWindow()
    return _window[0]


def open_library(category: str | None = None) -> None:
    """Open the app library from any thread. `category`: "mac" (On your Mac), one of app_library.CATEGORIES,
    app_library.MORE, or words to search for ("notion"); None keeps the last view."""
    AppHelper.callAfter(lambda: window().show(category))
