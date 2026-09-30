"""The chat panel: the conversation so far, a box to type in, and the controls.

Closed by default. Opened by clicking the orb, by ⌘J, or by asking ("show me
the chat"). While open it holds the keyboard; closing it hands the keyboard
back to the app the user was in, so Mint can type there.

Rows are laid out by hand in a flipped document view: user messages on the
right, Mint on the left, actions as small grey lines with ✓ / ✗.

Main thread only, except where noted.
"""

from __future__ import annotations

import json
import math

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import effects
from mint.ui import gfx
from mint.ui import look
from mint.core import prefs

W, H = 360, 470
HEADER = 54
INPUT = 56
PAD = 12


def _font(size, weight=AppKit.NSFontWeightRegular):
    return AppKit.NSFont.systemFontOfSize_weight_(size, weight)


def summarize_text(transcript: str) -> str:
    """A short summary of a chat transcript by a fast Flash model. Blocking."""
    from mint.screen import ground
    prompt = (f"Summarise this conversation between a user and {prefs.name()}, their Mac assistant, for the user. "
              "At most 5 short bullet points starting with '• ': what was asked, what was done (and what "
              "failed), and anything still open. Plain text, no markdown headings.\n\n" + transcript[-12000:])
    try:
        text, _ = ground._generate([prompt], models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                                                     "gemini-3.7-flash", "gemini-3.5-flash"], json_mode=False)
        return text.strip() or "(the summary came back empty)"
    except Exception as error:
        return f"Could not summarise right now: {str(error)[:120]}"


class _Flipped(AppKit.NSView):
    def isFlipped(self):
        return True


class _ChatWindow(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return bool(getattr(self, "allow_key", False))

    def canBecomeMainWindow(self):
        return False

    def cancelOperation_(self, sender):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner.close()

    def sendEvent_(self, event):
        # A click in the chat after Mint handed the keyboard back (to work
        # in another app) takes it again, so the user can type.
        owner = getattr(self, "owner", None)
        if (owner is not None and owner.is_open and not getattr(self, "allow_key", False)
                and event.type() == AppKit.NSEventTypeLeftMouseDown):
            owner.reclaim()
        objc.super(_ChatWindow, self).sendEvent_(event)


class _Target(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def mic_(self, sender):
        prefs.toggle("mic")

    def voice_(self, sender):
        prefs.toggle("voice")

    def gear_(self, sender):
        self.owner.show_settings(sender)

    def close_(self, sender):
        self.owner.close()

    def stop_(self, sender):
        self.owner.fire("stop")

    def sleep_(self, sender):
        self.owner.fire("sleep")

    def eye_(self, sender):
        from mint.ui import sharing
        sharing.set_visible(not sharing.visible())
        self.owner.refresh()

    def more_(self, sender):
        self.owner.show_more(sender)

    def summarize_(self, sender):
        # Summarise = compact: the session restarts from the summary.
        self.owner.note("compacting - summarising with Gemini Flash…")
        self.owner.fire("compact")

    def clearChat_(self, sender):
        self.owner.clear()

    def newSession_(self, sender):
        self.owner.fire("new_session")

    def send_(self, sender):
        self.owner.submit()

    @objc.typedSelector(b"Z@:@@:")
    def control_textView_doCommandBySelector_(self, control, view, selector):
        selector = selector.decode() if isinstance(selector, bytes) else str(selector)
        if selector == "insertNewline:":
            self.owner.submit()
            return True
        if selector == "cancelOperation:":
            self.owner.close()
            return True
        return False


class ChatPanel:
    def __init__(self, fire, menu_factory) -> None:
        self.fire = fire
        self.menu_factory = menu_factory
        self.window = None
        self.is_open = False
        self._previous_app = None
        self._rows: list[dict] = []       # {kind, text, view, label, done}
        self._open_turn = {"user": False, "mint": False}

    # --- construction -------------------------------------------------------------

    def build(self) -> None:
        style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
        window = _ChatWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H), style, AppKit.NSBackingStoreBuffered, False)
        window.owner = self
        window.allow_key = False
        window.setLevel_(AppKit.NSStatusWindowLevel)
        window.setOpaque_(False)
        window.setBackgroundColor_(AppKit.NSColor.clearColor())
        window.setHasShadow_(True)
        window.setHidesOnDeactivate_(False)
        window.setFloatingPanel_(True)
        window.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        window.setSharingType_(effects.SHARING)
        look.follow_system(window)             # light or dark with the system
        window.setAlphaValue_(0.0)
        self._target = _Target.alloc().initWithOwner_(self)

        # System glass rounded by a mask image: a layer cornerRadius left a square
        # patch of blur showing round the card.
        blur = look.glass(AppKit.NSMakeRect(0, 0, W, H), radius=20)
        blur.setWantsLayer_(True)
        window.setContentView_(blur)

        self._build_header(blur)
        self._build_body(blur)
        self._build_input(blur)
        self.window = window
        look.on_theme_change(self._retheme)
        from mint.screen import ground
        ground.OWN_CHAT = self
        self._load_history()
        self.refresh()

    def _button(self, parent, symbol, action, x, y, tip, size=26):
        button = AppKit.NSButton.buttonWithImage_target_action_(gfx.symbol(symbol, 12), self._target, action)
        button.setBordered_(False)
        button.setFrame_(AppKit.NSMakeRect(x, y, size, size))
        button.setContentTintColor_(AppKit.NSColor.secondaryLabelColor())     # plain toolbar symbol
        button.setToolTip_(tip)
        parent.addSubview_(button)
        return button

    def _build_header(self, parent) -> None:
        top = H - HEADER
        self._dot = Quartz.CALayer.layer()
        self._dot.setFrame_(Quartz.CGRectMake(PAD + 2, top + 22, 9, 9))
        self._dot.setCornerRadius_(4.5)
        parent.layer().addSublayer_(self._dot)
        title = AppKit.NSTextField.labelWithString_(prefs.name())
        title.setFont_(_font(14, AppKit.NSFontWeightSemibold))
        title.setFrame_(AppKit.NSMakeRect(PAD + 18, top + 25, 140, 18))
        parent.addSubview_(title)
        self._status = AppKit.NSTextField.labelWithString_("")
        self._status.setFont_(_font(11))
        self._status.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        self._status.setFrame_(AppKit.NSMakeRect(PAD + 18, top + 9, 80, 15))    # clear of the header buttons
        self._status.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        parent.addSubview_(self._status)
        x = W - PAD - 26
        self._button(parent, "xmark", "close:", x, top + 14, "Close (Esc)")
        self._button(parent, "slider.horizontal.3", "gear:", x - 30, top + 14, "Appearance and settings")
        self._voice = self._button(parent, "speaker.wave.2.fill", "voice:", x - 60, top + 14, "Spoken replies on/off")
        self._mic = self._button(parent, "mic.fill", "mic:", x - 90, top + 14, "Microphone on/off")
        self._sleep = self._button(parent, "moon.zzz.fill", "sleep:", x - 120, top + 14, "Sleep")
        self._more = self._button(parent, "ellipsis", "more:", x - 150, top + 14,
                                  "More: summarise & compact, clear chat, new session")
        # Seen or not in screen sharing (Google Meet, Zoom): hidden by default.
        self._eye = self._button(parent, "eye.slash", "eye:", x - 180, top + 14,
                                 "Visible in screen sharing - click to show or hide Mint in Meet/Zoom")
        self._stop = self._button(parent, "stop.fill", "stop:", x - 210, top + 14, "Stop everything")
        self._stop.setContentTintColor_(AppKit.NSColor.systemRedColor())
        self._stop.setHidden_(True)
        # A thin progress bar under the header for long tasks.
        self._track = Quartz.CALayer.layer()
        self._track.setFrame_(Quartz.CGRectMake(PAD, top, W - 2 * PAD, 2.5))
        self._track.setCornerRadius_(1.25)
        self._track.setBackgroundColor_(look.cg(AppKit.NSColor.quaternaryLabelColor(), parent))
        self._track.setOpacity_(0)
        parent.layer().addSublayer_(self._track)
        self._fill = Quartz.CALayer.layer()
        self._fill.setAnchorPoint_(Quartz.CGPointMake(0, 0.5))
        self._fill.setPosition_(Quartz.CGPointMake(PAD, top + 1.25))
        self._fill.setBounds_(Quartz.CGRectMake(0, 0, 0, 2.5))
        self._fill.setCornerRadius_(1.25)
        self._fill.setOpacity_(0)
        parent.layer().addSublayer_(self._fill)
        line = AppKit.NSBox.alloc().initWithFrame_(AppKit.NSMakeRect(0, top - 1, W, 1))
        line.setBoxType_(AppKit.NSBoxSeparator)
        line.setAlphaValue_(0.6)
        parent.addSubview_(line)

    def _build_body(self, parent) -> None:
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, INPUT, W, H - HEADER - INPUT - 2))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        doc = _Flipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, 10))
        scroll.setDocumentView_(doc)
        parent.addSubview_(scroll)
        self._scroll, self._doc = scroll, doc
        self._empty = AppKit.NSTextField.labelWithString_(
            "Say “Hey Mint”, or type below.\nSay “stop” any time to stop everything.")
        self._empty.setFont_(_font(12))
        self._empty.setAlignment_(AppKit.NSTextAlignmentCenter)
        self._empty.setTextColor_(AppKit.NSColor.tertiaryLabelColor())
        self._empty.setFrame_(AppKit.NSMakeRect(20, (H - HEADER - INPUT) / 2 - 10, W - 40, 36))
        parent.addSubview_(self._empty)

    def _build_input(self, parent) -> None:
        box = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(PAD, 11, W - 2 * PAD, 34))
        box.setWantsLayer_(True)
        box.layer().setCornerRadius_(17)
        box.layer().setBorderWidth_(0.5)
        parent.addSubview_(box)
        self._box = box
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(14, 7, W - 2 * PAD - 54, 20))
        field.setBezeled_(False)
        field.setDrawsBackground_(False)
        field.setEditable_(True)
        field.setSelectable_(True)
        field.setFocusRingType_(AppKit.NSFocusRingTypeNone)
        field.setFont_(_font(13))
        field.setTextColor_(AppKit.NSColor.labelColor())
        field.cell().setUsesSingleLineMode_(True)
        field.cell().setScrollable_(True)
        field.setPlaceholderAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            f"Message {prefs.name()}…", {AppKit.NSForegroundColorAttributeName: AppKit.NSColor.placeholderTextColor(),
                                AppKit.NSFontAttributeName: _font(13)}))
        field.setDelegate_(self._target)
        box.addSubview_(field)
        self._field = field
        send = AppKit.NSButton.buttonWithImage_target_action_(
            gfx.symbol("arrow.up.circle.fill", 20), self._target, "send:")
        send.setBordered_(False)
        send.setToolTip_("Send")
        send.setContentTintColor_(AppKit.NSColor.controlAccentColor())
        send.setFrame_(AppKit.NSMakeRect(W - 2 * PAD - 32, 3, 28, 28))
        box.addSubview_(send)
        self._paint_box()

    def _paint_box(self) -> None:
        """The message field: a soft system fill with a hairline, like Messages."""
        fill = getattr(AppKit.NSColor, "quaternarySystemFillColor", AppKit.NSColor.quaternaryLabelColor)()
        self._box.layer().setBackgroundColor_(look.cg(fill, self._box))
        self._box.layer().setBorderColor_(look.cg(AppKit.NSColor.separatorColor(), self._box))

    def _retheme(self) -> None:
        """Light/dark switched: layer colours are baked, so paint them again."""
        if self.window is None:
            return
        self._paint_box()
        self._track.setBackgroundColor_(look.cg(AppKit.NSColor.quaternaryLabelColor(), self.window.contentView()))
        self._layout(0)

    # --- rows ---------------------------------------------------------------------

    def _load_history(self) -> None:
        """The last few exchanges from memory, so the chat is never blank after a restart."""
        from mint.knowledge.conversation import HISTORY
        try:
            lines = HISTORY.read_text().splitlines()[-40:]
        except OSError:
            return
        added = 0
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        # "Clear chat" leaves a marker; nothing before it comes back.
        for i in range(len(entries) - 1, -1, -1):
            if entries[i].get("role") == "marker":
                entries = entries[i + 1:]
                break
        for entry in entries:
            role, text = entry.get("role"), entry.get("text", "")
            if role in ("user", "mint") and text:
                self._add("user" if role == "user" else "mint", text, layout=False)
                added += 1
        if added:
            self._add("note", "earlier conversation above", layout=False)
        self._open_turn = {"user": False, "mint": False}
        self._layout(0)

    def _attributed(self, kind, text):
        if kind == "user":
            return AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
                AppKit.NSFontAttributeName: _font(13), AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor()})
        if kind == "mint":
            return AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
                AppKit.NSFontAttributeName: _font(13),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor()})
        if kind == "summary":
            out = AppKit.NSMutableAttributedString.alloc().init()
            out.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                "Summary\n", {AppKit.NSFontAttributeName: _font(11, AppKit.NSFontWeightSemibold),
                               AppKit.NSForegroundColorAttributeName: gfx.ns(look.ink(gfx.accent(), self.window))}))
            out.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
                AppKit.NSFontAttributeName: _font(12.5),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor()}))
            return out
        color = {"ok": AppKit.NSColor.systemGreenColor(), "fail": AppKit.NSColor.systemRedColor()}.get(
            kind, AppKit.NSColor.secondaryLabelColor())
        return AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
            AppKit.NSFontAttributeName: _font(11.5), AppKit.NSForegroundColorAttributeName: color})

    def _add(self, kind: str, text: str, layout: bool = True) -> dict:
        view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 10))
        view.setWantsLayer_(True)
        label = AppKit.NSTextField.wrappingLabelWithString_("")
        label.setSelectable_(True)
        view.addSubview_(label)
        self._doc.addSubview_(view)
        row = {"kind": kind, "text": text, "view": view, "label": label}
        self._rows.append(row)
        if layout:
            self._layout(len(self._rows) - 1)
        return row

    def _row_text(self, row) -> str:
        kind, text = row["kind"], row["text"]
        if kind == "action":
            return f"◌  {text}"
        if kind == "ok":
            return f"✓  {text}"
        if kind == "fail":
            return f"✗  {text}"
        if kind == "note":
            return f"— {text} —"
        return text

    def _layout(self, start: int) -> None:
        y = PAD if start == 0 else self._rows[start - 1]["bottom"] + 8
        accent = gfx.state_rgb("awake")
        user_fill = tuple(c * 0.82 for c in accent)      # deep enough for white text
        for row in self._rows[start:]:
            kind = row["kind"]
            attributed = self._attributed(kind if kind in ("user", "mint", "ok", "fail", "summary") else "note",
                                          self._row_text(row))
            bubble = kind in ("user", "mint", "summary")
            max_w = (W - 2 * PAD) * (0.8 if kind in ("user", "mint") else 1.0) - (22 if bubble else 0)
            rect = attributed.boundingRectWithSize_options_(
                AppKit.NSMakeSize(max_w, 10000),
                AppKit.NSStringDrawingUsesLineFragmentOrigin | AppKit.NSStringDrawingUsesFontLeading)
            tw, th = math.ceil(rect.size.width) + 4, math.ceil(rect.size.height) + 2
            label, view = row["label"], row["view"]
            label.setAttributedStringValue_(attributed)
            if bubble:
                bw, bh = tw + 22, th + 14
                x = W - PAD - bw if kind == "user" else PAD
                view.setFrame_(AppKit.NSMakeRect(x, y, bw, bh))
                label.setFrame_(AppKit.NSMakeRect(11, 7, tw, th))
                view.layer().setCornerRadius_(min(17, bh / 2))
                # Like Messages: your words in a solid accent bubble, Mint's in a soft
                # system grey, both borderless, readable in light and dark.
                grey = getattr(AppKit.NSColor, "tertiarySystemFillColor", AppKit.NSColor.quaternaryLabelColor)()
                view.layer().setBackgroundColor_(
                    gfx.cg(user_fill) if kind == "user" else
                    gfx.cg(gfx.accent(), 0.16) if kind == "summary" else
                    look.cg(grey, self.window))
                view.layer().setBorderWidth_(0.0)
                row["bottom"] = y + bh
            else:
                x = (W - tw) / 2 if kind == "note" else PAD + 4
                view.setFrame_(AppKit.NSMakeRect(x, y, tw, th))
                label.setFrame_(AppKit.NSMakeRect(0, 0, tw, th))
                view.layer().setBackgroundColor_(None)
                row["bottom"] = y + th
            y = row["bottom"] + 8
        height = max(y + PAD, self._scroll.contentSize().height)
        self._doc.setFrame_(AppKit.NSMakeRect(0, 0, W, height))
        self._scroll_to_end()
        self._empty.setHidden_(bool(self._rows))

    def _scroll_to_end(self) -> None:
        clip = self._scroll.contentView()
        bottom = max(0.0, self._doc.frame().size.height - clip.bounds().size.height)
        clip.scrollToPoint_(AppKit.NSMakePoint(0, bottom))
        self._scroll.reflectScrolledClipView_(clip)

    # --- public (any thread; each hops to main) ---------------------------------------

    def said(self, who: str, text: str, new_turn: bool = False) -> None:
        """Streamed words from the user or Mint: extend the current bubble, or start one."""
        def apply():
            if self.window is None:
                return
            last = self._rows[-1] if self._rows else None
            if not new_turn and self._open_turn[who] and last is not None and last["kind"] == who:
                last["text"] += text
                self._layout(len(self._rows) - 1)
            else:
                self._add(who, text.lstrip())
            self._open_turn[who] = True
            self._open_turn["user" if who == "mint" else "mint"] = False
        AppHelper.callAfter(apply)

    def boundary(self) -> None:
        def apply():
            self._open_turn = {"user": False, "mint": False}
        AppHelper.callAfter(apply)

    def action(self, key, text: str) -> None:
        def apply():
            if self.window is None:
                return
            row = self._add("action", text)
            row["key"] = key
            self._open_turn = {"user": False, "mint": False}
        AppHelper.callAfter(apply)

    def action_done(self, key, ok: bool, text: str | None = None) -> None:
        def apply():
            for i in range(len(self._rows) - 1, -1, -1):
                row = self._rows[i]
                if row.get("key") == key and row["kind"] == "action":
                    row["kind"] = "ok" if ok else "fail"
                    if text:
                        row["text"] = text
                    self._layout(i)
                    return
        AppHelper.callAfter(apply)

    # --- clear / summarise / new session ---------------------------------------------

    def show_more(self, sender) -> None:
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        for title, action in (("Summarise & compact session", "summarize:"), ("Clear chat", "clearChat:"),
                              (None, None), ("New session (fresh start)", "newSession:")):
            if title is None:
                menu.addItem_(AppKit.NSMenuItem.separatorItem())
                continue
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            item.setTarget_(self._target)
            menu.addItem_(item)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, AppKit.NSMakePoint(0, -4), sender)

    def transcript(self) -> str:
        """The conversation shown in the chat (main thread), oldest first."""
        lines = []
        for row in self._rows:
            kind, text = row["kind"], " ".join(str(row["text"]).split())
            if not text or kind == "summary" or (kind == "note" and text == "earlier conversation above"):
                continue
            who = {"user": "User", "mint": prefs.name(), "ok": "Done", "fail": "Failed", "action": "Doing",
                   "note": "Note"}.get(kind, kind)
            lines.append(f"{who}: {text}")
        return "\n".join(lines)

    def reset(self, note: str = "") -> None:
        """Empty the chat (main thread). With a note, leave it as the first line."""
        for row in self._rows:
            row["view"].removeFromSuperview()
        self._rows = []
        self._open_turn = {"user": False, "mint": False}
        if note:
            self._add("note", note, layout=False)
        self._layout(0)

    def clear(self) -> None:
        """Clear chat: the window only. Long-term memory is kept; a marker in the
        history keeps the cleared lines from coming back after a restart."""
        from mint.knowledge.conversation import HISTORY
        import os
        import time as _time
        try:
            with open(HISTORY, "a") as f:
                f.write(json.dumps({"t": _time.strftime("%Y-%m-%d %H:%M"), "role": "marker",
                                    "text": "chat cleared"}) + "\n")
            os.chmod(HISTORY, 0o600)
        except OSError:
            pass
        self.reset()
        print("  [chat cleared]", flush=True)

    def summarize(self) -> None:
        """Summarise the chat into a card, in the background."""
        text = self.transcript()
        if not text.strip():
            self._add("note", "nothing to summarise yet")
            return
        row = self._add("summary", "Summarising…")
        import threading

        def work():
            summary = summarize_text(text)

            def show():
                row["text"] = summary
                index = self._rows.index(row) if row in self._rows else None
                if index is not None:
                    self._layout(index)
            AppHelper.callAfter(show)
        threading.Thread(target=work, daemon=True, name="mint-summarize-chat").start()

    def note(self, text: str) -> None:
        AppHelper.callAfter(lambda: self.window is not None and self._add("note", text))

    # --- main thread -----------------------------------------------------------------

    def set_status(self, state: str, text: str, busy: bool) -> None:
        self._dot.setBackgroundColor_(gfx.cg(gfx.state_rgb(state)))
        self._status.setStringValue_(text)
        self._status.setToolTip_(text)
        self._stop.setHidden_(not busy)
        # While working, Stop takes the place of Sleep, More and the eye (not needed mid-task), so the
        # task's name has room; the status gets all the space left of the buttons either way.
        x = W - PAD - 26
        for button in (self._sleep, self._more, self._eye):
            button.setHidden_(busy)
        top = self._stop.frame().origin.y
        self._stop.setFrameOrigin_(AppKit.NSMakePoint(x - (120 if busy else 210), top))
        leftmost = x - (120 if busy else 180)
        frame = self._status.frame()
        self._status.setFrame_(AppKit.NSMakeRect(frame.origin.x, frame.origin.y,
                                                 max(60, leftmost - frame.origin.x - 6), frame.size.height))
        # (The state shows in the header dot; the field keeps its hairline, as in Messages.)

    def progress(self, done: int, total: int, label: str = "") -> None:
        visible = total > 0
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.45)
        self._track.setOpacity_(1.0 if visible else 0.0)
        self._fill.setOpacity_(1.0 if visible else 0.0)
        width = (W - 2 * PAD) * (min(done, total) / total if total else 0)
        self._fill.setBounds_(Quartz.CGRectMake(0, 0, width, 2.5))
        self._fill.setBackgroundColor_(gfx.cg(gfx.accent()))
        Quartz.CATransaction.commit()

    def refresh(self) -> None:
        if self.window is None:
            return
        mic, voice = bool(prefs.get("mic")), bool(prefs.get("voice"))
        self._mic.setImage_(gfx.symbol("mic.fill" if mic else "mic.slash.fill", 12))
        self._voice.setImage_(gfx.symbol("speaker.wave.2.fill" if voice else "speaker.slash.fill", 12))
        on, off = AppKit.NSColor.secondaryLabelColor(), AppKit.NSColor.systemRedColor()
        self._mic.setContentTintColor_(on if mic else off)
        self._voice.setContentTintColor_(on if voice else off)
        from mint.ui import sharing
        shown = sharing.visible()
        self._eye.setImage_(gfx.symbol("eye.fill" if shown else "eye.slash", 12))
        self._eye.setContentTintColor_(AppKit.NSColor.systemRedColor() if shown else on)
        self._eye.setToolTip_("Mint is VISIBLE in screen sharing - click to hide it" if shown
                              else "Mint is hidden from screen sharing - click to show it")
        self._layout(0)

    def show_settings(self, sender) -> None:
        menu = self.menu_factory()
        menu.popUpMenuPositioningItem_atLocation_inView_(None, AppKit.NSMakePoint(0, -4), sender)

    def open(self, frame) -> None:
        if self.window is None:
            return
        if not self.is_open:
            front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            me = AppKit.NSRunningApplication.currentApplication()
            if front is not None and front.processIdentifier() != me.processIdentifier():
                self._previous_app = front
            self.is_open = True
            from mint.screen import ground
            ground.OWN_CHAT_OPEN = True
            self.window.allow_key = True
            # Grow out of the orb.
            start = AppKit.NSInsetRect(frame, 14, 18)
            self.window.setFrame_display_(start, False)
            self.window.orderFrontRegardless()
            AppKit.NSAnimationContext.beginGrouping()
            context = AppKit.NSAnimationContext.currentContext()
            context.setDuration_(0.26)
            context.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithControlPoints____(0.2, 0.9, 0.3, 1.1))
            self.window.animator().setAlphaValue_(1.0)
            self.window.animator().setFrame_display_(frame, True)
            AppKit.NSAnimationContext.endGrouping()
            AppHelper.callLater(0.3, self.window.invalidateShadow)   # shadow round the rounded glass
            self._scroll_to_end()
        self._take_keyboard()

    def move(self, frame) -> None:
        if self.window is not None and self.is_open:
            self.window.setFrame_display_(frame, True)

    def _take_keyboard(self, attempt: int = 0) -> None:
        app = AppKit.NSApplication.sharedApplication()
        app.activateIgnoringOtherApps_(True)
        if attempt and not app.isActive():
            # Cooperative activation can refuse a background app even after its
            # own hotkey; Mint holds Accessibility, which can bring it forward.
            try:
                import ApplicationServices as AX
                me = AX.AXUIElementCreateApplication(
                    AppKit.NSRunningApplication.currentApplication().processIdentifier())
                AX.AXUIElementSetAttributeValue(me, "AXFrontmost", True)
            except Exception:
                pass
        self.window.makeKeyAndOrderFront_(None)
        self.window.makeFirstResponder_(self._field)
        editor = self.window.fieldEditor_forObject_(False, self._field)
        if editor is not None:
            editor.setDrawsBackground_(False)     # else a solid black/white slab covers the glass field
        if self.is_open and attempt < 3 and not app.isActive():
            AppHelper.callLater(0.08, lambda: self._take_keyboard(attempt + 1))

    def holds_keyboard(self) -> bool:
        return self.is_open and bool(getattr(self.window, "allow_key", False))

    def release_keyboard(self) -> None:
        """Mint is about to click or type in another app: give the keyboard
        back but leave the chat on screen."""
        if not self.holds_keyboard():
            return
        self.window.allow_key = False
        self.window.makeFirstResponder_(None)
        previous, self._previous_app = self._previous_app, None
        if previous is not None and not previous.isTerminated():
            previous.activateWithOptions_(0)
        else:
            # Step back without hiding: hide_ hid EVERY Mint window, the orb and the notch included,
            # and Mint looked crashed until something unhid it.
            AppKit.NSApplication.sharedApplication().deactivate()
        self.window.orderFrontRegardless()

    def reclaim(self) -> None:
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        me = AppKit.NSRunningApplication.currentApplication()
        if front is not None and front.processIdentifier() != me.processIdentifier():
            self._previous_app = front
        self.window.allow_key = True
        self._take_keyboard()

    def close(self) -> None:
        if self.window is None or not self.is_open:
            return
        self.is_open = False
        from mint.screen import ground
        ground.OWN_CHAT_OPEN = False
        self.window.allow_key = False
        self.window.makeFirstResponder_(None)
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.18)
        self.window.animator().setAlphaValue_(0.0)
        AppKit.NSAnimationContext.endGrouping()
        AppHelper.callLater(0.2, lambda: None if self.is_open else self.window.orderOut_(None))
        # Hand the keyboard back to the app the user was in.
        previous, self._previous_app = self._previous_app, None
        if previous is not None and not previous.isTerminated():
            previous.activateWithOptions_(0)
        else:
            AppKit.NSApplication.sharedApplication().deactivate()      # not hide_: see release_keyboard

    def submit(self) -> None:
        text = str(self._field.stringValue() or "").strip()
        if not text:
            return
        self._field.setStringValue_("")
        # Keep the chat open: the answer shows up here. The keyboard goes back
        # to the user's app only if the request needs the screen, which the
        # HUD handles when a screen tool starts.
        self.fire("submit", text)

    def field_text(self) -> str:
        editor = self.window.fieldEditor_forObject_(False, self._field) if self.window else None
        return str(editor.string()) if editor is not None else ""
