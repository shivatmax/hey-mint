"""The island becomes an input (plans/grokbot-notch-overhaul.md, phase 5): type to Mint right in the notch.

    view, c = composer(w, h, on_send, on_cancel, on_mic=None, on_attach=None)
    c.focus(panel)      # put the keyboard in the field (the panel must be allowed to become key first)
    c.text() / c.set_text(s) / c.clear() / c.send() / c.cancel() / c.holding() / c.close()

A rounded dark field ("Ask Mint anything…", white 14 pt, several lines), a "+" circle bottom-left (attach:
on_attach() if given, else an Open panel whose picks are appended as paths), a mic circle (on_mic) and a white
send capsule bottom-right. Enter sends (Shift/Option+Enter = new line), Esc cancels. Sending flies the words
up and folds the field, calls on_send(text) at once, and the composer comes back empty the next time it shows.

The notch panel is non-activating and only becomes key while notch.allow_key is true (MintNotchPanel.
canBecomeKeyWindow): keep it true while the composer is up, then call focus(). Main thread.
"""

from __future__ import annotations

import logging
import shlex

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

from mint.ui import gfx
from mint.ui import kinetics
from mint.ui.notch_fx import CARD, HAIRLINE, RADIUS, MintFxAct, MintFxButton, Scene, _cgw, _sfx, _white, label

log = logging.getLogger("mint.ui.notch_composer")

PLACEHOLDER = "Ask Mint anything…"
FONT = 14.0
BTN = 28.0
SEND_W = 46.0


class MintComposerTextView(AppKit.NSTextView):
    owner = None

    def keyDown_(self, event):
        owner = self.owner
        if owner is not None and not self.hasMarkedText():          # never steal Return from an input method
            code = event.keyCode()
            if code in (36, 76):                                     # Return, keypad Enter
                flags = event.modifierFlags()
                if flags & (AppKit.NSEventModifierFlagShift | AppKit.NSEventModifierFlagOption):
                    self.insertNewlineIgnoringFieldEditor_(None)
                else:
                    owner.send()
                return
            if code == 53:                                           # Esc
                owner.cancel()
                return
        objc.super(MintComposerTextView, self).keyDown_(event)

    def cancelOperation_(self, sender):
        if self.owner is not None:
            self.owner.cancel()

    def acceptsFirstMouse_(self, event):
        return True


class MintComposerDelegate(AppKit.NSObject):
    owner = None

    def textDidChange_(self, note):
        if self.owner is not None:
            self.owner._changed()


class Composer(Scene):
    def __init__(self, w, h, on_send, on_cancel=None, on_mic=None, on_attach=None) -> None:
        super().__init__(w, h, card=False)
        self.on_send, self.on_cancel, self.on_mic, self.on_attach = on_send, on_cancel, on_mic, on_attach
        self._picking = False
        self._folded = False
        w, h = self.w, self.h
        # The field: its own card, a touch lighter than the island's cards, with the hairline.
        self.card.setBackgroundColor_(gfx.cg((0.105, 0.105, 0.115)))
        self.card.setAnchorPoint_(Quartz.CGPointMake(0.5, 1.0))
        self.card.setPosition_(Quartz.CGPointMake(w / 2, h))
        self.edge.setBorderColor_(_cgw(HAIRLINE + 0.03))
        self.edge.setAnchorPoint_(Quartz.CGPointMake(0.5, 1.0))
        self.edge.setPosition_(Quartz.CGPointMake(w / 2, h))
        pad = 14.0
        row = 10.0 + BTN + 6.0                               # the button row's height
        text_h = max(22.0, h - row - pad + 4)
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(pad, row, w - 2 * pad, text_h))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(False)
        scroll.setHasHorizontalScroller_(False)
        scroll.setBorderType_(AppKit.NSNoBorder)
        scroll.setWantsLayer_(True)
        size = scroll.contentSize()
        text = MintComposerTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, size.width, size.height))
        text.owner = self
        text.setMinSize_(AppKit.NSMakeSize(0, size.height))
        text.setMaxSize_(AppKit.NSMakeSize(1e7, 1e7))
        text.setVerticallyResizable_(True)
        text.setHorizontallyResizable_(False)
        text.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        text.textContainer().setContainerSize_(AppKit.NSMakeSize(size.width, 1e7))
        text.textContainer().setWidthTracksTextView_(True)
        text.textContainer().setLineFragmentPadding_(2.0)
        text.setTextContainerInset_(AppKit.NSMakeSize(0, 2))
        text.setDrawsBackground_(False)
        text.setRichText_(False)
        text.setImportsGraphics_(False)
        text.setAllowsUndo_(True)
        text.setAutomaticQuoteSubstitutionEnabled_(False)
        text.setAutomaticDashSubstitutionEnabled_(False)
        font = AppKit.NSFont.systemFontOfSize_weight_(FONT, AppKit.NSFontWeightRegular)
        text.setFont_(font)
        text.setTextColor_(_white(0.95))
        text.setInsertionPointColor_(_white(0.95))
        text.setTypingAttributes_({AppKit.NSFontAttributeName: font,
                                   AppKit.NSForegroundColorAttributeName: _white(0.95)})
        text.setSelectedTextAttributes_({AppKit.NSBackgroundColorAttributeName: _white(0.22)})
        delegate = MintComposerDelegate.alloc().init()
        delegate.owner = self
        text.setDelegate_(delegate)
        scroll.setDocumentView_(text)
        self.view.addSubview_(scroll)
        self.scroll, self.textview, self._delegate = scroll, text, delegate
        self.placeholder = label(PLACEHOLDER, FONT, AppKit.NSFontWeightRegular, _white(0.36))
        ph = self.placeholder.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, w, 100)).height
        self.placeholder.setFrame_(AppKit.NSMakeRect(pad, row + text_h - 2 - ph, w - 2 * pad, ph))
        self.view.addSubview_(self.placeholder)

        # The row: "+" on the left; mic and the white send capsule on the right.
        y = 10.0
        self.plus = self._round(AppKit.NSMakeRect(10, y, BTN, BTN), "plus", self.attach, "Attach files", 0.12)
        self.send_button = self._round(AppKit.NSMakeRect(w - 10 - SEND_W, y, SEND_W, BTN), "arrow.up", self.send,
                                       "Send (Return)", 1.0, ink=AppKit.NSColor.blackColor())
        self.mic = self._round(AppKit.NSMakeRect(w - 10 - SEND_W - 8 - BTN, y, BTN, BTN), "mic.fill", self.mic_clicked,
                               "Talk instead", 0.12)
        self._changed()

    def _round(self, frame, symbol, fn, tip, fill, ink=None):
        b = self._button(frame, fn, gfx.symbol(symbol, 12.5, "bold" if symbol == "arrow.up" else "semibold"), tip)
        b.setContentTintColor_(ink or _white(0.85))
        b.layer().setCornerRadius_(frame.size.height / 2)
        b.layer().setBackgroundColor_(_cgw(fill))
        return b

    # -- typing --

    def text(self) -> str:
        return str(self.textview.string() or "")

    def set_text(self, s: str) -> None:
        self.textview.setString_(s or "")
        self._changed()

    def clear(self) -> None:
        self.set_text("")

    def _changed(self) -> None:
        empty = not self.text().strip()
        self.placeholder.setHidden_(bool(self.text()))
        self.send_button.setAlphaValue_(0.45 if empty else 1.0)

    def focus(self, panel=None) -> None:
        """Keyboard into the field, the way the Search tab does it (notch._focus_search): make the panel key -
        asking the app to come forward if a background app is refused - then first responder."""
        def go():
            if not self.alive:
                return
            window = panel or self.view.window()
            if window is None:
                return
            window.makeKeyWindow()
            if not window.isKeyWindow():
                AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
                window.makeKeyWindow()
            window.makeFirstResponder_(self.textview)
            self.textview.setSelectedRange_((len(self.text()), 0))
        AppHelper.callLater(0.08, go)

    def holding(self) -> bool:
        """The Open panel is up: don't fold the notch under it."""
        return self._picking

    # -- buttons --

    def attach(self) -> None:
        if self.on_attach is not None:
            try:
                self.on_attach()
            except Exception:
                log.exception("attach failed")
            return
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(True)
        panel.setPrompt_("Attach")
        self._picking = True
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)

        def done(result):
            self._picking = False
            if result == AppKit.NSModalResponseOK:
                paths = [str(url.path()) for url in panel.URLs() or []]
                if paths:
                    current = self.text()
                    joined = " ".join(shlex.quote(p) for p in paths)
                    self.set_text((current.rstrip() + " " if current.strip() else "") + joined + " ")
            self.focus()
        panel.beginWithCompletionHandler_(done)

    def mic_clicked(self) -> None:
        if self.on_mic is not None:
            try:
                self.on_mic()
            except Exception:
                log.exception("mic failed")

    # -- send / cancel --

    def send(self) -> None:
        text = self.text().strip()
        if not text or self._folded:
            if not text:
                kinetics.shake(self.card, px=4.0)
                kinetics.shake(self.edge, px=4.0)
            return
        _sfx("send")
        self._fold()
        try:
            self.on_send(text)
        except Exception:
            log.exception("composer send failed")

    def cancel(self) -> None:
        if self.on_cancel is not None:
            try:
                self.on_cancel()
            except Exception:
                log.exception("composer cancel failed")

    def _fold(self) -> None:
        """The words fly up and fade; the field folds to a slim bar under the notch. The folded look is held
        (fill forwards) until the composer shows again, so it never springs back open while the notch moves on."""
        self._folded = True
        reduce = kinetics.reduce_motion()
        held = []

        def hold(layer, key, old, new, seconds, delay=0.0, spring=False, timing=None):
            if spring and not reduce:
                stiffness, damping, mass = kinetics.PRESETS["snappy"]
                anim = Quartz.CASpringAnimation.animationWithKeyPath_(key)
                anim.setStiffness_(stiffness)
                anim.setDamping_(damping)
                anim.setMass_(mass)
                anim.setDuration_(anim.settlingDuration())
            else:
                anim = Quartz.CABasicAnimation.animationWithKeyPath_(key)
                anim.setDuration_(seconds)
                anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(
                    timing or Quartz.kCAMediaTimingFunctionEaseOut))
            anim.setFromValue_(kinetics._value(old))
            anim.setToValue_(kinetics._value(new))
            anim.setFillMode_(Quartz.kCAFillModeBoth)
            anim.setRemovedOnCompletion_(False)
            if delay:
                anim.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
            layer.addAnimation_forKey_(anim, "fx-fold-" + key)
            held.append((layer, "fx-fold-" + key))
        words = self.scroll.layer()
        if words is not None:
            if not reduce:
                hold(words, "transform.translation.y", 0.0, 26.0, 0.32, timing=Quartz.kCAMediaTimingFunctionEaseIn)
            hold(words, "opacity", 1.0, 0.0, 0.26)
        for b in (self.plus, self.mic, self.send_button):
            hold(b.layer(), "opacity", 1.0, 0.0, 0.16)
        for layer in (self.card, self.edge):
            if reduce:
                hold(layer, "opacity", 1.0, 0.0, 0.2)
            else:
                hold(layer, "bounds", (0, 0, self.w, self.h), (0, 0, self.w * 0.96, 36.0), 0.3, delay=0.08,
                     spring=True)
        self._held = held
        AppHelper.callLater(0.9, self._reset_if_shown)

    def _reset(self) -> None:
        for layer, key in getattr(self, "_held", []):
            layer.removeAnimationForKey_(key)
        self._held = []
        self._folded = False
        self.clear()

    def _reset_if_shown(self) -> None:
        if self.alive and self._folded:
            if self.view.isHiddenOrHasHiddenAncestor() or self.view.window() is None:
                return                                   # reset when it shows again (_unhidden / _appeared)
            self._reset()
            kinetics.blur_in(self.card, 0.3)

    def _unhidden(self) -> None:
        if self._folded:
            self._reset()
        super()._unhidden()

    def _appeared(self) -> None:
        if self._folded:
            self._reset()
        super()._appeared()

    def _play(self) -> None:
        # The notch's own _reveal fades and grows the view in; only the buttons get an entrance of their own.
        # (AppKit anchors a button's layer at its corner, so the buttons rise and fade rather than scale.)
        def rise(layer, delay):
            kinetics.basic(layer, "opacity", 0.0, 1.0, 0.2, delay=delay, keep=False, anim_key="fx-rise-o")
            if not kinetics.reduce_motion():
                kinetics.spring(layer, "transform.translation.y", -6.0, 0.0, "snappy", delay=delay,
                                anim_key="fx-rise")
        kinetics.stagger([self.plus.layer(), self.mic.layer(), self.send_button.layer()], rise, start=0.12)


def composer(w: float, h: float, on_send, on_cancel=None, on_mic=None, on_attach=None):
    """The notch as an input: (view, controller). on_send(text) on Return or the send capsule; on_cancel() on
    Esc; on_mic() for the mic circle; on_attach() for "+" (default: an Open panel appending paths)."""
    scene = Composer(w, h, on_send, on_cancel, on_mic, on_attach)
    return scene.view, scene
