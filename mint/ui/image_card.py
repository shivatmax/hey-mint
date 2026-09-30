"""The image card: after Mint makes a picture it grows out of the orb as a small dark card to work on it.

    [ prompt as the title            × (•) ]      the little Mint face sits where the orb was
    [                                      ]
    [          the picture, 320 px         ]      drag it out into any app
    [                                      ]
    [ ▢ ▢ ▢  versions: variations, edits    ]      click one to go back to it
    [ Describe a change…                 ↑ ]      Return redraws the picture with the change
    [ Save…  Another  Improve  Copy  Playground ]

* Type a change and press Return: Image Playground redraws the picture (a shimmer and "Redrawing…" on it
  meanwhile) and the new one joins the versions strip, so earlier ones are a click away.
* Save… asks where (the Desktop by default, named after the prompt). Another draws the same prompt again,
  Improve has Gemini rewrite the prompt with more detail and draws that, Copy puts the picture on the
  clipboard (as Mint's clip), Playground opens it in the Image Playground app (Visual Edit, captions).
* × , Esc, ⌘W or a click on the face close it. Voice drives the same card through the make_image tool
  (imagegen.py): "make the sky purple", "save it to my Desktop", "make another one", "close it".

The work is done by imagegen.py; this module only shows it. Everything here runs on the main thread;
the module-level functions (start, add, fail, flash, close) can be called from any thread.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx

log = logging.getLogger("mint.ui.image_card")

W = 372                    # the clipboard card's width
PAD = 20
IMG = 332                  # the picture's box
FACE = 24
THUMB = 44
TILE_H = 50
SURFACE = (0.035, 0.035, 0.045)
INK = (1.0, 1.0, 1.0)
DIM = (0.62, 0.63, 0.68)
ORANGE = (1.0, 0.62, 0.04)
TILES = (("save", "Save…", "square.and.arrow.down", "Save a copy (⌘S)"),
         ("another", "Another", "arrow.triangle.2.circlepath", "Draw the same prompt again"),
         ("improve", "Improve", "wand.and.sparkles", "Rewrite the prompt with more detail, then draw it"),
         ("copy", "Copy", "doc.on.doc", "Copy the picture"),
         ("playground", "Playground", "apple.image.playground", "Open in Image Playground (Visual Edit, captions)"))
HINT = "Return redraws  ·  drag the picture out  ·  Esc closes"


def _cg(rgb, alpha=1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def _font(size, weight=AppKit.NSFontWeightRegular, rounded=False):
    font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
    if rounded:
        descriptor = font.fontDescriptor().fontDescriptorWithDesign_(AppKit.NSFontDescriptorSystemDesignRounded)
        if descriptor is not None:
            font = AppKit.NSFont.fontWithDescriptor_size_(descriptor, size) or font
    return font


def _label(text, size=13, weight=AppKit.NSFontWeightRegular, rgb=INK, alpha=1.0, rounded=False):
    field = AppKit.NSTextField.labelWithString_(text)
    field.setFont_(_font(size, weight, rounded))
    field.setTextColor_(_ns(rgb, alpha))
    field.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    return field


def _words(text: str, color, size=12, weight=AppKit.NSFontWeightSemibold, symbol: str = ""):
    """A button title (optionally with its SF Symbol inline), in one colour."""
    out = AppKit.NSMutableAttributedString.alloc().init()
    if symbol:
        config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(size - 1, weight)
        config = config.configurationByApplyingConfiguration_(
            AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_([color]))
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
        if image is not None:
            attachment = AppKit.NSTextAttachment.alloc().init()
            attachment.setImage_(image.imageWithSymbolConfiguration_(config))
            out.appendAttributedString_(AppKit.NSAttributedString.attributedStringWithAttachment_(attachment))
            text = "  " + text
    style = AppKit.NSMutableParagraphStyle.alloc().init()
    style.setAlignment_(AppKit.NSTextAlignmentCenter)
    out.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
        text, {AppKit.NSForegroundColorAttributeName: color, AppKit.NSFontAttributeName: _font(size, weight),
               AppKit.NSParagraphStyleAttributeName: style}))
    return out


def _people(text: str) -> str:
    """imagegen's messages are written for the model ("FAILED: ... Tell the user."): the part for people."""
    text = str(text or "").strip()
    if text.startswith("NOT DONE YET"):
        return "Mint needs its Image Playground shortcut: click “Add Shortcut” in Shortcuts, then try again."
    text = re.sub(r"^(FAILED|REFUSED|BUSY)\s*:\s*", "", text)
    text = re.sub(r"\s*\b(Tell (the user|them)|Say |Don't|Do not|do not)[^.]*\.", "", text)
    if re.sub(r"\s*\([^)]*\)\s*$", "", text).strip():
        text = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    text = text.strip()
    return (text[:1].upper() + text[1:]) if text else "Something went wrong."


class _ImageCardPanel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return True

    def keyDown_(self, event):
        owner = getattr(self, "owner", None)
        if owner is None or not owner.key(event):
            objc.super(_ImageCardPanel, self).keyDown_(event)

    def performKeyEquivalent_(self, event):
        owner = getattr(self, "owner", None)
        if owner is not None and owner.shortcut(event):
            return True
        return objc.super(_ImageCardPanel, self).performKeyEquivalent_(event)

    def cancelOperation_(self, sender):
        self.owner.close()


class _ImageCardButton(AppKit.NSButton):
    """Takes the first click, even when Mint isn't the active app."""

    def acceptsFirstMouse_(self, event):
        return True


class _ImageCardStrip(AppKit.NSView):
    pass


class _ImageCardPicture(AppKit.NSImageView):
    """The picture: drag it out into any app (as the PNG file)."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self.dragging = False

    def mouseDragged_(self, event):
        owner = getattr(self, "owner", None)
        path = owner.current() if owner is not None else None
        if getattr(self, "dragging", False) or path is None or not path.exists():
            return
        self.dragging = True
        item = AppKit.NSDraggingItem.alloc().initWithPasteboardWriter_(AppKit.NSURL.fileURLWithPath_(str(path)))
        where = self.convertPoint_fromView_(event.locationInWindow(), None)
        item.setDraggingFrame_contents_(AppKit.NSMakeRect(where.x - 40, where.y - 40, 80, 80), self.image())
        self.beginDraggingSessionWithItems_event_source_([item], event, self)

    def mouseUp_(self, event):
        self.dragging = False

    def draggingSession_sourceOperationMaskForDraggingContext_(self, session, context):
        return AppKit.NSDragOperationCopy

    def draggingSession_endedAtPoint_operation_(self, session, point, operation):
        self.dragging = False


class _ImageCardTarget(AppKit.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(_ImageCardTarget, self).init()
        if self is None:
            return None
        self.owner = owner
        return self

    def tile_(self, sender):
        self.owner.act(TILES[int(sender.tag())][0])

    def pick_(self, sender):
        self.owner.pick(int(sender.tag()))

    def submit_(self, sender):
        self.owner.submit()

    def dismissError_(self, sender):
        self.owner.error = ""
        self.owner.refresh()

    def close_(self, sender):
        self.owner.close()

    def tick_(self, timer):
        try:
            self.owner.tick()
        except Exception:
            log.debug("image card tick", exc_info=True)

    # The text field's delegate: Esc in it closes the card (a text view would otherwise try to complete).
    def control_textView_doCommandBySelector_(self, control, text_view, selector):
        if str(selector).strip("b'") == "cancelOperation:":
            self.owner.close()
            return True
        return False


class ImageCard:
    def __init__(self) -> None:
        self.panel = None
        self.versions: list[dict] = []      # {"path", "prompt", "note"}, oldest first
        self.index = -1
        self.style = ""
        self.title_hint = ""                # the title while there is no picture yet
        self.busy = ""                      # "Redrawing…" while imagegen works
        self.busy_detail = ""
        self.error = ""
        self.previous_app = None
        self.hud = None
        self.height = 0
        self._images: dict[str, object] = {}
        self._flash_until = 0.0
        self.opened = False

    # --- state ----------------------------------------------------------------------------------

    def is_open(self) -> bool:
        """Shown and not closing (a plain flag: imagegen asks from its own thread)."""
        return self.opened

    def current(self) -> Path | None:
        if 0 <= self.index < len(self.versions):
            return Path(self.versions[self.index]["path"])
        return None

    def version(self) -> dict:
        return self.versions[self.index] if 0 <= self.index < len(self.versions) else {}

    def _image(self, path: str):
        if path not in self._images:
            self._images[path] = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
        return self._images[path]

    # --- building -------------------------------------------------------------------------------

    def _build(self) -> None:
        H = 600
        panel = _ImageCardPanel.alloc().initWithContentRect_styleMask_backing_defer_(
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
        layer.setCornerRadius_(24)
        layer.setBorderColor_(_cg((1, 1, 1), 0.09))
        layer.setBorderWidth_(0.5)
        layer.setMasksToBounds_(True)
        card = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, W, H))
        card.setWantsLayer_(True)
        root.addSubview_(card)
        panel.setContentView_(root)
        self.panel, self.root, self.card = panel, root, card
        self.target = _ImageCardTarget.alloc().initWithOwner_(self)
        mint = tuple(gfx.accent())

        # Header: the prompt and what this version is; × and Mint's little face in the corner.
        self.title = _label("", 15, AppKit.NSFontWeightBold, rounded=True)
        card.addSubview_(self.title)
        self.subtitle = _label("", 11, AppKit.NSFontWeightMedium, DIM)
        card.addSubview_(self.subtitle)
        self.x_button = self._round_button("xmark", "close:", 24, (1, 1, 1), 0.1, INK, 9, "Close (Esc)")
        card.addSubview_(self.x_button)
        from mint.ui.orb import Orb
        self.face_host = Quartz.CALayer.layer()
        card.layer().addSublayer_(self.face_host)
        self.face = Orb(self.face_host, (20, 20), FACE)
        self.face.apply_state("awake", True)
        self._quiet_face()
        self.face_button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 40, 40))
        self.face_button.setTransparent_(True)
        self.face_button.setTarget_(self.target)
        self.face_button.setAction_("close:")
        self.face_button.setToolTip_("Close (Esc)")
        card.addSubview_(self.face_button)

        # The picture's box: the picture, a shimmer while Mint works, or what went wrong.
        box = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(PAD, 0, IMG, IMG))
        box.setWantsLayer_(True)
        box.layer().setCornerRadius_(16)
        box.layer().setMasksToBounds_(True)
        box.layer().setBackgroundColor_(_cg((1, 1, 1), 0.05))
        card.addSubview_(box)
        self.box = box
        self.empty = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("photo", 40, "regular"))
        self.empty.setContentTintColor_(_ns(INK, 0.16))
        self.empty.setFrame_(AppKit.NSMakeRect(0, 0, IMG, IMG))
        box.addSubview_(self.empty)
        picture = _ImageCardPicture.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, IMG, IMG))
        picture.owner = self
        picture.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        picture.setWantsLayer_(True)
        picture.setLayerUsesCoreImageFilters_(True)
        box.addSubview_(picture)
        self.picture = picture
        self._build_busy(box, mint)
        self._build_error(box, mint)

        # The versions strip: variations and earlier edits, the one shown ringed.
        self.strip_scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(PAD, 0, IMG, THUMB + 4))
        self.strip_scroll.setDrawsBackground_(False)
        self.strip_scroll.setHasHorizontalScroller_(False)
        self.strip = _ImageCardStrip.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, IMG, THUMB + 4))
        self.strip_scroll.setDocumentView_(self.strip)
        card.addSubview_(self.strip_scroll)

        # "Describe a change…" with a send button.
        field_box = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(PAD, 0, IMG, 38))
        field_box.setWantsLayer_(True)
        field_box.layer().setCornerRadius_(12)
        field_box.layer().setBackgroundColor_(_cg((1, 1, 1), 0.07))
        field_box.layer().setBorderColor_(_cg((1, 1, 1), 0.08))
        field_box.layer().setBorderWidth_(0.5)
        card.addSubview_(field_box)
        self.field_box = field_box
        icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("wand.and.stars", 12))
        icon.setContentTintColor_(_ns(mint))
        icon.setFrame_(AppKit.NSMakeRect(11, 10, 18, 18))
        field_box.addSubview_(icon)
        field = AppKit.NSTextField.alloc().initWithFrame_(AppKit.NSMakeRect(34, 10, IMG - 34 - 40, 18))
        field.setBezeled_(False)
        field.setBordered_(False)
        field.setDrawsBackground_(False)
        field.setFocusRingType_(AppKit.NSFocusRingTypeNone)
        field.setFont_(_font(13, AppKit.NSFontWeightMedium))
        field.setTextColor_(_ns(INK))
        field.cell().setScrollable_(True)
        field.cell().setWraps_(False)
        field.setPlaceholderAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            "Describe a change…", {AppKit.NSForegroundColorAttributeName: _ns(DIM, 0.85),
                                   AppKit.NSFontAttributeName: _font(13, AppKit.NSFontWeightMedium)}))
        field.setTarget_(self.target)
        field.setAction_("submit:")
        field.setDelegate_(self.target)
        field_box.addSubview_(field)
        self.field = field
        self.send = self._round_button("arrow.up", "submit:", 28, mint, 1.0, (0, 0, 0), 11, "Redraw with this change")
        self.send.setFrame_(AppKit.NSMakeRect(IMG - 33, 5, 28, 28))
        field_box.addSubview_(self.send)

        # The actions: five tiles, centred and evenly spaced; Save is the main one.
        self.tiles = []
        for tag, (_key, words, symbol, tip) in enumerate(TILES):
            plate = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 60, TILE_H))
            plate.setWantsLayer_(True)
            plate.layer().setCornerRadius_(12)
            if hasattr(plate, "setClipsToBounds_"):
                plate.setClipsToBounds_(False)
            icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol(symbol, 15))
            icon.setImageAlignment_(AppKit.NSImageAlignCenter)
            plate.addSubview_(icon)
            words_label = _label(words, 10.5, AppKit.NSFontWeightSemibold)
            words_label.setAlignment_(AppKit.NSTextAlignmentCenter)
            words_label.setLineBreakMode_(AppKit.NSLineBreakByClipping)
            plate.addSubview_(words_label)
            hit = _ImageCardButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 60, TILE_H))
            hit.setTransparent_(True)
            hit.setTag_(tag)
            hit.setTarget_(self.target)
            hit.setAction_("tile:")
            hit.setToolTip_(tip)
            plate.addSubview_(hit)
            card.addSubview_(plate)
            self.tiles.append((plate, icon, words_label, hit))

        self.hint = _label(HINT, 11, AppKit.NSFontWeightMedium, DIM)
        self.hint.setAlignment_(AppKit.NSTextAlignmentCenter)
        card.addSubview_(self.hint)

        self.ticker = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 20, self.target, "tick:", None, True)

    def _round_button(self, symbol, action, size, bg, alpha, ink, symbol_size, tip):
        button = _ImageCardButton.buttonWithImage_target_action_(gfx.symbol(symbol, symbol_size, "bold"),
                                                                 self.target, action)
        button.setBordered_(False)
        button.setFrame_(AppKit.NSMakeRect(0, 0, size, size))
        button.setWantsLayer_(True)
        button.layer().setCornerRadius_(size / 2)
        button.layer().setBackgroundColor_(_cg(bg, alpha))
        button.setContentTintColor_(_ns(ink))
        button.setToolTip_(tip)
        return button

    def _build_busy(self, box, mint) -> None:
        """A dark veil with a light band sweeping across it, a spinner and what Mint is doing."""
        veil = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, IMG, IMG))
        veil.setWantsLayer_(True)
        veil.layer().setBackgroundColor_(_cg((0, 0, 0), 0.38))
        box.addSubview_(veil)
        band = Quartz.CAGradientLayer.layer()
        band.setFrame_(Quartz.CGRectMake(-IMG * 0.8, 0, IMG * 0.8, IMG))
        band.setStartPoint_(Quartz.CGPointMake(0, 0.5))
        band.setEndPoint_(Quartz.CGPointMake(1, 0.5))
        band.setColors_([_cg(mint, 0.0), _cg(mint, 0.07), _cg((1, 1, 1), 0.16), _cg(mint, 0.07), _cg(mint, 0.0)])
        veil.layer().addSublayer_(band)
        arc = Quartz.CAShapeLayer.layer()
        arc.setBounds_(Quartz.CGRectMake(0, 0, 26, 26))
        arc.setPosition_(Quartz.CGPointMake(IMG / 2, IMG / 2 + 22))
        arc.setPath_(Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(2, 2, 22, 22), None))
        arc.setFillColor_(None)
        arc.setStrokeColor_(_cg(mint, 0.95))
        arc.setLineWidth_(2.6)
        arc.setLineCap_(Quartz.kCALineCapRound)
        arc.setStrokeEnd_(0.7)
        veil.layer().addSublayer_(arc)
        self.busy_words = _label("", 14, AppKit.NSFontWeightSemibold, rounded=True)
        self.busy_words.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.busy_words.setFrame_(AppKit.NSMakeRect(16, IMG / 2 - 16, IMG - 32, 20))
        veil.addSubview_(self.busy_words)
        self.busy_more = _label("", 11, AppKit.NSFontWeightMedium, INK, 0.7)
        self.busy_more.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.busy_more.setFrame_(AppKit.NSMakeRect(24, IMG / 2 - 36, IMG - 48, 16))
        veil.addSubview_(self.busy_more)
        veil.setHidden_(True)
        self.veil, self.band, self.arc = veil, band, arc

    def _animate_busy(self, on: bool) -> None:
        for layer in (self.band, self.arc):
            layer.removeAllAnimations()
        if on:
            sweep = Quartz.CABasicAnimation.animationWithKeyPath_("transform.translation.x")
            sweep.setFromValue_(0.0)
            sweep.setToValue_(IMG * 1.8)
            sweep.setDuration_(1.6)
            sweep.setRepeatCount_(1e9)
            sweep.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                Quartz.kCAMediaTimingFunctionEaseInEaseOut))
            self.band.addAnimation_forKey_(sweep, "sweep")
            turn = Quartz.CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
            turn.setFromValue_(0.0)
            turn.setToValue_(-2 * 3.14159265)
            turn.setDuration_(0.9)
            turn.setRepeatCount_(1e9)
            self.arc.addAnimation_forKey_(turn, "spin")
        blur = None
        if on and self.current() is not None:
            blur = Quartz.CIFilter.filterWithName_("CIGaussianBlur")
            if blur is not None:
                blur.setDefaults()
                blur.setValue_forKey_(7.0, "inputRadius")
        self.picture.layer().setFilters_([blur] if blur is not None else [])

    def _build_error(self, box, mint) -> None:
        sheet = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, IMG, IMG))
        sheet.setWantsLayer_(True)
        sheet.layer().setBackgroundColor_(_cg(SURFACE, 0.86))
        box.addSubview_(sheet)
        icon = AppKit.NSImageView.imageViewWithImage_(gfx.symbol("exclamationmark.triangle.fill", 22))
        icon.setContentTintColor_(_ns(ORANGE))
        icon.setFrame_(AppKit.NSMakeRect(IMG / 2 - 16, IMG / 2 + 62, 32, 28))
        sheet.addSubview_(icon)
        text = AppKit.NSTextField.wrappingLabelWithString_("")
        text.setFont_(_font(12.5, AppKit.NSFontWeightMedium))
        text.setTextColor_(_ns(INK, 0.88))
        text.setAlignment_(AppKit.NSTextAlignmentCenter)
        text.setFrame_(AppKit.NSMakeRect(28, IMG / 2 - 20, IMG - 56, 76))
        sheet.addSubview_(text)
        open_it = _ImageCardButton.alloc().initWithFrame_(AppKit.NSMakeRect(IMG / 2 - 104, IMG / 2 - 64, 208, 32))
        open_it.setBordered_(False)
        open_it.setAttributedTitle_(_words("Open in Image Playground", AppKit.NSColor.blackColor(),
                                           symbol="apple.image.playground"))
        open_it.setWantsLayer_(True)
        open_it.layer().setCornerRadius_(16)
        open_it.layer().setBackgroundColor_(_cg(mint))
        open_it.setTag_(4)
        open_it.setTarget_(self.target)
        open_it.setAction_("tile:")
        sheet.addSubview_(open_it)
        back = _ImageCardButton.alloc().initWithFrame_(AppKit.NSMakeRect(IMG / 2 - 60, IMG / 2 - 102, 120, 28))
        back.setBordered_(False)
        back.setAttributedTitle_(_words("Back to the picture", _ns(INK, 0.75), 11.5))
        back.setTarget_(self.target)
        back.setAction_("dismissError:")
        sheet.addSubview_(back)
        sheet.setHidden_(True)
        self.error_sheet, self.error_text, self.error_back = sheet, text, back
        self.error_icon, self.error_open = icon, open_it

    def _place_error(self, with_back: bool) -> None:
        """Icon, words, button (and "Back to the picture"), centred as one group in the box."""
        text_h = self.error_text.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, IMG - 56, 400)).height
        text_h = min(max(text_h, 16), 110)
        group = 28 + 12 + text_h + 18 + 32 + (8 + 26 if with_back else 0)
        top = IMG / 2 + group / 2
        self.error_icon.setFrame_(AppKit.NSMakeRect(IMG / 2 - 16, top - 28, 32, 28))
        self.error_text.setFrame_(AppKit.NSMakeRect(28, top - 40 - text_h, IMG - 56, text_h))
        self.error_open.setFrame_(AppKit.NSMakeRect(IMG / 2 - 104, top - 58 - text_h - 32, 208, 32))
        self.error_back.setFrame_(AppKit.NSMakeRect(IMG / 2 - 70, top - 98 - text_h - 26, 140, 26))

    # --- layout -----------------------------------------------------------------------------------

    def _strip_on(self) -> bool:
        return len(self.versions) > 1

    def _height(self) -> int:
        h = 16 + 38 + 12 + IMG + 14 + 38 + 12 + TILE_H + 10 + 16 + 12
        return h + (THUMB + 4 + 10 if self._strip_on() else 0)

    def _layout(self) -> None:
        H = self._height()
        self.height = H
        self.card.setFrame_(AppKit.NSMakeRect(0, 0, W, H))

        def at(view, top, h, x=PAD, w=IMG):
            view.setFrame_(AppKit.NSMakeRect(x, H - top - h, w, h))

        at(self.title, 16, 20, PAD, W - PAD - 82)
        at(self.subtitle, 37, 16, PAD, W - PAD - 82)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.face_host.setFrame_(Quartz.CGRectMake(W - 46, H - 46, 40, 40))
        Quartz.CATransaction.commit()
        self.face_button.setFrame_(AppKit.NSMakeRect(W - 46, H - 46, 40, 40))
        self.x_button.setFrame_(AppKit.NSMakeRect(W - 46 - 28, H - 38, 24, 24))
        y = 16 + 38 + 12
        at(self.box, y, IMG)
        y += IMG + 14
        self.strip_scroll.setHidden_(not self._strip_on())
        if self._strip_on():
            at(self.strip_scroll, y - 4, THUMB + 4, PAD - 2, IMG + 4)
            y += THUMB + 4 + 10 - 4
        at(self.field_box, y, 38)
        y += 38 + 12
        gap = 6
        tile_w = (IMG - gap * (len(self.tiles) - 1)) / len(self.tiles)
        for n, (plate, icon, words_label, hit) in enumerate(self.tiles):
            at(plate, y, TILE_H, PAD + n * (tile_w + gap), tile_w)
            icon.setFrame_(AppKit.NSMakeRect(0, 22, tile_w, 20))
            words_label.setFrame_(AppKit.NSMakeRect(-4, 7, tile_w + 8, 14))     # "Playground" is a snug fit
            hit.setFrame_(AppKit.NSMakeRect(0, 0, tile_w, TILE_H))
        y += TILE_H + 10
        at(self.hint, y, 16, PAD, IMG)

    # --- showing: grows out of the orb ----------------------------------------------------------------

    def _hud(self):
        if self.hud is None:
            try:
                from mint.ui.island import island
                self.hud = island.hud
            except Exception:
                self.hud = None
        return self.hud

    def _frame(self):
        """The card's frame, its corner on the orb (the little face sits where the orb was)."""
        H = self.height or self._height()
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
            return
        if alpha > 0:
            try:
                from mint.ui.clipboard_window import window as clipboard
                if clipboard.panel is not None and clipboard.panel.isVisible():
                    return                                   # the clipboard card is out of the orb too
            except Exception:
                pass
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(seconds)
        hud._orb_window.animator().setAlphaValue_(alpha)
        AppKit.NSAnimationContext.endGrouping()

    def _morph(self, opening: bool) -> None:
        frame, (cx, cy) = self._frame()
        H = frame.size.height
        layer = self.card.layer()
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
        if self.is_open():
            self.refresh()
            return
        self.opened = True
        front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None and front.processIdentifier() != os.getpid():
            self.previous_app = front
        self.refresh(place=False)
        self._layout()
        frame, _center = self._frame()
        self.panel.setFrame_display_(frame, False)
        self.panel.setAlphaValue_(0.0)
        self._orb_alpha(0.0, 0.12)
        AppKit.NSApp().activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self._morph(True)
        self.panel.makeFirstResponder_(self.field)

    def close(self) -> None:
        if not self.is_open():
            return
        self.opened = False
        self._morph(False)
        previous = self.previous_app

        def done():
            self.panel.orderOut_(None)
            self.card.layer().removeAnimationForKey_("morph")
            if self.opened:                                   # opened again meanwhile
                self.panel.setAlphaValue_(1.0)
                self.panel.orderFront_(None)
                return
            self._orb_alpha(1.0, 0.18)
            if previous is not None and not previous.isTerminated() and AppKit.NSApp().isActive():
                previous.activateWithOptions_(0)
        AppHelper.callLater(0.24, done)

    def tick(self) -> None:
        if self.panel is None or not self.panel.isVisible():
            return
        mouse = AppKit.NSEvent.mouseLocation()
        frame = self.panel.frame()
        fx, fy = frame.origin.x + W - 26, frame.origin.y + frame.size.height - 26
        self.face.tick(time.monotonic(), 0.0, (mouse.x - fx, mouse.y - fy), False, bool(self.busy))
        if self._flash_until and time.monotonic() > self._flash_until:
            self._flash_until = 0.0
            self._paint_hint()

    def _quiet_face(self) -> None:
        for ring in (self.face.ring, self.face.spinner):
            ring.removeAllAnimations()
            ring.setOpacity_(0.0)

    # --- painting ----------------------------------------------------------------------------------

    def refresh(self, place: bool = True) -> None:
        """Paint everything from the state; `place` also resizes the card when the strip comes or goes."""
        if self.panel is None:
            return
        mint = tuple(gfx.accent())
        version = self.version()
        prompt = version.get("prompt") or self.title_hint or "Image Playground"
        self.title.setStringValue_(prompt[:1].upper() + prompt[1:])
        self.title.setToolTip_(prompt)
        style = (self.style or "").capitalize()
        if version:
            note = version.get("note") or style or "Image Playground"
            count = f"{self.index + 1} of {len(self.versions)}  ·  " if len(self.versions) > 1 else ""
            self.subtitle.setStringValue_(count + note)
        else:
            self.subtitle.setStringValue_(" · ".join(x for x in (style, "Image Playground") if x))
        current = self.current()
        image = self._image(str(current)) if current is not None else None
        if image is not self.picture.image():
            fade = Quartz.CATransition.animation()
            fade.setType_(Quartz.kCATransitionFade)
            fade.setDuration_(0.35)
            self.picture.layer().addAnimation_forKey_(fade, "fade")
            self.picture.setImage_(image)
        self.picture.setToolTip_(f"{current.name} - drag it out" if current is not None else "")
        self.empty.setHidden_(image is not None or bool(self.busy) or bool(self.error))

        busy = bool(self.busy)
        self.veil.setHidden_(not busy)
        self.busy_words.setStringValue_(self.busy)
        self.busy_more.setStringValue_(self.busy_detail)
        self._animate_busy(busy)

        self.error_sheet.setHidden_(not self.error or busy)
        self.error_text.setStringValue_(self.error)
        self.error_back.setHidden_(current is None)
        self._place_error(current is not None)

        ready = current is not None and not busy
        for (key, _words, _symbol, _tip), (plate, icon, words_label, hit) in zip(TILES, self.tiles):
            on = ready or (key == "playground" and not busy)
            primary = key == "save"
            ink = (0.02, 0.05, 0.04) if primary else INK
            icon.setContentTintColor_(_ns(ink, 1.0 if primary else 0.9))
            words_label.setTextColor_(_ns(ink, 1.0 if primary else 0.9))
            plate.layer().setBackgroundColor_(_cg(mint) if primary else _cg((1, 1, 1), 0.08))
            hit.setEnabled_(on)
            plate.setAlphaValue_(1.0 if on else 0.35)
        self.send.setEnabled_(ready)
        self.send.setAlphaValue_(1.0 if ready else 0.4)
        self._paint_strip(mint)
        self._paint_hint()
        if place:
            old = self.height
            self._layout()
            if self.is_open() and old != self.height:
                frame, _center = self._frame()
                self.panel.setFrame_display_animate_(frame, True, False)

    def _paint_strip(self, mint) -> None:
        for view in list(self.strip.subviews()):
            view.removeFromSuperview()
        n = len(self.versions)
        if n < 2:
            return
        gap = 8
        total = n * THUMB + (n - 1) * gap
        width = max(total + 4, IMG + 4)
        self.strip.setFrame_(AppKit.NSMakeRect(0, 0, width, THUMB + 4))
        x = 2 + max(0, (IMG - total) / 2)
        for i, version in enumerate(self.versions):
            thumb = _ImageCardButton.alloc().initWithFrame_(AppKit.NSMakeRect(x, 2, THUMB, THUMB))
            thumb.setBordered_(False)
            thumb.setTitle_("")
            thumb.setImage_(self._image(version["path"]))
            thumb.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            thumb.setTag_(i)
            thumb.setTarget_(self.target)
            thumb.setAction_("pick:")
            thumb.setToolTip_(version.get("note") or version.get("prompt") or "")
            thumb.setWantsLayer_(True)
            thumb.layer().setCornerRadius_(9)
            thumb.layer().setMasksToBounds_(True)
            chosen = i == self.index
            thumb.layer().setBorderColor_(_cg(mint) if chosen else _cg((1, 1, 1), 0.12))
            thumb.layer().setBorderWidth_(2.0 if chosen else 1.0)
            thumb.setAlphaValue_(1.0 if chosen else 0.72)
            self.strip.addSubview_(thumb)
            x += THUMB + gap
        if total > IMG:
            chosen_x = 2 + self.index * (THUMB + gap)
            self.strip.scrollRectToVisible_(AppKit.NSMakeRect(chosen_x - gap, 0, THUMB + 2 * gap, THUMB))

    def _paint_hint(self) -> None:
        if self._flash_until:
            return
        self.hint.setTextColor_(_ns(DIM))
        self.hint.setStringValue_("Mint is working on it…" if self.busy else HINT)

    def flash(self, text: str) -> None:
        if self.panel is None:
            return
        self.hint.setStringValue_(text)
        self.hint.setTextColor_(_ns(tuple(gfx.accent())))
        self._flash_until = time.monotonic() + 2.6

    # --- what imagegen tells it (main thread) -------------------------------------------------------

    def start(self, prompt: str, style: str, words: str, versions: list[dict] | None = None,
              detail: str = "") -> None:
        """A fresh card: `versions` to start from (e.g. the picture being changed), busy with `words`."""
        if self.panel is None:
            self._build()
        self.versions = list(versions or [])
        self.index = len(self.versions) - 1
        self.style, self.title_hint = style, prompt
        self.busy, self.busy_detail, self.error = words, detail, ""
        self.field.setStringValue_("")
        self.show() if not self.is_open() else self.refresh()

    def work(self, words: str, detail: str = "") -> None:
        self.busy, self.busy_detail, self.error = words, detail, ""
        if self.is_open():
            self.refresh()

    def add(self, path: str, prompt: str, note: str = "", words: str = "", style: str = "") -> None:
        """A new version: shown, and the card stays busy with `words` if more are coming."""
        if self.panel is None:
            self._build()
        self.versions.append({"path": str(path), "prompt": prompt, "note": note})
        self.index = len(self.versions) - 1
        self.style = style or self.style
        self.busy, self.busy_detail = words, ("" if not words else self.busy_detail)
        self.error = ""
        self.show() if not self.is_open() else self.refresh()

    def fail(self, text: str) -> None:
        if self.panel is None:
            self._build()
        self.busy, self.busy_detail, self.error = "", "", _people(text)
        self.show() if not self.is_open() else self.refresh()

    # --- what the user does ---------------------------------------------------------------------------

    def pick(self, index: int) -> None:
        if 0 <= index < len(self.versions) and not self.busy:
            self.index, self.error = index, ""
            self.refresh()

    def submit(self) -> None:
        text = " ".join(str(self.field.stringValue() or "").split())
        current = self.current()
        if not text or current is None:
            return
        if self.busy:
            self.flash("Still drawing - press Return again when it's done")
            return
        self.field.setStringValue_("")
        self._run(lambda: _imagegen().edit(text, str(current)))

    def act(self, key: str) -> None:
        if self.busy:
            return
        current = self.current()
        if key == "save":
            self.ask_where()
        elif key == "another":
            self._run(lambda: _imagegen().another())
        elif key == "improve":
            self._run(lambda: _imagegen().improve())
        elif key == "copy":
            self._run(lambda: _imagegen().copy_image(str(current) if current else ""))
        elif key == "playground":
            self._run(lambda: _imagegen().open_app(str(current) if current else ""))
            self.flash("Opening in Image Playground…")

    @staticmethod
    def _run(job) -> None:
        def run():
            try:
                log.info("image card: %s", str(job())[:200])
            except Exception:
                log.exception("image card action")
        threading.Thread(target=run, daemon=True, name="image-card").start()

    def ask_where(self) -> None:
        current = self.current()
        if current is None:
            return
        gen = _imagegen()
        panel = AppKit.NSSavePanel.savePanel()
        panel.setDirectoryURL_(AppKit.NSURL.fileURLWithPath_(str(Path.home() / "Desktop")))
        panel.setNameFieldStringValue_(gen.file_name(self.version().get("prompt") or current.stem))
        panel.setAllowedFileTypes_(["png"])
        panel.setCanCreateDirectories_(True)
        panel.setLevel_(AppKit.NSStatusWindowLevel + 1)
        self.save_panel = panel
        AppKit.NSApp().activateIgnoringOtherApps_(True)

        def chosen(result):
            self.save_panel = None
            if result != AppKit.NSModalResponseOK or panel.URL() is None:
                return
            said = gen.save(str(panel.URL().path()), source=str(current), replace=True)
            log.info("image card save: %s", said)
            if said.startswith("FAILED"):
                self.fail(said)
        panel.beginWithCompletionHandler_(chosen)

    def key(self, event) -> bool:
        if int(event.keyCode()) == 53:                                   # esc
            self.close()
            return True
        return False

    def shortcut(self, event) -> bool:
        flags = int(event.modifierFlags())
        if not flags & AppKit.NSEventModifierFlagCommand:
            return False
        chars = str(event.charactersIgnoringModifiers() or "").lower()
        if chars == "w":
            self.close()
            return True
        if chars == "s" and self.current() is not None and not self.busy:
            self.ask_where()
            return True
        return False


def _imagegen():
    from mint.tools import imagegen
    return imagegen


card = ImageCard()


# --- from any thread ---------------------------------------------------------------------------------

def is_open() -> bool:
    return card.is_open()


def current() -> Path | None:
    """The picture the card shows, while it is open."""
    return card.current() if card.is_open() else None


def current_prompt() -> str:
    return str(card.version().get("prompt") or "") if card.is_open() else ""


def current_style() -> str:
    return card.style if card.is_open() else ""


def start(prompt: str, style: str, words: str, versions: list[dict] | None = None, detail: str = "") -> None:
    AppHelper.callAfter(card.start, prompt, style, words, versions, detail)


def work(words: str, detail: str = "") -> None:
    AppHelper.callAfter(card.work, words, detail)


def add(path, prompt: str, note: str = "", words: str = "", style: str = "") -> None:
    AppHelper.callAfter(card.add, str(path), prompt, note, words, style)


def fail(text: str) -> None:
    AppHelper.callAfter(card.fail, text)


def flash(text: str) -> None:
    AppHelper.callAfter(card.flash, text)


def close() -> None:
    AppHelper.callAfter(card.close)
