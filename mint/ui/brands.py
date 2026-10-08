"""Brand marks, the same everywhere Mint shows a service or an AI provider: Settings' rows (keys, accounts,
connectors, model providers) and anything else that wants them.

icon_tile(view, x, y, kind, size) puts the mark for `kind` ("openai", "google", "telegram", "perm:microphone"…)
in a rounded tile at (x, y). Where the mark comes from, best first:

  1. an official logo file in assets/brands/<kind>.svg (or .png) next to this module (or one level up, in the
     exported layout) - shown on a light tile, so a black logo (OpenAI, X, Ollama) stays visible in dark mode;
  2. the installed app's own icon when the brand has a Mac app (ChatGPT/Codex for OpenAI, Claude, Gemini,
     Telegram, Ollama, Shortcuts, Calendar, Mail) - the real logo, from the app bundle;
  3. a tile drawn here: Google's four-colour G, Gemini's sparkle, Telegram's plane… in the brand's colours.

brand_image(kind, size) gives the mark as an NSImage. Finding an app asks LaunchServices; warm() does that for
every kind off the main thread (Settings calls it from its readers), and the answers are kept.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import AppKit

log = logging.getLogger("mint.ui.brands")

ICON = 28                     # an icon tile in a row
_G_BLUE, _G_RED, _G_YELLOW, _G_GREEN = (0.26, 0.52, 0.96), (0.92, 0.26, 0.21), (0.98, 0.74, 0.02), (0.2, 0.66, 0.33)
# kind -> (background, glyph). Background: "white", one rgb (a lighter top is added) or (top rgb, bottom rgb).
# Glyph: ("symbol", "name|fallback", rgb), ("text", "g", rgb) or ("draw", name) - drawn by _DRAW below.
BRANDS = {
    "google": ("white", ("draw", "google")),
    "gmail": ((0.92, 0.26, 0.21), ("symbol", "envelope.fill")),
    "mail": (((0.3, 0.68, 1.0), (0.08, 0.45, 0.95)), ("symbol", "envelope.fill")),
    "calendar_app": ("white", ("draw", "calendar")),
    "meet": (((0.12, 0.72, 0.42), (0.0, 0.55, 0.3)), ("symbol", "video.fill")),
    "telegram": (((0.33, 0.76, 0.98), (0.13, 0.55, 0.88)), ("symbol", "paperplane.fill")),
    "email": (((1.0, 0.7, 0.25), (1.0, 0.47, 0.1)), ("symbol", "at")),
    "shortcuts": (((1.0, 0.36, 0.52), (0.38, 0.33, 0.96)), ("symbol", "square.2.layers.3d.fill|square.stack.3d.up.fill")),
    "icloud": (((0.45, 0.75, 1.0), (0.2, 0.5, 0.95)), ("symbol", "icloud.fill")),
    "microsoft": ("white", ("draw", "microsoft")),
    "models": (((0.66, 0.5, 1.0), (0.42, 0.3, 0.9)), ("symbol", "cpu.fill|cpu")),
    "library": (((0.35, 0.78, 1.0), (0.2, 0.45, 0.98)), ("symbol", "square.grid.2x2.fill")),
    "openai": (((0.2, 0.2, 0.2), (0.02, 0.02, 0.02)), ("draw", "openai")),
    "anthropic": ((0.85, 0.47, 0.34), ("draw", "anthropic")),
    "gemini": ("white", ("draw", "gemini")),
    "openrouter": (((0.45, 0.47, 0.98), (0.27, 0.27, 0.78)), ("symbol", "arrow.triangle.branch")),
    "groq": ((0.96, 0.31, 0.21), ("text", "g", (1.0, 1.0, 1.0))),
    "xai": (((0.2, 0.2, 0.2), (0.0, 0.0, 0.0)), ("draw", "xai")),
    "ollama": ("white", ("text", "\U0001F999", (0.0, 0.0, 0.0))),
    "typesafe": (((0.2, 0.82, 0.62), (0.05, 0.6, 0.48)), ("symbol", "cursorarrow.click.2")),
    "custom": ((0.5, 0.54, 0.62), ("symbol", "network")),
    "key": ((0.95, 0.68, 0.0), ("symbol", "key.fill")),
}
def rgb(value, alpha: float = 1.0):
    """(r, g, b) in 0-1 -> an sRGB NSColor."""
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(value[0], value[1], value[2], alpha)


def brand(kind: str):
    """(background, glyph) for `kind`: a brand above, "perm:<kind>" for a macOS permission, else a grey app tile."""
    if kind in BRANDS:
        return BRANDS[kind]
    if kind.startswith("perm:"):
        from mint.core import permissions
        for k, symbol, color, *_ in permissions.ALL:
            if k == kind[5:]:
                return color, ("symbol", symbol)
    return (0.55, 0.58, 0.65), ("symbol", "app.fill")


def _draw_symbol(names: str, s: float, fg) -> None:
    image = None
    for name in names.split("|"):
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if image is not None:
            break
    if image is None:
        return
    config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(s * 0.46, AppKit.NSFontWeightSemibold)
    config = config.configurationByApplyingConfiguration_(
        AppKit.NSImageSymbolConfiguration.configurationWithPaletteColors_([rgb(fg)]))
    image = image.imageWithSymbolConfiguration_(config)
    size = image.size()
    fit = min(1.0, s * 0.64 / max(size.width, size.height, 1))
    w, h = size.width * fit, size.height * fit
    image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
        AppKit.NSMakeRect((s - w) / 2, (s - h) / 2, w, h), AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver,
        1.0, True, None)


def _draw_text(text: str, s: float, fg, rect=None, scale: float = 0.62, weight=None) -> None:
    font = AppKit.NSFont.systemFontOfSize_weight_(s * scale, weight if weight is not None else AppKit.NSFontWeightHeavy)
    attrs = {AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: rgb(fg)}
    string = AppKit.NSString.stringWithString_(text)
    size = string.sizeWithAttributes_(attrs)
    x0, y0, w0, h0 = rect or (0, 0, s, s)
    y = y0 + (h0 - size.height) / 2
    string.drawAtPoint_withAttributes_(AppKit.NSMakePoint(x0 + (w0 - size.width) / 2, y), attrs)


def _stroke(points, width: float, color, cap=None) -> None:
    path = AppKit.NSBezierPath.bezierPath()
    path.moveToPoint_(points[0])
    for point in points[1:]:
        path.lineToPoint_(point)
    path.setLineWidth_(width)
    path.setLineCapStyle_(AppKit.NSLineCapStyleRound if cap is None else cap)
    rgb(color).setStroke()
    path.stroke()


def _draw_google(s: float) -> None:
    """Google's G: four arcs in its four colours and the blue bar."""
    c, r, t = s / 2, s * 0.245, s * 0.12
    for start, end, color in ((42, 140, _G_RED), (140, 222, _G_YELLOW), (222, 320, _G_GREEN), (320, 360, _G_BLUE)):
        arc = AppKit.NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((c, c), r, start, end, False)
        arc.setLineWidth_(t)
        arc.setLineCapStyle_(AppKit.NSLineCapStyleButt)
        rgb(color).setStroke()
        arc.stroke()
    rgb(_G_BLUE).setFill()
    AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(c - t * 0.05, c - t / 2, r + t / 2 + t * 0.05, t))


def _draw_gemini(s: float) -> None:
    """Gemini's four-point sparkle, blue to violet."""
    c, big = s / 2, s * 0.36
    tips = [(c, c + big), (c + big, c), (c, c - big), (c - big, c)]
    path = AppKit.NSBezierPath.bezierPath()
    path.moveToPoint_(tips[0])
    for i in range(4):
        a, b = tips[i], tips[(i + 1) % 4]
        path.curveToPoint_controlPoint1_controlPoint2_(b, (c + (a[0] - c) * 0.08, c + (a[1] - c) * 0.08),
                                                       (c + (b[0] - c) * 0.08, c + (b[1] - c) * 0.08))
    path.closePath()
    gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(rgb((0.62, 0.42, 0.95)),
                                                                             rgb((0.2, 0.5, 0.98)))
    gradient.drawInBezierPath_angle_(path, 45)


def _draw_openai(s: float) -> None:
    """A six-petal knot, like OpenAI's blossom."""
    c = s / 2
    AppKit.NSColor.whiteColor().setStroke()
    for k in range(6):
        turn = AppKit.NSAffineTransform.transform()
        turn.translateXBy_yBy_(c, c)
        turn.rotateByDegrees_(k * 60)
        w = s * 0.17
        petal = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSMakeRect(-w / 2 + s * 0.07, -s * 0.05, w, s * 0.33), w / 2, w / 2)
        petal.transformUsingAffineTransform_(turn)
        petal.setLineWidth_(s * 0.045)
        petal.stroke()


def _draw_anthropic(s: float) -> None:
    """Claude's sunburst: rays of two lengths from the middle."""
    import math
    c = s / 2
    for k in range(12):
        angle = math.radians(k * 30 + 15)
        length = s * (0.3 if k % 2 == 0 else 0.21)
        _stroke([(c, c), (c + math.cos(angle) * length, c + math.sin(angle) * length)], s * 0.075, (1.0, 0.96, 0.9))


def _draw_xai(s: float) -> None:
    _stroke([(s * 0.29, s * 0.24), (s * 0.71, s * 0.76)], s * 0.1, (1, 1, 1), AppKit.NSLineCapStyleButt)
    _stroke([(s * 0.29, s * 0.76), (s * 0.45, s * 0.57)], s * 0.07, (1, 1, 1), AppKit.NSLineCapStyleButt)
    _stroke([(s * 0.55, s * 0.43), (s * 0.71, s * 0.24)], s * 0.07, (1, 1, 1), AppKit.NSLineCapStyleButt)


def _draw_calendar(s: float) -> None:
    """A calendar page: a red band and today's date."""
    band = s * 0.3
    rgb((0.96, 0.3, 0.27)).setFill()
    AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(0, s - band, s, band))
    _draw_text(str(time.localtime().tm_mday), s, (0.12, 0.12, 0.14), rect=(0, 0, s, s - band), scale=0.5,
               weight=AppKit.NSFontWeightSemibold)


def _draw_microsoft(s: float) -> None:
    q, gap = s * 0.2, s * 0.04
    x0 = (s - 2 * q - gap) / 2
    for (dx, dy), color in (((0, 1), (0.95, 0.31, 0.13)), ((1, 1), (0.5, 0.73, 0.0)), ((0, 0), (0.0, 0.64, 0.94)),
                          ((1, 0), (1.0, 0.73, 0.0))):
        rgb(color).setFill()
        AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(x0 + dx * (q + gap), x0 + dy * (q + gap), q, q))


_DRAW = {"google": _draw_google, "gemini": _draw_gemini, "openai": _draw_openai, "anthropic": _draw_anthropic,
         "xai": _draw_xai, "calendar": _draw_calendar, "microsoft": _draw_microsoft}


def _light_tile(s: float) -> None:
    radius = s * 0.24
    tile = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(AppKit.NSMakeRect(0.5, 0.5, s - 1, s - 1),
                                                                         radius, radius)
    AppKit.NSColor.whiteColor().setFill()
    tile.fill()
    AppKit.NSColor.colorWithWhite_alpha_(0.0, 0.16).setStroke()
    tile.setLineWidth_(0.5)
    tile.stroke()


def draw_tile(kind: str, s: float) -> None:
    """The drawn tile for `kind`, s points square, into the current graphics context."""
    try:
        background, glyph = brand(kind)
        radius = s * 0.24
        tile = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSMakeRect(0.5, 0.5, s - 1, s - 1), radius, radius)
        if background == "white":
            AppKit.NSColor.whiteColor().setFill()
            tile.fill()
            AppKit.NSColor.colorWithWhite_alpha_(0.0, 0.16).setStroke()
            tile.setLineWidth_(0.5)
            tile.stroke()
        else:
            top, bottom = (background if isinstance(background[0], tuple) else
                           (tuple(min(1.0, v + (1 - v) * 0.22) for v in background), background))
            gradient = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(rgb(bottom), rgb(top))
            gradient.drawInBezierPath_angle_(tile, 90)
        AppKit.NSGraphicsContext.saveGraphicsState()
        tile.addClip()
        what = glyph[0]
        if what == "symbol":
            _draw_symbol(glyph[1], s, glyph[2] if len(glyph) > 2 else (1.0, 1.0, 1.0))
        elif what == "text":
            _draw_text(glyph[1], s, glyph[2] if len(glyph) > 2 else (1.0, 1.0, 1.0))
        elif glyph[1] in _DRAW:
            _DRAW[glyph[1]](s)
        AppKit.NSGraphicsContext.restoreGraphicsState()
    except Exception:
        log.debug("icon tile %s", kind, exc_info=True)


class _BrandTile(AppKit.NSView):
    """A rounded, app-icon-like tile with a brand glyph, drawn at the screen's scale (crisp on Retina) and
    again when the appearance changes."""

    def drawRect_(self, rect):
        logo = getattr(self, "logo", None)
        s = float(self.bounds().size.width)
        if logo is not None:
            _light_tile(s)
            inset = s * 0.18
            logo.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                AppKit.NSMakeRect(inset, inset, s - 2 * inset, s - 2 * inset), AppKit.NSZeroRect,
                AppKit.NSCompositingOperationSourceOver, 1.0, True, None)
            return
        draw_tile(getattr(self, "kind", ""), s)


# Brands with a Mac app whose icon is the brand's logo: the first one installed is used.
APPS = {"openai": ("com.openai.chat", "com.openai.codex"), "anthropic": ("com.anthropic.claudefordesktop",),
        "gemini": ("com.google.GeminiMacOS",), "telegram": ("ru.keepcoder.Telegram", "org.telegram.desktop"),
        "ollama": ("com.electron.ollama",), "shortcuts": ("com.apple.shortcuts",),
        "calendar_app": ("com.apple.iCal",), "mail": ("com.apple.mail",)}
_HERE = Path(__file__).resolve().parent
ASSET_DIRS = (_HERE / "assets" / "brands", _HERE.parent / "assets" / "brands")
_apps: dict[str, str | None] = {}          # kind -> the app's path (None: not installed)
_files: dict[str, str | None] = {}         # kind -> its logo file (None: none)
_images: dict[tuple, object] = {}
_lock = threading.Lock()


def logo_file(kind: str) -> str | None:
    if kind not in _files:
        found = None
        for folder in ASSET_DIRS:
            for suffix in (".svg", ".pdf", ".png"):
                if (folder / f"{kind}{suffix}").is_file():
                    found = str(folder / f"{kind}{suffix}")
                    break
            if found:
                break
        _files[kind] = found
    return _files[kind]


def app_path(kind: str) -> str | None:
    """The installed app whose icon is `kind`'s logo (LaunchServices; cached)."""
    with _lock:
        if kind in _apps:
            return _apps[kind]
    found = None
    for bundle in APPS.get(kind, ()):
        try:
            url = AppKit.NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle)
        except Exception:
            url = None
        if url is not None:
            found = str(url.path())
            break
    with _lock:
        _apps[kind] = found
    return found


def warm(kinds=None) -> None:
    """Find the apps and logo files for `kinds` (all) - off the main thread, so building a page never waits."""
    started = time.monotonic()
    for kind in kinds or list(APPS) + list(BRANDS):
        logo_file(kind)
        if kind in APPS:
            app_path(kind)
    log.debug("brands warmed in %.0f ms", 1000 * (time.monotonic() - started))


def source(kind: str) -> str:
    """'file', 'app' or 'drawn': where the mark for `kind` comes from on this Mac."""
    return "file" if logo_file(kind) else "app" if app_path(kind) else "drawn"


def brand_image(kind: str, size: float = ICON):
    """The mark for `kind` as an NSImage of `size` points (cached): the logo file, the app's icon, or the drawn
    tile."""
    key = (kind, float(size))
    if key in _images:
        return _images[key]
    image = None
    path = logo_file(kind)
    if path:
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
    if image is None and app_path(kind):
        image = AppKit.NSWorkspace.sharedWorkspace().iconForFile_(app_path(kind))
    if image is None:
        image = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(size, size))
        image.lockFocus()
        draw_tile(kind, float(size))
        image.unlockFocus()
    image.setSize_(AppKit.NSMakeSize(size, size))
    _images[key] = image
    return image


def icon_tile(view, x: float, y: float, kind: str, size: float = ICON):
    """The mark for `kind` at (x, y) of `view`, `size` points square - one look for a brand everywhere."""
    path = logo_file(kind)
    if path:                                   # an official logo: on a light tile, visible in dark mode too
        tile = _BrandTile.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, size, size))
        tile.kind, tile.logo = kind, AppKit.NSImage.alloc().initWithContentsOfFile_(path)
        view.addSubview_(tile)
        return tile
    app = app_path(kind)
    if app:                                    # the app's own icon (it has its own shape and margin)
        image = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(x - 2, y - 2, size + 4, size + 4))
        image.setImage_(AppKit.NSWorkspace.sharedWorkspace().iconForFile_(app))
        image.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
        view.addSubview_(image)
        return image
    tile = _BrandTile.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, size, size))
    tile.kind = kind
    view.addSubview_(tile)
    return tile
