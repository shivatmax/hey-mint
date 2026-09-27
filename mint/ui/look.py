"""Apple-style surfaces for Mint's panels: the caption bubble, the chat, the
voice-training window, the critters' name tags.

* glass(): a system material (NSVisualEffectView) that follows light and dark
  mode, rounded with a mask image. Rounding a behind-window blur with a layer's
  cornerRadius does not clip the blur: a square patch of blur showed round the
  rounded card. Apple's way is maskImage, and the window's shadow then follows
  the rounded shape too.
* System colours (label, secondary label, fills, separator) instead of fixed
  white-on-black, so text reads in both modes.
* on_theme_change(): layer colours are baked when set, so views that use them
  re-apply when the user switches light/dark.
"""

from __future__ import annotations

import logging

import AppKit
import Quartz  # noqa: F401  - registers CGColorRef, or CGColor() comes back as a raw pointer

log = logging.getLogger("mint.ui.look")

_masks: dict = {}


def rounded_mask(radius: float):
    """A stretchable rounded-rect image for NSVisualEffectView.setMaskImage_."""
    radius = float(radius)
    if radius in _masks:
        return _masks[radius]
    edge = 2 * radius + 1
    image = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
        AppKit.NSMakeSize(edge, edge), False,
        lambda rect: (AppKit.NSColor.blackColor().set(),
                      AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius).fill(),
                      True)[-1])
    image.setCapInsets_(AppKit.NSEdgeInsetsMake(radius, radius, radius, radius))
    image.setResizingMode_(AppKit.NSImageResizingModeStretch)
    _masks[radius] = image
    return image


def glass(frame, radius: float = 16.0, material=None):
    """A rounded, translucent system surface (behind-window blur) for a panel's content view."""
    view = AppKit.NSVisualEffectView.alloc().initWithFrame_(frame)
    view.setMaterial_(material if material is not None else AppKit.NSVisualEffectMaterialPopover)
    view.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
    view.setState_(AppKit.NSVisualEffectStateActive)
    view.setMaskImage_(rounded_mask(radius))
    view.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    return view


def follow_system(window) -> None:
    """Light or dark with the system, not forced dark."""
    window.setAppearance_(None)


def _base(view_or_window=None):
    """The plain light or dark appearance. Inside a glass view the appearance is
    'vibrant', and system fill colours resolve OPAQUE there (a solid black or white
    slab was drawn for the message field) - so colours are resolved in the base one."""
    appearance = (view_or_window.effectiveAppearance() if view_or_window is not None
                  else AppKit.NSApplication.sharedApplication().effectiveAppearance())
    match = appearance.bestMatchFromAppearancesWithNames_(
        [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
    return AppKit.NSAppearance.appearanceNamed_(match or AppKit.NSAppearanceNameAqua)


def dark(view_or_window=None) -> bool:
    appearance = (view_or_window.effectiveAppearance() if view_or_window is not None
                  else AppKit.NSApplication.sharedApplication().effectiveAppearance())
    match = appearance.bestMatchFromAppearancesWithNames_(
        [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua])
    return match == AppKit.NSAppearanceNameDarkAqua


def cg(color, view_or_window=None):
    """A system (dynamic) NSColor as a CGColor for the view's current appearance."""
    appearance = _base(view_or_window)
    out = []
    try:
        appearance.performAsCurrentDrawingAppearance_(lambda: out.append(color.CGColor()))
    except Exception:
        previous = AppKit.NSAppearance.currentAppearance()
        AppKit.NSAppearance.setCurrentAppearance_(appearance)
        out.append(color.CGColor())
        AppKit.NSAppearance.setCurrentAppearance_(previous)
    return out[0]


def rgb(color, view_or_window=None) -> tuple:
    """A system colour as (r, g, b) for the current appearance (for code that mixes colours)."""
    appearance = _base(view_or_window)
    out = []

    def resolve():
        c = color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
        out.append((c.redComponent(), c.greenComponent(), c.blueComponent()) if c is not None else (0.5, 0.5, 0.5))
    try:
        appearance.performAsCurrentDrawingAppearance_(resolve)
    except Exception:
        resolve()
    return out[0]


def ink(accent: tuple, view_or_window=None) -> tuple:
    """An accent colour that reads as text: lighter on dark, deeper on light."""
    if dark(view_or_window):
        return tuple(min(1.0, c * 0.35 + 0.72) for c in accent)
    return tuple(c * 0.62 for c in accent)


# --- theme changes -----------------------------------------------------------------------

_listeners: list = []
_observer = None


def on_theme_change(fn) -> None:
    """Call fn() on the main thread when the user switches light/dark."""
    global _observer
    _listeners.append(fn)
    if _observer is None:
        center = AppKit.NSDistributedNotificationCenter.defaultCenter()

        def changed(note):
            from PyObjCTools import AppHelper
            for listener in list(_listeners):
                AppHelper.callLater(0.05, _safe, listener)     # after AppKit has switched
        _observer = center.addObserverForName_object_queue_usingBlock_(
            "AppleInterfaceThemeChangedNotification", None, AppKit.NSOperationQueue.mainQueue(), changed)


def _safe(fn) -> None:
    try:
        fn()
    except Exception:
        log.debug("theme listener failed", exc_info=True)


def capsule(layer, view_or_window=None) -> None:
    """Style a CALayer as an Apple capsule: system background, hairline, soft shadow."""
    background = AppKit.NSColor.windowBackgroundColor().colorWithAlphaComponent_(0.94)
    layer.setBackgroundColor_(cg(background, view_or_window))
    layer.setBorderColor_(cg(AppKit.NSColor.separatorColor(), view_or_window))
    layer.setBorderWidth_(0.5)
    layer.setShadowColor_(AppKit.NSColor.blackColor().CGColor())
    layer.setShadowOpacity_(0.18)
    layer.setShadowRadius_(6)
    layer.setShadowOffset_(AppKit.NSMakeSize(0, -1.5))


def text_rgb(secondary: bool = False, view_or_window=None) -> tuple:
    return rgb(AppKit.NSColor.secondaryLabelColor() if secondary else AppKit.NSColor.labelColor(), view_or_window)
