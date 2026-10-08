"""Colours, themes, icons: shared by the HUD and the on-screen effects."""

from __future__ import annotations

import functools

import AppKit

from mint.core import prefs

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


def number_badge(text: str, diameter: float, fill, ink, font_size: float = 11, ring=None):
    """A round badge with its number truly centred (by the digits' cap height, not the line box).
    fill/ink/ring are CGColors (ring: an outline, for an empty checkbox). Returns a CALayer."""
    import Quartz
    badge = Quartz.CALayer.layer()
    badge.setBounds_(Quartz.CGRectMake(0, 0, diameter, diameter))
    badge.setCornerRadius_(diameter / 2)
    if fill is not None:
        badge.setBackgroundColor_(fill)
    if ring is not None:
        badge.setBorderColor_(ring)
        badge.setBorderWidth_(1.5)
    if text:
        font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(font_size, AppKit.NSFontWeightBold)
        layer = Quartz.CATextLayer.layer()
        layer.setString_(text)
        layer.setFont_(font)
        layer.setFontSize_(font_size)
        layer.setForegroundColor_(ink)
        layer.setAlignmentMode_(Quartz.kCAAlignmentCenter)
        layer.setContentsScale_(2.0)
        ascender, descender, cap = font.ascender(), font.descender(), font.capHeight()
        height = ascender - descender
        baseline = diameter / 2 - cap / 2
        layer.setFrame_(Quartz.CGRectMake(0, baseline + descender, diameter, height))
        badge.addSublayer_(layer)
    return badge


# --- the close button every card shares ------------------------------------------------------------

CLOSE = 20                 # the × circle's diameter
CLOSE_REST = 0.5           # how visible the × is while the pointer is not on its card


class CloseButton(AppKit.NSButton):
    """The small round × a card closes with (island scenes, the image card, the clipboard)."""

    def acceptsFirstMouse_(self, event):
        return True                          # one click, even on a panel that is not the key window

    def setHot_(self, hot):
        """Brighter while the pointer is on the × itself."""
        hot = bool(hot)
        if getattr(self, "_mint_hot", None) is hot:
            return
        self._mint_hot = hot
        base = getattr(self, "_mint_base", 0.12)
        self.layer().setBackgroundColor_(cg((1.0, 1.0, 1.0), base + 0.14 if hot else base))
        self.setContentTintColor_(ns((1.0, 1.0, 1.0), 1.0 if hot else 0.72 + base))


def close_button(target, action: str, tip: str = "Close", size: float = CLOSE, rest: float = CLOSE_REST,
                 base: float = 0.12) -> CloseButton:
    """A round × (SF Symbol xmark), 20 pt unless `size`, that calls `action` on `target`; place it with
    setFrameOrigin_. `rest`: how visible it is while the pointer is off its card; `base`: its circle's fill."""
    button = CloseButton.buttonWithImage_target_action_(symbol("xmark", max(9, round(size * 0.42)), "bold"), target,
                                                        action)
    button.setBordered_(False)
    button.setFrame_(AppKit.NSMakeRect(0, 0, size, size))
    button.setToolTip_(tip)
    button.setWantsLayer_(True)
    button.layer().setCornerRadius_(size / 2)
    button._mint_base, button._mint_rest = base, rest
    button.setHot_(False)
    button.setAlphaValue_(rest)
    return button


def track_close(button, on_card: bool, mouse) -> None:
    """Every frame of a card: the × shows fully while the pointer is on the card (faint otherwise, with a
    quick fade, as Apple's notifications do) and brightens while it is on the × itself. Polled rather
    than a tracking area: the island's panel ignores the mouse outside its capsule. `mouse` is in
    screen points."""
    window = button.window()
    if window is None:
        return
    want = 1.0 if on_card else getattr(button, "_mint_rest", CLOSE_REST)
    if getattr(button, "_mint_want", None) != want:
        button._mint_want = want
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.15)
        button.animator().setAlphaValue_(want)
        AppKit.NSAnimationContext.endGrouping()
    local = button.convertPoint_fromView_(window.convertPointFromScreen_(mouse), None)
    button.setHot_(on_card and AppKit.NSPointInRect(local, button.bounds()))
