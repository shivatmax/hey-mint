"""Colours, themes, icons: shared by the HUD and the on-screen effects."""

from __future__ import annotations

import functools

import AppKit

from . import prefs

GREY = (0.55, 0.57, 0.60)
RED = (1.00, 0.35, 0.35)
GREEN = (0.30, 0.85, 0.50)

# theme -> colours for the live states, and the accent used by effects.
THEMES = {
    "blue":   {"awake": (0.30, 0.62, 1.00), "thinking": (0.66, 0.55, 0.98),
               "working": (0.96, 0.65, 0.14), "speaking": (0.20, 0.83, 0.60),
               "accent": (0.40, 0.78, 1.00)},
    "aurora": {"awake": (0.18, 0.86, 0.80), "thinking": (0.62, 0.42, 1.00),
               "working": (1.00, 0.45, 0.75), "speaking": (0.45, 0.95, 0.55),
               "accent": (0.35, 0.95, 0.85)},
    "sunset": {"awake": (1.00, 0.55, 0.35), "thinking": (0.95, 0.38, 0.58),
               "working": (1.00, 0.78, 0.25), "speaking": (1.00, 0.45, 0.30),
               "accent": (1.00, 0.66, 0.40)},
    "mint":   {"awake": (0.30, 0.90, 0.70), "thinking": (0.40, 0.80, 0.95),
               "working": (0.85, 0.95, 0.40), "speaking": (0.25, 0.88, 0.55),
               "accent": (0.50, 1.00, 0.80)},
    "rose":   {"awake": (1.00, 0.50, 0.70), "thinking": (0.80, 0.55, 1.00),
               "working": (1.00, 0.70, 0.45), "speaking": (1.00, 0.60, 0.82),
               "accent": (1.00, 0.65, 0.82)},
    "mono":   {"awake": (0.86, 0.88, 0.92), "thinking": (0.72, 0.74, 0.78),
               "working": (0.95, 0.95, 0.95), "speaking": (1.00, 1.00, 1.00),
               "accent": (1.00, 1.00, 1.00)},
}


def palette() -> dict:
    return THEMES.get(prefs.get("theme"), THEMES["mint"])


def state_rgb(state: str) -> tuple:
    if state in ("starting", "sleeping", "paused"):
        return GREY
    if state == "offline":
        return RED
    return palette().get(state, palette()["awake"])


def accent() -> tuple:
    return palette()["accent"]


def ns(rgb, alpha=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(rgb[0], rgb[1], rgb[2], alpha)


def cg(rgb, alpha=1.0):
    return ns(rgb, alpha).CGColor()


def light(rgb) -> tuple:
    return tuple(min(1.0, c * 0.35 + 0.72) for c in rgb)


def dark(rgb) -> tuple:
    return tuple(c * 0.55 for c in rgb)


def mix(a, b, t: float) -> tuple:
    return tuple(x + (y - x) * t for x, y in zip(a, b))


@functools.lru_cache(maxsize=128)
def symbol(name: str, size: float = 14, weight: str = "semibold", white: bool = False):
    """An SF Symbol as an NSImage. `white` bakes the colour in, for layers
    whose colour is multiplied in later (particles)."""
    image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is None:
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("sparkles", None)
    weights = {"regular": AppKit.NSFontWeightRegular, "medium": AppKit.NSFontWeightMedium,
               "semibold": AppKit.NSFontWeightSemibold, "bold": AppKit.NSFontWeightBold}
    config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        size, weights.get(weight, AppKit.NSFontWeightSemibold))
    if white:
        config = config.configurationByApplyingConfiguration_(
            AppKit.NSImageSymbolConfiguration.configurationWithHierarchicalColor_(AppKit.NSColor.whiteColor()))
    return image.imageWithSymbolConfiguration_(config)


def cg_image(image, scale: float = 2.0):
    """NSImage -> CGImage (particle cells want a CGImage).

    Drawn into a bitmap by hand: CGImageForProposedRect returns nothing for
    SF Symbol images, which left the particle cells empty in testing."""
    size = image.size()
    w, h = max(1, size.width), max(1, size.height)
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, int(w * scale), int(h * scale), 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    rep.setSize_((w, h))
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep))
    image.drawInRect_(AppKit.NSMakeRect(0, 0, w, h))
    AppKit.NSGraphicsContext.restoreGraphicsState()
    return rep.CGImage()


@functools.lru_cache(maxsize=64)
def app_icon(kind: str, value: str):
    """The real icon of an app: ('bundle', id), ('name', 'Figma') or ('path', ...)."""
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    path = None
    try:
        if kind == "bundle":
            url = workspace.URLForApplicationWithBundleIdentifier_(value)
            path = url.path() if url is not None else None
        elif kind == "name":
            path = workspace.fullPathForApplication_(value)
            if path is None:
                for app in workspace.runningApplications():
                    if (app.localizedName() or "").lower() == value.lower() and app.bundleURL():
                        path = app.bundleURL().path()
                        break
        elif kind == "path":
            path = value
    except Exception:
        path = None
    if not path:
        return None
    return workspace.iconForFile_(path)
