"""The clipboard: Mint turns into it. A compact black card grows out of the orb (top right, or
wherever the orb sits) with Mint's little face in its corner, like the island.

Open it with ⌃⌥V (Settings ▸ Shortcuts), "open my clipboard", or the menu bar; the same key,
Esc, the × or a click on the face closes it.

* Everything is in it: what you copy (⌘C), every screenshot, and what Mint copies.
* Three tabs: All · Mint · Pinned (up to 20 pins, kept for good), and a search box.
* Select: click one; tap its circle (or ⌘-click) to add or remove; ⇧-click for a run; ⌘A for all. Selected clips
  show their order (1, 2, 3) - the order they are pasted in.
* Double-click or Return: paste into the app you were in (the clip stays). Copy puts the
  selection on the clipboard for you to paste (several pictures or files as files, several
  texts joined). Pin keeps it. Delete removes it. Drag a clip out into any app.
* Each clip has its own Copy button (⧉): it goes on the clipboard and to the top of the list, for you to paste
  wherever and whenever you like (nothing is pasted).
* Rest the pointer on a clip and its preview opens beside the card: the picture large, or the whole text
  (scrollable). Space shows or hides it for the selected clip.

The history is clip_tools.HISTORY (on this Mac; a copied key or token only masked, in memory, for a few minutes); this card shows it and acts
on it. Like all of Mint's windows it never appears in screen shares.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.tools import clipboard as clip_tools
from mint.ui import gfx

log = logging.getLogger("mint.ui.clipboard_window")

W, H = 364, 480
ROW = 50
FACE = 24
MAX_PINS = 20
TABS = (("all", "All"), ("mint", "Mint"), ("pinned", "Pinned"))
X_SIZE = 28                # the card's ×: big and plain to see
PREVIEW_W, PREVIEW_MAX_H = 340, 420
PREVIEW_AFTER = 0.45       # resting on a clip this long opens its preview
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".tiff", ".bmp")
SURFACE = (0.055, 0.055, 0.068)      # a shade lighter than pure black, so the card reads on a dark screen
EDGE = 0.22                          # its border: plain to see on black
INK = (1.0, 1.0, 1.0)
DIM = (0.62, 0.63, 0.68)
SOURCES = {"you": ("You", (0.62, 0.66, 0.74)), "mint": ("Mint", None), "screenshot": ("Screenshot", (0.35, 0.62, 1.0)),
           "pinned": ("Pinned", (1.0, 0.62, 0.1)), "phone": ("Phone", (0.45, 0.8, 0.5))}


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _font(size, weight=AppKit.NSFontWeightRegular):
    return AppKit.NSFont.systemFontOfSize_weight_(size, weight)


def _ago(at: float) -> str:
    seconds = int(time.time() - at)
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _label(text, size=13, weight=AppKit.NSFontWeightRegular, rgb=INK, alpha=1.0):
    field = AppKit.NSTextField.labelWithString_(text)
    field.setFont_(_font(size, weight))
    field.setTextColor_(_ns(rgb, alpha))
    field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    return field


def _icon_title(symbol: str, words: str, color):
    """A button title with its SF Symbol inline, both in one colour and centred together."""
    config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(10, AppKit.NSFontWeightSemibold)
    config = config.configurationByApplyingConfiguration_(
        AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_([color]))
    image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
    text = AppKit.NSMutableAttributedString.alloc().init()
    if image is not None:
        attachment = AppKit.NSTextAttachment.alloc().init()
        attachment.setImage_(image.imageWithSymbolConfiguration_(config))
        text.appendAttributedString_(AppKit.NSAttributedString.attributedStringWithAttachment_(attachment))
    text.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
        "  " + words, {AppKit.NSForegroundColorAttributeName: color,
                       AppKit.NSFontAttributeName: _font(12, AppKit.NSFontWeightSemibold)}))
    return text


class _ClipboardPanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def keyDown_(self, event):
        owner = getattr(self, "owner", None)
        if owner is None or not owner.key(event):
            objc.super(_ClipboardPanel, self).keyDown_(event)

    def cancelOperation_(self, sender):
        self.owner.close()


class _ClipboardList(AppKit.NSView):
    def isFlipped(self):
        return True


class _ClipboardRow(AppKit.NSView):
    """One clip: click to select, ⌘/⇧-click for more, double-click to paste, drag it out."""

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        flags = int(event.modifierFlags())
        if event.clickCount() == 2:
            self.owner.paste([self.clip_id])
            return
        where = self.convertPoint_fromView_(event.locationInWindow(), None)
        on_check = where.x > self.frame().size.width - 44
        self.owner.click(self.clip_id, cmd=on_check or bool(flags & AppKit.NSEventModifierFlagCommand),
                         shift=bool(flags & AppKit.NSEventModifierFlagShift))

    def mouseDragged_(self, event):
        if getattr(self, "dragging", False):
            return
        self.dragging = True
        item = self.owner.drag_item(self.clip_id)
        if item is None:
            return
        drag = AppKit.NSDraggingItem.alloc().initWithPasteboardWriter_(item)
        drag.setDraggingFrame_contents_(AppKit.NSMakeRect(8, 8, 44, 44), self.owner.drag_picture(self.clip_id))
        self.beginDraggingSessionWithItems_event_source_([drag], event, self)

    def mouseUp_(self, event):
        self.dragging = False

    def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
        return AppKit.NSDragOperationCopy

    def draggingSession_endedAtPoint_operation_(self, session, point, operation):
        self.dragging = False


class MintClipRowButton(AppKit.NSButton):
    def acceptsFirstMouse_(self, event):
        return True


class MintClipPreviewPanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return False


class _ClipboardTarget(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_ClipboardTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tab_(self, sender):
        self.owner.set_filter(TABS[int(sender.tag())][0])

    def search_(self, sender):
        self.owner.search = str(sender.stringValue() or "").strip().lower()
        self.owner.rebuild()

    def copy_(self, sender):
        self.owner.copy_selection()

    def copyRow_(self, sender):
        self.owner.copy_one(getattr(sender, "clip_id", ""))

    def paste_(self, sender):
        self.owner.paste(list(self.owner.selected))

    def pin_(self, sender):
        self.owner.pin_selection()

    def delete_(self, sender):
        self.owner.delete_selection()

    def close_(self, sender):
        self.owner.close()

    def tick_(self, timer):
        try:
            self.owner.tick()
        except Exception:
            log.debug("clipboard tick", exc_info=True)


class ClipboardWindow:
    def __init__(self) -> None:
        self.panel = None
        self.filter = "all"
        self.search = ""
        self.selected: list[str] = []          # ids, in the order they were picked
        self.anchor = None
        self.version = -1
        self.shown_ids: list[str] = []
        self.previous_app = None
        self.hud = None
        self.preview = None                    # the preview panel beside the card
        self.preview_id = ""                   # the clip it shows
        self.hover_id, self.hover_since, self.left_at = "", 0.0, 0.0
        self.sticky = False                    # opened with Space: stays until Space or Esc
        self.copied_id, self.copied_at = "", 0.0

    # --- building -------------------------------------------------------------------------------

    def _build(self) -> None:
        panel = _ClipboardPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(0, 0, W, H), AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        panel.owner = self
        panel.setOpaque_(False)
        panel.setBackgroundColor_(AppKit.NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setLevel_(AppKit.NSStatusWindowLevel)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
        panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        try:
            from mint.ui.effects import SHARING
            panel.setSharingType_(SHARING)
        except Exception:
            pass
        root = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        root.setWantsLayer_(True)
        root.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
        layer = root.layer()
        layer.setBackgroundColor_(_cg(SURFACE))
        layer.setCornerRadius_(22)
        layer.setBorderColor_(_cg((1, 1, 1), EDGE))
        layer.setBorderWidth_(1.0)
        layer.setMasksToBounds_(True)
        # The card is built at full size in its own view and scaled while it grows out of the orb.
        card = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        root.addSubview_(card)
        panel.setContentView_(root)
        self.panel, self.root, self.card = panel, root, card
        self.target = _ClipboardTarget.alloc().initWithOwner_(self)
        card.setWantsLayer_(True)
        mint = tuple(gfx.accent())

        # Header: the title and count on the left, Mint's little face in the top-right corner.
        title = _label("Clipboard", 15, AppKit.NSFontWeightBold)
        title.setFrame_(AppKit.NSMakeRect(20, H - 40, 100, 22))
        card.addSubview_(title)
        self.count = _label("", 11, AppKit.NSFontWeightMedium, DIM)
        self.count.setFrame_(AppKit.NSMakeRect(112, H - 37, 120, 16))
        card.addSubview_(self.count)
        from mint.ui.orb import Orb
        self.face_host = Quartz.CALayer.layer()
        self.face_host.setFrame_(Quartz.CGRectMake(W - 46, H - 46, 40, 40))
        card.layer().addSublayer_(self.face_host)
        self.face = Orb(self.face_host, (20, 20), FACE)
        self.face.apply_state("awake", True)
        for ring in (self.face.ring, self.face.spinner):
            ring.removeAllAnimations()
            ring.setOpacity_(0.0)
        close = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(W - 46, H - 46, 40, 40))
        close.setTransparent_(True)
        close.setTarget_(self.target)
        close.setAction_("close:")
        close.setToolTip_("Close (Esc)")
        card.addSubview_(close)
        # A plain, big × beside the face: the face alone didn't read as a way to close, and a small faint × was
        # hard to see on black.
        self.x_button = gfx.close_button(self.target, "close:", "Close (Esc)", size=X_SIZE, rest=0.92, base=0.16)
        self._place_x()
        card.addSubview_(self.x_button)

        # Search, full width.
        field = AppKit.NSSearchField.alloc().initWithFrame_(AppKit.NSMakeRect(16, H - 82, W - 32, 28))
        field.setPlaceholderString_("Search clips")
        field.setTarget_(self.target)
        field.setAction_("search:")
        card.addSubview_(field)
        self.field = field

        # Three tabs, centred, equal widths.
        tab_w, gap = 96, 6
        x = (W - (len(TABS) * tab_w + (len(TABS) - 1) * gap)) / 2
        self.tabs = []
        for tag, (_key, words) in enumerate(TABS):
            tab = AppKit.NSButton.buttonWithTitle_target_action_(words, self.target, "tab:")
            tab.setTag_(tag)
            tab.setBordered_(False)
            tab.setFrame_(AppKit.NSMakeRect(x, H - 120, tab_w, 26))
            tab.setWantsLayer_(True)
            tab.layer().setCornerRadius_(13)
            card.addSubview_(tab)
            self.tabs.append(tab)
            x += tab_w + gap

        # The list.
        top = H - 132
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 62, W, top - 62))
        scroll.setHasVerticalScroller_(True)
        scroll.setDrawsBackground_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        self.list = _ClipboardList.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, 10))
        scroll.setDocumentView_(self.list)
        card.addSubview_(scroll)
        self.scroll = scroll

        # The bar: a hint, or the actions centred and evenly spaced.
        divider = Quartz.CALayer.layer()
        divider.setFrame_(Quartz.CGRectMake(16, 61, W - 32, 0.5))
        divider.setBackgroundColor_(_cg((1, 1, 1), 0.08))
        card.layer().addSublayer_(divider)
        self.hint = _label("", 11, AppKit.NSFontWeightMedium, DIM)
        self.hint.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.hint.setFrame_(AppKit.NSMakeRect(16, 23, W - 32, 16))
        card.addSubview_(self.hint)
        self.actions = []
        specs = (("Paste", "paste:", "arrow.down.doc.fill", True), ("Copy", "copy:", "doc.on.doc", False),
                 ("Pin", "pin:", "pin.fill", False), ("Delete", "delete:", "trash", False))
        button_w, gap = 78, 8
        x = (W - (len(specs) * button_w + (len(specs) - 1) * gap)) / 2
        for words, action, symbol, primary in specs:
            color = AppKit.NSColor.blackColor() if primary else AppKit.NSColor.whiteColor()
            button = AppKit.NSButton.buttonWithTitle_target_action_(words, self.target, action)
            button.setBordered_(False)
            button.setAttributedTitle_(_icon_title(symbol, words, color))
            button.setFrame_(AppKit.NSMakeRect(x, 15, button_w, 32))
            button.setWantsLayer_(True)
            button.layer().setCornerRadius_(16)
            button.layer().setBackgroundColor_(_cg(mint) if primary else _cg((1, 1, 1), 0.1))
            if primary:
                button.setKeyEquivalent_("\r")
            card.addSubview_(button)
            self.actions.append(button)
            x += button_w + gap

        self.ticker = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 20, self.target, "tick:", None, True)
        self._paint_tabs()

    # --- showing: Mint turns into the clipboard -----------------------------------------------------

    def _hud(self):
        if self.hud is None:
            try:
                from mint.ui.island import island
                self.hud = island.hud
            except Exception:
                self.hud = None
        return self.hud

    def _place_x(self) -> None:
        """The ×: right above the column of pick circles (the face's corner is taken only in orb mode)."""
        try:
            from mint.ui import notch
            in_notch = bool(notch.enabled())
        except Exception:
            in_notch = False
        self.face_host.setHidden_(in_notch)
        right = W - 32 if in_notch else W - 46 - 4 - X_SIZE / 2          # the circles' centre, or left of the face
        self.x_button.setFrameOrigin_(AppKit.NSMakePoint(right - X_SIZE / 2, H - 28 - X_SIZE / 2))

    def _frame(self):
        """The card's frame, its corner on the orb (the little face sits where the orb was)."""
        screen = AppKit.NSScreen.mainScreen().visibleFrame()
        hud = self._hud()
        center = hud.orb_center() if hud is not None else None
        if center is None:
            center = (screen.origin.x + screen.size.width - 40, screen.origin.y + screen.size.height - 40)
        cx, cy = center
        right = cx > screen.origin.x + screen.size.width / 2
        upper = cy > screen.origin.y + screen.size.height / 2
        x = cx + 26 - W if right else cx - 26
        y = cy + 26 - H if upper else cy - 26
        try:
            from mint.ui import notch
            if notch.enabled() and getattr(notch.notch, "top", None):
                # Dynamic Island mode: hang centred under the notch, not off a corner of it.
                n = notch.notch
                x, y = n.cx - W / 2, (n.top - getattr(n, "nh", 32)) - 10 - H
        except Exception:
            pass
        x = min(max(x, screen.origin.x + 8), screen.origin.x + screen.size.width - W - 8)
        y = min(max(y, screen.origin.y + 8), screen.origin.y + screen.size.height - H - 8)
        return AppKit.NSMakeRect(x, y, W, H), (cx, cy)

    def _orb_alpha(self, alpha: float, seconds: float) -> None:
        hud = self._hud()
        if hud is None or getattr(hud, "_orb_window", None) is None:
            return
        if alpha > 0 and getattr(hud, "island_active", False):
            return                                               # the island keeps the orb hidden
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(seconds)
        hud._orb_window.animator().setAlphaValue_(alpha)
        AppKit.NSAnimationContext.endGrouping()

    def _morph(self, opening: bool) -> None:
        """Grow out of (or shrink back into) the orb: the card scales from the orb's corner."""
        frame, (cx, cy) = self._frame()
        layer = self.card.layer()
        # Scale about the corner nearest the orb.
        ax = 1.0 if cx > frame.origin.x + W / 2 else 0.0
        ay = 1.0 if cy > frame.origin.y + H / 2 else 0.0
        layer.setAnchorPoint_(Quartz.CGPointMake(ax, ay))
        layer.setPosition_(Quartz.CGPointMake(ax * W, ay * H))
        grow = Quartz.CASpringAnimation.animationWithKeyPath_("transform.scale")
        grow.setDamping_(20)
        grow.setStiffness_(260)
        grow.setMass_(1)
        grow.setFromValue_(0.12 if opening else 1.0)
        grow.setToValue_(1.0 if opening else 0.12)
        grow.setDuration_(0.45 if opening else 0.25)
        grow.setFillMode_(Quartz.kCAFillModeForwards)
        grow.setRemovedOnCompletion_(opening)
        layer.addAnimation_forKey_(grow, "morph")
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.16 if opening else 0.2)
        self.panel.animator().setAlphaValue_(1.0 if opening else 0.0)
        AppKit.NSAnimationContext.endGrouping()

    def show(self) -> None:
        if self.panel is None:
            self._build()
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None and front.processIdentifier() != os.getpid():
            self.previous_app = front
        clip_tools._load()
        self.version = -1
        self._place_x()
        frame, _center = self._frame()
        self.panel.setFrame_display_(frame, False)
        self.panel.setAlphaValue_(0.0)
        self.tick(force=True)
        self._orb_alpha(0.0, 0.12)
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self._morph(True)
        self.panel.makeFirstResponder_(self.panel.contentView())

    def close(self) -> None:
        if self.panel is None or not self.panel.isVisible():
            return
        self.sticky = False
        self.hide_preview()
        self._morph(False)

        def done():
            self.panel.orderOut_(None)
            self.card.layer().removeAnimationForKey_("morph")
            self._orb_alpha(1.0, 0.18)
        AppHelper.callLater(0.24, done)

    def toggle(self) -> None:
        if self.panel is not None and self.panel.isVisible():
            self.close()
        else:
            self.show()

    def tick(self, force: bool = False) -> None:
        if self.panel is None or (not force and not self.panel.isVisible()):
            return
        mouse = AppKit.NSEvent.mouseLocation()
        frame = self.panel.frame()
        fx, fy = frame.origin.x + W - 26, frame.origin.y + H - 26
        self.face.tick(time.monotonic(), 0.0, (mouse.x - fx, mouse.y - fy), False, False)
        gfx.track_close(self.x_button, AppKit.NSPointInRect(mouse, frame), mouse)
        if force or clip_tools.VERSION[0] != self.version:
            self.version = clip_tools.VERSION[0]
            self.rebuild()
        self._hover_preview(mouse)

    # --- what is listed ----------------------------------------------------------------------------

    def clips(self) -> list[dict]:
        clip_tools._load()
        clip_tools.drop_old_secrets()
        if self.filter == "pinned":
            rows = [dict(p, id="pin:" + p["name"].lower(), source="pinned")
                    for p in sorted(clip_tools._pins().values(), key=lambda p: -p.get("at", 0))]
        elif self.filter == "mint":
            rows = [h for h in clip_tools.HISTORY if h.get("source") in ("mint", "screenshot")]
        else:
            rows = list(clip_tools.HISTORY)
        if self.search:
            rows = [h for h in rows if self.search in (h.get("label", "") + " " + ("" if h.get("secret") else h.get("text", "")) + " "
                                                        + h.get("name", "") + " " + " ".join(h.get("files", []))).lower()]
        return rows

    def _find(self, clip_id: str) -> dict | None:
        if clip_id.startswith("pin:"):
            pin = clip_tools._pins().get(clip_id[4:])
            return dict(pin, id=clip_id, source="pinned") if pin else None
        return clip_tools.entry(clip_id)

    def set_filter(self, key: str) -> None:
        self.filter = key
        self.selected = []
        self._paint_tabs()
        self.rebuild()

    def _paint_tabs(self) -> None:
        mint = tuple(gfx.accent())
        pins = len(clip_tools._pins())
        for tab, (key, words) in zip(self.tabs, TABS):
            on = key == self.filter
            text = f"{words}  {pins}" if key == "pinned" and pins else words
            tab.layer().setBackgroundColor_(_cg(mint, 0.95) if on else _cg((1, 1, 1), 0.07))
            tab.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                text, {AppKit.NSForegroundColorAttributeName: AppKit.NSColor.blackColor() if on
                       else _ns(INK, 0.8), AppKit.NSFontAttributeName: _font(12, AppKit.NSFontWeightSemibold)}))

    def rebuild(self) -> None:
        for view in list(self.list.subviews()):
            view.removeFromSuperview()
        rows = self.clips()
        self.shown_ids = [h["id"] for h in rows]
        self.selected = [i for i in self.selected if i in self.shown_ids]
        width = self.scroll.contentSize().width
        self.list.setFrame_(AppKit.NSMakeRect(0, 0, width, max(len(rows) * ROW + 8, self.scroll.contentSize().height)))
        for i, h in enumerate(rows):
            self._row(h, i, width)
        if not rows:
            words = {"pinned": f"No pins yet. Select clips and press Pin (up to {MAX_PINS}).",
                     "mint": "Nothing from Mint yet."}.get(self.filter, "Copy something (⌘C) or take a screenshot.")
            empty = _label("No clip matches that." if self.search else words, 12, AppKit.NSFontWeightMedium, DIM)
            empty.setAlignment_(AppKit.NSTextAlignmentCenter)
            empty.setFrame_(AppKit.NSMakeRect(20, 80, width - 40, 18))
            self.list.addSubview_(empty)
        total, n = len(clip_tools.HISTORY), len(self.selected)
        self.count.setStringValue_(f"{n} selected" if n else f"{total} clip{'s' * (total != 1)}")
        self.count.setTextColor_(_ns(tuple(gfx.accent())) if n else _ns(DIM))
        self._paint_tabs()
        self._paint_bar()

    def _row(self, h: dict, i: int, width: float) -> None:
        mint = tuple(gfx.accent())
        row = _ClipboardRow.alloc().initWithFrame_(AppKit.NSMakeRect(10, 2 + i * ROW, width - 20, ROW - 4))
        row.owner, row.clip_id = self, h["id"]
        row.setWantsLayer_(True)
        row.layer().setCornerRadius_(12)
        chosen = h["id"] in self.selected
        row.layer().setBackgroundColor_(_cg(mint, 0.18) if chosen else _cg((1, 1, 1), 0.06))
        if chosen:
            row.layer().setBorderColor_(_cg(mint, 0.7))
            row.layer().setBorderWidth_(1)
        self.list.addSubview_(row)
        mid = (ROW - 4) / 2
        thumb = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(8, mid - 16, 32, 32))
        thumb.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        thumb.setWantsLayer_(True)
        thumb.layer().setCornerRadius_(8)
        thumb.layer().setMasksToBounds_(True)
        if h.get("image") and Path(h["image"]).exists():
            thumb.setImageScaling_(AppKit.NSImageScaleAxesIndependently)
            thumb.setImage_(AppKit.NSImage.alloc().initWithContentsOfFile_(h["image"]))
        elif h["kind"] == "files" and h.get("files"):
            thumb.setImage_(AppKit.NSWorkspace.sharedWorkspace().iconForFile_(h["files"][0]))
        else:
            thumb.setImageScaling_(AppKit.NSImageScaleNone)
            thumb.setImage_(gfx.symbol("text.alignleft", 13))
            thumb.setContentTintColor_(_ns(DIM))
            thumb.layer().setBackgroundColor_(_cg((1, 1, 1), 0.08))
        row.addSubview_(thumb)
        title, detail = self._words(h)
        text_w = width - 20 - 50 - 46 - 34
        name = _label(title, 13, AppKit.NSFontWeightSemibold)
        name.setFrame_(AppKit.NSMakeRect(50, mid - 17, text_w, 18))
        row.addSubview_(name)
        words, rgb = SOURCES.get(h.get("source", "you"), SOURCES["you"])
        parts = [words] + ([detail] if detail else []) + [_ago(h.get("at", time.time()))]
        sub = _label(" · ".join(parts), 11, AppKit.NSFontWeightMedium, rgb or mint, 0.9)
        sub.setFrame_(AppKit.NSMakeRect(50, mid + 2, text_w, 15))
        row.addSubview_(sub)
        # On the right, a round checkbox: empty, or filled with its place in the paste order.
        check = gfx.number_badge(str(self.selected.index(h["id"]) + 1) if chosen else "", 20,
                                 _cg(mint) if chosen else None, _cg((0.03, 0.08, 0.06)), 11,
                                 ring=None if chosen else _cg((1, 1, 1), 0.28))
        check.setPosition_(Quartz.CGPointMake(width - 20 - 22, mid))           # (its centre: W - 32 on the card)
        check.setGeometryFlipped_(True)                        # the row is flipped: flip back for the text
        row.layer().addSublayer_(check)
        # Copy: on the clipboard and to the top of the list, for pasting later (nothing is pasted now).
        just = h["id"] == self.copied_id and time.monotonic() - self.copied_at < 1.4
        copy = MintClipRowButton.buttonWithImage_target_action_(gfx.symbol("checkmark" if just else "doc.on.doc", 11),
                                                         self.target, "copyRow:")
        copy.clip_id = h["id"]
        copy.setBordered_(False)
        copy.setFrame_(AppKit.NSMakeRect(width - 20 - 32 - 8 - 26, mid - 13, 26, 26))
        copy.setWantsLayer_(True)
        copy.layer().setCornerRadius_(13)
        copy.layer().setBackgroundColor_(_cg(mint, 0.9) if just else _cg((1, 1, 1), 0.09))
        copy.setContentTintColor_(AppKit.NSColor.blackColor() if just else _ns(INK, 0.85))
        copy.setToolTip_("Copy - it goes to the top, ready to paste with ⌘V")
        row.addSubview_(copy)
        row.setToolTip_("Rest here for a preview · ⧉ copies · ◯ picks several")

    @staticmethod
    def _words(h: dict) -> tuple[str, str]:
        if h.get("source") == "pinned":
            return h.get("name", "Pinned"), h.get("label", "")[:60]
        if h.get("secret"):
            return h.get("label", "Secret"), f"key or token · copy works · gone in {clip_tools.SECRET_MINUTES} min, never saved"
        if h["kind"] == "text":
            text = " ".join(h.get("text", "").split())
            return text[:80] or "(blank)", f"{len(h.get('text', ''))} characters"
        if h["kind"] == "files":
            files = h.get("files", [])
            return (os.path.basename(files[0]) if files else "Files") + (f" +{len(files) - 1}" if len(files) > 1 else ""), \
                f"{len(files)} file{'s' * (len(files) != 1)}"
        label = h.get("label", "Picture")
        return (label.split(" · ")[0].capitalize() if h.get("source") == "screenshot" else "Picture"), \
            " · ".join(label.split(" · ")[1:2])

    # --- choosing ----------------------------------------------------------------------------------

    def click(self, clip_id: str, cmd: bool, shift: bool) -> None:
        if cmd:
            if clip_id in self.selected:
                self.selected.remove(clip_id)
            else:
                self.selected.append(clip_id)
        elif shift and self.anchor in self.shown_ids:
            a, b = sorted((self.shown_ids.index(self.anchor), self.shown_ids.index(clip_id)))
            for i in self.shown_ids[a:b + 1]:
                if i not in self.selected:
                    self.selected.append(i)
        else:
            self.selected = [] if self.selected == [clip_id] else [clip_id]
        self.anchor = clip_id
        self.rebuild()

    def key(self, event) -> bool:
        chars = str(event.charactersIgnoringModifiers() or "")
        flags = int(event.modifierFlags())
        if chars == "a" and flags & AppKit.NSEventModifierFlagCommand:
            self.selected = list(self.shown_ids)
            self.rebuild()
            return True
        if chars == "c" and flags & AppKit.NSEventModifierFlagCommand:
            self.copy_selection()
            return True
        if int(event.keyCode()) in (51, 117) and self.selected:          # delete
            self.delete_selection()
            return True
        if int(event.keyCode()) == 49 and not self.field.currentEditor():  # space: the preview, Quick Look style
            if self.preview_id and self.preview is not None and self.preview.isVisible():
                self.sticky = False
                self.hide_preview()
            else:
                clip_id = self.selected[0] if self.selected else self.hover_id
                if clip_id:
                    self.sticky = True
                    self.show_preview(clip_id)
            return True
        if int(event.keyCode()) == 53:                                   # esc
            if self.preview is not None and self.preview.isVisible():
                self.sticky = False
                self.hide_preview()
                return True
            self.close()
            return True
        return False

    def _paint_bar(self) -> None:
        n = len(self.selected)
        if time.monotonic() < getattr(self, "flash_until", 0.0):
            return                                              # a "Copied" note is showing: leave it
        self.hint.setStringValue_("Hover to preview · ⧉ copy · double-click to paste")
        self.hint.setHidden_(bool(n))
        for button in self.actions:
            button.setHidden_(not n)

    # --- doing ------------------------------------------------------------------------------------

    def copy_selection(self) -> None:
        clips = [c for c in (self._find(i) for i in self.selected) if c]
        if not clips:
            return
        clip_tools._restoring[0] = True
        try:
            if len(clips) == 1:
                clip_tools._put_back(clips[0])
            elif all(c["kind"] == "text" for c in clips):
                clip_tools._put_text("\n\n".join(c.get("text", "") for c in clips))
            else:
                # Several pictures or files: as files, so one paste attaches them all.
                paths = []
                for c in clips:
                    paths += c.get("files") or ([c["image"]] if c.get("image") else [])
                clip_tools._put_files(paths)
        finally:
            clip_tools._restoring[0] = False
        self._flash(f"Copied {len(clips)} clip{'s' * (len(clips) != 1)} - paste with ⌘V")

    def paste(self, ids: list[str]) -> None:
        clips = [c for c in (self._find(i) for i in ids) if c]
        if not clips:
            return
        self.close()
        app = self.previous_app

        def run():
            from mint.tools.fastinput import press_key
            from mint.tools.harness import _password_field
            time.sleep(0.25)
            if app is not None and not app.isTerminated():
                app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
                time.sleep(0.3)
            if _password_field():
                return
            for c in clips:
                if clip_tools._put_back(c):
                    continue
                press_key("v", ["command"])
                time.sleep(0.45 if c["kind"] == "image" else 0.2)
        threading.Thread(target=run, daemon=True, name="clipboard-paste").start()

    def pin_selection(self) -> None:
        pins = clip_tools._pins()
        added, full = 0, False
        for c in (self._find(i) for i in self.selected):
            if not c or c.get("source") == "pinned":
                continue
            name = self._words(c)[0][:40]
            if name.lower() not in pins and len(pins) >= MAX_PINS:
                full = True
                break
            pins[name.lower()] = dict(c, name=name, at=time.time())
            added += 1
        clip_tools._save_pins(pins)
        clip_tools.VERSION[0] += 1
        if full:
            self._flash(f"Pins are full ({MAX_PINS}) - unpin one first")
        elif added:
            self._flash(f"Pinned {added} · {len(pins)} of {MAX_PINS}")

    def delete_selection(self) -> None:
        ids = list(self.selected)
        pins = clip_tools._pins()
        for i in ids:
            if i.startswith("pin:"):
                pins.pop(i[4:], None)
        clip_tools._save_pins(pins)
        clip_tools.delete([i for i in ids if not i.startswith("pin:")])
        self.selected = []
        self.rebuild()

    def _flash(self, text: str) -> None:
        self.flash_until = time.monotonic() + 1.8
        self.hint.setStringValue_(text)
        self.hint.setHidden_(False)
        for button in self.actions:
            button.setHidden_(True)
        AppHelper.callLater(1.85, self._paint_bar)

    def copy_one(self, clip_id: str) -> None:
        """The row's Copy: on the clipboard, and the clip moves to the top of the list. Nothing is pasted."""
        c = self._find(clip_id)
        if not c:
            return
        why = clip_tools._put_back(c)
        if why:
            self._flash(why.replace("FAILED: ", "").capitalize())
            return
        if not clip_id.startswith("pin:"):
            clip_tools.to_top(clip_id)
        self.version = clip_tools.VERSION[0]                   # (already shown: no second rebuild)
        self.copied_id, self.copied_at = clip_id, time.monotonic()
        self.selected = []
        self.rebuild()
        self.list.scrollPoint_(AppKit.NSMakePoint(0, 0))
        AppHelper.callLater(1.5, self.rebuild)                 # the ✓ goes back to the copy icon
        self._flash("Copied - it's at the top · paste anywhere with ⌘V")

    # --- the preview beside the card ------------------------------------------------------------------

    def _hover_row(self, mouse) -> str:
        """The clip under the pointer ('' when it isn't over the list)."""
        if self.panel is None or not AppKit.NSPointInRect(mouse, self.panel.frame()):
            return ""
        visible = self.scroll.contentView().documentVisibleRect()
        point = self.list.convertPoint_fromView_(self.panel.convertPointFromScreen_(mouse), None)
        if not AppKit.NSPointInRect(point, visible):
            return ""
        i = int((point.y - 2) // ROW)
        return self.shown_ids[i] if 0 <= i < len(self.shown_ids) else ""

    def _hover_preview(self, mouse) -> None:
        now = time.monotonic()
        hover = self._hover_row(mouse)
        on_preview = self.preview is not None and self.preview.isVisible() and \
            AppKit.NSPointInRect(mouse, self.preview.frame())
        if hover != self.hover_id:
            self.hover_id, self.hover_since = hover, now
        if hover:
            self.left_at = 0.0
            if (hover != self.preview_id or not self.preview.isVisible()) and now - self.hover_since > PREVIEW_AFTER:
                self.sticky = False
                self.show_preview(hover)
        elif not on_preview and not self.sticky and self.preview is not None and self.preview.isVisible():
            self.left_at = self.left_at or now
            if now - self.left_at > 0.2:
                self.hide_preview()

    def _preview_panel(self):
        if self.preview is None:
            panel = MintClipPreviewPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                AppKit.NSMakeRect(0, 0, PREVIEW_W, 200), AppKit.NSWindowStyleMaskBorderless
                | AppKit.NSWindowStyleMaskNonactivatingPanel, AppKit.NSBackingStoreBuffered, False)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(AppKit.NSColor.clearColor())
            panel.setHasShadow_(True)
            panel.setLevel_(AppKit.NSStatusWindowLevel)
            panel.setHidesOnDeactivate_(False)
            panel.setReleasedWhenClosed_(False)
            panel.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                         | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
            panel.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
            try:
                from mint.ui.effects import SHARING
                panel.setSharingType_(SHARING)
            except Exception:
                pass
            self.preview = panel
        return self.preview

    def show_preview(self, clip_id: str) -> None:
        c = self._find(clip_id)
        if c is None or self.panel is None:
            return
        panel = self._preview_panel()
        inner_w = PREVIEW_W - 24
        root = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, PREVIEW_W, 100))
        root.setWantsLayer_(True)
        layer = root.layer()
        layer.setBackgroundColor_(_cg(SURFACE))
        layer.setCornerRadius_(18)
        layer.setBorderColor_(_cg((1, 1, 1), EDGE))
        layer.setBorderWidth_(1.0)
        layer.setMasksToBounds_(True)
        title, detail = self._words(c)
        caption = _label(title, 12, AppKit.NSFontWeightSemibold)
        sub = _label(detail or "", 11, AppKit.NSFontWeightMedium, DIM)
        picture = None
        if c.get("image") and Path(c["image"]).exists():
            picture = c["image"]
        elif c["kind"] == "files" and c.get("files") and Path(c["files"][0]).suffix.lower() in IMAGE_EXTS \
                and Path(c["files"][0]).exists():
            picture = c["files"][0]
        if picture:
            image = AppKit.NSImage.alloc().initWithContentsOfFile_(picture)
            size = image.size() if image is not None else AppKit.NSMakeSize(1, 1)
            scale = min(inner_w / max(size.width, 1), (PREVIEW_MAX_H - 64) / max(size.height, 1), 1.0 if
                        size.width > 120 else 3.0)
            w, h = max(40, size.width * scale), max(30, size.height * scale)
            body_h = h
            view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect((PREVIEW_W - w) / 2, 46, w, h))
            view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            view.setImage_(image)
            view.setWantsLayer_(True)
            view.layer().setCornerRadius_(10)
            view.layer().setMasksToBounds_(True)
            view.layer().setBorderColor_(_cg((1, 1, 1), 0.14))
            view.layer().setBorderWidth_(0.5)
            if size.width and size.height:
                sub.setStringValue_(f"{int(size.width)} × {int(size.height)}" + (f" · {detail}" if detail else ""))
        else:
            if c["kind"] == "files":
                text = "\n".join(c.get("files", [])[:40])
            elif c.get("source") == "pinned" and c["kind"] != "text" or c.get("secret"):
                text = c.get("label", "")         # a key or token is never shown in full
            else:
                text = c.get("text", "") or c.get("label", "")
            text = text[:20000]
            scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(12, 46, inner_w, 100))
            scroll.setDrawsBackground_(False)
            scroll.setHasVerticalScroller_(True)
            scroll.setAutohidesScrollers_(True)
            scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
            words = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, inner_w, 100))
            words.setEditable_(False)
            words.setSelectable_(True)
            words.setDrawsBackground_(False)
            words.setTextContainerInset_(AppKit.NSMakeSize(0, 2))
            words.textContainer().setWidthTracksTextView_(True)
            words.setString_(text)
            words.setFont_(_font(12.5))
            words.setTextColor_(_ns(INK, 0.92))
            words.layoutManager().ensureLayoutForTextContainer_(words.textContainer())
            used = words.layoutManager().usedRectForTextContainer_(words.textContainer()).size.height + 8
            body_h = max(24, min(PREVIEW_MAX_H - 64, used))
            scroll.setFrame_(AppKit.NSMakeRect(12, 46, inner_w, body_h))
            words.setFrame_(AppKit.NSMakeRect(0, 0, inner_w, max(used, body_h)))
            scroll.setDocumentView_(words)
            view = scroll
        total = body_h + 46 + 14
        root.setFrame_(AppKit.NSMakeRect(0, 0, PREVIEW_W, total))
        view.setFrameOrigin_(AppKit.NSMakePoint(view.frame().origin.x, total - 12 - body_h))
        root.addSubview_(view)
        caption.setFrame_(AppKit.NSMakeRect(14, 22, PREVIEW_W - 28, 16))
        sub.setFrame_(AppKit.NSMakeRect(14, 8, PREVIEW_W - 28, 14))
        root.addSubview_(caption)
        root.addSubview_(sub)
        panel.setContentView_(root)
        # Beside the card, on the side with room, level with the clip.
        card = self.panel.frame()
        screen = (self.panel.screen() or AppKit.NSScreen.mainScreen()).visibleFrame()
        left = card.origin.x + W / 2 > screen.origin.x + screen.size.width / 2
        x = card.origin.x - PREVIEW_W - 10 if left else card.origin.x + W + 10
        y_mid = card.origin.y + card.size.height / 2
        if clip_id in self.shown_ids:
            i = self.shown_ids.index(clip_id)
            point = self.list.convertPoint_toView_(AppKit.NSMakePoint(0, 2 + i * ROW + ROW / 2), None)
            y_mid = self.panel.convertPointToScreen_(point).y
        y = min(max(y_mid - total / 2, screen.origin.y + 8), screen.origin.y + screen.size.height - total - 8)
        panel.setFrame_display_(AppKit.NSMakeRect(x, y, PREVIEW_W, total), True)
        if not panel.isVisible():
            panel.setAlphaValue_(0.0)
            panel.orderFront_(None)
            AppKit.NSAnimationContext.beginGrouping()
            AppKit.NSAnimationContext.currentContext().setDuration_(0.14)
            panel.animator().setAlphaValue_(1.0)
            AppKit.NSAnimationContext.endGrouping()
        self.preview_id = clip_id

    def hide_preview(self) -> None:
        self.preview_id = ""
        self.left_at = 0.0
        panel = self.preview
        if panel is None or not panel.isVisible():
            return
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.1)
        panel.animator().setAlphaValue_(0.0)
        AppKit.NSAnimationContext.endGrouping()
        AppHelper.callLater(0.11, lambda: self.preview_id or panel.orderOut_(None))

    # --- dragging a clip out ------------------------------------------------------------------------

    def drag_item(self, clip_id: str):
        c = self._find(clip_id)
        if c is None:
            return None
        if c["kind"] == "text":
            return AppKit.NSString.stringWithString_(c.get("text", ""))
        path = (c.get("files") or [c.get("image")])[0]
        return AppKit.NSURL.fileURLWithPath_(path) if path else None

    def drag_picture(self, clip_id: str):
        c = self._find(clip_id) or {}
        if c.get("image") and Path(c["image"]).exists():
            return AppKit.NSImage.alloc().initWithContentsOfFile_(c["image"])
        if c.get("files"):
            return AppKit.NSWorkspace.sharedWorkspace().iconForFile_(c["files"][0])
        return gfx.symbol("text.alignleft", 24)


window = ClipboardWindow()


def show() -> None:
    """From any thread."""
    AppHelper.callAfter(window.show)


def toggle() -> None:
    AppHelper.callAfter(window.toggle)
