"""Whether Mint shows up in screen sharing, recordings and screenshots.

Hidden by default: every Mint window - the orb, its bubble, the chat, the
click sparks, the marks it draws - is excluded from screen capture, so a Google
Meet or Zoom share shows the user's screen without Mint on it. Turn visibility
on ("Mint, be visible", the eye button in the chat, or the menu) and all of
them appear in the share, for showing someone what Mint does.

Two details make that work:

* It applies to every window Mint has, including ones opened later: each
  module sets its own windows to hidden when it creates them, so a light timer
  brings any window that disagrees back in line.
* Mint still must not see itself. When it takes a screenshot to read or click
  something, its own orb and captions would be in it. So while visible, Mint
  hides itself for the split second of each capture (mss.grab is wrapped),
  then reappears - in a share it blinks out for a moment, and its reading of
  the screen stays clean.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time

import AppKit
from PyObjCTools import AppHelper

from mint.core import prefs

log = logging.getLogger("mint.ui.sharing")

PREF = "share_visible"
_private = 0                 # captures in progress (Mint hidden for them)
_lock = threading.Lock()
_installed = False


def visible() -> bool:
    """True when Mint should appear in screen shares."""
    return bool(os.environ.get("MINT_CAPTURE")) or bool(prefs.get(PREF))


def _desired():
    if _private:
        return AppKit.NSWindowSharingNone
    return AppKit.NSWindowSharingReadOnly if visible() else AppKit.NSWindowSharingNone


def apply() -> None:
    """Main thread: give every Mint window the sharing type it should have."""
    want = _desired()
    for window in AppKit.NSApplication.sharedApplication().windows():
        if str(window.className()).startswith("NSStatusBar"):
            continue              # the menu bar icon belongs to the menu bar, shared or not
        try:
            if window.sharingType() != want:
                window.setSharingType_(want)
        except Exception:
            log.debug("could not set sharing on a window", exc_info=True)
    _indicator(visible() and not _private)


def set_visible(on: bool) -> str:
    prefs.set(PREF, bool(on))
    AppHelper.callAfter(apply)
    if on:
        return ("Mint is now VISIBLE in screen sharing and recordings: the orb, the chat and anything "
                "it marks will show in Google Meet or Zoom. A small red dot on the orb shows this.")
    return "Mint is hidden from screen sharing again: people you share with will not see it."


def _on_main(fn, wait: float = 0.5) -> None:
    if AppKit.NSThread.isMainThread():
        fn()
        return
    done = threading.Event()

    def run():
        try:
            fn()
        finally:
            done.set()
    AppHelper.callAfter(run)
    done.wait(wait)


@contextlib.contextmanager
def private():
    """Hide Mint for the duration of a screen capture, if it is visible."""
    global _private
    if not visible() or os.environ.get("MINT_CAPTURE"):
        yield
        return
    with _lock:
        _private += 1
        first = _private == 1
    if first:
        _on_main(apply)
        time.sleep(0.03)            # let the window server take it before the grab
    try:
        yield
    finally:
        with _lock:
            _private -= 1
            last = _private == 0
        if last:
            AppHelper.callAfter(apply)


# --- the little "you are on air" dot ------------------------------------------------

_dot = None


def _indicator(on: bool) -> None:
    """A small red dot on the orb while Mint is visible to screen sharing."""
    global _dot
    if os.environ.get("MINT_CAPTURE_CLEAN"):       # the guide's recordings: visible, without the dot
        on = False
    try:
        from mint.ui.emotes import emotes
        orb = emotes.orb
    except Exception:
        orb = None
    if orb is None:
        return
    import Quartz

    from mint.ui import gfx
    if on and _dot is None:
        bx, by = orb._bc
        dot = Quartz.CALayer.layer()
        dot.setBounds_(Quartz.CGRectMake(0, 0, 8, 8))
        dot.setCornerRadius_(4)
        dot.setBackgroundColor_(gfx.cg((1.0, 0.25, 0.3)))
        dot.setBorderWidth_(1.2)
        dot.setBorderColor_(AppKit.NSColor.whiteColor().CGColor())
        dot.setPosition_(Quartz.CGPointMake(bx + orb.d * 0.38, by + orb.d * 0.38))
        orb.body.addSublayer_(dot)
        pulse = Quartz.CAKeyframeAnimation.animationWithKeyPath_("opacity")
        pulse.setValues_([1.0, 0.45, 1.0])
        pulse.setDuration_(1.8)
        pulse.setRepeatCount_(float("inf"))
        dot.addAnimation_forKey_(pulse, "pulse")
        _dot = dot
    elif not on and _dot is not None:
        _dot.removeFromSuperlayer()
        _dot = None


def _refresh_chat() -> None:
    try:
        from mint.ui.emotes import emotes
        chat = getattr(emotes.hud, "chat", None)
        if chat is not None:
            chat.refresh()
    except Exception:
        log.debug("chat refresh failed", exc_info=True)


# --- installation -------------------------------------------------------------------

class _SharingKeeper(AppKit.NSObject):
    def tick_(self, timer):
        if not _private:
            apply()


_keeper = None


def install() -> None:
    """Main thread, once: wrap screen capture, keep windows in line, follow the setting."""
    global _installed, _keeper
    if _installed:
        return
    _installed = True
    try:
        import mss.base
        original = mss.base.MSSBase.grab

        def grab(self, *args, **kwargs):
            with private():
                return original(self, *args, **kwargs)
        mss.base.MSSBase.grab = grab
    except Exception:
        log.warning("could not wrap screen capture; Mint may see itself while visible", exc_info=True)
    _keeper = _SharingKeeper.alloc().init()
    AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.0, _keeper, "tick:", None, True)
    def changed(key, value):
        if key == PREF:
            AppHelper.callAfter(apply)
            AppHelper.callAfter(_refresh_chat)
    prefs.on_change(changed)
    apply()
